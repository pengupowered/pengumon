"""v25: slimmer raw probe rows. `samples(ts, target, ok, latency_ms, jitter_ms, error)` with latency and jitter as 8-byte
REALs becomes `samples(ts, target, ok, latency_cms, jitter_cms, error)` with both as integer hundredths of a millisecond
(db/schema.py, schema v25): still one row per probe, clustered by time, the time as it was (REAL seconds, bit for bit).
Every stored latency and jitter is a whole hundredth (the prober rounds them), so the conversion is exact.

The rows are copied a day at a time in time order (so the new table is written in its key order) into a table beside the
old one, which is then dropped and the new one renamed. All of it is one transaction with the version bump (the
migration runner's rule), so an interrupted start leaves the old table as it was and the next start copies again. The
runner returns the dropped table's pages to the file system afterwards (incremental vacuum)."""

import logging
import math
import time

import aiosqlite

from pengumon.db.migrations.common import ROLLUP_CHUNK_SECONDS, columns, scalar, table_exists
from pengumon.db.schema import create_samples_table

logger = logging.getLogger("pengumon.db")

VERSION = 25
COPY_CHUNK_SECONDS = ROLLUP_CHUNK_SECONDS  # a day of probes per statement: about 1.2 M rows at 14 targets a second
NEW_TABLE = "samples_v25"

# SQLite's ROUND (halves away from zero), which schema.sample_cms mirrors for the rows the flush writes.
_COPY = f"""
INSERT INTO {NEW_TABLE} (ts, target, ok, latency_cms, jitter_cms, error)
SELECT ts, target, ok, CAST(ROUND(latency_ms * 100) AS INTEGER), CAST(ROUND(jitter_ms * 100) AS INTEGER), error
FROM samples
WHERE ts >= ? AND ts < ?
ORDER BY ts, target
"""


async def migrate(db: aiosqlite.Connection):
    """Copies the old rows into the new shape and replaces the table. Idempotent: a table that already has the new
    columns is left alone."""
    if "latency_cms" in await columns(db, "samples"):
        return
    await db.execute(f"DROP TABLE IF EXISTS {NEW_TABLE}")
    await db.execute(create_samples_table(NEW_TABLE))
    if not await table_exists(db, "samples"):  # cannot happen past v2; the new table is all it needs
        await db.execute(f"ALTER TABLE {NEW_TABLE} RENAME TO samples")
        return
    first = await scalar(db, "SELECT MIN(ts) FROM samples")
    if first is not None:
        last = await scalar(db, "SELECT MAX(ts) FROM samples")
        total = await scalar(db, "SELECT COUNT(*) FROM samples")
        started = time.monotonic()
        logger.info(f"Schema v25: converting {total:,} probe samples to slimmer rows")
        copied = 0
        lo = math.floor(first / COPY_CHUNK_SECONDS) * COPY_CHUNK_SECONDS
        while lo <= last:
            hi = lo + COPY_CHUNK_SECONDS
            cursor = await db.execute(_COPY, (lo, hi))
            copied += max(cursor.rowcount, 0)
            await cursor.close()
            logger.info(f"Schema v25: samples {min(1.0, (hi - first) / max(last - first, 1)):6.1%} "
                        f"({copied:,} rows, {time.monotonic() - started:.0f} s)")
            lo = hi
        if copied != total:  # the chunks cover [first, last]: a row left out is a bug, and the transaction rolls back
            raise RuntimeError(f"Schema v25: copied {copied:,} of {total:,} probe samples")
        logger.info(f"Schema v25: {copied:,} probe samples converted ({time.monotonic() - started:.1f} s)")
    await db.execute("DROP TABLE samples")
    await db.execute(f"ALTER TABLE {NEW_TABLE} RENAME TO samples")
