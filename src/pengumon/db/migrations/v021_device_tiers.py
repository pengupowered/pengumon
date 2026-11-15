"""v21: the router's and the mesh's storage tiers (device_rollup_1m, device_rollup_15m, mesh_rollup_15m) and the router's
hourly app lists (traffic_app_hours)."""

import logging
import time

import aiosqlite

from pengumon.db.migrations.common import ROLLUP_CHUNK_SECONDS, scalar
from pengumon.db.schema import (
    CREATE_DEVICE_ROLLUP_15M_TABLE,
    CREATE_DEVICE_ROLLUP_1M_TABLE,
    CREATE_MESH_ROLLUP_15M_TABLE,
    CREATE_TRAFFIC_APP_HOURS_TABLE,
    UPSERT_DEVICE_ROLLUP_15M_FROM_1M_ALL,
    UPSERT_DEVICE_ROLLUP_1M_FROM_SAMPLES_ALL,
    UPSERT_MESH_ROLLUP_15M_FROM_SAMPLES,
)

logger = logging.getLogger("pengumon.db")

VERSION = 21
CHUNK_MINUTES = ROLLUP_CHUNK_SECONDS // 60  # a multiple of 15, so no quarter straddles two chunks


async def migrate(db: aiosqlite.Connection):
    """Creates the four tables and fills the device and mesh tiers from the raw rows present (a day at a time): the
    minutes from every raw poll, the quarters from the minutes, the mesh quarters from every raw reading. The oldest
    bucket of each is partial when the raw rows of its start were already pruned (its averages are of what was
    there). traffic_app_hours starts empty: the stored windows are the latest ones, written from now on. Idempotent:
    the tiers are recomputed from their sources."""
    for statement in (CREATE_DEVICE_ROLLUP_1M_TABLE, CREATE_DEVICE_ROLLUP_15M_TABLE, CREATE_MESH_ROLLUP_15M_TABLE,
                      CREATE_TRAFFIC_APP_HOURS_TABLE):
        await db.execute(statement)

    first = await scalar(db, "SELECT MIN(ts) FROM device_samples")
    if first is not None:
        last = await scalar(db, "SELECT MAX(ts) FROM device_samples")
        started = time.monotonic()
        logger.info("Schema v21: summing the router polls into minutes and quarter hours")
        for t in range(int(first // 900) * 900, int(last) + 1, ROLLUP_CHUNK_SECONDS):
            await db.execute(UPSERT_DEVICE_ROLLUP_1M_FROM_SAMPLES_ALL, (t, t + ROLLUP_CHUNK_SECONDS))
        first_minute = await scalar(db, "SELECT MIN(minute) FROM device_rollup_1m")
        last_minute = await scalar(db, "SELECT MAX(minute) FROM device_rollup_1m")
        for m in range(first_minute // 15 * 15, last_minute + 1, CHUNK_MINUTES):
            await db.execute(UPSERT_DEVICE_ROLLUP_15M_FROM_1M_ALL, (m, m + CHUNK_MINUTES))
        logger.info(f"Schema v21: router tiers done ({time.monotonic() - started:.1f} s)")

    first = await scalar(db, "SELECT MIN(ts) FROM mesh_samples")
    if first is not None:
        last = await scalar(db, "SELECT MAX(ts) FROM mesh_samples")
        started = time.monotonic()
        for t in range(int(first // 900) * 900, int(last) + 1, ROLLUP_CHUNK_SECONDS):
            await db.execute(UPSERT_MESH_ROLLUP_15M_FROM_SAMPLES, (t, t + ROLLUP_CHUNK_SECONDS, None))
        logger.info(f"Schema v21: mesh quarter hours done ({time.monotonic() - started:.1f} s)")
