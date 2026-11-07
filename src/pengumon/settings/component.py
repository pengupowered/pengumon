"""The settings component: the secrets file, the settings store (settings/manager.py) and the hub components
subscribe to for live changes (settings/hub.py).

It runs right after the storage (it needs the database) and before every feature: its `load` makes the live config
(`state.config`) the store's (the one-time import of config.yaml at the first start, then the stored document with
the environment overrides on top), so the devices, the prober and the rest are built from it. Its live part is the
settings revision, which tells open pages that the settings changed.

The appliers here are the ones whose running parts belong to no feature component: the repository's retention days,
the log levels, the Grafana links and the sign-in (pengumon.access: the password from secrets.json and the viewers
setting go into the app's SecurityConfig here, before the app answers its first request).
"""

from pengumon import paths
from pengumon.access.manager import AccessManager
from pengumon.components import Component, Context, LivePart
from pengumon.observability import get_logger, grafana
from pengumon.observability.logs import apply_levels
from pengumon.secrets import SecretStore
from pengumon.settings.hub import SettingsHub
from pengumon.settings.manager import SettingsManager

_log = get_logger("pengumon.settings")


def live_part(manager: SettingsManager) -> LivePart:
    return LivePart("settings_revision", lambda diagnosis: manager.revision)


def _apply_retention(config, repository) -> None:
    repository.retention_days = config.database.retention_days
    repository.rollup_retention_days = config.database.rollup_retention_days


def subscribe(hub: SettingsHub, manager: SettingsManager, access: AccessManager) -> None:
    config, repository = manager.config, manager.repository

    @hub.on(["access.allow_viewers"], name="settings.access")
    async def _viewers(change):
        await access.apply_viewers(config.access.allow_viewers)

    @hub.on(["database.retention_days", "database.rollup_retention_days"], name="settings.retention")
    def _retention(change):
        _apply_retention(config, repository)
        _log.info(f"Retention policy now: raw probe data {config.database.retention_days} days, per-minute rollups "
                  f"{config.database.rollup_retention_days} days (applied at the next hourly cleanup)")

    @hub.on(["server.log_level", "server.log_levels"], name="settings.logging")
    def _logging(change):
        apply_levels(config.server.log_level, config.server.log_levels)

    @hub.on(["display.grafana_url"], name="settings.grafana")
    def _grafana(change):
        grafana.configure_url(config.display.grafana_url)


async def register(ctx: Context) -> Component:
    security = getattr(ctx.app.state, "security_config", None)
    secret_store = SecretStore()  # data/secrets.json, read once and shared by everything that reads or writes it
    hub = SettingsHub()
    manager = SettingsManager(ctx.config, ctx.state.repository, secret_store, hub, config_path=paths.config_path(),
                              token_set=lambda: bool(getattr(security, "access_token", "")))
    await manager.load()
    _apply_retention(ctx.config, ctx.state.repository)  # before the retention component's start-up prune
    apply_levels(ctx.config.server.log_level, ctx.config.server.log_levels)
    grafana.configure_url(ctx.config.display.grafana_url)
    access = AccessManager(security, secret_store, ctx.state.repository)
    await access.load()
    await access.apply_viewers(ctx.config.access.allow_viewers)
    subscribe(hub, manager, access)
    ctx.state.secret_store, ctx.state.settings, ctx.settings = secret_store, manager, hub
    ctx.state.access = access
    return Component("settings", live_part=live_part(manager))
