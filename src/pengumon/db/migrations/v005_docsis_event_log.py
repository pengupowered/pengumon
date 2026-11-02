"""v5: whether each DOCSIS snapshot read the event log; event-log rows stored once."""

import aiosqlite

from pengumon.db.migrations.common import columns
from pengumon.db.schema import (
    CREATE_DOCSIS_LOGS_UNIQUE_INDEX,
)

VERSION = 5


async def migrate(db: aiosqlite.Connection):
    """Whether each DOCSIS snapshot managed to read the event log; event-log rows stored once."""
    if "event_log_ok" not in await columns(db, "docsis_signals"):
        await db.execute("ALTER TABLE docsis_signals ADD COLUMN event_log_ok INTEGER")
    # Every poll used to re-insert the day's events; keep the first row of each (timestamp, event_text).
    await db.execute(
        "DELETE FROM docsis_logs WHERE id NOT IN (SELECT MIN(id) FROM docsis_logs GROUP BY timestamp, event_text)"
    )
    await db.execute(CREATE_DOCSIS_LOGS_UNIQUE_INDEX)
