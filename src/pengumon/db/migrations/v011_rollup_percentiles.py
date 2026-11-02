"""v11: per-minute latency percentiles in rollup_1m."""

import logging
import time

import aiosqlite

from pengumon.db.migrations.common import ROLLUP_CHUNK_SECONDS, columns, scalar
from pengumon.db.schema import (
    UPSERT_ROLLUP_1M_FROM_SAMPLES,
)

logger = logging.getLogger("pengumon.db")

VERSION = 11


async def migrate(db: aiosqlite.Connection):
    """Per-minute latency percentiles (lat_p10, lat_p90) in rollup_1m, for the latency band of long charts. Minutes
    whose raw samples are still stored (the retention window) are recomputed a day at a time; older minutes keep NULL
    (their raw probes are gone, so no band is invented). Idempotent: a database that already has the columns (new, or
    built through v2 of this build) is left alone."""
    have = await columns(db, "rollup_1m")
    if {"lat_p10", "lat_p90"} <= have:
        return
    for column in ("lat_p10", "lat_p90"):
        if column not in have:
            await db.execute(f"ALTER TABLE rollup_1m ADD COLUMN {column} REAL")
    first = await scalar(db, "SELECT MIN(ts) FROM samples")
    last = await scalar(db, "SELECT MAX(ts) FROM samples")
    if first is None:
        return
    started = time.monotonic()
    logger.info("Schema v11: computing per-minute latency percentiles from the stored raw samples")
    start = int(first // 60) * 60
    for t in range(start, int(last) + 60, ROLLUP_CHUNK_SECONDS):
        await db.execute(UPSERT_ROLLUP_1M_FROM_SAMPLES, (t, t + ROLLUP_CHUNK_SECONDS))
    logger.info(f"Schema v11: percentiles done ({time.monotonic() - started:.0f} s)")
