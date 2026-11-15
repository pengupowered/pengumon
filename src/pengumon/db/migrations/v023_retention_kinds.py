"""v23: retention by kind of data. The stored setting `database.long_rollup_retention_days` (the probes' quarter-hour
tier) is now `database.latency_retention_days`: the value the owner set is kept under the new key, as a settings
version of its own (source "upgrade"). A value that is not stored stays unstored (it takes the new default). Idempotent:
with no old key stored there is nothing to do, and a new key already stored wins (the old one is just dropped)."""

import json
import logging
import time

import aiosqlite

from pengumon.db.migrations.common import scalar, table_exists

logger = logging.getLogger("pengumon.db")

VERSION = 23

OLD_KEY = "database.long_rollup_retention_days"
NEW_KEY = "database.latency_retention_days"


async def migrate(db: aiosqlite.Connection):
    from pengumon.db.settings import SETTINGS_REVISION_KEY, _document, _put_meta, _revision, _store_version

    if not await table_exists(db, "settings_values"):
        return
    raw = await scalar(db, "SELECT value FROM settings_values WHERE key = ?", (OLD_KEY,))
    if raw is None:
        return
    has_new = await scalar(db, "SELECT COUNT(*) FROM settings_values WHERE key = ?", (NEW_KEY,))
    now = time.time()
    try:
        days = json.loads(raw)
    except ValueError:
        days = None
    if not has_new and isinstance(days, int) and not isinstance(days, bool) and days >= 30:
        await db.execute("INSERT INTO settings_values (key, value, updated) VALUES (?, ?, ?)", (NEW_KEY, raw, now))
        summary = f"Keep latency history for: {days} days, the value of the old quarter-hour history setting"
    else:
        summary = "The old quarter-hour history setting is gone (the new setting keeps its value or its default)"
    await db.execute("DELETE FROM settings_values WHERE key = ?", (OLD_KEY,))
    revision = await _revision(db) + 1
    await _put_meta(db, SETTINGS_REVISION_KEY, str(revision))
    await _store_version(db, revision, "upgrade", summary, await _document(db), now)
    logger.info(f"Schema v23: the stored {OLD_KEY} is now {NEW_KEY}")
