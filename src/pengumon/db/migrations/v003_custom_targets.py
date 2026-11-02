"""v3: targets added from the dashboard."""

import aiosqlite

from pengumon.db.schema import (
    CREATE_CUSTOM_TARGETS_TABLE,
)

VERSION = 3


async def migrate(db: aiosqlite.Connection):
    """Targets added from the dashboard (they used to vanish on restart)."""
    await db.execute(CREATE_CUSTOM_TARGETS_TABLE)
