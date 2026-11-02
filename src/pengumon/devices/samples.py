"""Router snapshots and their stored form (pengumon.db.samples.DeviceSample), in one place each way.

`device_sample` is what RouterMonitor stores for a poll; `router_snapshot` and `RouterHistory` turn stored rows back
into the RouterMetricsSnapshot that /api/devices/{id}/history and the incident inspector answer with.
"""

import math
from typing import Dict, List, Optional

from pengumon.capabilities import BAND_2G, BAND_5G, ClientsReading, MeshReading
from pengumon.db.samples import DeviceSample, MeshCount
from pengumon.devices.snapshots import RouterMetricsSnapshot


def client_counts(reading: ClientsReading) -> Dict[str, int]:
    """The aggregate counts of a client list, as the DeviceSample fields that store them: online clients, those on a
    cable, and the Wi-Fi ones per band (any other band than 2.4 and 5 GHz counts as other)."""
    online = [c for c in reading.clients if c.online]
    wifi = [c for c in online if c.connection == "wifi"]
    return {
        "clients_online": len(online),
        "clients_wired": len(online) - len(wifi),
        "clients_wifi_2g": sum(c.band == BAND_2G for c in wifi),
        "clients_wifi_5g": sum(c.band == BAND_5G for c in wifi),
        "clients_wifi_other": sum(c.band not in (BAND_2G, BAND_5G) for c in wifi),
    }


def mesh_counts(device_id: str, ts: float, mesh: MeshReading) -> List[MeshCount]:
    """One stored row per mesh node of a reading."""
    return [MeshCount(device_id, ts, node.id, node.clients) for node in mesh.nodes]


def device_sample(device_id: str, snapshot: RouterMetricsSnapshot,
                  clients: Optional[ClientsReading] = None) -> DeviceSample:
    """The stored form of a router snapshot: WAN counters and rates, system readings, the configured model, and the
    client counts of a poll that read the client list (`clients`)."""
    return DeviceSample(
        **(client_counts(clients) if clients is not None else {}),
        device_id=device_id,
        ts=snapshot.timestamp,
        status=snapshot.status,
        error_kind=snapshot.error_kind,
        error_message=snapshot.error_message,
        wan_rx_bytes=snapshot.wan_rx_bytes,
        wan_tx_bytes=snapshot.wan_tx_bytes,
        wan_rx_mbps=snapshot.wan_rx_rate_mbps,
        wan_tx_mbps=snapshot.wan_tx_rate_mbps,
        cpu_pct=snapshot.cpu_usage_pct,
        mem_pct=snapshot.mem_usage_pct,
        client_count=snapshot.client_count,
        model=snapshot.model_name,
    )


def router_snapshot(sample: DeviceSample) -> RouterMetricsSnapshot:
    """A stored poll as the snapshot the history answers with. The error message was never stored before schema v9 and
    stays null here."""
    return RouterMetricsSnapshot(
        timestamp=sample.ts,
        wan_rx_bytes=sample.wan_rx_bytes,
        wan_tx_bytes=sample.wan_tx_bytes,
        wan_rx_rate_mbps=sample.wan_rx_mbps,
        wan_tx_rate_mbps=sample.wan_tx_mbps,
        cpu_usage_pct=sample.cpu_pct,
        mem_usage_pct=sample.mem_pct,
        client_count=sample.client_count,
        model_name=sample.model,
        status=sample.status or "unconfigured",
        error_kind=sample.error_kind,
    )


class RouterHistory:
    """One device's stored polls as RouterMetricsSnapshot."""

    def __init__(self, repository, device_id: str):
        self.repository = repository
        self.device_id = device_id

    async def get_router_snapshots_in_range(self, start_time: float, end_time: float,
                                            padding_seconds: float = 120.0) -> List[RouterMetricsSnapshot]:
        """Snapshots with start_time - padding_seconds <= ts <= end_time + padding_seconds, oldest first."""
        samples = await self.repository.get_device_samples(self.device_id, start_time, end_time, padding_seconds)
        return [router_snapshot(s) for s in samples]


BUCKET_FIELDS = ("ts", "rx_avg", "rx_max", "tx_avg", "tx_max", "cpu_avg", "cpu_max", "mem_avg", "mem_max",
                 "clients_avg", "clients_max")
DEFAULT_MAX_POINTS = 400
MAX_POINTS_LIMIT = 1000


def bucket_width(span_seconds: float, bucket_seconds: Optional[int] = None, max_points: Optional[int] = None) -> int:
    """The bucket width for a range: the requested one, widened so the range has at most max_points buckets
    (DEFAULT_MAX_POINTS), and rounded up to 10 s below a minute, to whole minutes above it."""
    points = min(max_points or DEFAULT_MAX_POINTS, MAX_POINTS_LIMIT)
    width = max(int(bucket_seconds or 1), math.ceil(max(span_seconds, 0.0) / points), 1)
    unit = 10 if width < 60 else 60
    return math.ceil(width / unit) * unit


async def bucketed_router_history(repository, device_id: str, start_time: float, end_time: float, bucket: int) -> list:
    """Rows [bucket start, then avg and max of rx Mbps, tx Mbps, CPU %, memory %, clients] (BUCKET_FIELDS), oldest
    first; a figure no poll of the bucket measured is null. Rates, CPU and memory are rounded to 2 decimals, the
    client average to 1."""
    rows = await repository.get_device_samples_bucketed(device_id, start_time, end_time, bucket)
    return [[int(r[0]), *(None if v is None else round(v, 1 if i >= 8 else 2) for i, v in enumerate(r[1:]))]
            for r in rows]
