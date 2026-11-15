"""Device and line samples (schema v9) per device id, and the WAN rates read from them."""

import math
import time
from bisect import bisect_left, bisect_right
from dataclasses import astuple
from typing import List, Optional, Tuple

from pengumon.db.device_tiers import device_parts
from pengumon.db.schema import (
    DEVICE_ROLLUP_FIGURES, UPSERT_DEVICE_ROLLUP_15M_FROM_1M, UPSERT_DEVICE_ROLLUP_1M_FROM_SAMPLES,
)
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
        two pollers for one device would be a bug, and the monitor logs storage failures. In the same transaction the
        poll's minute (device_rollup_1m) and its quarter hour (device_rollup_15m) are recomputed from what is stored,
        as the probes' flush does, so a rollup is never ahead of or behind its source."""
        async with self._db.write() as db:
            await db.execute(self._DEVICE_INSERT, astuple(sample))
            minute = int(sample.ts // 60)
            await db.execute(UPSERT_DEVICE_ROLLUP_1M_FROM_SAMPLES, (minute * 60, (minute + 1) * 60, sample.device_id))
            await db.execute(UPSERT_DEVICE_ROLLUP_15M_FROM_1M,
                             (minute // 15 * 15, (minute // 15 + 1) * 15, sample.device_id))

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
        (bucket start, then avg and max of rx Mbps, tx Mbps, CPU %, memory %, clients, CPU temperature, then the lowest
        WAN link speed). NULLs (a poll that could not
        measure) are left out of both aggregates; a bucket with none gives NULL. Oldest first.

        Raw polls are read where they are kept (`retention_days`); before that the minute tier and then the quarter-hour
        tier (db/device_tiers.py), with the same figures: while raw covers the range the answer is exactly the raw one.
        Where the tiers are read, a bucket narrower than the tier (60 s, 900 s) is widened to it, and the start of the
        range is rounded out to the whole minute (quarter) it falls in."""
        b = max(1, int(bucket_seconds))
        async with self._db.read() as db:
            async with db.execute("SELECT MIN(minute) FROM device_rollup_1m WHERE device_id = ?", (device_id,)) as c:
                floor = (await c.fetchone())[0]
            merged = device_parts(device_id, start_time, end_time, b, time.time(), self.retention_days, floor)
            if merged is None:
                return await self._raw_bucketed(db, device_id, start_time, end_time, b)
            sql, params = merged
            if not sql:
                return []
            outer = ", ".join(self._MERGE_COLUMNS)
            async with db.execute(f"SELECT bucket, {outer} FROM ({sql}) GROUP BY bucket ORDER BY bucket",
                                  params) as cursor:
                return [tuple(r) for r in await cursor.fetchall()]

    # avg (sum / count) and max of the six figures, then the lowest link speed, of the partial aggregates of
    # db/device_tiers.py
    _MERGE_COLUMNS = ([f"SUM({c}_sum) * 1.0 / SUM({c}_n), MAX({c}_max)" for c in DEVICE_ROLLUP_FIGURES]
                      + ["MIN(wan_link_min)"])

    @staticmethod
    async def _raw_bucketed(db, device_id: str, start_time: float, end_time: float, b: int) -> List[tuple]:
        cols = ", ".join(f"AVG({c}), MAX({c})" for c in ("wan_rx_mbps", "wan_tx_mbps", "cpu_pct", "mem_pct",
                                                        "client_count"))
        cols += ", AVG(cpu_temp_c), MAX(cpu_temp_c), MIN(wan_link_mbps)"
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
                               padding_seconds: float = 0.0,
                               bucket_seconds: Optional[float] = None) -> List[LineSample]:
        """The device's line readings with start_time - padding_seconds <= ts <= end_time + padding_seconds, oldest
        first. With `bucket_seconds` only the newest reading of every bucket of that many seconds (epoch-aligned), so a
        long range reads about as many readings as it has buckets."""
        lo, hi = start_time - padding_seconds, end_time + padding_seconds
        newest = ""
        params: list = [device_id, lo, hi]
        if bucket_seconds:
            newest = (" AND ts IN (SELECT MAX(ts) FROM line_samples WHERE device_id = ? AND ts >= ? AND ts <= ? "
                      "GROUP BY CAST(ts / ? AS INTEGER))")
            params += [device_id, lo, hi, float(bucket_seconds)]
        async with self._db.read() as db:
            async with db.execute(
                f"{self._LINE_SELECT} WHERE device_id = ? AND ts >= ? AND ts <= ?{newest} ORDER BY ts", params,
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
