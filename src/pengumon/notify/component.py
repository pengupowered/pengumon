"""The notify component: the notifier is always built, from the environment, the Settings secrets and the config, and
left on the context for the classifier and the service checks (with no channel it announces nothing). A saved change
to the channels or the rules reaches it through `Notifier.reconfigure`, with no restart."""

from typing import Optional

from pengumon.components import Component, Context
from pengumon.notify.channels import build_channels
from pengumon.notify.notifier import Notifier
from pengumon.notify.policy import AlertPolicy
from pengumon.observability import get_logger


def _describe(notifier: Notifier) -> str:
    return ", ".join(c["name"] if c["kind"] == "ntfy" else f"webhook {c['name']} ({c['format']})"
                     for c in notifier.channels()) or "no channel"


async def register(ctx: Context) -> Optional[Component]:
    config, store = ctx.config, ctx.state.secret_store

    def policy() -> AlertPolicy:
        return AlertPolicy(config.alerts, config.general.timezone, config.incidents)

    def secret_value():
        manager = getattr(ctx.state, "settings", None)  # W1's SettingsManager; None where the store is not running (tests)
        return manager.secret_value if manager is not None else None

    notifier = Notifier.build(config.notifications, store, policy=policy(), secret_value=secret_value())
    channels = notifier.channels()
    log = get_logger("pengumon")  # the app's lifecycle logger name
    log.info(f"Incident notifications enabled: {_describe(notifier)}." if channels
             else "Incident notifications: no channel configured yet.")
    ctx.notifier = ctx.state.notifier = notifier

    hub = getattr(ctx, "settings", None)  # the settings hub (W1); without it the notifier is set at start only
    if hub is not None:
        @hub.on(keys=["notifications.public_url", "general.timezone"], prefixes=["alerts.", "incidents."], lists=["webhooks"],
                slots=["ntfy.", "webhook.", "webhook_header."], name="notify.reconfigure")
        def _reconfigure(change) -> None:
            built = build_channels(config.notifications, store, secret_value=secret_value())
            notifier.reconfigure(built.url, built.token, built.senders, config.notifications.public_url, policy())
            log.info(f"Notifications reconfigured: {_describe(notifier)}.")

    return Component("notify", stop=notifier.aclose)
