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


@dataclass(frozen=True)
class SystemReading:
    """CPU and memory use in percent, the connected-client count, and `model`: what the device calls itself (its board
    or system description), when it says; the `model` of its config entry is shown instead when one is set."""

    cpu_pct: Optional[float] = None
    mem_pct: Optional[float] = None
    client_count: Optional[int] = None
    model: Optional[str] = None


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


Reading = object  # WanCounters | SystemReading | LineReading | ClientsReading | MeshReading


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
