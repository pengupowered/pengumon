"""v12: the loss span of an incident (pengumon.incidents.loss), next to its detected start and end."""

import aiosqlite

from pengumon.db.migrations.common import columns

VERSION = 12


async def migrate(db: aiosqlite.Connection):
    """Two nullable columns: loss_start (the first lost probe) and loss_seconds (first to last lost probe plus one
    interval; 0 when none was lost). NULL means not computed: the repository fills them when an incident closes and, at
    start, for older incidents whose raw samples are still kept. Idempotent."""
    have = await columns(db, "incidents")
    for column in ("loss_start", "loss_seconds"):
        if column not in have:
            await db.execute(f"ALTER TABLE incidents ADD COLUMN {column} REAL")
