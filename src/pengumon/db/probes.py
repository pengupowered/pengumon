"""Probe samples: buffered inserts, raw / bucket / rollup history and the per-target totals."""

import math
import time
from typing import Any, Dict, List, Optional, Tuple

import aiosqlite

from pengumon.incidents.loss import TargetLoss
from pengumon.models.probes import ProbeSample
from pengumon.db.core import DAY
from pengumon.db.schema import UPSERT_ROLLUP_1M_FROM_SAMPLES
from pengumon.observability import StateLog, get_logger
from pengumon.observability.telemetry import db_flush_duration, db_flush_failures, db_flush_rows, db_samples_dropped

# /api/history rules: raw samples only for short ranges, sub-minute buckets (computed from raw samples)
# up to a day, per-minute rollups for everything else. Every answer is capped.
HISTORY_RAW_MAX_SECONDS = 6 * 3600
HISTORY_SMALL_BUCKET_MAX_SECONDS = 24 * 3600
# 50 000 rows is about 2 MB of JSON and 25 MB of peak memory; 200 000 was 8 MB and 100 MB. The buckets widen to fit.
HISTORY_ROW_CAP = 50_000
# While every flush fails (disk full, read-only volume, corrupt file) the buffer keeps the newest samples only: about
# ten minutes at the usual 12 samples a second, some 12 MB.
SAMPLE_BUFFER_MAX = 10_000
# Exact percentiles for sub-minute buckets sort every answered probe of the range (0.1 s for 1 h, about 2 s for 24 h on
# 10 targets), so the band is only computed for ranges up to this long; longer ones leave it None.
HISTORY_BAND_RAW_MAX_SECONDS = 6 * 3600

# latency_p10_ms / latency_p90_ms are the latency band of a bucket (None when not known: see _raw_bucket_query)
_BUCKET_KEYS = ("target_id", "timestamp", "latency_ms", "packet_loss_pct", "jitter_ms", "successful_count",
                "total_count", "latency_p10_ms", "latency_p90_ms")


_log = get_logger("pengumon.db")


class ProbeQueries:
    async def insert_samples(self, samples: List[ProbeSample]):
        """Buffers probe samples and commits them in batches (every `sample_flush_seconds`)."""
        if not samples:
            return
        self._sample_buffer.extend(samples)
        self._cap_sample_buffer()
        if time.monotonic() - self._last_flush >= self.sample_flush_seconds:
            try:
                await self.flush_samples()
            except Exception:
                pass  # logged by flush_samples; the round still classifies and publishes, the samples are retried

    def _cap_sample_buffer(self):
        """While writes fail: drops the oldest buffered samples beyond SAMPLE_BUFFER_MAX; logs once when it starts.
        (A healthy buffer is never cut: it is written within `sample_flush_seconds`, whatever one call brought.)"""
        if not self._flush_state.failing:
            return
        extra = len(self._sample_buffer) - SAMPLE_BUFFER_MAX
        if extra <= 0:
            return
        del self._sample_buffer[:extra]
        db_samples_dropped.add(extra)
        if not self._dropping:
            self._dropping = True
            _log.warning(f"Sample buffer full ({SAMPLE_BUFFER_MAX} samples) while writes fail: dropping the oldest")

    async def flush_samples(self):
        """Writes any buffered samples. Called before reads and on shutdown."""
        if not self._sample_buffer:
            return
        batch, self._sample_buffer = self._sample_buffer, []
        self._last_flush = started = time.monotonic()
        try:
            async with self._db.write() as db:
                ids = await self._resolve_target_ids(db, {s.target_id for s in batch})
                await db.executemany(
                    """
                    INSERT OR IGNORE INTO samples (ts, target, ok, latency_ms, jitter_ms, error)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (s.timestamp, ids[s.target_id], 1 if s.success else 0, s.latency_ms, s.jitter_ms, s.error)
                        for s in batch
                    ],
                )
                first_minute = int(min(s.timestamp for s in batch) // 60)
                last_minute = int(max(s.timestamp for s in batch) // 60)
                await db.execute(UPSERT_ROLLUP_1M_FROM_SAMPLES, (first_minute * 60, (last_minute + 1) * 60))
        except Exception as e:
            self._sample_buffer = batch + self._sample_buffer  # keep them for the next attempt
            db_flush_failures.add(1, {"error": type(e).__name__})
            self._flush_state.failure(f"{type(e).__name__}: {e}", buffered=len(self._sample_buffer))
            self._cap_sample_buffer()
            raise
        self._flush_state.success(rows=len(batch))
        self._dropping = False
        self._remember_targets(ids)
        db_flush_duration.record(time.monotonic() - started)
        db_flush_rows.add(len(batch))

    def _target_filter(self, target_ids: Optional[List[str]]) -> Optional[List[int]]:
        """None = all targets; [] = none of the requested targets has ever reported."""
        if not target_ids:
            return None
        return [self._target_ids[k] for k in target_ids if k in self._target_ids]

    @staticmethod
    def _target_clause(column: str, ids: Optional[List[int]]) -> Tuple[str, list]:
        if ids is None:
            return "", []
        return f" AND {column} IN ({','.join('?' for _ in ids)})", list(ids)

    def _raw_query(self, start: float, end: float, ids: Optional[List[int]] = None,
                   limit: Optional[int] = None) -> Tuple[str, list]:
        clause, params = self._target_clause("target", ids)
        sql = (
            "SELECT target, ts, ok, latency_ms, jitter_ms, error FROM samples "
            f"WHERE ts >= ? AND ts <= ?{clause} ORDER BY ts LIMIT ?"
        )
        return sql, [start, end, *params, limit or HISTORY_ROW_CAP]

    def _raw_bucket_query(self, start: float, end: float, bucket: int, ids: Optional[List[int]] = None,
                          limit: Optional[int] = None, band: bool = False) -> Tuple[str, list]:
        """Buckets of raw samples. With band=True the 10th and 90th percentile (nearest rank) of the answered probes
        of each bucket are computed too, which sorts them; without it the two columns are NULL."""
        clause, params = self._target_clause("target", ids)
        b = int(bucket)
        if band:
            sql = f"""
                SELECT target, bucket,
                       ROUND(AVG(CASE WHEN ok = 1 THEN latency_ms END), 2),
                       ROUND((COUNT(*) - SUM(ok)) * 100.0 / COUNT(*), 1),
                       ROUND(AVG(jitter_ms), 2),
                       SUM(ok), COUNT(*),
                       ROUND(MIN(CASE WHEN ok = 1 AND rank * 10 >= answered THEN latency_ms END), 2),
                       ROUND(MIN(CASE WHEN ok = 1 AND rank * 10 >= answered * 9 THEN latency_ms END), 2)
                FROM (
                    SELECT target, CAST(ts / {b} AS INT) * {b} AS bucket, ok, latency_ms, jitter_ms,
                           ROW_NUMBER() OVER (PARTITION BY target, CAST(ts / {b} AS INT), ok ORDER BY latency_ms) AS rank,
                           COUNT(*) OVER (PARTITION BY target, CAST(ts / {b} AS INT), ok) AS answered
                    FROM samples
                    WHERE ts >= ? AND ts <= ?{clause}
                )
                GROUP BY target, bucket
                ORDER BY bucket
                LIMIT ?
            """
        else:
            sql = f"""
                SELECT target, CAST(ts / {b} AS INT) * {b} AS bucket,
                       ROUND(AVG(CASE WHEN ok = 1 THEN latency_ms END), 2),
                       ROUND((COUNT(*) - SUM(ok)) * 100.0 / COUNT(*), 1),
                       ROUND(AVG(jitter_ms), 2),
                       SUM(ok), COUNT(*), NULL, NULL
                FROM samples
                WHERE ts >= ? AND ts <= ?{clause}
                GROUP BY target, bucket
                ORDER BY bucket
                LIMIT ?
            """
        return sql, [start, end, *params, limit or HISTORY_ROW_CAP]

    def _rollup_query(self, start: float, end: float, bucket: int, ids: Optional[List[int]] = None,
                      limit: Optional[int] = None) -> Tuple[str, list]:
        """bucket must be a multiple of 60. A division by a zero count yields NULL in SQLite. The latency band is the
        mean of the minutes' own 10th and 90th percentiles, weighted by their answered probes: not the exact
        percentile of the whole bucket (which cannot be merged), but it keeps the spread a min/max band would lose
        to a single outlier. Minutes from before the percentiles were stored count for nothing; none left gives NULL."""
        clause, params = self._target_clause("target", ids)
        b = int(bucket)
        sql = f"""
            SELECT target, (minute * 60 / {b}) * {b} AS bucket,
                   ROUND(SUM(lat_sum) / SUM(ok), 2),
                   ROUND((SUM(n) - SUM(ok)) * 100.0 / SUM(n), 1),
                   ROUND(SUM(jit_sum) / SUM(jit_n), 2),
                   SUM(ok), SUM(n),
                   ROUND(SUM(lat_p10 * ok) / SUM(CASE WHEN lat_p10 IS NOT NULL THEN ok END), 2),
                   ROUND(SUM(lat_p90 * ok) / SUM(CASE WHEN lat_p90 IS NOT NULL THEN ok END), 2)
            FROM rollup_1m
            WHERE minute >= ? AND minute <= ?{clause}
            GROUP BY target, bucket
            ORDER BY bucket
            LIMIT ?
        """
        return sql, [int(start // 60), int(end // 60), *params, limit or HISTORY_ROW_CAP]

    async def _fetch_raw(self, db, start: float, end: float, ids: Optional[List[int]]) -> List[Dict[str, Any]]:
        sql, params = self._raw_query(start, end, ids)
        async with db.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
        keys = self._target_keys
        return self.shown([
            {
                "target_id": keys.get(target, str(target)),
                "timestamp": ts,
                "success": ok,
                "latency_ms": latency,
                "packet_loss_pct": (1 - ok) * 100.0,
                "jitter_ms": jitter,
                "error": error,
            }
            for target, ts, ok, latency, jitter, error in rows
        ])

    async def get_raw_samples(
        self, start_time: float, end_time: float, target_ids: Optional[List[str]] = None
    ) -> List[Dict[str, Any]]:
        """Raw probe samples in a window of any length (capped at HISTORY_ROW_CAP rows)."""
        await self.flush_samples()
        ids = self._target_filter(target_ids)
        if ids == []:
            return []
        async with self._db.read() as db:
            return await self._fetch_raw(db, start_time, end_time, ids)

    async def get_history(
        self,
        start_time: float,
        end_time: float,
        target_ids: Optional[List[str]] = None,
        bucket_seconds: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Probe history in a time range: raw samples (ranges up to 6 h), sub-minute buckets computed from
        raw samples (up to 24 h), otherwise per-minute rollups re-aggregated into buckets of >= 60 s.
        Ranges are clipped to what is retained; buckets grow when needed to stay under HISTORY_ROW_CAP."""
        rows, _ = await self.get_history_buckets(start_time, end_time, target_ids, bucket_seconds)
        return rows

    async def get_history_buckets(
        self,
        start_time: float,
        end_time: float,
        target_ids: Optional[List[str]] = None,
        bucket_seconds: Optional[int] = None,
    ) -> Tuple[List[Dict[str, Any]], int]:
        """get_history plus the width of the buckets it actually used, in seconds (0 for raw samples): the
        requested width can grow to stay under HISTORY_ROW_CAP and is a multiple of 60 for rollups."""
        await self.flush_samples()
        now = time.time()
        start_time = max(start_time, now - self.rollup_retention_days * DAY)
        if end_time < start_time:
            return [], 0
        ids = self._target_filter(target_ids)
        if ids == []:
            return [], 0
        span = end_time - start_time
        raw_start = max(start_time, now - self.retention_days * DAY)
        raw_available = end_time >= raw_start
        bucket = int(bucket_seconds or 0)
        n_targets = len(ids) if ids is not None else max(len(self._target_keys), 1)
        min_bucket = math.ceil(span * n_targets / HISTORY_ROW_CAP)

        async with self._db.read() as db:
            if bucket <= 1 and span <= HISTORY_RAW_MAX_SECONDS and raw_available:
                return await self._fetch_raw(db, raw_start, end_time, ids), 0
            if 1 < bucket < 60 and span <= HISTORY_SMALL_BUCKET_MAX_SECONDS and raw_available:
                width = max(bucket, min_bucket)
                sql, params = self._raw_bucket_query(raw_start, end_time, width, ids,
                                                     band=span <= HISTORY_BAND_RAW_MAX_SECONDS)
            else:
                width = math.ceil(max(bucket, min_bucket, 60) / 60) * 60
                sql, params = self._rollup_query(start_time, end_time, width, ids)
            async with db.execute(sql, params) as cursor:
                rows = await cursor.fetchall()
        keys = self._target_keys
        return self.shown([dict(zip(_BUCKET_KEYS, (keys.get(row[0], str(row[0])), *row[1:]))) for row in rows]), width

    async def probe_loss_stats(self, start_time: float, end_time: float, target_ids: List[str]) -> List[TargetLoss]:
        """Per target of target_ids, over the raw samples with start_time <= ts <= end_time: probes sent and answered,
        the first and last lost probe and the first and last probe (incidents.loss reads an incident's loss span from
        them). One aggregate query, no row cap; targets without a sample in the range are left out."""
        await self.flush_samples()
        ids = self._target_filter(target_ids)
        if not ids:
            return []
        clause, params = self._target_clause("target", ids)
        async with self._db.read() as db:
            async with db.execute(
                f"""
                SELECT target, COUNT(*), SUM(ok), MIN(CASE WHEN ok = 0 THEN ts END), MAX(CASE WHEN ok = 0 THEN ts END),
                       MIN(ts), MAX(ts)
                FROM samples
                WHERE ts >= ? AND ts <= ?{clause}
                GROUP BY target
                """,
                (start_time, end_time, *params),
            ) as cursor:
                rows = await cursor.fetchall()
        keys = self._target_keys
        return [TargetLoss(keys.get(target, str(target)), sent, received or 0, first_lost, last_lost, first, last)
                for target, sent, received, first_lost, last_lost, first, last in rows]

    async def get_sample_stats(self, start_time: float, end_time: float) -> List[Dict[str, Any]]:
        """Per-target totals over the raw samples with start_time <= ts <= end_time, whatever the window length:
        one aggregate query, no row cap. Latency is that of answered probes; jitter is whatever probes reported."""
        await self.flush_samples()
        async with self._db.read() as db:
            async with db.execute(
                """
                SELECT target, COUNT(*), SUM(ok),
                       AVG(CASE WHEN ok = 1 THEN latency_ms END),
                       MIN(CASE WHEN ok = 1 THEN latency_ms END),
                       MAX(CASE WHEN ok = 1 THEN latency_ms END),
                       AVG(jitter_ms)
                FROM samples
                WHERE ts >= ? AND ts <= ?
                GROUP BY target
                ORDER BY target
                """,
                (start_time, end_time),
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

    async def get_rollup_stats(self, start_time: float, end_time: float) -> List[Dict[str, Any]]:
        """get_sample_stats from the per-minute rollups, for windows whose raw samples are past retention. Rollups are
        whole minutes, so the window grows to the minutes it touches (start_time // 60 .. end_time // 60 inclusive).
        Latency is that of answered probes; jitter is the mean over the probes that reported one."""
        await self.flush_samples()
        async with self._db.read() as db:
            async with db.execute(
                """
                SELECT target, SUM(n), SUM(ok), SUM(lat_sum) / SUM(ok), MIN(lat_min), MAX(lat_max),
                       SUM(jit_sum) / SUM(jit_n)
                FROM rollup_1m
                WHERE minute >= ? AND minute <= ?
                GROUP BY target
                ORDER BY target
                """,
                (int(start_time // 60), int(end_time // 60)),
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

    async def get_hop_stats(self, start_time: float, end_time: Optional[float] = None) -> List[Dict[str, Any]]:
        """Per-target probe totals since start_time (up to end_time when given: whole minutes, as get_rollup_stats),
        from the per-minute rollups."""
        await self.flush_samples()
        until, bound = ("", ()) if end_time is None else (" AND minute <= ?", (int(end_time // 60),))
        async with self._db.read() as db:
            async with db.execute(
                f"""
                SELECT target, SUM(n), SUM(n) - SUM(ok), SUM(lat_sum) / SUM(ok), MAX(lat_max)
                FROM rollup_1m
                WHERE minute >= ?{until}
                GROUP BY target
                """,
                (int(start_time // 60), *bound),
            ) as cursor:
                rows = await cursor.fetchall()
        return self.shown([
            {
                "target_id": self._target_keys.get(target, str(target)),
                "sent": sent,
                "lost": lost,
                "loss_pct": round((lost / sent) * 100.0, 2) if sent > 0 else 0.0,
                "avg_ms": round(avg_ms or 0, 2),
                "max_ms": round(max_ms or 0, 2),
            }
            for target, sent, lost, avg_ms, max_ms in rows
        ])
