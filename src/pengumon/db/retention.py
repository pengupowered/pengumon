"""The retention component: removes what is older than the database section's retention_days (raw samples),
slot_retention_days (10-second rollups), rollup_retention_days (per-minute rollups) and long_rollup_retention_days (quarter-hour rollups and
speed tests), once at start and then every hour. It starts after the storage component, whose migration builds the
quarter-hour rollups from the per-minute ones before this first cleanup can delete any of them."""

import asyncio

from pengumon.components import Component, Context
from pengumon.observability import get_logger

_log = get_logger("pengumon")  # the app's lifecycle lines keep the logger name they have always had

INTERVAL_SECONDS = 3600


async def cleanup_background_worker(config, repository) -> None:
    """Periodically purges metrics older than retention_days."""
    _log.info(
        f"Retention policy active: raw probe data {config.database.retention_days} days, "
        f"per-minute rollups {config.database.rollup_retention_days} days, "
        f"quarter-hour rollups {config.database.long_rollup_retention_days} days."
    )
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
