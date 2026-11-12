"""UPnP Internet Gateway Device: the one credential-free reading most home routers offer.

1. SSDP: an M-SEARCH for `urn:schemas-upnp-org:device:InternetGatewayDevice:1` (and :2) to 239.255.255.250:1900,
   multicast TTL 2, answers collected for SEARCH_WAIT_S. An answer counts only when its LOCATION is plain http on
   the private address the answer came from, and (in a discovery run) that address is a hop of the traced path: a
   description is never fetched from another host, and a device that is not on the path is never asked.
2. The device description (one GET of LOCATION, no redirects, at most XML_MAX bytes): its name and model, and the
   WAN services it offers.
3. Only for the services the description lists, and only these read-only SOAP actions: GetExternalIPAddress and
   GetStatusInfo (WANIPConnection / WANPPPConnection), GetCommonLinkProperties (WANCommonInterfaceConfig). The
   control URL must be on the same host too.

XML is parsed safely: the parser refuses any DOCTYPE or entity declaration (where "billion laughs" and external
entities live) whatever the encoding, and nothing over XML_MAX bytes is read. The external address is used to judge where the home
ends (public: this device is the edge; private or CGNAT: another NAT follows) and never shown or logged.
"""

import asyncio
import socket
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Collection, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlsplit

import httpx

from pengumon.drivers.http import read_capped
from pengumon.drivers.upnp_wire import (
    COMMON_CONFIG, DATAGRAM_MAX, IGD_TYPES, IP_CONNECTION, PPP_CONNECTION, SSDP_GROUP, XML_MAX, msearch,
    parse_description, parse_soap_answer, soap_body, ssdp_location,
)
from pengumon.engine.discovery.addresses import CGNAT, LINK_LOCAL, PRIVATE, address_class, parse
from pengumon.observability import get_logger

_log = get_logger("pengumon.discovery")

SEARCH_WAIT_S = 2.0
MAX_ANSWERS = 64
MAX_ANSWERS_PER_SOURCE = 4  # a device answers once per search type; one noisy source cannot crowd the others out
# service type prefix -> the read-only actions asked of it
SERVICE_ACTIONS = {
    IP_CONNECTION: ("GetExternalIPAddress", "GetStatusInfo"),
    PPP_CONNECTION: ("GetExternalIPAddress", "GetStatusInfo"),
    COMMON_CONFIG: ("GetCommonLinkProperties",),
}

Search = Callable[[], Awaitable[List[Tuple[str, bytes]]]]  # -> (source address, datagram) of each answer


@dataclass
class IgdInfo:
    address: str  # the device's (private) LAN address
    friendly_name: Optional[str] = None
    manufacturer: Optional[str] = None
    model: Optional[str] = None
    external_ip: Optional[str] = None  # never shown or logged
    connection_status: Optional[str] = None
    uptime_s: Optional[int] = None
    access_type: Optional[str] = None  # WANAccessType: "DSL", "Cable", "Ethernet", "POTS", ...
    up_bps: Optional[int] = None
    down_bps: Optional[int] = None
    link_status: Optional[str] = None
    services: List[str] = field(default_factory=list)

    @property
    def external_class(self) -> Optional[str]:
        return address_class(self.external_ip)

    def describe(self) -> str:
        what = " ".join(p for p in (self.manufacturer, self.model) if p) or self.friendly_name or "a device"
        parts = [f"announces itself as an internet gateway (UPnP): {what}"]
        cls = self.external_class
        if cls == CGNAT:
            parts.append("its internet address is a carrier-grade NAT address: the provider's NAT comes next")
        elif cls in (PRIVATE, LINK_LOCAL):
            parts.append("its internet address is private: another router (NAT) is between it and the provider")
        elif cls is not None:
            parts.append("its internet address is public: it is the edge of your network")
        if self.access_type:
            parts.append(f"line type {self.access_type}")
        if self.down_bps and self.up_bps:
            parts.append(f"link {self.down_bps / 1e6:g}/{self.up_bps / 1e6:g} Mbit/s")
        return "; ".join(parts)


# --- SSDP -------------------------------------------------------------------------------------------------------

def parse_ssdp_answer(source: str, datagram: bytes) -> Optional[str]:
    """The description URL of an IGD's search answer, or None when the answer is not one, or its LOCATION is not
    plain http on the private address it came from."""
    location = ssdp_location(datagram)
    if location is None:
        return None
    parts = urlsplit(location)
    if parts.scheme != "http" or address_class(source) not in (PRIVATE, LINK_LOCAL):
        return None
    host_ip, source_ip = parse(parts.hostname), parse(source)
    if host_ip is None or host_ip != source_ip:
        return None
    return location


class _Collector(asyncio.DatagramProtocol):
    def __init__(self):
        self.answers: List[Tuple[str, bytes]] = []

    def datagram_received(self, data, addr):
        from_source = sum(1 for source, _ in self.answers if source == addr[0])
        if len(self.answers) < MAX_ANSWERS and from_source < MAX_ANSWERS_PER_SOURCE:
            self.answers.append((addr[0], data[:DATAGRAM_MAX + 1]))


def lan_source_address() -> Optional[str]:
    """The address this host would send the SSDP multicast from (connect() on a UDP socket sends nothing), or None."""
    probe = None
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(SSDP_GROUP)
        address = probe.getsockname()[0]
        return None if address in ("0.0.0.0", "") else address
    except OSError:
        return None
    finally:
        if probe is not None:
            probe.close()


async def ssdp_search(wait: float = SEARCH_WAIT_S) -> List[Tuple[str, bytes]]:
    """The answers to an IGD search on this host's LAN (IPv4 multicast)."""
    # Only the LAN interface the search goes out on listens for the answers (for the few seconds of the search), never
    # every interface: on a host that also has a public interface nothing listens there. No LAN address, no search.
    source = lan_source_address()
    if not source:
        return []
    loop = asyncio.get_running_loop()
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sock.setblocking(False)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(source))
        sock.bind((source, 0))
        transport, collector = await loop.create_datagram_endpoint(_Collector, sock=sock)
    except BaseException as exc:  # a socket error, or a cancel between creating the endpoint and returning
        if sock is not None:
            sock.close()
        if isinstance(exc, OSError):
            return []
        raise
    try:
        for st in IGD_TYPES:
            transport.sendto(msearch(st), SSDP_GROUP)
        await asyncio.sleep(wait)
    except OSError:
        pass
    finally:
        transport.close()
    return collector.answers


# --- XML: see drivers/upnp_wire.py ----------------------------------------

def _key(address: str) -> str:
    ip = parse(address)
    return str(ip) if ip is not None else (address or "").strip().lower()


def _int(value: Optional[str]) -> Optional[int]:
    """Plain ASCII digits as a number, else None (str.isdigit alone takes "²", which int() refuses)."""
    return int(value) if value and value.isascii() and value.isdigit() and len(value) < 16 else None


async def read_igd(location: str, client: httpx.AsyncClient, source: str) -> Optional[IgdInfo]:
    """The IGD behind a search answer: its description, then the read-only actions of the services it lists."""
    try:
        status, _headers, body, truncated = await read_capped(client, "GET", location, limit=XML_MAX)
    except httpx.HTTPError:
        return None
    if status != 200 or truncated:
        return None
    description = parse_description(body)
    if description is None:
        return None
    info = IgdInfo(address=source, friendly_name=description.friendly_name, manufacturer=description.manufacturer,
                   model=description.model, services=[st for st, _ in description.services])
    base = description.url_base or location
    answers: Dict[str, str] = {}
    for service_type, control in description.services:
        url = urljoin(base, control)
        target = parse(urlsplit(url).hostname)
        if urlsplit(url).scheme != "http" or target is None or target != parse(source):
            continue  # a control URL on another host is never called
        actions = next(a for prefix, a in SERVICE_ACTIONS.items() if service_type.startswith(prefix))
        for action in actions:
            headers = {"Content-Type": 'text/xml; charset="utf-8"', "SOAPAction": f'"{service_type}#{action}"'}
            try:
                status, _h, body, truncated = await read_capped(client, "POST", url, limit=XML_MAX,
                                                                headers=headers, content=soap_body(service_type, action))
            except httpx.HTTPError:
                continue
            if status == 200 and not truncated:
                for key, value in parse_soap_answer(body, action).items():
                    answers.setdefault(key, value)
    external = answers.get("NewExternalIPAddress")
    info.external_ip = external if parse(external) is not None else None
    info.connection_status = answers.get("NewConnectionStatus")
    info.uptime_s = _int(answers.get("NewUptime"))
    info.access_type = answers.get("NewWANAccessType")
    info.up_bps = _int(answers.get("NewLayer1UpstreamMaxBitRate"))
    info.down_bps = _int(answers.get("NewLayer1DownstreamMaxBitRate"))
    info.link_status = answers.get("NewPhysicalLinkStatus")
    return info


async def discover_igds(client: httpx.AsyncClient, search: Optional[Search] = None,
                        skip: Callable[[str], bool] = lambda address: False,
                        allowed: Optional[Collection[str]] = None) -> List[IgdInfo]:
    """Every IGD that answers on the LAN (one per address). `skip(address)` leaves a device alone after its
    search answer (no description fetched). With `allowed` (the addresses of the traced path), an answer from any
    other address is ignored: nothing is fetched from it, so a device that merely claims to be a gateway is never
    asked anything. A gateway whose answers break the reading is left out (logged); the others are still read."""
    answers = await (search or ssdp_search)()
    locations: Dict[str, str] = {}
    for source, datagram in answers:
        location = parse_ssdp_answer(source, datagram)
        if allowed is not None and _key(source) not in {_key(a) for a in allowed}:
            continue
        if location and source not in locations and not skip(source):
            locations[source] = location
    found = await asyncio.gather(*(read_igd(loc, client, src) for src, loc in locations.items()),
                                 return_exceptions=True)
    for source, result in zip(locations, found):
        if isinstance(result, Exception):
            _log.warning("A UPnP gateway's answers could not be read; it is left out", address=source,
                         error=type(result).__name__)
    return [igd for igd in found if isinstance(igd, IgdInfo)]
