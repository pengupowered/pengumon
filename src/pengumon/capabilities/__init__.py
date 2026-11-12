"""What a device can tell pengumon, independent of who made it.

A *driver* talks to one kind of device (an Xfinity XB gateway, an AsusWRT router) and turns its answers into the
readings below. Everything else (polling, rates, health, storage, the API) works on these readings and on
`Capability` names, never on a vendor. A new modem or router is a new driver; nothing else changes.

Readings carry what was measured and nothing else: a value the device did not report is None, and a capability
that produced nothing in a poll is listed in `PollResult.missing` with the reason.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, ClassVar, FrozenSet, Literal, Mapping, Optional, Protocol, Tuple

if TYPE_CHECKING:
    from pengumon.lines.docsis_model import DocsisChannel


class Capability(str, Enum):
    WAN_COUNTERS = "wan_counters"    # cumulative WAN byte counters (rates, line load)
    ACCESS_LINE = "access_line"      # the physical line to the ISP (DOCSIS RF levels and codeword errors, ...)
    ROUTER_SYSTEM = "router_system"  # router CPU, memory, connected clients
    CLIENT_INVENTORY = "client_inventory"  # the connected devices: connection type, band, signal, link rate, mesh node
    MESH = "mesh"                          # the mesh nodes and how many clients each carries
    TRAFFIC_USAGE = "traffic_usage"        # bytes per client and clock hour, and per app, as the device counted them


ErrorKind = Literal["unconfigured", "auth", "unreachable", "timeout", "protocol", "unsupported", "internal"]


class DriverError(Exception):
    """A poll that produced nothing. `kind` says what kind of failure it was (for logs, metrics and the UI);
    `transport` is True when the device could not be talked to at all (connection, timeout, HTTP layer)."""

    def __init__(self, kind: ErrorKind, message: str, *, transport: bool = False):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.transport = transport


@dataclass(frozen=True)
class WanCounters:
    """Cumulative WAN bytes since the router started. `counter_bits` is the counter width when the device's
    counters wrap (32 for UPnP IGD v1), None when they don't in practice (64-bit)."""

    rx_bytes: int
    tx_bytes: int
    counter_bits: Optional[int] = None
    read_at: Optional[float] = None  # wall-clock time of the answer that carried them (else: the end of the poll)


# An uptime longer than this (20 years) is not an uptime but a bad number from the device: it is dropped.
MAX_UPTIME_SECONDS = 20 * 365 * 86400


def valid_uptime(value: object) -> Optional[int]:
    """`value` as an uptime in whole seconds when it is one (a number from 0 to MAX_UPTIME_SECONDS), else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not 0 <= value <= MAX_UPTIME_SECONDS:  # (False for NaN too)
        return None
    return int(value)


@dataclass(frozen=True)
class SystemReading:
    """CPU and memory use in percent, the connected-client count, and `model`: what the device calls itself (its board
    or system description), when it says; the `model` of its config entry is shown instead when one is set. A driver
    that reads the uptime, the CPU temperature or the WAN link speed less often than every poll leaves them None on
    the polls in between (they are "read at this poll", not "the latest known"). `uptime_wrap_s` is set by a driver
    whose uptime counter wraps (SNMP sysUpTime: 2^32 hundredths of a second, about 497 days): an uptime that went back
    by that much is the counter wrapping, not a restart."""

    cpu_pct: Optional[float] = None
    mem_pct: Optional[float] = None
    client_count: Optional[int] = None
    model: Optional[str] = None
    uptime_s: Optional[int] = None        # seconds since the device last started, 0 to MAX_UPTIME_SECONDS
    cpu_temp_c: Optional[float] = None    # CPU temperature, °C
    wan_link_mbps: Optional[int] = None   # the WAN port's link speed; 0: no link
    uptime_wrap_s: Optional[float] = None  # the uptime goes back to 0 after this many seconds (None: it does not)


@dataclass(frozen=True)
class LineReading:
    """The access line. `technology` selects how it is judged (pengumon.lines); `channels` are DOCSIS channels
    for technology "docsis"."""

    technology: Literal["docsis"]
    channels: Tuple["DocsisChannel", ...] = ()


# Wi-Fi bands a client can be on. A driver that cannot tell a band leaves `Client.band` None; a band that is none of
# the first three (a second high band whose frequency the device does not say) is BAND_OTHER, never guessed as 6 GHz.
BAND_2G = "2.4 GHz"
BAND_5G = "5 GHz"
BAND_6G = "6 GHz"
BAND_OTHER = "5 GHz (2nd)/other"

Connection = Literal["wired", "wifi"]


@dataclass(frozen=True)
class Client:
    """One device connected (or recently connected) to the network. A value the device did not report is None.

    `key` identifies it for the lifetime of the process (the driver's choice, normally the MAC). `label_parts` are
    the names the device knows it by, best first (an owner-set name, a host name); `vendor` is the maker, if known; `kind` is what the device says it is in one plain word (phone, computer, game,
    network, printer), None when it is not known.
    `node` is the id of the mesh node it is on (MeshNode.id). `link_*_mbps` are the radio's negotiated link rates,
    not traffic. `connected_s` is how long it has been connected. An offline client keeps no signal or rates."""

    key: str
    label_parts: Tuple[str, ...] = ()
    vendor: Optional[str] = None
    kind: Optional[str] = None
    ip: Optional[str] = None
    mac: Optional[str] = None
    connection: Connection = "wired"
    band: Optional[str] = None
    node: Optional[str] = None
    rssi_dbm: Optional[int] = None
    link_tx_mbps: Optional[float] = None
    link_rx_mbps: Optional[float] = None
    connected_s: Optional[int] = None
    online: bool = True


@dataclass(frozen=True)
class ClientsReading:
    """The client list at one moment (CLIENT_INVENTORY). Offline entries the device still remembers are included."""

    clients: Tuple[Client, ...] = ()


@dataclass(frozen=True)
class MeshNode:
    """One node of a mesh. `id` is short and stable (the last six hex digits of its MAC, lower case); `label` is
    what to call it. `clients` counts the online clients on it, `wired` of them over a cable. `online` is whether the
    device says the node is up (None: it does not say); `backhaul` is how the node reaches its parent, "wired" or
    "wifi" (None: not known, or the main router), with the radio's `backhaul_band` and the signal
    `backhaul_rssi_dbm` for Wi-Fi."""

    id: str
    label: str
    clients: int = 0
    wired: int = 0
    online: Optional[bool] = None
    backhaul: Optional[str] = None
    backhaul_band: Optional[str] = None
    backhaul_rssi_dbm: Optional[int] = None


@dataclass(frozen=True)
class MeshReading:
    """The mesh nodes and their client counts (MESH)."""

    nodes: Tuple[MeshNode, ...] = ()


@dataclass(frozen=True)
class TrafficHour:
    """Bytes one client (or the whole WAN: `client` "") moved in one clock hour, as the device counted them. `client`
    is a MAC in normal form (drivers.common.normal_mac); `hour` the hour's start (epoch seconds)."""

    client: str
    hour: int
    up: int
    down: int


@dataclass(frozen=True)
class TrafficApps:
    """Bytes per application (the device's own app names) of one client ("" = every client) over the 24 hours that end
    at `window_end` (the start of the hour in progress when it was read: that hour is never counted). `apps` are
    (name, up, down), as the device listed them."""

    client: str
    window_end: int
    apps: Tuple[Tuple[str, int, int], ...] = ()


@dataclass(frozen=True)
class TrafficReading:
    """What one poll read of the device's traffic statistics (TRAFFIC_USAGE): a reading round spans several polls, and
    each poll hands on what it got. Hours that were read again replace what was stored for them."""

    read_at: float
    hours: Tuple[TrafficHour, ...] = ()
    apps: Tuple[TrafficApps, ...] = ()


Reading = object  # WanCounters | SystemReading | LineReading | ClientsReading | MeshReading | TrafficReading


@dataclass(frozen=True)
class PollResult:
    """What one poll produced: readings per capability, and for every offered capability without a reading,
    why (e.g. "router answer had no WAN counters")."""

    readings: Mapping[Capability, Reading]
    missing: Mapping[Capability, str] = field(default_factory=dict)


class Driver(Protocol):
    """One kind of device. `poll` raises DriverError (or lets a transport exception through, which the runner
    classifies) when the device produced nothing at all."""

    name: ClassVar[str]
    offers: ClassVar[FrozenSet[Capability]]
    # Drivers offering ACCESS_LINE also set `line_technology` ("docsis", ...): it selects how line faults are
    # judged and described (pengumon.lines).
    options_model: ClassVar[type]  # pydantic model of the `options:` a devices: entry may give (extra keys refused)
    default_poll_seconds: ClassVar[float]  # used when the devices: entry sets no poll_interval_seconds
    # Optional: `needs_sign_in = False` for a device with no password (UPnP): the settings page then asks for none.
    # Optional: `min_poll_seconds`, the least time between two reads of a device that is easily upset by them. A
    # shorter configured interval is raised to it, and Device never reads the device more often, whoever asks.

    @classmethod
    def from_config(cls, cfg, credentials, *, client=None) -> "Driver":
        """The driver for a DeviceConfig and its secrets.Credentials (`client`: an httpx client for tests).
        Everything the driver needs from config is read here, so adding a driver changes nothing elsewhere."""

    @property
    def host(self) -> str: ...

    def missing_configuration(self) -> Optional[str]:
        """None when the driver can poll, otherwise what is missing (e.g. "no password")."""

    def set_credentials(self, username: str, password: str) -> None: ...

    async def poll(self) -> PollResult: ...

    async def aclose(self) -> None: ...
