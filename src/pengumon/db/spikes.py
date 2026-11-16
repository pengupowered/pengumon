"""Latency spikes (table latency_spikes, created by db/migrations/v0*_latency_spikes.py; incidents/spikes.py): brief
latency-only episodes that ended before they became incidents. The classifier's tracker writes them; GET
/api/latency-spikes, the reliability report, the dossier, the AI pack and the MCP `incidents` tool count them
(reports/spikes.py). Pruned with the incidents (db/core.py _prune)."""

from typing import Any, Dict, List

import aiosqlite

from pengumon.incidents.spikes import LatencySpike

_COLUMNS = "id, start_time, end_time, kind, stage, expected, peak_ms"


class SpikeQueries:
    async def create_latency_spike(self, spike: LatencySpike) -> int:
        """Stores one spike and returns its id. Bumps incidents_version: the reports that count spikes are kept until
        an incident changes, and the dashboard refetches its incident figures when it does."""
        async with self._db.write() as db:
            cursor = await db.execute(
                "INSERT INTO latency_spikes (start_time, end_time, kind, stage, expected, peak_ms) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (spike.start_time, spike.end_time, spike.kind, spike.stage, 1 if spike.expected else 0, spike.peak_ms))
            spike_id = cursor.lastrowid
        self._incidents_version += 1
        return spike_id

    async def latency_spikes_between(self, start_time: float, end_time: float) -> List[Dict[str, Any]]:
        """The spikes that started in [start_time, end_time], oldest first, as plain rows (expected as a bool)."""
        async with self._db.read() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                f"SELECT {_COLUMNS} FROM latency_spikes WHERE start_time >= ? AND start_time <= ? "
                "ORDER BY start_time, id", (start_time, end_time),
            ) as cursor:
                return [{**dict(r), "expected": bool(r["expected"])} for r in await cursor.fetchall()]
