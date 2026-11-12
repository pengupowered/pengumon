"""Connections, schema migration, the target-id map, key/value state and retention: what every other part of
the repository builds on. Holds the state the other mixins share (`_db`, the target maps, the incident cache)."""

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import aiosqlite

from pengumon import paths
from pengumon._vendor.homeguard.db import Database
from pengumon.models.probes import ProbeSample
from pengumon.db.migrations import backfill_incident_kinds, migrate
from pengumon.observability import StateLog, get_logger
from pengumon.observability.telemetry import db_retention_duration, db_retention_rows, db_vacuum_pages, tracer

logger = logging.getLogger("pengumon.db")

DAY = 86400
PRUNE_BATCH_ROWS = 5000  # rows one retention delete removes per write transaction (tens of milliseconds)
PRUNE_VACUUM_PAGES = 2000  # free pages one incremental_vacuum step returns to the file per write transaction
SPEED_TEST_MIN_DAYS = 32  # speed tests are kept at least this long (the monthly data cap needs the whole month)


# Read-only connections (homeguard Database readers): enough for a page load's slow report plus its quick reads.
READERS = 3

class RepositoryCore:
    def __init__(
        self,
        db_path: Optional[str] = None,
        sample_flush_seconds: float = 10.0,
        retention_days: int = 7,
        rollup_retention_days: int = 90,
        long_rollup_retention_days: int = 400,
        traffic_retention_days: int = 400,
        slot_retention_days: int = 7,
        incident_retention_days: int = 0,
    ):
        self.db_path = paths.db_path(db_path)
        self._ensure_dir()
        self.retention_days = retention_days
        self.rollup_retention_days = rollup_retention_days
        self.long_rollup_retention_days = long_rollup_retention_days
        self.traffic_retention_days = traffic_retention_days
        self.slot_retention_days = slot_retention_days  # the 10-second rollups; keeping them while raw is shorter is its point
        self.incident_retention_days = incident_retention_days  # 0: resolved incidents are kept forever
        # targets table cache (key <-> id); tiny, and only grows when a new target first reports.
        self._target_ids: Dict[str, int] = {}
        self._target_keys: Dict[int, str] = {}
        # Hops removed from the path (db/path.py): their history is kept but left out of every per-target answer.
        self.hidden_targets: Set[str] = set()
        # Probe samples are committed in batches (see insert_samples); 0 commits every call.
        self.sample_flush_seconds = sample_flush_seconds
        self._sample_buffer: List[ProbeSample] = []
        self._last_flush = time.monotonic()
        self._flush_state = StateLog(get_logger("pengumon.db"), "Writing probe samples")
        self._dropping = False
        # Connections for the process lifetime: write() for everything that changes data (one transaction at a
        # time) and read() for queries (read-only, whole results) on up to READERS connections, so a slow report does
        # not hold up the quick reads of a page load (one reader made them wait 300-500 ms). synchronous=NORMAL is
        # crash-safe in WAL (a power cut can lose the last moments of telemetry, never corrupt the file) and avoids an
        # fsync per commit.
        self._db = Database(self.db_path, pragmas=("synchronous=NORMAL",), wal=True, readers=READERS)
        self._migrated = False
        # Bumped whenever an incident is created, resolved or annotated; keys the incident-list cache.
        self._incidents_version = 0
        self._incidents_cache: Dict[tuple, tuple] = {}
        # Bumped whenever the path is applied or a removed hop's history is deleted (db/path.py); keys the report cache.
        self._path_changes = 0

    def read(self):
        """The read-only connection, as an async context manager: for queries that live outside the repository
        (the report queries in pengumon.reports.queries)."""
        return self._db.read()

    def target_key(self, target_id: int) -> str:
        """The key of a target id as stored in the samples tables (the id itself, as text, if none is known)."""
        return self._target_keys.get(target_id, str(target_id))

    def shown(self, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """The rows (each with a "target_id") of targets that are not hidden (removed from the path)."""
        hidden = self.hidden_targets
        return [r for r in rows if r["target_id"] not in hidden] if hidden else rows

    @property
    def sample_buffer_size(self) -> int:
        """Probe samples waiting for the next commit (the telemetry gauge reads it)."""
        return len(self._sample_buffer)

    def file_sizes(self) -> Dict[str, int]:
        """Bytes of the database file and of its write-ahead log (0 while there is none: it is checkpointed away)."""
        sizes = {}
        for name, path in (("main", self.db_path), ("wal", f"{self.db_path}-wal")):
            try:
                sizes[name] = os.stat(path).st_size
            except OSError:
                sizes[name] = 0
        return sizes

    @property
    def incidents_version(self) -> int:
        return self._incidents_version

    @property
    def path_changes(self) -> int:
        return self._path_changes

    async def close(self):
        try:
            await self.flush_samples()
        except Exception:
            pass  # logged; the samples of a failing database are lost either way
        await self._db.close()
        self._migrated = False

    def _ensure_dir(self):
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

    async def initialize(self):
        """Migrates the schema, loads the target map and resolves orphaned previous-session incidents."""
        if not self._migrated:
            # Before the connections are first opened: the one-time VACUUM needs the database to itself.
            with tracer.start_as_current_span("db.migrate") as span:
                span.set_attribute("db.schema_version", await migrate(self.db_path))
            self._migrated = True
        async with self._db.write() as db:
            # Incidents left open by a crash: the last sample after their start is the last moment the
            # monitor was running, so the outage lasted at most until then.
            await db.execute(
                """
                UPDATE incidents
                SET end_time = COALESCE(
                    (SELECT MAX(ts) FROM samples WHERE ts > incidents.start_time), start_time + 10.0)
                WHERE resolved = 0
                """
            )
            await db.execute(
                "UPDATE incidents SET duration_seconds = end_time - start_time, resolved = 1 WHERE resolved = 0"
            )
            # Incidents written by an older build running against this database have no kind.
            if await backfill_incident_kinds(db):
                self._incidents_version += 1
        async with self._db.read() as db:
            async with db.execute("SELECT id, key FROM targets") as cursor:
                for target_id, key in await cursor.fetchall():
                    self._target_ids[key] = target_id
                    self._target_keys[target_id] = key
            async with db.execute("SELECT id FROM path_targets WHERE deleted_at IS NOT NULL") as cursor:
                self.hidden_targets = {row[0] for row in await cursor.fetchall()}
        # Needs the target map: the loss span of incidents closed above or by an older build (db/incidents.py).
        await self.backfill_incident_loss()
        logger.info(f"Database initialized at {self.db_path}")

    async def _resolve_target_ids(self, db: aiosqlite.Connection, keys) -> Dict[str, int]:
        """Target key -> id, inserting unknown keys (inside the caller's transaction; the cache is only
        updated by _remember_targets once that transaction committed)."""
        ids = {k: self._target_ids[k] for k in keys if k in self._target_ids}
        missing = [k for k in keys if k not in ids]
        if missing:
            await db.executemany("INSERT OR IGNORE INTO targets (key) VALUES (?)", [(k,) for k in missing])
            placeholders = ",".join("?" for _ in missing)
            async with db.execute(f"SELECT key, id FROM targets WHERE key IN ({placeholders})", missing) as cursor:
                ids.update(dict(await cursor.fetchall()))
        return ids

    def _remember_targets(self, ids: Dict[str, int]):
        for key, target_id in ids.items():
            self._target_ids[key] = target_id
            self._target_keys[target_id] = key

    async def get_meta(self, key: str) -> Optional[str]:
        async with self._db.read() as db:
            async with db.execute("SELECT value FROM meta WHERE key = ?", (key,)) as cursor:
                row = await cursor.fetchone()
                return row[0] if row else None

    async def set_meta(self, key: str, value: str):
        async with self._db.write() as db:
            await db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))

    async def cleanup_old_samples(
        self, retention_days: Optional[int] = None, rollup_retention_days: Optional[int] = None,
        long_rollup_retention_days: Optional[int] = None,
    ) -> Dict[str, int]:
        """Prunes raw telemetry older than retention_days (probe samples, device and line samples), 10-second rollups
        older than slot_retention_days, per-minute rollups older than rollup_retention_days and quarter-hour rollups (and speed tests and
        device events) older than long_rollup_retention_days, then returns the freed pages to the file system.

        Incidents are kept unless database.incident_retention_days is set: they're the record of outages and are tiny
        (hundreds of rows). Then resolved ones older than that go, open ones never."""
        if retention_days is None:
            retention_days = self.retention_days
        if rollup_retention_days is None:
            rollup_retention_days = self.rollup_retention_days
        if long_rollup_retention_days is None:
            long_rollup_retention_days = self.long_rollup_retention_days
        with tracer.start_as_current_span("db.retention") as span:
            removed = await self._prune(retention_days, rollup_retention_days, long_rollup_retention_days)
            span.set_attributes({f"db.removed.{table}": n for table, n in removed.items()})
        return removed

    async def _delete_old(self, table: str, where: str, params: tuple, keys: str) -> int:
        """Deletes the rows of `table` matching `where` (the table's own name and SQL; `keys`: the columns that identify
        a row) in batches of PRUNE_BATCH_ROWS, each its own write transaction. Returns the rows deleted. After a long stop
        or a lowered retention the backlog is millions of rows: one transaction would hold the write lock for all of
        it, and the probe loop's sample flush (and every other writer) would wait. The lock goes back to the queue
        between batches, so a waiting writer goes first."""
        total = 0
        while True:
            async with self._db.write() as db:
                cursor = await db.execute(
                    f"DELETE FROM {table} WHERE ({keys}) IN (SELECT {keys} FROM {table} WHERE {where} LIMIT ?)",
                    (*params, PRUNE_BATCH_ROWS))
                n = cursor.rowcount
            total += n
            if n < PRUNE_BATCH_ROWS:
                return total
            await asyncio.sleep(0)

    @staticmethod
    async def _free_pages(db: aiosqlite.Connection) -> int:
        async with db.execute("PRAGMA freelist_count") as cursor:
            return (await cursor.fetchone())[0]

    async def _prune(self, retention_days: int, rollup_retention_days: int,
                     long_rollup_retention_days: int) -> Dict[str, int]:
        await self.flush_samples()  # so buffered samples are pruned by the same rule
        started = time.monotonic()
        now = time.time()
        cutoff_ts = now - (retention_days * DAY)
        slot_cutoff = now - max(self.slot_retention_days, retention_days) * DAY
        removed: Dict[str, int] = {}
        delete = self._delete_old
        removed["samples"] = await delete("samples", "ts < ?", (cutoff_ts,), "ts, target")
        removed["rollup_10s"] = await delete("rollup_10s", "slot < ?", (int(slot_cutoff // 10),), "slot, target")
        removed["rollup_1m"] = await delete(
            "rollup_1m", "minute < ?", (int((now - rollup_retention_days * DAY) // 60),), "minute, target")
        removed["rollup_15m"] = await delete(
            "rollup_15m", "quarter < ?", (int((now - long_rollup_retention_days * DAY) // 900),), "quarter, target")
        # the speed tests are a few rows a day, kept as long as the longest rollups, and never less than a
        # calendar month and a day: the monthly data cap sums this month's tests
        removed["speed_tests"] = await delete(
            "speed_tests", "ts < ?", (now - max(long_rollup_retention_days, SPEED_TEST_MIN_DAYS) * DAY,), "rowid")
        # every device at once
        removed["device_samples"] = await delete("device_samples", "ts < ?", (cutoff_ts,), "device_id, ts")
        removed["line_samples"] = await delete("line_samples", "ts < ?", (cutoff_ts,), "rowid")
        removed["mesh_samples"] = await delete("mesh_samples", "ts < ?", (cutoff_ts,), "device_id, ts, node_id")
        # the router's traffic: database.traffic_retention_days (the latest app windows go with their age too)
        traffic_cutoff = now - self.traffic_retention_days * DAY
        removed["traffic_hourly"] = await delete(
            "traffic_hourly", "hour < ?", (int(traffic_cutoff),), "device_id, hour, client")
        removed["traffic_apps"] = await delete("traffic_apps", "window_end < ?", (int(traffic_cutoff),), "rowid")
        # device events (restarts, WAN link changes) are a few rows a month and explain old incidents: kept as long
        # as the quarter-hour rollups
        removed["device_events"] = await delete(
            "device_events", "ts < ?", (now - long_rollup_retention_days * DAY,), "rowid")
        if self.incident_retention_days > 0:
            # resolved incidents only: an open one is the outage going on, whatever its age
            removed["incidents"] = await delete(
                "incidents", "resolved = 1 AND COALESCE(end_time, start_time) < ?",
                (now - self.incident_retention_days * DAY,), "rowid")
            if removed["incidents"]:
                self._incidents_version += 1  # the incident-list caches and the dashboards refetch
        async with self._db.write() as db:
            freed = await self._free_pages(db)
        # Each step of the pragma frees pages, so it has to be stepped to the end: in bounded steps, each its own
        # write transaction, so the probe loop's flush gets in between.
        while True:
            async with self._db.write() as db:
                before = await self._free_pages(db)
                async with db.execute(f"PRAGMA incremental_vacuum({PRUNE_VACUUM_PAGES})") as cursor:
                    await cursor.fetchall()
                after = await self._free_pages(db)
            if after <= 0 or after >= before:
                break
            await asyncio.sleep(0)
        freed -= after
        for table, n in removed.items():
            if n:
                db_retention_rows.add(n, {"table": table})
        if freed > 0:
            db_vacuum_pages.add(freed)
        db_retention_duration.record(time.monotonic() - started)
        logger.info(
            f"Retention ({retention_days} days raw, {rollup_retention_days} days per-minute, "
            f"{long_rollup_retention_days} days quarter-hour): removed "
            + ", ".join(f"{n:,} {t}" for t, n in removed.items() if n)
            + f" in {(time.monotonic() - started) * 1000:.0f} ms"
            if any(removed.values()) else f"Retention ({retention_days} days): nothing to remove"
        )
        return removed
