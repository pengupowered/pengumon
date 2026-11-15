"""v22: each incident keeps its own evidence (incident_evidence, incident_evidence_probes, incident_evidence_readings).

Fills them for every resolved incident from what is stored now, before the retention of the shared tables can take it
away: raw probes while `samples` has them, else the per-minute rollups, else a row that says there was nothing."""

import logging
import time

import aiosqlite

from pengumon.db.schema import (
    CREATE_INCIDENT_EVIDENCE_PROBES_TABLE,
    CREATE_INCIDENT_EVIDENCE_READINGS_TABLE,
    CREATE_INCIDENT_EVIDENCE_TABLE,
)

logger = logging.getLogger("pengumon.db")

VERSION = 22


async def migrate(db: aiosqlite.Connection):
    """The tables, then the evidence of every resolved incident that has none. Idempotent: an incident with a row is
    skipped."""
    from pengumon.db.evidence import store_evidence  # not at import: db.core imports the migrations

    await db.execute(CREATE_INCIDENT_EVIDENCE_TABLE)
    await db.execute(CREATE_INCIDENT_EVIDENCE_PROBES_TABLE)
    await db.execute(CREATE_INCIDENT_EVIDENCE_READINGS_TABLE)
    async with db.execute(
        "SELECT id, start_time, end_time, duration_seconds FROM incidents WHERE resolved = 1 "
        "AND id NOT IN (SELECT incident_id FROM incident_evidence) ORDER BY id"
    ) as cursor:
        incidents = await cursor.fetchall()
    if not incidents:
        return
    started = time.monotonic()
    logger.info(f"Schema v22: keeping the evidence of {len(incidents)} incidents")
    counts = {}
    for incident_id, start_time, end_time, duration in incidents:
        resolution = await store_evidence(db, incident_id, start_time, end_time, duration, True)
        counts[resolution] = counts.get(resolution, 0) + 1
    logger.info(f"Schema v22: incident evidence done ({time.monotonic() - started:.1f} s; {counts})")
