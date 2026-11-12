"""v19: router uptime, CPU temperature and WAN link speed (device_samples columns), device events and the router's
traffic per client and hour (traffic_hourly, traffic_apps)."""

import aiosqlite

from pengumon.db.migrations.common import columns
from pengumon.db.schema import (
    CREATE_DEVICE_EVENTS_INDEX, CREATE_DEVICE_EVENTS_TABLE, CREATE_TRAFFIC_APPS_INDEX, CREATE_TRAFFIC_APPS_TABLE,
    CREATE_TRAFFIC_HOURLY_TABLE, DEVICE_SAMPLES_V19_COLUMNS,
)

VERSION = 19


async def migrate(db: aiosqlite.Connection):
    """Three nullable columns on device_samples (existing rows keep NULL) and three tables. Idempotent."""
    have = await columns(db, "device_samples")
    for column, kind in DEVICE_SAMPLES_V19_COLUMNS:
        if column not in have:
            await db.execute(f"ALTER TABLE device_samples ADD COLUMN {column} {kind}")
    for statement in (CREATE_DEVICE_EVENTS_TABLE, CREATE_DEVICE_EVENTS_INDEX, CREATE_TRAFFIC_HOURLY_TABLE,
                      CREATE_TRAFFIC_APPS_TABLE, CREATE_TRAFFIC_APPS_INDEX):
        await db.execute(statement)
