"""v7: the kind of error behind an error snapshot."""

import aiosqlite

from pengumon.db.migrations.common import columns

VERSION = 7


async def migrate(db: aiosqlite.Connection):
    """The kind of error behind an error snapshot, so "the device could not be reached" (timeout, unreachable) is
    told apart from "it answered with something unusable" (auth, protocol). Nullable: older builds keep working
    against a v7 database, and their rows (and those from before v7) read as "unknown kind"."""
    for table in ("docsis_signals", "router_metrics"):
        if "error_kind" not in await columns(db, table):
            await db.execute(f"ALTER TABLE {table} ADD COLUMN error_kind TEXT")
