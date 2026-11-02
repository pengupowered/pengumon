"""v4: key/value state."""

import aiosqlite

from pengumon.db.schema import (
    CREATE_META_TABLE,
)

VERSION = 4


async def migrate(db: aiosqlite.Connection):
    """Key/value state, e.g. that the one-time incident load backfill has run (see engine/load.py)."""
    await db.execute(CREATE_META_TABLE)
