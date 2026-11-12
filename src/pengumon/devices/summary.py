"""The short form of a device's snapshot that rides the live stream: status, grades and the headline figures, never
the per-channel detail (that is GET /api/devices/{id}, which the channels view fetches while it is open).

A device that lists its clients adds `clients`: the counts of its last client-list read and its mesh nodes' counts,
never a client row (names, addresses and MACs stay in GET /api/devices/{id}/clients, behind the privacy setting and
its rate limit). The router card shows these counts without fetching the list.

A summary only changes when the device is polled, so `LiveSummaries` keeps the last one per device and builds a new
one only when the monitor holds a different snapshot object (every poll, error and "not configured" makes a new one)
or a different client or mesh reading.
"""

from typing import Any, Dict, Optional

from pengumon.capabilities import BAND_OTHER, ClientsReading, MeshReading
from pengumon.devices.registry import DeviceRegistry
from pengumon.devices.snapshots import RouterMetricsSnapshot
from pengumon.lines.docsis_model import DocsisSignalSnapshot

_LINE_FIELDS = ("downstream_power_avg", "downstream_snr_avg", "upstream_power_avg", "uncorrectables_total",
                "uncorrectables_recent", "uncorrectables_interval_seconds", "uncorrectables_ratio",
                "errors_affected_channels", "errors_span_mhz", "errors_note", "errors_probe_loss")
_ROUTER_FIELDS = ("wan_rx_rate_mbps", "wan_tx_rate_mbps", "wan_rx_load_pct", "wan_tx_load_pct", "cpu_usage_pct", "mem_usage_pct",
                  "client_count", "model_name", "uptime_s", "cpu_temp_c", "wan_link_mbps")


# A failed poll's message is the driver's own words, which can name the device's host, the login it tried (OpenWrt:
# "refused the login of 'admin'") or the address a socket dialed. Whoever may not edit (a viewer, the MCP tools, the
# read token) gets a fixed sentence by the kind of failure instead. The messages of a device that is not set up
# ("unconfigured") are Pengumon's own, with the device id only, and stay.
POLL_FAILURES = {
    "auth": "The device refused Pengumon's sign-in: check its username and password in Settings.",
    "unreachable": "The device could not be reached.",
    "timeout": "The device did not answer in time.",
    "protocol": "The device answered, but not in a way Pengumon understands.",
    "unsupported": "The device does not offer what Pengumon reads from it.",
    "internal": "Pengumon failed while reading the device; see its log.",
}
POLL_FAILED = "The last poll of the device failed."
SIGN_IN_FOR_MORE = " Sign in to see the device's own message."


def viewer_error(status: Optional[str], kind: Optional[str], message: Optional[str]) -> Optional[str]:
    """A failed poll's message as a viewer gets it: fixed words by its kind (POLL_FAILURES)."""
    if not message or status == "unconfigured":
        return message
    return POLL_FAILURES.get(kind, POLL_FAILED) + SIGN_IN_FOR_MORE


def viewer_devices(devices: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """The live message's `devices` as a connection that may not edit gets it: each failed poll's message in fixed
    words (viewer_error). The same dict when no device has one (the usual case); else a copy, the entries that change
    copied too."""
    if not any(d.get("error_message") for d in devices.values()):
        return devices
    return {i: ({**d, "error_message": viewer_error(d.get("status"), d.get("error_kind"), d["error_message"])}
                if d.get("error_message") else d) for i, d in devices.items()}


def device_label(device: Any) -> str:
    """The device as a person names it: what it is (the driver's `label`, e.g. "ASUS router") and its model from
    config.yaml, or its id when no model is set ("Xfinity gateway · modem"); never only an address. GET /api/devices
    and the MCP `devices` tool show it."""
    kind = getattr(device.driver, "label", None) or device.driver.name
    model = getattr(device.config, "model", None)
    return f"{kind} · {str(model).replace('_', ' ') if model else device.id}"


def summarize_snapshot(snapshot: Any) -> Dict[str, Any]:
    """The summary of one snapshot: `kind` ("line" or "router"), its status and error, timestamp, grades and
    the headline figures of its kind."""
    if isinstance(snapshot, DocsisSignalSnapshot):
        kind, fields = "line", _LINE_FIELDS
    elif isinstance(snapshot, RouterMetricsSnapshot):
        kind, fields = "router", _ROUTER_FIELDS
    else:
        return {"kind": "unknown", "status": getattr(snapshot, "status", None)}
    out: Dict[str, Any] = {"kind": kind, "status": snapshot.status, "timestamp": snapshot.timestamp,
                           "grades": dict(snapshot.grades)}
    if snapshot.error_message:
        out["error_message"] = snapshot.error_message
        out["error_kind"] = snapshot.error_kind  # what kind of failure (viewer_devices words the message by it)
    out.update({name: getattr(snapshot, name) for name in fields})
    if kind == "line":
        out["channel_count"] = len(snapshot.channels)
    return out


def summarize_clients(reading: Optional[ClientsReading]) -> Dict[str, Any]:
    """Counts of the online clients of a reading: online, wired, Wi-Fi clients by band and clients by mesh node id."""
    online = [c for c in (reading.clients if reading is not None else ()) if c.online]
    by_band: Dict[str, int] = {}
    by_node: Dict[str, int] = {}
    for c in online:
        if c.connection == "wifi":
            by_band[c.band or BAND_OTHER] = by_band.get(c.band or BAND_OTHER, 0) + 1
        if c.node:
            by_node[c.node] = by_node.get(c.node, 0) + 1
    return {"online": len(online), "wired": sum(c.connection == "wired" for c in online),
            "by_band": by_band, "by_node": by_node}


def mesh_nodes(mesh: Optional[MeshReading]) -> list:
    """The mesh nodes as plain dicts: id, label, their client counts, whether the node is online (null: not known) and
    how it connects to its parent (`backhaul`: "wired", "wifi" or null, with `backhaul_band` and `backhaul_rssi_dbm`)."""
    return [{"id": n.id, "label": n.label, "clients": n.clients, "wired": n.wired, "online": n.online,
             "backhaul": n.backhaul, "backhaul_band": n.backhaul_band, "backhaul_rssi_dbm": n.backhaul_rssi_dbm}
            for n in (mesh.nodes if isinstance(mesh, MeshReading) else ())]


def client_counts(monitor: Any) -> Optional[Dict[str, Any]]:
    """The `clients` part of a summary from a monitor's last client and mesh readings; None until a list was read."""
    reading = getattr(monitor, "latest_clients", None)
    if not isinstance(reading, ClientsReading):
        return None
    return {"ts": getattr(monitor, "clients_at", None), **summarize_clients(reading),
            "nodes": mesh_nodes(getattr(monitor, "latest_mesh", None))}


class LiveSummaries:
    """Summaries of the enabled devices of a registry, rebuilt per device only when its snapshot changed."""

    def __init__(self, registry: Optional[DeviceRegistry]):
        self._registry = registry
        self._cache: Dict[str, tuple] = {}  # device id -> ((snapshot, clients, mesh) it was built from, summary)
        self.builds = 0  # how many summaries were built (tests: the cost per tick)

    def forget_missing(self) -> None:
        """Drops the cached summaries of devices no longer in the registry (after the devices were rebuilt)."""
        present = {d.id for d in self._registry} if self._registry is not None else set()
        for device_id in [i for i in self._cache if i not in present]:
            del self._cache[device_id]

    def current(self) -> Dict[str, Dict[str, Any]]:
        out: Dict[str, Dict[str, Any]] = {}
        if self._registry is None:
            return out
        for device in self._registry:
            if not device.enabled:
                continue
            snapshot = device.latest_snapshot
            if snapshot is None:  # not polled yet
                continue
            monitor = device.monitor
            source = (snapshot, getattr(monitor, "latest_clients", None), getattr(monitor, "latest_mesh", None))
            cached = self._cache.get(device.id)
            if cached is None or any(a is not b for a, b in zip(cached[0], source)):
                summary = summarize_snapshot(snapshot)
                counts = client_counts(monitor)
                if counts is not None:
                    summary["clients"] = counts
                cached = (source, summary)
                self._cache[device.id] = cached
                self.builds += 1
            out[device.id] = cached[1]
        return out
