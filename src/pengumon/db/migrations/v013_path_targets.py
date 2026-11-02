"""v13: the probe path in the database (path_targets), edited from the dashboard."""

import aiosqlite

from pengumon.db.schema import (
    CREATE_PATH_TARGETS_TABLE,
)

VERSION = 13


async def migrate(db: aiosqlite.Connection):
    """The table only: the one-time import of config.yaml's targets and the custom targets runs at start
    (pengumon.engine.path), which knows the config."""
    await db.execute(CREATE_PATH_TARGETS_TABLE)
