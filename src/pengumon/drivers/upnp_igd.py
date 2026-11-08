"""Any router that answers UPnP's Internet Gateway Device profile, with no password: WAN byte counters.

Read-only by protocol: only the SOAP actions GetTotalBytesReceived and GetTotalBytesSent (WANCommonInterfaceConfig)
and GetStatusInfo (WANIPConnection or WANPPPConnection, for its uptime), nothing that sets or opens anything, and
never the external IP address (it would be stored). Many routers have UPnP switched off; those answer nothing.

The device's address is either the URL of its UPnP description (`http://192.168.1.1:5000/rootDesc.xml`, as a
discovery run or the router's UPnP page shows it) or just its host. A bare host is asked directly, over UDP to its
port 1900, where the description is (unicast: no multicast at poll time); an answer that points anywhere but the
configured host is ignored. The description is read once and kept; it is read again after any failed poll, because
some routers choose a new port at every start. The control URLs it lists must be on the configured host, and no
redirect is followed: only the configured device is ever contacted.

UPnP counters are 32 bits wide (UPnP IGD v1), so a counter that went backwards wrapped, unless the router's uptime
went back too: then it restarted, and the poll says so (no rate for it). A router that reports a value over 32
bits has wider counters, and is read as such from then on. The link speed (GetCommonLinkProperties) is a router's
claim about its line, not a measurement, and has no place in the stored readings, so it is not read.
"""

import asyncio
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Awaitable, Callable, ClassVar, Optional, Tuple
from urllib.parse import urljoin, urlsplit

import httpx
from pydantic import BaseModel, ConfigDict

from pengumon.capabilities import Capability, DriverError, PollResult, WanCounters
from pengumon.capabilities.contrib import DriverNotes
from pengumon.drivers.http import DeviceHttp, Verify, device_verify
from pengumon.drivers.upnp_wire import (
    COMMON_CONFIG, IGD_TYPES, IP_CONNECTION, PPP_CONNECTION, SSDP_GROUP, XML_MAX, msearch, parse_description,
    parse_soap_answer, soap_body, ssdp_location,
)
from pengumon.observability import get_logger

if TYPE_CHECKING:
    from pengumon.devices.settings import DeviceConfig
    from pengumon.secrets import Credentials

SEARCH_WAIT_SECONDS = 2.0
COUNTER_BITS = 32
RX_ACTION, TX_ACTION, STATUS_ACTION = "GetTotalBytesReceived", "GetTotalBytesSent", "GetStatusInfo"
RX_FIELD, TX_FIELD, UPTIME_FIELD = "NewTotalBytesReceived", "NewTotalBytesSent", "NewUptime"

# Looks for the description of a host: its address (a datagram), as a coroutine so that tests can answer for it.
Locate = Callable[[str], Awaitable[Optional[str]]]


class UpnpIgdOptions(BaseModel):
    """The driver takes no options (a devices: entry's `options:` must be empty)."""
    model_config = ConfigDict(extra="forbid")


@dataclass(frozen=True)
class _Layout:
    """Where the actions go: the control URL and service type of each service the description lists."""

    counters_url: str
    counters_type: str
    status_url: Optional[str]
    status_type: Optional[str]


class _Collector(asyncio.DatagramProtocol):
    def __init__(self, found: "asyncio.Future[Optional[str]]"):
        self._found = found

    def datagram_received(self, data, addr):
        location = ssdp_location(data)
        if location and not self._found.done():
            self._found.set_result(location)


async def ask_host(host: str, wait: float = SEARCH_WAIT_SECONDS, port: int = SSDP_GROUP[1]) -> Optional[str]:
    """The LOCATION that `host` itself answers to an IGD search (unicast to its UDP port, 1900), or None. The socket is
    connected to the host, so no datagram from any other address is received."""
    loop = asyncio.get_running_loop()
    found: "asyncio.Future[Optional[str]]" = loop.create_future()
    try:
        transport, _ = await loop.create_datagram_endpoint(lambda: _Collector(found),
                                                           remote_addr=(host, port))
    except OSError:
        return None
    try:
        for st in IGD_TYPES:
            transport.sendto(msearch(st))
        return await asyncio.wait_for(found, wait)
    except (asyncio.TimeoutError, OSError):
        return None
    finally:
        transport.close()


def _same_host(url: str, host: str) -> bool:
    return (urlsplit(url).hostname or "").lower() == host.lower()


def _counter(answer: dict, name: str) -> Optional[int]:
    """The field as a whole number, or None when it is missing or not plain ASCII digits (str.isdigit alone takes "²",
    which int() refuses, and other scripts' digits)."""
    value = answer.get(name)
    return int(value) if value is not None and value.isascii() and value.isdigit() and len(value) <= 20 else None


class UpnpIgdDriver:
    name = "upnp_igd"
    label = "UPnP router"  # what the device is, in the dashboard's words (devices.summary.device_label)
    offers = frozenset({Capability.WAN_COUNTERS})
    options_model = UpnpIgdOptions
    needs_sign_in = False  # UPnP has no password: the settings page asks for none
    default_poll_seconds = 30.0  # two or three small requests per poll
    # The AI pack's note on this driver (capabilities/contrib.py). Each statement is established by this file:
    #  - UPnP gateway protocol, no sign-in: the module docstring;
    #  - counters read every {poll}: the device entry's interval, `default_poll_seconds` when it sets none;
    #  - 32-bit counters, a counter that went backwards is a wrap unless the uptime also went back: COUNTER_BITS and
    #    `_poll` (`restarted`);
    #  - no CPU, memory or client list: `offers` is WAN_COUNTERS alone.
    pack_notes: ClassVar[DriverNotes] = DriverNotes(
        full=("Device notes: the router is read through the standard UPnP gateway protocol, which needs no sign-in. "
              "Its WAN byte counters are read every {poll}. They are 32-bit counters (they wrap at 4.3 GB): a "
              "counter that goes back is taken as a wrap, unless the router's uptime also went back, which is a "
              "restart. It reports no CPU, memory or client list."),
        anonymized=("Device notes: the router reports WAN byte counters every {poll}, as 32-bit counters (a wrap "
                    "at 4.3 GB is not a restart). It reports no CPU, memory or client list."),
    )

    def __init__(self, url: str, *, client: Optional[httpx.AsyncClient] = None, verify: Verify = True,
                 locate: Optional[Locate] = None, clock: Callable[[], float] = time.monotonic,
                 wall_clock: Callable[[], float] = time.time):
        self._url = url
        self._verify = verify
        self._locate = locate or ask_host
        self._wall_clock = wall_clock
        self.http = DeviceHttp(url, self._new_client, driver=self.name, client=client)
        self._log = get_logger("pengumon.drivers.upnp_igd", host=self.http.host)
        self._layout: Optional[_Layout] = None
        self._bits: Optional[int] = COUNTER_BITS
        self._uptime: Optional[int] = None

    @classmethod
    def from_config(cls, cfg: "DeviceConfig", credentials: "Credentials", *,
                    client: Optional[httpx.AsyncClient] = None, **clocks: Callable[[], float]) -> "UpnpIgdDriver":
        """`credentials` is not used: UPnP has none. `clocks` (clock, wall_clock) are for tests."""
        cls.options_model.model_validate(cfg.options)
        return cls(cfg.url, client=client, verify=device_verify(cfg), **clocks)

    def _new_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=6.0, verify=self._verify, follow_redirects=False)

    @property
    def host(self) -> str:
        return self.http.host

    def missing_configuration(self) -> Optional[str]:
        return None

    def set_credentials(self, username: str, password: str) -> None:
        """Nothing to keep: the device has no sign-in."""

    async def aclose(self) -> None:
        await self.http.aclose()

    # --- the description ------------------------------------------------------------------------------------------

    async def _description_url(self) -> str:
        if urlsplit(self._url).path not in ("", "/"):
            return self._url
        location = await self._locate(self.host)
        if not location or urlsplit(location).scheme != "http" or not _same_host(location, self.host):
            raise DriverError("unsupported", f"{self.host} did not tell where its UPnP description is. UPnP may be "
                                             "switched off on the router; or give the full address of its "
                                             "description (http://.../rootDesc.xml) as the device's address")
        return location

    async def _get(self, url: str, action: Optional[str] = None, service_type: str = "") -> Tuple[int, bytes]:
        """(status, body) of the description (a GET) or of one SOAP action (a POST); a redirect, a body over the
        limit and a cut-off body are protocol errors. Transport errors propagate."""
        if action is None:
            status, _, body, truncated = await self.http.request_capped("GET", url, XML_MAX)
        else:
            headers = {"Content-Type": 'text/xml; charset="utf-8"', "SOAPAction": f'"{service_type}#{action}"'}
            status, _, body, truncated = await self.http.request_capped(
                "POST", url, XML_MAX, headers=headers, content=soap_body(service_type, action))
        if 300 <= status < 400:
            raise DriverError("protocol", f"{self.host} answered {action or 'the description request'} with a "
                                          "redirect, which is not followed")
        if truncated:
            raise DriverError("protocol", f"{self.host}'s answer was longer than {XML_MAX // 1024} KB")
        return status, body

    async def _read_layout(self) -> _Layout:
        url = await self._description_url()
        status, body = await self._get(url)
        if status != 200:
            raise DriverError("protocol", f"{self.host} answered the UPnP description request with HTTP {status}")
        description = parse_description(body)
        if description is None:
            raise DriverError("protocol", f"{self.host} did not answer with a UPnP internet gateway description")
        base = description.url_base or url
        found = {}
        for service_type, control in description.services:
            target = urljoin(base, control)
            if urlsplit(target).scheme not in ("http", "https") or not _same_host(target, self.host):
                continue  # a control URL on another host is never called
            for prefix in (COMMON_CONFIG, IP_CONNECTION, PPP_CONNECTION):
                if service_type.startswith(prefix):
                    found.setdefault(prefix, (target, service_type))
        if COMMON_CONFIG not in found:
            raise DriverError("unsupported", f"{self.host} offers no WAN traffic counters through UPnP "
                                             "(no WANCommonInterfaceConfig service)")
        status_service = found.get(IP_CONNECTION) or found.get(PPP_CONNECTION)
        return _Layout(found[COMMON_CONFIG][0], found[COMMON_CONFIG][1],
                       status_service[0] if status_service else None, status_service[1] if status_service else None)

    # --- poll -----------------------------------------------------------------------------------------------------

    async def _action(self, url: str, service_type: str, action: str, field: str) -> Optional[int]:
        status, body = await self._get(url, action, service_type)
        if status != 200:
            raise DriverError("protocol", f"{self.host} refused {action} (HTTP {status})")
        return _counter(parse_soap_answer(body, action, (field,)), field)

    async def poll(self) -> PollResult:
        try:
            return await self._poll()
        except (httpx.HTTPError, DriverError):
            self._layout = None  # the description is read again next time (the router may have a new port)
            raise

    async def _poll(self) -> PollResult:
        if self._layout is None:
            self._layout = await self._read_layout()
        layout = self._layout
        rx = await self._action(layout.counters_url, layout.counters_type, RX_ACTION, RX_FIELD)
        tx = await self._action(layout.counters_url, layout.counters_type, TX_ACTION, TX_FIELD)
        read_at = self._wall_clock()
        if rx is None or tx is None:
            raise DriverError("protocol", f"{self.host} answered without WAN byte counters")
        restarted = False
        if layout.status_url and layout.status_type:
            restarted = await self._restarted(layout)
        if max(rx, tx) >> COUNTER_BITS and self._bits:
            self._log.info("the router reports counters over 32 bits: read as wider from now on")
            self._bits = None
        return PollResult(readings={Capability.WAN_COUNTERS: WanCounters(
            rx_bytes=rx, tx_bytes=tx, counter_bits=None if restarted else self._bits, read_at=read_at)})

    async def read_wan_counters(self) -> Optional[WanCounters]:
        """The WAN counters alone, for a running speed test (Device.read_wan_counters, about once a second): the two
        counter actions of the poll, with the description the polls read (None before the first poll read it). No
        uptime check: a restart in the middle of a test is left to the sampler, which drops a reading that goes back
        further than a wrap explains. Gateways commonly read the interface statistics when asked (miniupnpd does);
        32-bit counters wrap every 34 s at 1 Gbit/s, which one reading a second follows."""
        layout = self._layout
        if layout is None:
            return None
        rx = await self._action(layout.counters_url, layout.counters_type, RX_ACTION, RX_FIELD)
        tx = await self._action(layout.counters_url, layout.counters_type, TX_ACTION, TX_FIELD)
        if rx is None or tx is None:
            return None
        return WanCounters(rx_bytes=rx, tx_bytes=tx, counter_bits=self._bits, read_at=self._wall_clock())

    async def _restarted(self, layout: _Layout) -> bool:
        """Whether the router's uptime went back since the last poll (its counters started again from zero, which is
        not a wrap). A router that does not answer GetStatusInfo is never taken to have restarted."""
        try:
            uptime = await self._action(layout.status_url, layout.status_type, STATUS_ACTION, UPTIME_FIELD)
        except (httpx.HTTPError, DriverError):
            uptime = None
        before, self._uptime = self._uptime, uptime
        return before is not None and uptime is not None and uptime < before
