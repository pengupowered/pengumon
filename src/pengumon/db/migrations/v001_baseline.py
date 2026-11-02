"""v1: the pre-versioning schema, including the column additions it used to apply on every start."""

import aiosqlite

from pengumon.db.migrations.common import columns
from pengumon.db.schema import (
    CREATE_PROBE_SAMPLES_TABLE,
    CREATE_PROBE_SAMPLES_INDEX,
    CREATE_INCIDENTS_TABLE,
    CREATE_INCIDENTS_INDEX,
    CREATE_DOCSIS_SIGNALS_TABLE,
    CREATE_DOCSIS_SIGNALS_INDEX,
    CREATE_DOCSIS_LOGS_TABLE,
    CREATE_DOCSIS_LOGS_INDEX,
    CREATE_ROUTER_METRICS_TABLE,
    CREATE_ROUTER_METRICS_INDEX,
)

VERSION = 1


async def migrate(db: aiosqlite.Connection):
    """The pre-versioning schema, including the column additions it used to apply on every start."""
    for stmt in (
        CREATE_PROBE_SAMPLES_TABLE, CREATE_PROBE_SAMPLES_INDEX,
        CREATE_INCIDENTS_TABLE, CREATE_INCIDENTS_INDEX,
        CREATE_DOCSIS_SIGNALS_TABLE, CREATE_DOCSIS_SIGNALS_INDEX,
        CREATE_DOCSIS_LOGS_TABLE, CREATE_DOCSIS_LOGS_INDEX,
        CREATE_ROUTER_METRICS_TABLE, CREATE_ROUTER_METRICS_INDEX,
    ):
        await db.execute(stmt)
    if "path_trace" not in await columns(db, "incidents"):
        await db.execute("ALTER TABLE incidents ADD COLUMN path_trace TEXT")
    if "uncorrectables_recent" not in await columns(db, "docsis_signals"):
        await db.execute("ALTER TABLE docsis_signals ADD COLUMN uncorrectables_recent INTEGER DEFAULT 0")
