"""v24: latency spikes (incidents/spikes.py). Creates
`latency_spikes` and converts the stored history to the rule the classifier follows from now on: a resolved incident
of a latency-only kind (no probe lost: loss_seconds NULL or 0) that lasted under 120 s
(classifier.latency_incident_after_seconds' default) becomes a spike row, and the incident is deleted with its
evidence (incident_evidence*), in the migration's one transaction. The peak is the slowest answered probe of the
stage's hops in the incident's own evidence (raw probes, or a bucket's highest), NULL without evidence.

The kinds, stages and categories below are a frozen copy of incidents/taxonomy.py and incidents/spikes.py as of this
migration: a migration must not change with the code it was written for. Idempotent: a converted incident is gone, so
a re-run finds nothing more."""

import json
import logging
import time
from typing import Dict, List, Optional, Tuple

import aiosqlite

from pengumon.db.migrations.common import columns, table_exists
from pengumon.db.schema import CREATE_LATENCY_SPIKES_INDEX, CREATE_LATENCY_SPIKES_TABLE

logger = logging.getLogger("pengumon.db")

VERSION = 24

UNDER_SECONDS = 120.0
# latency-only kind -> its stage (cause category); None: the cause code decides (CAUSE_STAGE)
KIND_STAGE: Dict[str, Optional[str]] = {
    "local_latency": "router", "gateway_latency": "gateway", "upstream_latency": "isp", "busy_latency": "load",
    "legacy_latency": None,
}
CAUSE_STAGE = {
    "nominal": "unknown", "local_wifi_mesh_drop": "router", "local_switch_cable_fault": "router",
    "gateway_unreachable": "gateway", "double_nat_congestion": "gateway", "access_line_drop": "line",
    "xfinity_last_mile_coax_drop": "line", "dns_resolver_failure": "dns", "internet_backbone_transit_loss": "isp",
    "target_specific_outage": "internet", "unknown_outage": "unknown", "load_induced_latency": "load",
    "speed_test": "load", "service_unavailable": "service",
}
STAGE_CATEGORIES = {
    "router": ("local",), "gateway": ("gateway",), "line": ("isp", "public"), "isp": ("isp", "public"),
    "internet": ("public",), "load": ("isp", "public"),
}


def stage_of(kind: str, cause_code: Optional[str]) -> str:
    return KIND_STAGE.get(kind) or CAUSE_STAGE.get(cause_code or "", "unknown")


async def _categories(db: aiosqlite.Connection) -> Dict[int, str]:
    """targets.id (the samples' and the evidence's target) -> the path hop's category, for the hops on record."""
    if not (await table_exists(db, "path_targets") and await table_exists(db, "targets")):
        return {}
    async with db.execute("SELECT t.id, p.config FROM targets t JOIN path_targets p ON p.id = t.key") as cursor:
        rows = await cursor.fetchall()
    out = {}
    for target, config in rows:
        try:
            category = json.loads(config).get("category")
        except (TypeError, ValueError, AttributeError):
            category = None
        if isinstance(category, str):
            out[target] = category
    return out


async def _peak(db: aiosqlite.Connection, incident_id: int, start: float, end: float,
                targets: Tuple[int, ...]) -> Optional[float]:
    """The slowest answered probe of `targets` in [start, end] in the incident's evidence (a bucket's highest)."""
    if not targets:
        return None
    marks = ",".join("?" * len(targets))
    async with db.execute(
        f"SELECT MAX(CASE WHEN width = 0 THEN latency_ms ELSE lat_max END) FROM incident_evidence_probes "
        f"WHERE incident_id = ? AND target IN ({marks}) AND ts <= ? AND ts + width >= ?",
        (incident_id, *targets, end, start),
    ) as cursor:
        row = await cursor.fetchone()
    return None if row is None or row[0] is None else round(float(row[0]), 2)


async def migrate(db: aiosqlite.Connection):
    await db.execute(CREATE_LATENCY_SPIKES_TABLE)
    await db.execute(CREATE_LATENCY_SPIKES_INDEX)
    have = await columns(db, "incidents")
    if not {"kind", "loss_seconds", "expected"} <= have:
        return  # (every database that reaches this has them: v2, v6 and v12 added them)
    kinds = tuple(KIND_STAGE)
    async with db.execute(
        f"SELECT id, start_time, end_time, kind, cause_code, COALESCE(expected, 0) FROM incidents "
        f"WHERE resolved = 1 AND end_time IS NOT NULL AND kind IN ({','.join('?' * len(kinds))}) "
        f"AND COALESCE(loss_seconds, 0) = 0 AND COALESCE(duration_seconds, end_time - start_time) < ? "
        f"ORDER BY start_time",
        (*kinds, UNDER_SECONDS),
    ) as cursor:
        rows = await cursor.fetchall()
    if not rows:
        return
    started = time.monotonic()
    evidence = await table_exists(db, "incident_evidence_probes")
    categories = await _categories(db) if evidence else {}
    spikes: List[tuple] = []
    by_kind: Dict[str, int] = {}
    for incident_id, start, end, kind, cause_code, expected in rows:
        stage = stage_of(kind, cause_code)
        wanted = STAGE_CATEGORIES.get(stage, ())
        targets = tuple(sorted(t for t, c in categories.items() if c in wanted))
        peak = await _peak(db, incident_id, start, max(start, end), targets) if evidence else None
        spikes.append((start, max(start, end), kind, stage, 1 if expected else 0, peak))
        by_kind[kind] = by_kind.get(kind, 0) + 1
    await db.executemany(
        "INSERT INTO latency_spikes (start_time, end_time, kind, stage, expected, peak_ms) VALUES (?, ?, ?, ?, ?, ?)",
        spikes)
    ids = [(r[0],) for r in rows]
    for table in ("incident_evidence_probes", "incident_evidence_readings", "incident_evidence"):
        if await table_exists(db, table):
            await db.executemany(f"DELETE FROM {table} WHERE incident_id = ?", ids)
    await db.executemany("DELETE FROM incidents WHERE id = ?", ids)
    logger.info(f"Schema v{VERSION}: {len(rows)} latency incidents under {UNDER_SECONDS:g} s without loss are latency "
                f"spikes now ({', '.join(f'{k} {n}' for k, n in sorted(by_kind.items()))}; "
                f"{time.monotonic() - started:.1f} s)")
