"""The storage component: the SQLite repository. It is built before any component (repository_for, called by the
app's lifespan; building it opens nothing) and opened here, first: the settings store lives in it, and every later
component reads its settings from there. Its live part is the incidents version, which tells the dashboards when to
fetch the incident list again."""

from pengumon.components import Component, Context, LivePart
from pengumon.db.repository import DatabaseRepository


def repository_for(config) -> DatabaseRepository:
    """The repository of `config.database` (an AppConfig), not opened yet. The retention days are set again by the
    settings component once the stored settings are loaded."""
    return DatabaseRepository(
        db_path=config.database.db_path,
        retention_days=config.database.retention_days,
        rollup_retention_days=config.database.rollup_retention_days,
    )


def live_part(repository: DatabaseRepository) -> LivePart:
    return LivePart("incidents_version", lambda diagnosis: repository.incidents_version)


async def register(ctx: Context) -> Component:
    repository = ctx.state.repository
    await repository.initialize()
    return Component("storage", stop=repository.close, live_part=live_part(repository))
