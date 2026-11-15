"""Client and mesh-node counts (schema v10): aggregates only; and the names given to clients in Pengumon (v20)."""

import time
from dataclasses import astuple
from typing import Dict, List, Optional, Tuple

from pengumon.db.device_tiers import device_parts, mesh_parts
from pengumon.db.samples import MeshCount
from pengumon.db.schema import UPSERT_MESH_ROLLUP_15M_FROM_SAMPLES


class ClientQueries:
    async def get_client_names(self) -> Dict[str, str]:
        """MAC -> the name given to that client in Pengumon."""
        async with self._db.read() as db:
            async with db.execute("SELECT mac, name FROM client_names") as cursor:
                return {mac: name for mac, name in await cursor.fetchall()}

    async def set_client_name(self, mac: str, name: Optional[str]) -> None:
        """Gives the client `name`, or (None) takes its name away: the router's is shown again."""
        async with self._db.write() as db:
            if name:
                await db.execute("INSERT OR REPLACE INTO client_names (mac, name, updated_at) VALUES (?, ?, ?)",
                                 (mac, name, time.time()))
            else:
                await db.execute("DELETE FROM client_names WHERE mac = ?", (mac,))

    async def insert_mesh_counts(self, counts: List[MeshCount]):
        """Stores the per-node client counts of one client-list reading, and recomputes the quarter hours they fall in
        (mesh_rollup_15m) from the stored readings, in the same transaction."""
        if not counts:
            return
        async with self._db.write() as db:
            await db.executemany("INSERT OR REPLACE INTO mesh_samples (device_id, ts, node_id, clients) VALUES (?, ?, ?, ?)",
                                 [astuple(c) for c in counts])
            for device_id, quarter in sorted({(c.device_id, int(c.ts // 900)) for c in counts}):
                await db.execute(UPSERT_MESH_ROLLUP_15M_FROM_SAMPLES, (quarter * 900, (quarter + 1) * 900, device_id))

    _CLIENT_COLUMNS = ("clients_online", "clients_wired", "clients_wifi_2g", "clients_wifi_5g", "clients_wifi_other")

    async def get_client_counts(self, device_id: str, start_time: float, end_time: float,
                                bucket_seconds: float) -> List[Tuple]:
        """(bucket_start, online, wired, wifi_2g, wifi_5g, wifi_other) per bucket of `bucket_seconds`, oldest first:
        the mean of the readings in it (one decimal), over the readings that carry counts. The grouping is done by
        SQLite, so a long range costs one pass over the range, not a transfer of every reading.

        Raw readings where they are kept (`retention_days`), before that the minute and quarter-hour tiers of the
        device (db/device_tiers.py); while raw covers the range the answer is exactly the raw one. Where a tier is read,
        a bucket narrower than the tier (60 s, 900 s) is widened to it and the range start is rounded out to the
        whole minute (quarter) it falls in."""
        async with self._db.read() as db:
            async with db.execute("SELECT MIN(minute) FROM device_rollup_1m WHERE device_id = ?", (device_id,)) as c:
                floor = (await c.fetchone())[0]
            merged = device_parts(device_id, start_time, end_time, int(bucket_seconds), time.time(),
                                  self.retention_days, floor)
            if merged is None:
                means = ", ".join(f"ROUND(AVG({c}), 1)" for c in self._CLIENT_COLUMNS)
                async with db.execute(
                    f"SELECT CAST(ts / ?1 AS INTEGER) * ?1 AS bucket, {means} FROM device_samples "
                    "WHERE device_id = ?2 AND ts >= ?3 AND ts <= ?4 AND clients_online IS NOT NULL "
                    "GROUP BY bucket ORDER BY bucket",
                    (bucket_seconds, device_id, start_time, end_time),
                ) as cursor:
                    return [tuple(r) for r in await cursor.fetchall()]
            sql, params = merged
            if not sql:
                return []
            means = ", ".join(f"ROUND(SUM({c}_sum) * 1.0 / SUM({c}_n), 1)" for c in self._CLIENT_COLUMNS)
            # a bucket counts only where a reading carried counts (the raw query's clients_online IS NOT NULL)
            async with db.execute(
                f"SELECT bucket, {means} FROM ({sql}) GROUP BY bucket HAVING SUM(clients_online_n) > 0 ORDER BY bucket",
                params,
            ) as cursor:
                return [tuple(r) for r in await cursor.fetchall()]

    async def get_mesh_counts(self, device_id: str, start_time: float, end_time: float,
                              bucket_seconds: float) -> List[Tuple[float, str, float]]:
        """(bucket_start, node_id, mean clients) per bucket and node, oldest first. Raw readings where they are kept,
        before that the quarter-hour tier (mesh_rollup_15m: buckets widened to 900 s, the range start rounded out to its
        quarter); while raw covers the range the answer is exactly the raw one."""
        async with self._db.read() as db:
            merged = mesh_parts(device_id, start_time, end_time, int(bucket_seconds), time.time(), self.retention_days)
            if merged is None:
                async with db.execute(
                    "SELECT CAST(ts / ?1 AS INTEGER) * ?1 AS bucket, node_id, ROUND(AVG(clients), 1) FROM mesh_samples "
                    "WHERE device_id = ?2 AND ts >= ?3 AND ts <= ?4 GROUP BY bucket, node_id "
                    "ORDER BY bucket, node_id",
                    (bucket_seconds, device_id, start_time, end_time),
                ) as cursor:
                    return [tuple(r) for r in await cursor.fetchall()]
            sql, params = merged
            if not sql:
                return []
            async with db.execute(
                f"SELECT bucket, node_id, ROUND(SUM(s) * 1.0 / SUM(n), 1) FROM ({sql}) GROUP BY bucket, node_id "
                "ORDER BY bucket, node_id", params,
            ) as cursor:
                return [tuple(r) for r in await cursor.fetchall()]
