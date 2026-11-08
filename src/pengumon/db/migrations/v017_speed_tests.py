"""v17: the speed tests (speed_tests)."""

import aiosqlite

from pengumon.db.schema import CREATE_SPEED_TESTS_INDEX, CREATE_SPEED_TESTS_TABLE

VERSION = 17


async def migrate(db: aiosqlite.Connection):
    """The table and its index. Idempotent."""
    await db.execute(CREATE_SPEED_TESTS_TABLE)
    await db.execute(CREATE_SPEED_TESTS_INDEX)
