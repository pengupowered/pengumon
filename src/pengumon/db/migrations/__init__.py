"""Schema versioning with PRAGMA user_version.

Version 0 is a database without a version: either brand new, or created before versioning existed (it has
`probe_samples`). Both go through v1, whose statements are idempotent, so the old in-place `ALTER TABLE`
upgrades still reach very old databases. Each migration runs in one transaction together with the
user_version bump, so a crash part-way leaves the previous version intact and the next start re-runs it.

Each migration is a module here named `v<NNN>_<what>.py` with `VERSION` (the NNN) and `async def migrate(db)`; the
runner finds them with pkgutil, so a new migration is a new file and edits no list. Versions must be unique and
contiguous from 1 (tests/test_migration_modules.py). Two branches that both add the next number collide on the file
name or the version; rename one.
"""

import importlib
import logging
import pkgutil
import time

import aiosqlite

from pengumon.db.migrations.common import scalar
# Re-exported: the callers outside this package (repository core, secrets, tests) import these from here.
from pengumon.db.migrations.v006_incident_kind import backfill_incident_kinds, legacy_incident_kind
from pengumon.db.migrations.v009_device_tables import LEGACY_MODEM_ID, LEGACY_ROUTER_ID

logger = logging.getLogger("pengumon.db")

AUTO_VACUUM_INCREMENTAL = 2


def _discover() -> list:
    """(version, migrate) of every v<NNN>_*.py module in this package, oldest first."""
    found = []
    for info in pkgutil.iter_modules(__path__):
        if len(info.name) > 4 and info.name[0] == "v" and info.name[1:4].isdigit() and info.name[4] == "_":
            module = importlib.import_module(f"{__name__}.{info.name}")
            found.append((module.VERSION, module.migrate))
    return sorted(found, key=lambda m: m[0])


MIGRATIONS = _discover()
LATEST_VERSION = max(version for version, _ in MIGRATIONS)


async def migrate(db_path: str) -> int:
    """Brings the database at db_path to LATEST_VERSION. Must run before other connections are opened
    (VACUUM needs the database to itself). Returns the resulting version."""
    async with aiosqlite.connect(db_path, isolation_level=None) as db:
        await scalar(db, "PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA synchronous=NORMAL")
        version = await scalar(db, "PRAGMA user_version")
        if version > LATEST_VERSION:
            logger.warning(f"Database schema v{version} is newer than this build (v{LATEST_VERSION}); not migrating")
            return version

        migrated_from = version
        if not await scalar(db, "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'"):
            # A new database can switch auto_vacuum for free before its first table exists.
            await db.execute(f"PRAGMA auto_vacuum={AUTO_VACUUM_INCREMENTAL}")

        for target_version, step in MIGRATIONS:
            if version >= target_version:
                continue
            started = time.monotonic()
            await db.execute("BEGIN IMMEDIATE")
            try:
                await step(db)
                await db.execute(f"PRAGMA user_version={target_version}")
                await db.execute("COMMIT")
            except BaseException:
                if db.in_transaction:  # SQLite may already have rolled back (e.g. disk full)
                    await db.execute("ROLLBACK")
                raise
            version = target_version
            logger.info(f"Database schema migrated to v{version} ({time.monotonic() - started:.1f} s)")

        # Converting an existing database needs a VACUUM, which cannot run inside a transaction. Done as its
        # own idempotent step, so a crash between the v2 commit and here is repaired on the next start.
        if await scalar(db, "PRAGMA auto_vacuum") != AUTO_VACUUM_INCREMENTAL:
            started = time.monotonic()
            logger.info("Enabling incremental auto-vacuum (one-time VACUUM, rewrites the database file)")
            await db.execute(f"PRAGMA auto_vacuum={AUTO_VACUUM_INCREMENTAL}")
            await db.execute("VACUUM")
            await scalar(db, "PRAGMA wal_checkpoint(TRUNCATE)")
            logger.info(f"VACUUM finished ({time.monotonic() - started:.1f} s)")
        elif migrated_from < version:
            await _release_free_pages(db)
        return version


async def _release_free_pages(db: aiosqlite.Connection) -> None:
    """Returns the pages a migration freed (dropped tables) to the file system now, instead of at the next retention
    run. Only with incremental auto-vacuum, which every database has after its first start."""
    free = await scalar(db, "PRAGMA freelist_count")
    if not free:
        return
    page_size = await scalar(db, "PRAGMA page_size")
    async with db.execute("PRAGMA incremental_vacuum") as cursor:
        await cursor.fetchall()  # each step frees pages: run it to the end
    logger.info(f"Returned {free:,} free pages ({free * page_size / 1e6:.1f} MB) to the file system")
