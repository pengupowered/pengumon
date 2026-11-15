"""What the database costs on disk, by kind of data: the answer of GET /api/storage (api/storage.py), which Settings >
Retention turns into its table of "now" and "when full".

Every table belongs to one kind (KIND_OF_TABLE; a test fails for a table that has none). The bytes of a kind are the
bytes of its tables and their indexes, from SQLite's `dbstat` virtual table; where a build has no `dbstat` (it is a
compile option), a table is estimated as its rows times the average row size of a sample of its rows, and the answer
says `estimate: true`. `per_day` is what a day of the kind's data costs: its rows of the last 7 days (or of what is
stored, if less) at the table's average bytes per row.

`dbstat` reads every page of the database, so the answer is kept for a minute (CACHE_SECONDS).
"""

import asyncio
import os
import shutil
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from pengumon.db.core import TRAFFIC_APP_HOURS_DAYS

DAY = 86400
CACHE_SECONDS = 60
MEASURE_DAYS = 7  # per_day is measured over the rows of this many days
SAMPLE_ROWS = 500  # the estimate without dbstat looks at this many rows of a table
ROW_OVERHEAD = 12  # bytes per row the estimate adds to the column lengths (cell header, rowid, page slack)
FIXED_TIERED_DAYS = 30  # a kind with a quarter-hour tier keeps at least this much (the setting's minimum)
STEADY_STATE_SLACK = 0.1 * DAY  # a table pruned hourly is a little longer than its retention

# kind id -> (label, setting key without "database.", whether its 15-minute rows are "tiered": the first 30 days are
# the minimum every setting allows, so they count as fixed)
KINDS: Tuple[Tuple[str, str, Optional[str], bool], ...] = (
    ("full", "Full detail", None, False),
    ("latency", "Latency history", "latency_retention_days", True),
    ("router", "Router history", "router_retention_days", True),
    ("mesh", "Mesh history", "mesh_retention_days", True),
    ("line", "Cable line history", "line_retention_days", False),
    ("traffic", "Internet use", "traffic_retention_days", False),
    ("speedtest", "Speed tests and router events", "speedtest_retention_days", False),
    ("incident", "Incidents and their evidence", "incident_retention_days", False),
    ("other", "Other", None, False),
)

KIND_OF_TABLE: Dict[str, str] = {
    # full detail: fixed periods, the same for every kind of data
    "samples": "full", "rollup_10s": "full", "rollup_1m": "full", "device_samples": "full",
    "device_rollup_1m": "full", "mesh_samples": "full", "traffic_app_hours": "full",
    # by kind
    "rollup_15m": "latency", "device_rollup_15m": "router", "mesh_rollup_15m": "mesh", "line_samples": "line",
    "traffic_hourly": "traffic", "traffic_apps": "traffic", "speed_tests": "speedtest", "device_events": "speedtest",
    "incidents": "incident", "incident_evidence": "incident", "incident_evidence_probes": "incident",
    "incident_evidence_readings": "incident",
    # everything else: the probe path, the settings store, names, bookkeeping
    "targets": "other", "custom_targets": "other", "path_targets": "other", "path_versions": "other",
    "path_events": "other", "meta": "other", "settings_values": "other", "settings_items": "other",
    "settings_versions": "other", "client_names": "other", "sqlite_sequence": "other",
}

# table -> (time column, seconds per unit): the age of the oldest row and the rows of the last days
TIME_COLUMN: Dict[str, Tuple[str, int]] = {
    "samples": ("ts", 1), "rollup_10s": ("slot", 10), "rollup_1m": ("minute", 60), "rollup_15m": ("quarter", 900),
    "device_samples": ("ts", 1), "device_rollup_1m": ("minute", 60), "device_rollup_15m": ("quarter", 900),
    "mesh_samples": ("ts", 1), "mesh_rollup_15m": ("quarter", 900), "line_samples": ("ts", 1),
    "traffic_hourly": ("hour", 1), "traffic_apps": ("window_end", 1), "traffic_app_hours": ("window_end", 1),
    "speed_tests": ("ts", 1), "device_events": ("ts", 1), "incidents": ("start_time", 1),
}

# how long each full-detail table is kept (repository attribute; None: a constant number of days)
FULL_DETAIL_DAYS = {
    "samples": "retention_days", "rollup_10s": "slot_retention_days", "rollup_1m": "rollup_retention_days",
    "device_samples": "retention_days", "device_rollup_1m": "rollup_retention_days",
    "mesh_samples": "retention_days", "traffic_app_hours": TRAFFIC_APP_HOURS_DAYS,
}


def kind_of(table: str) -> str:
    """The kind of a table; an unknown one counts as "other" (the test of every table having a kind is what keeps the
    map complete)."""
    return KIND_OF_TABLE.get(table, "other")


@dataclass
class TableStat:
    name: str
    bytes: int
    rows: int
    oldest: Optional[float]  # epoch seconds of the oldest row, None without a time column or without rows
    recent_rows: int  # rows of the last MEASURE_DAYS days
    span_days: float  # days those rows cover (at most MEASURE_DAYS)
    estimate: bool

    @property
    def per_day(self) -> float:
        if not self.rows or self.span_days <= 0:
            return 0.0
        return self.bytes / self.rows * self.recent_rows / self.span_days


async def _has_dbstat(db) -> bool:
    try:
        async with db.execute("SELECT 1 FROM dbstat LIMIT 1") as cursor:
            await cursor.fetchall()
        return True
    except Exception:  # sqlite3.OperationalError: no such table: dbstat
        return False


async def _tables(db) -> Dict[str, str]:
    """{every b-tree name (table or index): its table}."""
    async with db.execute("SELECT name, tbl_name FROM sqlite_master WHERE type IN ('table', 'index')") as cursor:
        return {name: tbl for name, tbl in await cursor.fetchall()}


async def _real_tables(db) -> List[str]:
    async with db.execute("SELECT name FROM sqlite_master WHERE type = 'table'") as cursor:
        return [r[0] for r in await cursor.fetchall()]


async def _dbstat_sizes(db) -> Tuple[Dict[str, int], Dict[str, int]]:
    """({table: bytes of the table and its indexes}, {table: rows}). Rows are the leaf cells of the table's own tree."""
    owner = await _tables(db)
    sizes: Dict[str, int] = {}
    rows: Dict[str, int] = {}
    async with db.execute(
        "SELECT name, SUM(pgsize), SUM(CASE WHEN pagetype = 'leaf' THEN ncell ELSE 0 END) FROM dbstat GROUP BY name"
    ) as cursor:
        for name, size, cells in await cursor.fetchall():
            table = owner.get(name, name)
            sizes[table] = sizes.get(table, 0) + int(size or 0)
            if name == table:
                rows[table] = int(cells or 0)
    return sizes, rows


async def _estimate_table(db, table: str) -> Tuple[int, int]:
    """(estimated bytes, rows) of a table without dbstat: rows x the average row of a sample (the column lengths
    plus a fixed overhead per row); indexes are not counted."""
    async with db.execute(f'SELECT COUNT(*) FROM "{table}"') as cursor:
        rows = (await cursor.fetchone())[0]
    if not rows:
        return 0, 0
    async with db.execute(f'PRAGMA table_info("{table}")') as cursor:
        columns = [r[1] for r in await cursor.fetchall()]
    if not columns:
        return 0, rows
    width = " + ".join(f'COALESCE(LENGTH(CAST("{c}" AS BLOB)), 1)' for c in columns)
    async with db.execute(f'SELECT AVG({width}) FROM (SELECT * FROM "{table}" LIMIT {SAMPLE_ROWS})') as cursor:
        average = (await cursor.fetchone())[0] or 0
    return int(rows * (average + ROW_OVERHEAD)), rows


async def _time_facts(db, table: str, now: float, rows: int) -> Tuple[Optional[float], int, float]:
    """(oldest row's time, rows of the last MEASURE_DAYS days, days those rows span: at most MEASURE_DAYS)."""
    column = TIME_COLUMN.get(table)
    if column is None or not rows:
        return None, 0, 0.0
    name, unit = column
    async with db.execute(f'SELECT MIN("{name}") FROM "{table}"') as cursor:
        first = (await cursor.fetchone())[0]
    if first is None:
        return None, 0, 0.0
    oldest = float(first) * unit
    age_days = max((now - oldest) / DAY, 0.0)
    if age_days <= MEASURE_DAYS + STEADY_STATE_SLACK / DAY:
        return oldest, rows, max(age_days, 1e-9)  # everything stored is recent
    since = (now - MEASURE_DAYS * DAY) / unit
    async with db.execute(f'SELECT COUNT(*) FROM "{table}" WHERE "{name}" >= ?', (since,)) as cursor:
        recent = (await cursor.fetchone())[0]
    return oldest, recent, float(MEASURE_DAYS)


async def table_stats(db, now: float, force_estimate: bool = False) -> List[TableStat]:
    """Every table's bytes, rows and recent growth. `force_estimate`: take the path of a build without dbstat."""
    estimate = force_estimate or not await _has_dbstat(db)
    sizes: Dict[str, int] = {}
    rows: Dict[str, int] = {}
    if not estimate:
        sizes, rows = await _dbstat_sizes(db)
    out: List[TableStat] = []
    for table in await _real_tables(db):
        if estimate:
            size, count = await _estimate_table(db, table)
        else:
            size, count = sizes.get(table, 0), rows.get(table, 0)
        oldest, recent, span = await _time_facts(db, table, now, count)
        out.append(TableStat(table, size, count, oldest, recent, span, estimate))
    if not estimate and "sqlite_schema" in sizes:  # the schema's own pages, not a table of sqlite_master
        out.append(TableStat("sqlite_schema", sizes["sqlite_schema"], 0, None, 0, 0.0, False))
    return out


def _full_detail_days(repository, table: str) -> float:
    spec = FULL_DETAIL_DAYS[table]
    days = spec if isinstance(spec, int) else getattr(repository, spec)
    if table == "rollup_10s":
        days = max(days, repository.retention_days)
    return float(days)


def kinds_answer(stats: List[TableStat], repository, now: float) -> List[Dict[str, Any]]:
    """The per-kind rows of the answer from the table stats."""
    by_kind: Dict[str, List[TableStat]] = {k[0]: [] for k in KINDS}
    for stat in stats:
        by_kind[kind_of(stat.name)].append(stat)
    answer: List[Dict[str, Any]] = []
    for kind, label, setting, tiered in KINDS:
        tables = by_kind[kind]
        size = sum(t.bytes for t in tables)
        dated = [t for t in tables if t.oldest is not None]
        oldest = min((t.oldest for t in dated), default=None)
        days_stored = round((now - oldest) / DAY, 2) if oldest is not None else 0.0
        if kind == "full":
            # steady state: every table at the days it is kept for
            per_day = sum(t.per_day for t in tables)
            fixed = sum(t.per_day * _full_detail_days(repository, t.name) for t in tables if t.name in FULL_DETAIL_DAYS)
            fixed += sum(t.bytes for t in tables if t.name not in FULL_DETAIL_DAYS)
        elif kind == "incident":
            per_day = size / days_stored if days_stored >= 1 else float(size)  # a few incidents a day: use all
            fixed = 0.0
        elif kind == "other":
            per_day, fixed = 0.0, float(size)
        else:
            per_day = sum(t.per_day for t in tables)
            fixed = per_day * FIXED_TIERED_DAYS if tiered else 0.0
        answer.append({
            "id": kind, "label": label, "setting": f"database.{setting}" if setting else None,
            "tables": sorted(t.name for t in tables), "bytes": size, "days_stored": days_stored,
            "per_day": round(per_day), "fixed": round(fixed), "tiered": tiered,
            "estimate": any(t.estimate for t in tables),
        })
    return answer


def disk_answer(db_path: str) -> Dict[str, Any]:
    """The database file, its write-ahead log and the disk they are on."""
    def size(path: str) -> int:
        try:
            return os.path.getsize(path)
        except OSError:
            return 0

    folder = os.path.dirname(os.path.abspath(db_path)) or "."
    try:
        usage = shutil.disk_usage(folder)
        disk: Dict[str, Any] = {"total": usage.total, "used": usage.used, "free": usage.free}
    except OSError:
        disk = {"total": None, "used": None, "free": None}
    return {"file_bytes": size(db_path), "wal_bytes": size(db_path + "-wal"), "disk": disk}


async def storage_answer(repository, force_estimate: bool = False) -> Dict[str, Any]:
    """The whole answer, cached for CACHE_SECONDS (not when `force_estimate`, which is for tests)."""
    now = time.time()
    cached = getattr(repository, "_storage_cache", None)
    if cached is not None and not force_estimate and now - cached[0] < CACHE_SECONDS:
        return cached[1]
    started = time.monotonic()
    async with repository._db.read() as db:
        stats = await table_stats(db, now, force_estimate)
        async with db.execute("PRAGMA freelist_count") as cursor:
            free_pages = (await cursor.fetchone())[0]
        async with db.execute("PRAGMA page_size") as cursor:
            page_size = (await cursor.fetchone())[0]
    kinds = kinds_answer(stats, repository, now)
    answer = {
        "generated_at": now, "took_seconds": round(time.monotonic() - started, 3),
        "estimate": any(s.estimate for s in stats), "fixed_days": FIXED_TIERED_DAYS,
        "kinds": kinds, "total_bytes": sum(k["bytes"] for k in kinds), "free_bytes": free_pages * page_size,
        **await asyncio.to_thread(disk_answer, repository.db_path),
    }
    if not force_estimate:
        repository._storage_cache = (now, answer)
    return answer
