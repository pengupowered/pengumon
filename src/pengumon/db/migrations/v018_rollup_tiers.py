"""v18: 10-second and quarter-hour rollups (rollup_10s, rollup_15m), and the per-minute retention's new default."""

import json
import logging
import time

import aiosqlite

from pengumon.db.migrations.common import ROLLUP_CHUNK_SECONDS, scalar, table_exists
from pengumon.db.schema import (
    CREATE_ROLLUP_10S_TABLE,
    CREATE_ROLLUP_15M_TABLE,
    UPSERT_ROLLUP_10S_FROM_SAMPLES,
    UPSERT_ROLLUP_15M_FROM_1M,
)

logger = logging.getLogger("pengumon.db")

VERSION = 18

RETENTION_KEY = "database.rollup_retention_days"
OLD_DEFAULT_DAYS = 400  # the per-minute retention's default before v18 (now 90: config.DatabaseConfig)
CHUNK_MINUTES = ROLLUP_CHUNK_SECONDS // 60  # a multiple of 15, so no quarter straddles two chunks


async def migrate(db: aiosqlite.Connection):
    """Creates both tables and fills them from what is stored: rollup_15m from every rollup_1m row, rollup_10s from
    every raw sample (a day at a time each). Then a per-minute retention stored at the old default (400 days, which
    the one-time import of config.yaml stored as the owner's when the example file was copied) goes back to the
    default, 90 days, as a settings version of its own. The rollups older than that are deleted by the next hourly
    cleanup, which runs after this migration (the storage component migrates before the retention component starts),
    so their quarter-hours are already in rollup_15m. Idempotent: the tables are recomputed from their sources."""
    await db.execute(CREATE_ROLLUP_10S_TABLE)
    await db.execute(CREATE_ROLLUP_15M_TABLE)

    first = await scalar(db, "SELECT MIN(minute) FROM rollup_1m")
    if first is not None:
        last = await scalar(db, "SELECT MAX(minute) FROM rollup_1m")
        started = time.monotonic()
        logger.info("Schema v18: summing the per-minute rollups into quarter hours")
        for m in range(first // 15 * 15, last + 1, CHUNK_MINUTES):
            await db.execute(UPSERT_ROLLUP_15M_FROM_1M, (m, m + CHUNK_MINUTES))
        logger.info(f"Schema v18: quarter hours done ({time.monotonic() - started:.1f} s)")

    first = await scalar(db, "SELECT MIN(ts) FROM samples")
    if first is not None:
        last = await scalar(db, "SELECT MAX(ts) FROM samples")
        started = time.monotonic()
        logger.info("Schema v18: computing 10-second rollups from the stored raw samples")
        for t in range(int(first // 10) * 10, int(last) + 10, ROLLUP_CHUNK_SECONDS):
            await db.execute(UPSERT_ROLLUP_10S_FROM_SAMPLES, (t, t + ROLLUP_CHUNK_SECONDS))
        logger.info(f"Schema v18: 10-second rollups done ({time.monotonic() - started:.1f} s)")

    await _retention_to_new_default(db)


async def _retention_to_new_default(db: aiosqlite.Connection) -> None:
    from pengumon.db.settings import SETTINGS_REVISION_KEY, _document, _put_meta, _revision, _store_version

    if not await table_exists(db, "settings_values"):
        return
    raw = await scalar(db, "SELECT value FROM settings_values WHERE key = ?", (RETENTION_KEY,))
    try:
        stored = json.loads(raw) if raw is not None else None
    except ValueError:
        return
    if stored != OLD_DEFAULT_DAYS:
        return
    await db.execute("DELETE FROM settings_values WHERE key = ?", (RETENTION_KEY,))
    revision = await _revision(db) + 1
    now = time.time()
    await _put_meta(db, SETTINGS_REVISION_KEY, str(revision))
    await _store_version(db, revision, "upgrade",
                         f"Keep per-minute history for: {OLD_DEFAULT_DAYS} days, the old default, to the new default "
                         "(90 days); older ranges use quarter-hour detail", await _document(db), now)
    logger.info(f"Schema v18: the per-minute history setting was the old default ({OLD_DEFAULT_DAYS} days); it is now "
                "the new default, 90 days (quarter-hour history is kept 400 days)")
