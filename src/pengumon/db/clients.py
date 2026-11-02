"""Client and mesh-node counts (schema v10): aggregates only."""

from dataclasses import astuple
from typing import List, Tuple

from pengumon.db.samples import MeshCount


class ClientQueries:
    async def insert_mesh_counts(self, counts: List[MeshCount]):
        """Stores the per-node client counts of one client-list reading."""
        if not counts:
            return
        async with self._db.write() as db:
            await db.executemany("INSERT OR REPLACE INTO mesh_samples (device_id, ts, node_id, clients) VALUES (?, ?, ?, ?)",
                                 [astuple(c) for c in counts])

    _CLIENT_COLUMNS = ("clients_online", "clients_wired", "clients_wifi_2g", "clients_wifi_5g", "clients_wifi_other")

    async def get_client_counts(self, device_id: str, start_time: float, end_time: float,
                                bucket_seconds: float) -> List[Tuple]:
        """(bucket_start, online, wired, wifi_2g, wifi_5g, wifi_other) per bucket of `bucket_seconds`, oldest first:
        the mean of the readings in it (one decimal), over the readings that carry counts. The grouping is done by
        SQLite, so a long range costs one pass over the range, not a transfer of every reading."""
        means = ", ".join(f"ROUND(AVG({c}), 1)" for c in self._CLIENT_COLUMNS)
        async with self._db.read() as db:
            async with db.execute(
                f"SELECT CAST(ts / ?1 AS INTEGER) * ?1 AS bucket, {means} FROM device_samples "
                "WHERE device_id = ?2 AND ts >= ?3 AND ts <= ?4 AND clients_online IS NOT NULL "
                "GROUP BY bucket ORDER BY bucket",
                (bucket_seconds, device_id, start_time, end_time),
            ) as cursor:
                return [tuple(r) for r in await cursor.fetchall()]

    async def get_mesh_counts(self, device_id: str, start_time: float, end_time: float,
                              bucket_seconds: float) -> List[Tuple[float, str, float]]:
        """(bucket_start, node_id, mean clients) per bucket and node, oldest first."""
        async with self._db.read() as db:
            async with db.execute(
                "SELECT CAST(ts / ?1 AS INTEGER) * ?1 AS bucket, node_id, ROUND(AVG(clients), 1) FROM mesh_samples "
                "WHERE device_id = ?2 AND ts >= ?3 AND ts <= ?4 GROUP BY bucket, node_id ORDER BY bucket, node_id",
                (bucket_seconds, device_id, start_time, end_time),
            ) as cursor:
                return [tuple(r) for r in await cursor.fetchall()]
