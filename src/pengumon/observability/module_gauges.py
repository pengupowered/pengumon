"""The gauges of the device modules (cable line, line load, router, Wi-Fi and mesh), in one table that `gauges.py`
appends to GAUGES at its end (import `pengumon.observability.gauges` first, never this module on its own: it needs
`Gauge` from there).

Each gauge reads the latest state of the devices that offer a capability (`state.devices.providing`), synchronously, as
the export-thread rule in `gauges.py` requires. A device without a reading yet (not polled, error, not configured)
adds no sample, so a series is absent rather than zero. Labels are the device id and small fixed enums, never an
address, a name, a node label or a channel id (channel ids move when a modem re-ranges, and a series per channel would be
about 120 per modem): the per-channel view is Pengumon's own channel history.
"""

from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from pengumon.capabilities import BAND_2G, BAND_5G, BAND_6G, Capability, ClientsReading, MeshReading
from pengumon.devices.snapshots import RouterMetricsSnapshot
from pengumon.grades import CRITICAL, SIGNAL_LEVELS, WARN
from pengumon.lines.docsis import NO_READING_STATUSES
from pengumon.lines.docsis_model import DocsisSignalSnapshot
from pengumon.observability.gauges import Gauge, Sample

WEAK_SIGNAL_DBM = SIGNAL_LEVELS[-1][0]  # at or below this a client's signal is "Weak"
_BANDS = {BAND_2G: "2g", BAND_5G: "5g", BAND_6G: "6g"}  # any other band: "other"
_BAND_NAMES = ("2g", "5g", "6g", "other")
_GRADES = ("good", "warn", "critical")


def _devices(state: Any, capability: Capability) -> tuple:
    return state.devices.providing(capability) if state.devices is not None else ()


def _line_snapshots(state: Any) -> Iterable[Tuple[str, DocsisSignalSnapshot]]:
    for device in _devices(state, Capability.ACCESS_LINE):
        snap = device.latest_snapshot
        if isinstance(snap, DocsisSignalSnapshot) and snap.status not in NO_READING_STATUSES:
            yield device.id, snap


def _router_snapshots(state: Any, capability: Capability) -> Iterable[Tuple[str, RouterMetricsSnapshot]]:
    for device in _devices(state, capability):
        snap = device.latest_snapshot
        if isinstance(snap, RouterMetricsSnapshot) and snap.status not in NO_READING_STATUSES:
            yield device.id, snap


def _readings(state: Any, attribute: str, kind: type) -> Iterable[Tuple[str, Any]]:
    """(device id, the monitor's latest client or mesh reading) for devices that have read one."""
    capability = Capability.CLIENT_INVENTORY if attribute == "latest_clients" else Capability.MESH
    for device in _devices(state, capability):
        reading = getattr(device.monitor, attribute, None)
        if isinstance(reading, kind):
            yield device.id, reading


def _min_avg_max(values: List[float]) -> Dict[str, float]:
    return {"min": min(values), "avg": sum(values) / len(values), "max": max(values)}


def _percentile(values: List[float], fraction: float) -> float:
    """Nearest rank from below: the value at index floor(fraction * (n - 1)) of the sorted list."""
    ordered = sorted(values)
    return ordered[int(fraction * (len(ordered) - 1))]


# --- cable line (DOCSIS) -------------------------------------------------------------------------------------------

def _docsis_power(state: Any) -> List[Sample]:
    samples: List[Sample] = []
    for device, snap in _line_snapshots(state):
        for direction in ("downstream", "upstream"):
            values = [c.power_dbmv for c in snap.channels if c.direction == direction]
            if values:
                samples += [({"device": device, "direction": direction, "stat": stat}, value)
                            for stat, value in _min_avg_max(values).items()]
    return samples


def _docsis_snr(state: Any) -> List[Sample]:
    samples: List[Sample] = []
    for device, snap in _line_snapshots(state):
        values = [c.snr_db for c in snap.channels if c.direction == "downstream" and c.snr_db is not None]
        if values:
            stats = _min_avg_max(values)
            samples += [({"device": device, "stat": stat}, stats[stat]) for stat in ("min", "avg")]
    return samples


def _worst_grade(grades: Dict[str, str]) -> Optional[str]:
    if not grades:
        return None
    return "critical" if CRITICAL in grades.values() else "warn" if WARN in grades.values() else "good"


def _docsis_channels(state: Any) -> List[Sample]:
    samples: List[Sample] = []
    for device, snap in _line_snapshots(state):
        counts = {(c.direction, g): 0 for c in snap.channels for g in _GRADES}
        for channel in snap.channels:
            grade = _worst_grade(channel.grades)
            if grade is not None:
                counts[(channel.direction, grade)] += 1
        samples += [({"device": device, "direction": direction, "grade": grade}, n)
                    for (direction, grade), n in sorted(counts.items())]
    return samples


# --- line load and the router --------------------------------------------------------------------------------------

def _wan_rate(state: Any) -> List[Sample]:
    samples: List[Sample] = []
    for device, snap in _router_snapshots(state, Capability.WAN_COUNTERS):
        samples += [({"device": device, "direction": direction}, rate)
                    for direction, rate in (("down", snap.wan_rx_rate_mbps), ("up", snap.wan_tx_rate_mbps))
                    if rate is not None]
    return samples


def _router_figure(field: str) -> Callable[[Any], List[Sample]]:
    def collect(state: Any) -> List[Sample]:
        return [({"device": device}, getattr(snap, field)) for device, snap in
                _router_snapshots(state, Capability.ROUTER_SYSTEM) if getattr(snap, field) is not None]
    return collect


# --- Wi-Fi and mesh ------------------------------------------------------------------------------------------------

def _band(client) -> str:
    return _BANDS.get(client.band, "other")


def _online_wifi(reading: ClientsReading) -> list:
    return [c for c in reading.clients if c.online and c.connection == "wifi"]


def _clients(state: Any) -> List[Sample]:
    samples: List[Sample] = []
    for device, reading in _readings(state, "latest_clients", ClientsReading):
        wifi = _online_wifi(reading)
        samples.append(({"device": device, "connection": "wired", "band": "none"},
                        sum(c.online and c.connection == "wired" for c in reading.clients)))
        samples += [({"device": device, "connection": "wifi", "band": band}, sum(_band(c) == band for c in wifi))
                    for band in _BAND_NAMES]
    return samples


def _wifi_signal(state: Any) -> List[Sample]:
    samples: List[Sample] = []
    for device, reading in _readings(state, "latest_clients", ClientsReading):
        for band in _BAND_NAMES:
            values = [c.rssi_dbm for c in _online_wifi(reading) if _band(c) == band and c.rssi_dbm is not None]
            if values:
                samples += [({"device": device, "band": band, "stat": "p10"}, _percentile(values, 0.1)),
                            ({"device": device, "band": band, "stat": "median"}, _percentile(values, 0.5))]
    return samples


def _wifi_weak(state: Any) -> List[Sample]:
    return [({"device": device, "band": band},
             sum(_band(c) == band and c.rssi_dbm is not None and c.rssi_dbm <= WEAK_SIGNAL_DBM
                 for c in _online_wifi(reading)))
            for device, reading in _readings(state, "latest_clients", ClientsReading) for band in _BAND_NAMES]


def _mesh_nodes(state: Any) -> List[Sample]:
    samples: List[Sample] = []
    for device, mesh in _readings(state, "latest_mesh", MeshReading):
        if mesh.nodes:
            states = [("online" if n.online else "offline" if n.online is False else "unknown") for n in mesh.nodes]
            samples += [({"device": device, "state": s}, states.count(s)) for s in ("online", "offline", "unknown")]
    return samples


def _mesh_node_clients(state: Any) -> List[Sample]:
    samples: List[Sample] = []
    for device, mesh in _readings(state, "latest_mesh", MeshReading):
        if mesh.nodes:
            counts = [n.clients for n in mesh.nodes]
            samples += [({"device": device, "stat": "min"}, min(counts)), ({"device": device, "stat": "max"}, max(counts))]
    return samples


def _mesh_backhaul(state: Any) -> List[Sample]:
    samples: List[Sample] = []
    for device, mesh in _readings(state, "latest_mesh", MeshReading):
        values = [n.backhaul_rssi_dbm for n in mesh.nodes if n.backhaul == "wifi" and n.backhaul_rssi_dbm is not None]
        if values:
            samples.append(({"device": device, "stat": "min"}, min(values)))
    return samples


# Prometheus names are what the collector's OTLP translation makes of the OTel name and unit (a unit that is not in
# braces is appended: dBmV, dB, dBm, percent), so /metrics and Prometheus-over-OTLP agree.
MODULE_GAUGES: List[Gauge] = [
    Gauge("pengumon_line_docsis_power_dBmV", "pengumon.line.docsis.power", "dBmV",
          "Channel power of the cable line by direction (downstream, upstream) and stat (min, avg, max).", _docsis_power),
    Gauge("pengumon_line_docsis_snr_dB", "pengumon.line.docsis.snr", "dB",
          "Downstream channel SNR of the cable line, by stat (min, avg).", _docsis_snr),
    Gauge("pengumon_line_docsis_channels", "pengumon.line.docsis.channels", "{channel}",
          "Cable line channels by direction and grade (good, warn, critical; a channel counts at its worst grade).",
          _docsis_channels),
    Gauge("pengumon_wan_rate_Mbit_per_second", "pengumon.wan.rate", "Mbit/s",
          "WAN rate of the router by direction (down, up).", _wan_rate),
    Gauge("pengumon_router_cpu_percent", "pengumon.router.cpu", "%", "Router CPU use.", _router_figure("cpu_usage_pct")),
    Gauge("pengumon_router_memory_percent", "pengumon.router.memory", "%", "Router memory use.",
          _router_figure("mem_usage_pct")),
    Gauge("pengumon_clients", "pengumon.clients", "{client}",
          "Online clients by connection (wired, wifi) and band (2g, 5g, 6g, other; none for wired).", _clients),
    Gauge("pengumon_clients_wifi_signal_dBm", "pengumon.clients.wifi.signal", "dBm",
          "Signal of the online Wi-Fi clients by band and stat (p10: the weakest tenth, median).", _wifi_signal),
    Gauge("pengumon_clients_wifi_weak", "pengumon.clients.wifi.weak", "{client}",
          "Online Wi-Fi clients at or below -75 dBm (a Weak signal), by band.", _wifi_weak),
    Gauge("pengumon_mesh_nodes", "pengumon.mesh.nodes", "{node}",
          "Mesh nodes by state (online, offline, unknown).", _mesh_nodes),
    Gauge("pengumon_mesh_node_clients", "pengumon.mesh.node.clients", "{client}",
          "Clients per mesh node, the fewest and the most (stat min, max).", _mesh_node_clients),
    Gauge("pengumon_mesh_backhaul_signal_dBm", "pengumon.mesh.backhaul.signal", "dBm",
          "Signal of the weakest Wi-Fi backhaul link in the mesh.", _mesh_backhaul),
]
