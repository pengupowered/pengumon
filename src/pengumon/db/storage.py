"""The storage component: the SQLite repository. It is built before any component (repository_for, called by the
app's lifespan; building it opens nothing) and opened here, first: the settings store lives in it, and every later
component reads its settings from there. Its live part is the incidents version, which tells the dashboards when to
fetch the incident list again.

It also empties the backup download's temp folder (api/backup.py), which a crash in the middle of a download leaves
full of a copy of the database."""

import asyncio
import os

from pengumon.components import Component, Context, LivePart
from pengumon.db.backup_temp import backup_temp_dir, remove_backup_leftovers
from pengumon.db.repository import DatabaseRepository
from pengumon.observability import get_logger

_log = get_logger(__name__)

def _clean_backup_temp() -> None:
    folder = backup_temp_dir()
    try:
        if os.path.lexists(folder):  # a link or a file is refused by the call, and logged
            remove_backup_leftovers(folder)
    except OSError as e:  # not worth stopping the start for: the next backup request tries again
        _log.warning("Could not empty the backup temp folder", folder=str(folder), error=type(e).__name__)


def repository_for(config) -> DatabaseRepository:
    """The repository of `config.database` (an AppConfig), not opened yet. The retention days are set again by the
    settings component once the stored settings are loaded."""
    return DatabaseRepository(
        db_path=config.database.db_path,
        retention_days=config.database.retention_days,
        rollup_retention_days=config.database.rollup_retention_days,
        long_rollup_retention_days=config.database.long_rollup_retention_days,
        traffic_retention_days=config.database.traffic_retention_days,
        slot_retention_days=config.database.slot_retention_days,
        incident_retention_days=config.database.incident_retention_days,
    )


def live_part(repository: DatabaseRepository) -> LivePart:
    return LivePart("incidents_version", lambda diagnosis: repository.incidents_version)


async def register(ctx: Context) -> Component:
    repository = ctx.state.repository
    await repository.initialize()
    await asyncio.to_thread(_clean_backup_temp)
    return Component("storage", stop=repository.close, live_part=live_part(repository))
