"""v10: client counts."""

import aiosqlite

from pengumon.db.schema import (
    DEVICE_SAMPLES_V10_COLUMNS,
    CREATE_MESH_SAMPLES_TABLE,
)

VERSION = 10


async def migrate(db: aiosqlite.Connection):
    """Client counts: five nullable columns on device_samples (existing rows keep NULL: no history to invent) and the
    table of per-node counts. Idempotent, so a crash between the statements and the version bump is harmless."""
    async with db.execute("PRAGMA table_info(device_samples)") as cursor:
        have = {row[1] for row in await cursor.fetchall()}
    for column in DEVICE_SAMPLES_V10_COLUMNS:
        if column not in have:
            await db.execute(f"ALTER TABLE device_samples ADD COLUMN {column} INTEGER")
    await db.execute(CREATE_MESH_SAMPLES_TABLE)
