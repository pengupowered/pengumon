"""Incident evidence: what the inspector shows for an incident (probes, the modem's and the router's readings), copied
when the incident has closed and kept as long as the incident is.

The shared tables (samples, rollup_10s, rollup_1m, device_samples, line_samples) are pruned by age, so an old incident
would show less and less. `store_evidence` copies the rows its inspection window reads into incident_evidence_probes
and incident_evidence_readings, once per incident (the incident_evidence row says it is done), and the inspector reads
them first (analysis/incident.py). The window and its constants are the inspector's own (inspection_window,
INSPECT_RAW_MAX_SECONDS, SNAPSHOT_PADDING_SECONDS), so they cannot drift apart.

What is kept: every probe of the window; when the window is longer than INSPECT_RAW_MAX_SECONDS, every probe of its
first and last EVIDENCE_RAW_EDGE_SECONDS and 10-second buckets between (the hop table is exact either way: the episode
it covers lies inside the raw edges, and a bucket holds the sums the hop table needs). When no raw probe is stored any
more, the per-minute rollups of the window (resolution "minute"); when there is nothing, a row that says "none".

The functions here take a connection inside a transaction: the migration (v021) and the repository's writer use the same
code. Plain SQL, no repository state."""

import json
import math
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

import aiosqlite

from pengumon.analysis import incident as inspector
from pengumon.db.samples import DeviceSample, LineSample

EVIDENCE_RAW_EDGE_SECONDS = 900  # raw probes kept at each end of a window longer than the inspector's raw limit
RESOLUTIONS = ("raw", "mixed", "minute", "none")
ROUTER, LINE = "router", "line"
# An incident's evidence is written once nothing that belongs to it can arrive any more: the window's end, the
# readings up to the padding (or the modem's poll interval, when there is none in the window) past it, and a margin
# for the probe buffer's flush.
SETTLE_MARGIN_SECONDS = 30.0

# The columns of a stored reading that the inspector rebuilds its snapshots from (the error message is not read back).
_ROUTER_FIELDS = ("status", "error_kind", "wan_rx_bytes", "wan_tx_bytes", "wan_rx_mbps", "wan_tx_mbps", "cpu_pct",
                  "mem_pct", "client_count", "model", "uptime_s", "cpu_temp_c", "wan_link_mbps")
_LINE_FIELDS = ("technology", "status", "error_kind", "errors_total", "errors_recent", "summary", "detail")


def settle_seconds(reach: float = inspector.SNAPSHOT_REACH_SECONDS) -> float:
    """How long after an incident's end its evidence is complete: `reach` is the modem's poll interval."""
    return inspector.WINDOW_PADDING_SECONDS + max(inspector.SNAPSHOT_PADDING_SECONDS, reach) + SETTLE_MARGIN_SECONDS


# --- writing -------------------------------------------------------------------------------------------------------

_COPY_RAW = (
    "INSERT INTO incident_evidence_probes (incident_id, target, ts, width, ok, latency_ms, jitter_ms, error) "
    "SELECT ?, target, ts, 0, ok, latency_ms, jitter_ms, error FROM samples WHERE ts >= ? AND ts {op} ?"
)
_COPY_SLOTS = """
INSERT INTO incident_evidence_probes (incident_id, target, ts, width, n, ok, lat_sum, lat_min, lat_max, jit_sum, jit_n)
SELECT ?, target, CAST(ts / 10 AS INTEGER) * 10, 10, COUNT(*), SUM(ok),
       SUM(CASE WHEN ok = 1 THEN latency_ms END), MIN(CASE WHEN ok = 1 THEN latency_ms END),
       MAX(CASE WHEN ok = 1 THEN latency_ms END), SUM(jitter_ms), COUNT(jitter_ms)
FROM samples WHERE ts >= ? AND ts < ?
GROUP BY target, CAST(ts / 10 AS INTEGER)
"""
_COPY_MINUTES = """
INSERT INTO incident_evidence_probes (incident_id, target, ts, width, n, ok, lat_sum, lat_min, lat_max, jit_sum, jit_n)
SELECT ?, target, minute * 60, 60, n, ok, lat_sum, lat_min, lat_max, jit_sum, jit_n
FROM rollup_1m WHERE minute >= ? AND minute <= ?
"""


async def _copy_probes(db: aiosqlite.Connection, incident_id: int, window_start: float, window_end: float) -> str:
    """Copies the probes of the window; returns the resolution they give ("none": nothing was stored)."""
    copied = 0
    mixed = False
    if window_end - window_start > inspector.INSPECT_RAW_MAX_SECONDS:
        # raw at both ends, 10-second slots between: whole slots, so the raw ends take the rest
        mid_lo = math.ceil((window_start + EVIDENCE_RAW_EDGE_SECONDS) / 10) * 10
        mid_hi = math.floor((window_end - EVIDENCE_RAW_EDGE_SECONDS) / 10) * 10
        mixed = mid_lo < mid_hi
    if mixed:
        for sql, params in ((_COPY_RAW.format(op="<"), (window_start, mid_lo)), (_COPY_SLOTS, (mid_lo, mid_hi)),
                            (_COPY_RAW.format(op="<="), (mid_hi, window_end))):
            cursor = await db.execute(sql, (incident_id, *params))
            copied += cursor.rowcount
    else:
        cursor = await db.execute(_COPY_RAW.format(op="<="), (incident_id, window_start, window_end))
        copied = cursor.rowcount
    if copied > 0:
        return "mixed" if mixed else "raw"
    cursor = await db.execute(_COPY_MINUTES, (incident_id, int(window_start // 60), int(window_end // 60)))
    return "minute" if cursor.rowcount > 0 else "none"


def _payload(row: Sequence, names: Sequence[str]) -> str:
    return json.dumps({k: v for k, v in zip(names, row) if v is not None}, separators=(",", ":"))


async def _copy_readings(db: aiosqlite.Connection, incident_id: int, window_start: float, window_end: float,
                         reach: float) -> None:
    """The router's polls within SNAPSHOT_PADDING_SECONDS of the window and the modem's readings the inspector may show:
    those within the padding, or, for a device with none there, those within the poll interval `reach` (the nearest one
    is picked when the incident is read)."""
    pad = inspector.SNAPSHOT_PADDING_SECONDS
    async with db.execute(
        f"SELECT device_id, ts, {', '.join(_ROUTER_FIELDS)} FROM device_samples WHERE ts >= ? AND ts <= ?",
        (window_start - pad, window_end + pad),
    ) as cursor:
        rows = await cursor.fetchall()
    await db.executemany(
        "INSERT OR IGNORE INTO incident_evidence_readings (incident_id, kind, device_id, ts, payload) "
        "VALUES (?, ?, ?, ?, ?)",
        [(incident_id, ROUTER, r[0], r[1], _payload(r[2:], _ROUTER_FIELDS)) for r in rows],
    )
    wide = max(reach, inspector.SNAPSHOT_REACH_SECONDS)
    async with db.execute(
        f"SELECT device_id, ts, {', '.join(_LINE_FIELDS)} FROM line_samples WHERE ts >= ? AND ts <= ? "
        "ORDER BY device_id, ts",
        (window_start - max(wide, pad), window_end + max(wide, pad)),
    ) as cursor:
        rows = await cursor.fetchall()
    inside = {r[0] for r in rows if window_start - pad <= r[1] <= window_end + pad}
    await db.executemany(
        "INSERT OR IGNORE INTO incident_evidence_readings (incident_id, kind, device_id, ts, payload) "
        "VALUES (?, ?, ?, ?, ?)",
        [(incident_id, LINE, r[0], r[1], _payload(r[2:], _LINE_FIELDS)) for r in rows
         if r[0] not in inside or window_start - pad <= r[1] <= window_end + pad],
    )


async def store_evidence(db: aiosqlite.Connection, incident_id: int, start_time: float, end_time: Optional[float],
                         duration_seconds: Optional[float], resolved: bool, *,
                         reach: float = inspector.SNAPSHOT_REACH_SECONDS) -> Optional[str]:
    """Writes the evidence of one incident, inside the caller's transaction, unless it has some already. Returns the
    resolution written, None when there was a row. An incident with no probe left gets only its row (readings
    of a stretch that has no probes are not evidence of anything the inspector would show)."""
    async with db.execute("SELECT 1 FROM incident_evidence WHERE incident_id = ?", (incident_id,)) as cursor:
        if await cursor.fetchone() is not None:
            return None
    win = inspector.inspection_window(start_time, end_time, duration_seconds, resolved)
    resolution = await _copy_probes(db, incident_id, win.window_start, win.window_end)
    if resolution != "none":
        await _copy_readings(db, incident_id, win.window_start, win.window_end, reach)
    await db.execute(
        "INSERT INTO incident_evidence (incident_id, written_at, window_start, window_end, resolution) "
        "VALUES (?, ?, ?, ?, ?)",
        (incident_id, time.time(), win.window_start, win.window_end, resolution),
    )
    return resolution


# --- reading -------------------------------------------------------------------------------------------------------

class IncidentEvidence:
    """The header of an incident's evidence."""

    def __init__(self, incident_id: int, written_at: float, window_start: float, window_end: float, resolution: str):
        self.incident_id = incident_id
        self.written_at = written_at
        self.window_start = window_start
        self.window_end = window_end
        self.resolution = resolution

    def __repr__(self) -> str:
        return f"IncidentEvidence({self.incident_id}, {self.resolution})"


# The stored rows as the columns the aggregates sum: a raw probe is a bucket of one.
_AS_BUCKETS = """
SELECT target, ts, CASE WHEN width = 0 THEN 1 ELSE n END AS n, ok,
       CASE WHEN width = 0 THEN CASE WHEN ok = 1 THEN latency_ms END ELSE lat_sum END AS lat_sum,
       CASE WHEN width = 0 THEN CASE WHEN ok = 1 THEN latency_ms END ELSE lat_min END AS lat_min,
       CASE WHEN width = 0 THEN CASE WHEN ok = 1 THEN latency_ms END ELSE lat_max END AS lat_max,
       CASE WHEN width = 0 THEN jitter_ms ELSE jit_sum END AS jit_sum,
       CASE WHEN width = 0 THEN (jitter_ms IS NOT NULL) ELSE jit_n END AS jit_n
FROM incident_evidence_probes
WHERE incident_id = ? AND ts <= ? AND CASE WHEN width = 0 THEN ts >= ? ELSE ts + width > ? END
"""


def _as_buckets(incident_id: int, start: float, end: float) -> Tuple[str, list]:
    """Probes of the evidence that overlap [start, end]: a raw probe lies inside it, a bucket (a 10-second slot, or a
    whole minute, as the rollups read them) touches it."""
    return _AS_BUCKETS, [incident_id, end, start, start]


class EvidenceQueries:
    async def get_incident_evidence(self, incident_id: int) -> Optional[IncidentEvidence]:
        async with self._db.read() as db:
            async with db.execute(
                "SELECT incident_id, written_at, window_start, window_end, resolution FROM incident_evidence "
                "WHERE incident_id = ?", (incident_id,)
            ) as cursor:
                row = await cursor.fetchone()
        return IncidentEvidence(*row) if row else None

    async def evidence_raw_samples(self, incident_id: int, start_time: float, end_time: float) -> List[Dict[str, Any]]:
        """get_raw_samples from the evidence: the raw probes of [start_time, end_time], oldest first."""
        async with self._db.read() as db:
            async with db.execute(
                "SELECT target, ts, ok, latency_ms, jitter_ms, error FROM incident_evidence_probes "
                "WHERE incident_id = ? AND width = 0 AND ts >= ? AND ts <= ? ORDER BY ts, target",
                (incident_id, start_time, end_time),
            ) as cursor:
                rows = await cursor.fetchall()
        return self._shape_history("raw", rows, False)

    async def evidence_buckets(self, incident_id: int, start_time: float, end_time: float,
                               bucket_seconds: int) -> List[Dict[str, Any]]:
        """get_history_buckets from the evidence, rows of get_history: buckets of bucket_seconds (a multiple of the
        widest stored bucket, 10 or 60), computed from raw probes and stored buckets alike."""
        b = int(bucket_seconds)
        source, params = _as_buckets(incident_id, start_time, end_time)
        async with self._db.read() as db:
            async with db.execute(
                f"""
                SELECT target, CAST(ts / {b} AS INTEGER) * {b} AS bucket,
                       ROUND(SUM(lat_sum) / SUM(ok), 2),
                       ROUND((SUM(n) - SUM(ok)) * 100.0 / SUM(n), 1),
                       ROUND(SUM(jit_sum) / SUM(jit_n), 2),
                       SUM(ok), SUM(n), NULL, NULL
                FROM ({source})
                GROUP BY target, bucket
                ORDER BY bucket
                """,
                params,
            ) as cursor:
                rows = await cursor.fetchall()
        return self._shape_history("bucket", rows, False)

    async def evidence_stats(self, incident_id: int, start_time: float, end_time: float) -> List[Dict[str, Any]]:
        """get_sample_stats from the evidence (get_rollup_stats when it holds minutes): per target, over the probes
        of [start_time, end_time]."""
        source, params = _as_buckets(incident_id, start_time, end_time)
        async with self._db.read() as db:
            async with db.execute(
                f"""
                SELECT target, SUM(n), SUM(ok), SUM(lat_sum) / SUM(ok), MIN(lat_min), MAX(lat_max),
                       SUM(jit_sum) / SUM(jit_n)
                FROM ({source})
                GROUP BY target
                ORDER BY target
                """,
                params,
            ) as cursor:
                rows = await cursor.fetchall()
        return self.shown([
            {
                "target_id": self._target_keys.get(target, str(target)),
                "sent": sent,
                "received": received,
                "avg_ms": None if avg_ms is None else round(avg_ms, 2),
                "min_ms": None if min_ms is None else round(min_ms, 2),
                "max_ms": None if max_ms is None else round(max_ms, 2),
                "jitter_ms": None if jitter is None else round(jitter, 2),
            }
            for target, sent, received, avg_ms, min_ms, max_ms, jitter in rows
        ])

    async def evidence_bucket_width(self, incident_id: int, span_seconds: float, unit: int) -> int:
        """The width of the chart buckets for a window of span_seconds: about INSPECT_MAX_POINTS of them, widened to keep
        every target's buckets under the history row cap (as get_history_buckets does), in whole `unit` seconds."""
        from pengumon.db.probes import HISTORY_ROW_CAP
        async with self._db.read() as db:
            async with db.execute("SELECT COUNT(DISTINCT target) FROM incident_evidence_probes WHERE incident_id = ?",
                                  (incident_id,)) as cursor:
                targets = (await cursor.fetchone())[0] or 1
        width = max(math.ceil(span_seconds / inspector.INSPECT_MAX_POINTS), math.ceil(span_seconds * targets / HISTORY_ROW_CAP),
                    unit)
        return math.ceil(width / unit) * unit

    async def _evidence_readings(self, incident_id: int, kind: str, device_id: str) -> List[Tuple[float, dict]]:
        async with self._db.read() as db:
            async with db.execute(
                "SELECT ts, payload FROM incident_evidence_readings "
                "WHERE incident_id = ? AND kind = ? AND device_id = ? ORDER BY ts",
                (incident_id, kind, device_id),
            ) as cursor:
                return [(ts, json.loads(payload)) for ts, payload in await cursor.fetchall()]

    async def evidence_device_samples(self, incident_id: int, device_id: str) -> List[DeviceSample]:
        """The device's polls kept for the incident, as get_device_samples reads them, oldest first."""
        return [DeviceSample(device_id=device_id, ts=ts, **payload)
                for ts, payload in await self._evidence_readings(incident_id, ROUTER, device_id)]

    async def evidence_line_samples(self, incident_id: int, device_id: str) -> List[LineSample]:
        """The device's line readings kept for the incident, as get_line_samples reads them, oldest first."""
        return [LineSample(device_id=device_id, ts=ts, **payload)
                for ts, payload in await self._evidence_readings(incident_id, LINE, device_id)]

    async def incidents_needing_evidence(self, settled_before: float, limit: int) -> List[tuple]:
        """(id, start_time, end_time, duration_seconds) of resolved incidents without evidence that ended at or before
        `settled_before`, oldest first."""
        async with self._db.read() as db:
            async with db.execute(
                "SELECT id, start_time, end_time, duration_seconds FROM incidents WHERE resolved = 1 "
                "AND COALESCE(end_time, start_time) <= ? "
                "AND id NOT IN (SELECT incident_id FROM incident_evidence) ORDER BY id LIMIT ?",
                (settled_before, limit),
            ) as cursor:
                return [tuple(r) for r in await cursor.fetchall()]

    async def write_incident_evidence(self, incident_id: int, reach: float = inspector.SNAPSHOT_REACH_SECONDS
                                      ) -> Optional[str]:
        """Writes the evidence of a resolved incident, in one transaction; the buffered probes are written first.
        Returns the resolution, None when it has some already or is not a resolved incident."""
        await self.flush_samples()
        async with self._db.write() as db:
            async with db.execute(
                "SELECT start_time, end_time, duration_seconds FROM incidents WHERE id = ? AND resolved = 1",
                (incident_id,),
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                return None
            return await store_evidence(db, incident_id, row[0], row[1], row[2], True, reach=reach)

    async def prune_incident_evidence(self) -> int:
        """Deletes the evidence of incidents that no longer exist (db/core.py _prune calls it after the incidents).
        Returns the rows deleted. The children go before their header, so an interrupted run is finished by the next."""
        async with self._db.read() as db:
            async with db.execute(
                "SELECT incident_id FROM incident_evidence WHERE incident_id NOT IN (SELECT id FROM incidents)"
            ) as cursor:
                gone = [r[0] for r in await cursor.fetchall()]
        total = 0
        for incident_id in gone:
            for table in ("incident_evidence_probes", "incident_evidence_readings"):
                total += await self._delete_old(table, "incident_id = ?", (incident_id,),
                                                "incident_id, target, ts" if table.endswith("probes")
                                                else "incident_id, kind, device_id, ts")
            total += await self._delete_old("incident_evidence", "incident_id = ?", (incident_id,), "incident_id")
        return total
