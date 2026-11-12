"""The stored forms of device readings (schema v9): one row per poll in `device_samples`, one per access-line reading
in `line_samples`, both keyed by device id.

These are storage records, not API models: the repository writes and reads them as they are, and the code that knows
a device kind converts them (pengumon.devices.samples for router snapshots, pengumon.lines.docsis for DOCSIS ones).
Field names are the column names.
"""

from dataclasses import dataclass, fields
from typing import Optional, Tuple


@dataclass(frozen=True)
class DeviceSample:
    """One poll of a device: its outcome and its scalar readings, one nullable column per reading. A device that does
    not offer a capability leaves that capability's columns NULL, which SQLite stores in one header byte each."""

    device_id: str
    ts: float
    status: str
    error_kind: Optional[str] = None     # DriverError kind of an error row
    error_message: Optional[str] = None
    # wan_counters: the cumulative counters and the rates derived from them (None: no rate for this poll)
    wan_rx_bytes: Optional[int] = None
    wan_tx_bytes: Optional[int] = None
    wan_rx_mbps: Optional[float] = None
    wan_tx_mbps: Optional[float] = None
    # router_system
    cpu_pct: Optional[float] = None
    mem_pct: Optional[float] = None
    client_count: Optional[int] = None
    model: Optional[str] = None  # the configured model (the device API does not report one)
    # client_inventory (schema v10): aggregate counts of online clients, on the polls that read the client list (NULL
    # on the others). Never a client: no names, addresses or MACs are stored.
    clients_online: Optional[int] = None
    clients_wired: Optional[int] = None
    clients_wifi_2g: Optional[int] = None
    clients_wifi_5g: Optional[int] = None
    clients_wifi_other: Optional[int] = None  # any other band
    # router_system (schema v19), on the polls that read them (NULL on the others)
    uptime_s: Optional[int] = None
    cpu_temp_c: Optional[float] = None
    wan_link_mbps: Optional[int] = None  # 0: no link on the WAN port


@dataclass(frozen=True)
class LineSample:
    """One reading of an access line. The counters every line technology has are columns; the rest is the
    technology's own business: `summary` (a few values) and `detail` (per channel, per pair, ...) are compact JSON
    that only the technology's module (pengumon.lines.<technology>) writes and reads."""

    device_id: str
    ts: float
    technology: str  # "docsis", ...
    status: str
    error_kind: Optional[str] = None
    error_message: Optional[str] = None
    errors_total: Optional[int] = None   # uncorrectable errors since the device started counting; None: no reading
    errors_recent: Optional[int] = None  # of those, new since the previous reading
    summary: Optional[str] = None
    detail: Optional[str] = None


@dataclass(frozen=True)
class MeshCount:
    """Online clients on one mesh node at one client-list reading (schema v10)."""

    device_id: str
    ts: float  # the ts of the device_samples row of the same reading
    node_id: str
    clients: int


DEVICE_SAMPLE_COLUMNS: Tuple[str, ...] = tuple(f.name for f in fields(DeviceSample))
LINE_SAMPLE_COLUMNS: Tuple[str, ...] = tuple(f.name for f in fields(LineSample))
