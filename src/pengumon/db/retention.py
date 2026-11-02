"""The retention component: removes what is older than the database section's retention_days (raw samples) and
rollup_retention_days (per-minute rollups), once at start and then every hour."""

import asyncio

from pengumon.components import Component, Context
from pengumon.observability import get_logger

_log = get_logger("pengumon")  # the app's lifecycle lines keep the logger name they have always had

INTERVAL_SECONDS = 3600


async def cleanup_background_worker(config, repository) -> None:
    """Periodically purges metrics older than retention_days."""
    _log.info(
        f"Retention policy active: raw probe data {config.database.retention_days} days, "
        f"per-minute rollups {config.database.rollup_retention_days} days."
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
