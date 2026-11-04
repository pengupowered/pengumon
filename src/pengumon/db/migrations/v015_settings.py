"""v15: the settings store (settings_values, settings_items, settings_versions)."""

import aiosqlite

from pengumon.db.schema import (
    CREATE_SETTINGS_ITEMS_TABLE,
    CREATE_SETTINGS_VALUES_TABLE,
    CREATE_SETTINGS_VERSIONS_TABLE,
)

VERSION = 15


async def migrate(db: aiosqlite.Connection):
    """The tables only: the one-time import of config.yaml runs at start (pengumon.settings.manager), which knows
    the config. Idempotent."""
    await db.execute(CREATE_SETTINGS_VALUES_TABLE)
    await db.execute(CREATE_SETTINGS_ITEMS_TABLE)
    await db.execute(CREATE_SETTINGS_VERSIONS_TABLE)
