"""v14: the applied versions of the probe path (path_versions) and the path's events (path_events)."""

import json
import time

import aiosqlite

from pengumon.db.schema import CREATE_PATH_EVENTS_INDEX, CREATE_PATH_EVENTS_TABLE, CREATE_PATH_VERSIONS_TABLE

VERSION = 14

BASELINE_SUMMARY = "The path as it was when Pengumon started keeping versions"


async def migrate(db: aiosqlite.Connection):
    """The two tables. A path applied before this version gets its current revision stored as the first version (so
    the path editor has something to go back to); a database whose path was never imported or applied gets none.
    Idempotent."""
    await db.execute(CREATE_PATH_VERSIONS_TABLE)
    await db.execute(CREATE_PATH_EVENTS_TABLE)
    await db.execute(CREATE_PATH_EVENTS_INDEX)
    async with db.execute("SELECT value FROM meta WHERE key = 'path_revision'") as cursor:
        row = await cursor.fetchone()
    revision = int(row[0]) if row and str(row[0]).isdigit() else 0
    if revision <= 0:
        return
    async with db.execute("SELECT 1 FROM path_versions LIMIT 1") as cursor:
        if await cursor.fetchone() is not None:
            return
    async with db.execute(
        "SELECT config FROM path_targets WHERE deleted_at IS NULL ORDER BY position, id"
    ) as cursor:
        rows = await cursor.fetchall()
    hops = []
    for (raw,) in rows:
        try:
            hop = json.loads(raw)
        except ValueError:
            continue
        if isinstance(hop, dict):
            hops.append({**hop, "position": len(hops)})
    await db.execute(
        "INSERT INTO path_versions (revision, applied_at, hop_count, summary, hops) VALUES (?, ?, ?, ?, ?)",
        (revision, time.time(), len(hops), BASELINE_SUMMARY, json.dumps(hops, separators=(",", ":"))))
