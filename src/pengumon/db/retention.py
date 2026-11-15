"""The retention component: removes what is older than the database section's retention settings (raw samples,
10-second and per-minute rollups, then one number per kind of data: db/core.py cleanup_old_samples), once at start and
then every hour. It starts after the storage component, whose migration builds the quarter-hour rollups from the
per-minute ones before this first cleanup can delete any of them."""

import asyncio

from pengumon.components import Component, Context
from pengumon.db.core import KIND_SETTINGS
from pengumon.observability import get_logger

_log = get_logger("pengumon")  # the app's lifecycle lines keep the logger name they have always had

INTERVAL_SECONDS = 3600


async def cleanup_background_worker(config, repository) -> None:
    """Periodically purges metrics older than retention_days."""
    database = config.database
    kinds = ", ".join(f"{name.removesuffix('_retention_days')} {getattr(database, name) or 'forever'}"
                      for name in KIND_SETTINGS)
    _log.info(f"Retention policy active: full detail {database.retention_days} / {database.slot_retention_days} / "
              f"{database.rollup_retention_days} days; kept for, in days: {kinds}.")
    while True:
        try:
            await asyncio.sleep(INTERVAL_SECONDS)
            await repository.cleanup_old_samples()
        except asyncio.CancelledError:
            break
        except Exception as e:
            _log.error(f"Error during retention cleanup: {e}")


async def register(ctx: Context) -> Component:
    repository = ctx.state.repository
    await repository.cleanup_old_samples()  # before the load backfills read the stored history

    async def start() -> None:
        ctx.spawn(cleanup_background_worker(ctx.config, repository))

    return Component("retention", start=start)
