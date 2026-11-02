"""The storage component: the SQLite repository. It is built before any component (repository_for, called by the
app's lifespan; building it opens nothing), so the devices can be handed it, and opened here, after the devices: a
device config that cannot run stops the start before any migration. Its live part is the incidents version, which
tells the dashboards when to fetch the incident list again."""

from pengumon.components import Component, Context, LivePart
from pengumon.db.repository import DatabaseRepository
from pengumon.observability import get_logger

_log = get_logger("pengumon")  # the app's lifecycle lines keep the logger name they have always had


def repository_for(config) -> DatabaseRepository:
    """The repository of `config.database` (an AppConfig), not opened yet."""
    return DatabaseRepository(
        db_path=config.database.db_path,
        retention_days=config.database.retention_days,
        rollup_retention_days=config.database.rollup_retention_days,
    )


def live_part(repository: DatabaseRepository) -> LivePart:
    return LivePart("incidents_version", lambda diagnosis: repository.incidents_version)


async def warn_unconfigured_history(repository: DatabaseRepository, devices) -> None:
    """One warning naming the device ids that have stored readings but are not in the config: their history is kept
    until retention removes it, but nothing shows it."""
    try:
        stored = await repository.get_stored_device_ids()
    except Exception as e:
        _log.error(f"Could not list the devices with stored readings: {e}")
        return
    orphans = [device_id for device_id in stored if devices.get(device_id) is None]
    if orphans:
        _log.warning(f"Stored readings of devices not in the config: {', '.join(orphans)}; their history is not "
                     "shown (retention removes it)")


async def register(ctx: Context) -> Component:
    repository = ctx.state.repository
    await repository.initialize()
    await warn_unconfigured_history(repository, ctx.state.devices)
    return Component("storage", stop=repository.close, live_part=live_part(repository))
