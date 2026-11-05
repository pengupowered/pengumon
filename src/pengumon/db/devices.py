"""Device and line samples (schema v9) per device id, and the WAN rates read from them."""

import math
from bisect import bisect_left, bisect_right
from dataclasses import astuple
from typing import List, Optional, Tuple

from pengumon.db.samples import DEVICE_SAMPLE_COLUMNS, LINE_SAMPLE_COLUMNS, DeviceSample, LineSample


class PackSamples:
    """The device polls of a window as one tuple per column (time order), for the AI pack: the columns its
    contributions read and nothing else (no device id, error message, model or client_count). A week of polls is 60k
    rows; column tuples are built in C and walked with zip, where a row object per poll cost more than the rules."""
    COLUMNS = ("ts", "status", "error_kind", "wan_rx_bytes", "wan_tx_bytes", "wan_rx_mbps", "wan_tx_mbps", "cpu_pct",
               "mem_pct", "clients_online", "clients_wired", "clients_wifi_2g", "clients_wifi_5g", "clients_wifi_other")
    __slots__ = COLUMNS

    def __init__(self, rows: List[tuple]):
        columns = list(zip(*rows)) if rows else [()] * len(self.COLUMNS)
        for name, column in zip(self.COLUMNS, columns):
            setattr(self, name, column)

    def span(self, start: float, end: float) -> Tuple[int, int]:
        """The index range [lo, hi) of the polls with start <= ts <= end."""
        return bisect_left(self.ts, start), bisect_right(self.ts, end)


class DeviceQueries:
    _DEVICE_INSERT = (f"INSERT INTO device_samples ({', '.join(DEVICE_SAMPLE_COLUMNS)}) "
                      f"VALUES ({', '.join('?' for _ in DEVICE_SAMPLE_COLUMNS)})")

    _DEVICE_SELECT = f"SELECT {', '.join(DEVICE_SAMPLE_COLUMNS)} FROM device_samples"

    _LINE_INSERT = (f"INSERT INTO line_samples ({', '.join(LINE_SAMPLE_COLUMNS)}) "
                    f"VALUES ({', '.join('?' for _ in LINE_SAMPLE_COLUMNS)})")

    _LINE_SELECT = f"SELECT {', '.join(LINE_SAMPLE_COLUMNS)} FROM line_samples"

    # Polls with a measured rate in both directions; NULL means the poll could not measure one, not an idle line.
    _MEASURED = "device_id = ? AND ts >= ? AND wan_rx_mbps IS NOT NULL AND wan_tx_mbps IS NOT NULL"

    async def insert_device_sample(self, sample: DeviceSample):
        """Stores one poll of a device. A second row with the same device id and time is refused (IntegrityError):
        two pollers for one device would be a bug, and the monitor logs storage failures."""
        async with self._db.write() as db:
            await db.execute(self._DEVICE_INSERT, astuple(sample))

    async def get_device_samples(self, device_id: str, start_time: float, end_time: float,
                                 padding_seconds: float = 0.0) -> List[DeviceSample]:
        """The device's polls with start_time - padding_seconds <= ts <= end_time + padding_seconds, oldest first."""
        async with self._db.read() as db:
            async with db.execute(
                f"{self._DEVICE_SELECT} WHERE device_id = ? AND ts >= ? AND ts <= ? ORDER BY ts",
                (device_id, start_time - padding_seconds, end_time + padding_seconds),
            ) as cursor:
                return [DeviceSample(*r) for r in await cursor.fetchall()]

    async def get_device_pack_samples(self, device_id: str, start_time: float, end_time: float,
                                      padding_seconds: float = 0.0) -> PackSamples:
        """get_device_samples for the AI pack: the same polls in the same order, only the PackSamples columns."""
        async with self._db.read() as db:
            async with db.execute(
                f"SELECT {', '.join(PackSamples.COLUMNS)} FROM device_samples "
                "WHERE device_id = ? AND ts >= ? AND ts <= ? ORDER BY ts",
                (device_id, start_time - padding_seconds, end_time + padding_seconds),
            ) as cursor:
                return PackSamples(await cursor.fetchall())

    async def get_device_samples_bucketed(self, device_id: str, start_time: float, end_time: float,
                                        bucket_seconds: int) -> List[tuple]:
        """The device's polls with start_time <= ts <= end_time, grouped into buckets of bucket_seconds: per bucket
        (bucket start, then avg and max of rx Mbps, tx Mbps, CPU %, memory %, clients). NULLs (a poll that could not
        measure) are left out of both aggregates; a bucket with none gives NULL. Oldest first."""
        b = max(1, int(bucket_seconds))
        cols = ", ".join(f"AVG({c}), MAX({c})" for c in ("wan_rx_mbps", "wan_tx_mbps", "cpu_pct", "mem_pct",
                                                        "client_count"))
        async with self._db.read() as db:
            async with db.execute(
                f"SELECT CAST(ts / ? AS INTEGER) * ? AS b, {cols} FROM device_samples "
                "WHERE device_id = ? AND ts >= ? AND ts <= ? GROUP BY b ORDER BY b",
                (b, b, device_id, start_time, end_time),
            ) as cursor:
                return [tuple(r) for r in await cursor.fetchall()]

    async def insert_line_sample(self, sample: LineSample):
        """Stores one access-line reading; `summary` and `detail` are the technology's own JSON, stored as given."""
        async with self._db.write() as db:
            await db.execute(self._LINE_INSERT, astuple(sample))

    async def get_line_samples(self, device_id: str, start_time: float, end_time: float,
                               padding_seconds: float = 0.0) -> List[LineSample]:
        """The device's line readings with start_time - padding_seconds <= ts <= end_time + padding_seconds, oldest
        first."""
        async with self._db.read() as db:
            async with db.execute(
                f"{self._LINE_SELECT} WHERE device_id = ? AND ts >= ? AND ts <= ? ORDER BY ts",
                (device_id, start_time - padding_seconds, end_time + padding_seconds),
            ) as cursor:
                return [LineSample(*r) for r in await cursor.fetchall()]

    async def get_latest_line_sample(self, device_id: str) -> Optional[LineSample]:
        async with self._db.read() as db:
            async with db.execute(
                f"{self._LINE_SELECT} WHERE device_id = ? ORDER BY ts DESC LIMIT 1", (device_id,)
            ) as cursor:
                row = await cursor.fetchone()
        return LineSample(*row) if row else None

    async def get_stored_device_ids(self) -> List[str]:
        """The device ids that have rows in device_samples or line_samples, sorted. A loose index scan: one seek per
        distinct id on the (device_id, ts) primary key and index, never a scan of the rows."""
        ids = set()
        async with self._db.read() as db:
            for table in ("device_samples", "line_samples"):
                async with db.execute(
                    f"WITH RECURSIVE ids(id) AS (SELECT MIN(device_id) FROM {table} UNION ALL "
                    f"SELECT (SELECT MIN(device_id) FROM {table} WHERE device_id > ids.id) FROM ids "
                    "WHERE ids.id IS NOT NULL) SELECT id FROM ids WHERE id IS NOT NULL"
                ) as cursor:
                    ids.update(r[0] for r in await cursor.fetchall())
        return sorted(ids)

    async def get_wan_rates(self, device_id: str, start_time: float,
                            end_time: float) -> List[Tuple[float, float, float]]:
        """(ts, rx_mbps, tx_mbps) of the device's polls with start_time <= ts <= end_time, oldest first. Polls without
        a measured rate (stored as NULL) are left out, not counted as an idle line."""
        async with self._db.read() as db:
            async with db.execute(
                f"SELECT ts, wan_rx_mbps, wan_tx_mbps FROM device_samples WHERE {self._MEASURED} AND ts <= ? "
                "ORDER BY ts",
                (device_id, start_time, end_time),
            ) as cursor:
                return [tuple(r) for r in await cursor.fetchall()]

    async def get_wan_rate_percentile(
        self, device_id: str, since: float, fraction: float
    ) -> Optional[Tuple[int, float, float]]:
        """(sample count, rx Mbps, tx Mbps) at the given fraction (0..1) of the device's rates since `since`, each
        direction ranked on its own (nearest rank). None without samples. Only polls with a measured rate count.

        The rank is read from whichever end is nearer: SQLite keeps only LIMIT + OFFSET rows while sorting, so the
        99.9th percentile of a week of 10 s polls sorts 60 rows instead of 60,000 (about 4x faster)."""
        async with self._db.read() as db:
            async with db.execute(f"SELECT COUNT(*) FROM device_samples WHERE {self._MEASURED}",
                                  (device_id, since)) as cursor:
                n = (await cursor.fetchone())[0]
            if not n:
                return None
            rank = min(n - 1, max(0, math.ceil(fraction * n) - 1))  # 0-based, ascending
            order, offset = ("ASC", rank) if rank < n / 2 else ("DESC", n - 1 - rank)
            values = []
            for column in ("wan_rx_mbps", "wan_tx_mbps"):
                async with db.execute(
                    f"SELECT {column} AS v FROM device_samples WHERE {self._MEASURED} ORDER BY v {order} "
                    "LIMIT 1 OFFSET ?",
                    (device_id, since, offset),
                ) as cursor:
                    row = await cursor.fetchone()
                if row is None:  # retention removed rows between the count and this query
                    return None
                values.append(row[0])
            return n, values[0], values[1]
