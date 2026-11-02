"""v2: probe_samples -> samples (clustered by time) + rollup_1m, and the incident load columns."""

import logging
import time

import aiosqlite

from pengumon.db.migrations.common import ROLLUP_CHUNK_SECONDS, columns, scalar, table_exists
from pengumon.db.schema import (
    CREATE_TARGETS_TABLE,
    CREATE_SAMPLES_TABLE,
    CREATE_ROLLUP_1M_TABLE,
    UPSERT_ROLLUP_1M_FROM_SAMPLES,
)

logger = logging.getLogger("pengumon.db")

VERSION = 2
COPY_CHUNK_ROWS = 250_000


async def migrate(db: aiosqlite.Connection):
    """probe_samples -> samples (clustered by time) + rollup_1m; incident load columns."""
    for stmt in (CREATE_TARGETS_TABLE, CREATE_SAMPLES_TABLE, CREATE_ROLLUP_1M_TABLE):
        await db.execute(stmt)
    incident_cols = await columns(db, "incidents")
    if "load_pct" not in incident_cols:
        await db.execute("ALTER TABLE incidents ADD COLUMN load_pct REAL")
    if "expected" not in incident_cols:
        await db.execute("ALTER TABLE incidents ADD COLUMN expected INTEGER DEFAULT 0")

    if await table_exists(db, "probe_samples"):
        await db.execute("INSERT OR IGNORE INTO targets (key) SELECT DISTINCT target_id FROM probe_samples")
        lo = await scalar(db, "SELECT MIN(id) FROM probe_samples")
        hi = await scalar(db, "SELECT MAX(id) FROM probe_samples")
        if lo is not None:
            started = time.monotonic()
            total = hi - lo + 1
            copied = 0
            logger.info(f"Schema v2: copying probe samples (ids {lo:,}..{hi:,}) into the new samples table")
            for chunk_start in range(lo, hi + 1, COPY_CHUNK_ROWS):
                chunk_end = chunk_start + COPY_CHUNK_ROWS
                cursor = await db.execute(
                    """
                    INSERT OR IGNORE INTO samples (ts, target, ok, latency_ms, jitter_ms, error)
                    SELECT p.timestamp, t.id, p.success, p.latency_ms, p.jitter_ms, p.error
                    FROM probe_samples p JOIN targets t ON t.key = p.target_id
                    WHERE p.id >= ? AND p.id < ?
                    """,
                    (chunk_start, chunk_end),
                )
                copied += max(cursor.rowcount, 0)
                await cursor.close()
                done = min(chunk_end, hi + 1) - lo
                logger.info(
                    f"Schema v2: samples {done / total:6.1%} ({copied:,} rows, {time.monotonic() - started:.0f} s)"
                )

            first_ts = await scalar(db, "SELECT MIN(ts) FROM samples")
            last_ts = await scalar(db, "SELECT MAX(ts) FROM samples")
            if first_ts is not None:
                t = int(first_ts // 60) * 60
                while t <= last_ts:
                    await db.execute(UPSERT_ROLLUP_1M_FROM_SAMPLES, (t, t + ROLLUP_CHUNK_SECONDS))
                    t += ROLLUP_CHUNK_SECONDS
                minutes = await scalar(db, "SELECT COUNT(*) FROM rollup_1m")
                logger.info(
                    f"Schema v2: built {minutes:,} per-minute rollup rows ({time.monotonic() - started:.0f} s)"
                )
        await db.execute("DROP TABLE probe_samples")  # drops its index too
