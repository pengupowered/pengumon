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
import re
import socket
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Collection, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlsplit
from xml.etree import ElementTree
from xml.parsers import expat

import httpx

from pengumon.drivers.common import clean_text
from pengumon.engine.discovery.addresses import CGNAT, LINK_LOCAL, PRIVATE, address_class, parse
from pengumon.engine.discovery.devices import read_capped

SSDP_GROUP = ("239.255.255.250", 1900)
IGD_TYPES = ("urn:schemas-upnp-org:device:InternetGatewayDevice:1",
             "urn:schemas-upnp-org:device:InternetGatewayDevice:2")
SEARCH_WAIT_S = 2.0
DATAGRAM_MAX = 2048
MAX_ANSWERS = 16
XML_MAX = 64 * 1024

# service type prefix -> the read-only actions asked of it
SERVICE_ACTIONS = {
    "urn:schemas-upnp-org:service:WANIPConnection:": ("GetExternalIPAddress", "GetStatusInfo"),
    "urn:schemas-upnp-org:service:WANPPPConnection:": ("GetExternalIPAddress", "GetStatusInfo"),
    "urn:schemas-upnp-org:service:WANCommonInterfaceConfig:": ("GetCommonLinkProperties",),
}
_ANSWER_FIELDS = ("NewExternalIPAddress", "NewConnectionStatus", "NewUptime", "NewWANAccessType",
                  "NewLayer1UpstreamMaxBitRate", "NewLayer1DownstreamMaxBitRate", "NewPhysicalLinkStatus")

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

def msearch(st: str, mx: int = 2) -> bytes:
    return ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: \"ssdp:discover\"\r\n"
            f"MX: {mx}\r\nST: {st}\r\n\r\n").encode("ascii")


def parse_ssdp_answer(source: str, datagram: bytes) -> Optional[str]:
    """The description URL of an IGD's search answer, or None when the answer is not one, or its LOCATION is not
    plain http on the private address it came from."""
    if not isinstance(datagram, (bytes, bytearray)) or len(datagram) > DATAGRAM_MAX:
        return None
    text = bytes(datagram).decode("latin-1")
    lines = text.split("\r\n") if "\r\n" in text else text.split("\n")
    if not re.match(r"^HTTP/1\.[01] 200\b", lines[0] if lines else ""):
        return None
    headers: Dict[str, str] = {}
    for line in lines[1:]:
        name, sep, value = line.partition(":")
        if sep:
            headers[name.strip().lower()] = value.strip()
    if headers.get("st") not in IGD_TYPES:
        return None
    location = headers.get("location", "")
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
        if len(self.answers) < MAX_ANSWERS:
            self.answers.append((addr[0], data[:DATAGRAM_MAX + 1]))


async def ssdp_search(wait: float = SEARCH_WAIT_S) -> List[Tuple[str, bytes]]:
    """The answers to an IGD search on this host's LAN (IPv4 multicast)."""
    loop = asyncio.get_running_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sock.setblocking(False)
        sock.bind(("0.0.0.0", 0))
        transport, collector = await loop.create_datagram_endpoint(_Collector, sock=sock)
    except OSError:
        sock.close()
        return []
    try:
        for st in IGD_TYPES:
            transport.sendto(msearch(st), SSDP_GROUP)
        await asyncio.sleep(wait)
    except OSError:
        pass
    finally:
        transport.close()
    return collector.answers


# --- XML ----------------------------------------------------------------------------------------------------------

class _Refused(Exception):
    """The document declares a DTD or an entity."""


def _refuse(*_args) -> None:
    raise _Refused


def safe_xml(body: bytes) -> Optional[ElementTree.Element]:
    """The parsed document, or None for one that is too big, declares a DOCTYPE or an entity or is not well-formed.
    A first pass with an expat parser whose DOCTYPE and entity handlers raise decides on what the parser itself reads,
    whatever the encoding (a byte search for "<!DOCTYPE" misses UTF-16); only a document without a DTD is then built."""
    if not isinstance(body, (bytes, bytearray)) or len(body) > XML_MAX:
        return None
    data = bytes(body)
    guard = expat.ParserCreate()
    guard.StartDoctypeDeclHandler = _refuse
    guard.EntityDeclHandler = _refuse
    guard.UnparsedEntityDeclHandler = _refuse
    guard.ExternalEntityRefHandler = _refuse
    guard.SkippedEntityHandler = _refuse
    try:
        guard.Parse(data, True)
        return ElementTree.fromstring(data)
    except (_Refused, expat.ExpatError, ElementTree.ParseError, ValueError):
        return None


def _key(address: str) -> str:
    ip = parse(address)
    return str(ip) if ip is not None else (address or "").strip().lower()


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _child_text(element: ElementTree.Element, name: str) -> Optional[str]:
    for child in element:
        if _local(child.tag) == name:
            return clean_text(child.text or "", limit=100)
    return None


@dataclass
class Description:
    device_type: Optional[str]
    friendly_name: Optional[str]
    manufacturer: Optional[str]
    model: Optional[str]
    services: List[Tuple[str, str]]  # (service type, control URL as written)
    url_base: Optional[str] = None


def parse_description(body: bytes) -> Optional[Description]:
    """The root device and every WAN service listed anywhere in its device tree, or None when the document is not
    an IGD description (or is unsafe, see safe_xml)."""
    root = safe_xml(body)
    if root is None or _local(root.tag) != "root":
        return None
    device = next((c for c in root if _local(c.tag) == "device"), None)
    if device is None:
        return None
    device_type = _child_text(device, "deviceType")
    if device_type not in IGD_TYPES:
        return None
    services = []
    for element in device.iter():
        if _local(element.tag) != "service":
            continue
        st, control = _child_text(element, "serviceType"), _child_text(element, "controlURL")
        if st and control and any(st.startswith(prefix) for prefix in SERVICE_ACTIONS):
            services.append((st, control))
    model = " ".join(p for p in (_child_text(device, "modelName"), _child_text(device, "modelNumber")) if p)
    return Description(device_type=device_type, friendly_name=_child_text(device, "friendlyName"),
                       manufacturer=_child_text(device, "manufacturer"), model=clean_text(model) if model else None,
                       services=services[:8], url_base=_child_text(root, "URLBase"))


def soap_body(service_type: str, action: str) -> bytes:
    return ('<?xml version="1.0"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
            f'<s:Body><u:{action} xmlns:u="{service_type}"/></s:Body></s:Envelope>').encode("utf-8")


def parse_soap_answer(body: bytes, action: str) -> Dict[str, str]:
    """The known output arguments of an action's response ({} for a fault, an unsafe or a malformed answer)."""
    root = safe_xml(body)
    if root is None:
        return {}
    response = next((e for e in root.iter() if _local(e.tag) == f"{action}Response"), None)
    if response is None:
        return {}
    out = {}
    for child in response:
        name = _local(child.tag)
        if name in _ANSWER_FIELDS:
            value = clean_text(child.text or "", limit=64)
            if value:
                out[name] = value
    return out


def _int(value: Optional[str]) -> Optional[int]:
    return int(value) if value and value.isdigit() and len(value) < 16 else None


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
    asked anything."""
    answers = await (search or ssdp_search)()
    locations: Dict[str, str] = {}
    for source, datagram in answers:
        location = parse_ssdp_answer(source, datagram)
        if allowed is not None and _key(source) not in {_key(a) for a in allowed}:
            continue
        if location and source not in locations and not skip(source):
            locations[source] = location
    found = await asyncio.gather(*(read_igd(loc, client, src) for src, loc in locations.items()))
    return [igd for igd in found if igd is not None]
