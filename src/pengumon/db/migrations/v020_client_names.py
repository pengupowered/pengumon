"""v20: the names given to connected devices in Pengumon (client_names)."""

import aiosqlite

from pengumon.db.schema import CREATE_CLIENT_NAMES_TABLE

VERSION = 20


async def migrate(db: aiosqlite.Connection):
    """The table. Idempotent."""
    await db.execute(CREATE_CLIENT_NAMES_TABLE)
