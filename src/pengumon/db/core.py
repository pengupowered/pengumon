"""Connections, schema migration, the target-id map, key/value state and retention: what every other part of
the repository builds on. Holds the state the other mixins share (`_db`, the target maps, the incident cache)."""

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


class RepositoryCore:
    def __init__(
        self,
        db_path: Optional[str] = None,
        sample_flush_seconds: float = 10.0,
        retention_days: int = 7,
        rollup_retention_days: int = 400,
    ):
        self.db_path = paths.db_path(db_path)
        self._ensure_dir()
        self.retention_days = retention_days
        self.rollup_retention_days = rollup_retention_days
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
        # Two connections for the process lifetime: write() for everything that changes data (one transaction at
        # a time) and read() for queries (read-only, whole results). synchronous=NORMAL is crash-safe in WAL
        # (a power cut can lose the last moments of telemetry, never corrupt the file) and avoids an fsync per commit.
        self._db = Database(self.db_path, pragmas=("synchronous=NORMAL",), wal=True)
        self._migrated = False
        # Bumped whenever an incident is created, resolved or annotated; keys the incident-list cache.
        self._incidents_version = 0
        self._incidents_cache: Dict[int, tuple] = {}

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
        self, retention_days: Optional[int] = None, rollup_retention_days: Optional[int] = None
    ) -> Dict[str, int]:
        """Prunes raw telemetry older than retention_days (probe samples, device and line samples) and per-minute
        rollups older than rollup_retention_days, then returns the freed pages to the file system.

        Incidents are kept: they're the record of outages and are tiny (hundreds of rows)."""
        if retention_days is None:
            retention_days = self.retention_days
        if rollup_retention_days is None:
            rollup_retention_days = self.rollup_retention_days
        with tracer.start_as_current_span("db.retention") as span:
            removed = await self._prune(retention_days, rollup_retention_days)
            span.set_attributes({f"db.removed.{table}": n for table, n in removed.items()})
        return removed

    @staticmethod
    async def _free_pages(db: aiosqlite.Connection) -> int:
        async with db.execute("PRAGMA freelist_count") as cursor:
            return (await cursor.fetchone())[0]

    async def _prune(self, retention_days: int, rollup_retention_days: int) -> Dict[str, int]:
        await self.flush_samples()  # so buffered samples are pruned by the same rule
        started = time.monotonic()
        now = time.time()
        cutoff_ts = now - (retention_days * DAY)
        removed: Dict[str, int] = {}
        async with self._db.write() as db:
            cursor = await db.execute("DELETE FROM samples WHERE ts < ?", (cutoff_ts,))
            removed["samples"] = cursor.rowcount
            cursor = await db.execute(
                "DELETE FROM rollup_1m WHERE minute < ?", (int((now - rollup_retention_days * DAY) // 60),)
            )
            removed["rollup_1m"] = cursor.rowcount
            for table in ("device_samples", "line_samples", "mesh_samples"):
                # Every device at once: a scan of the table (a few ms for a week of 10 s polls), once an hour.
                cursor = await db.execute(f"DELETE FROM {table} WHERE ts < ?", (cutoff_ts,))
                removed[table] = cursor.rowcount
            await db.commit()
            freed = await self._free_pages(db)
            # Each step of the pragma frees pages, so it has to be stepped to the end.
            async with db.execute("PRAGMA incremental_vacuum") as cursor:
                await cursor.fetchall()
            freed -= await self._free_pages(db)
        for table, n in removed.items():
            if n:
                db_retention_rows.add(n, {"table": table})
        if freed > 0:
            db_vacuum_pages.add(freed)
        db_retention_duration.record(time.monotonic() - started)
        logger.info(
            f"Retention ({retention_days} days raw, {rollup_retention_days} days rollups): removed "
            + ", ".join(f"{n:,} {t}" for t, n in removed.items() if n)
            + f" in {(time.monotonic() - started) * 1000:.0f} ms"
            if any(removed.values()) else f"Retention ({retention_days} days): nothing to remove"
        )
        return removed
