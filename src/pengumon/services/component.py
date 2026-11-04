"""The services component: the health checks of the apps on this host, and the pattern for an optional feature.

`router` (GET /api/services) is always mounted; without an enabled service it answers []. `register` always builds
the ServiceChecker, wired to the notifier, and polls it in its own task: with no enabled service a poll does nothing,
and a service added on the Settings page is checked at the next poll. The interval is read every loop, and the
settings hub tells the checker when the list changed (`set_services`). A failure here is contained
(OPTIONAL): it costs the service checks, never the start. At shutdown the checker's open incidents are resolved and
its HTTP clients closed. Its gauges (pengumon_service_up, pengumon_service_check_seconds) are rows of
observability/gauges.py, which read state.service_checker.
"""

import asyncio
import time
from typing import Callable, Optional, Union

from pengumon.components import Component, Context
from pengumon.observability import get_logger
from pengumon.services.api import router
from pengumon.services.checker import ServiceChecker

__all__ = ["OPTIONAL", "collector_background_worker", "register", "router"]

OPTIONAL = True
MAX_SLEEP_SECONDS = 2.0  # longest single sleep between polls, so a changed interval is noticed within it
_log = get_logger("pengumon")  # the app's lifecycle lines keep the logger name they have always had


async def collector_background_worker(name: str, collector, interval: Union[float, Callable[[], float]]):
    """Polls a slow, HTTP-based collector (the service health checks) on its own schedule. Devices (modem,
    router) have their own loop, Device.run. `interval` may be a function, read every loop, so a changed setting
    applies at the next one.
    """
    every = interval if callable(interval) else (lambda: interval)
    _log.info(f"Starting {name} collector (interval {every():g}s)...")
    while True:
        started = time.monotonic()
        try:
            await collector.poll(force=True)
        except asyncio.CancelledError:
            break
        except Exception as e:
            _log.error(f"{name} collector error: {e}", exc_info=True)
        # Re-read the interval while waiting, so a shortened interval does not wait out the old one.
        while True:
            left = every() - (time.monotonic() - started)
            await asyncio.sleep(max(0.1, min(left, MAX_SLEEP_SECONDS)))
            if every() - (time.monotonic() - started) <= 0:
                break


async def register(ctx: Context) -> Optional[Component]:
    settings, notifier = ctx.config.service_checks, ctx.notifier
    checker = ServiceChecker(
        settings, ctx.state.repository,
        on_service_down=notifier.notify_service_down if notifier else None,
        on_service_up=notifier.notify_service_up if notifier else None,
    )
    ctx.state.service_checker = checker

    async def start() -> None:
        ctx.spawn(collector_background_worker("service health", checker, lambda: settings.interval_seconds))

    async def stop() -> None:
        try:
            await checker.resolve_open()
        except Exception as e:
            _log.error(f"Error resolving service incidents on shutdown: {e}")
        await checker.aclose()

    hub = getattr(ctx, "settings", None)  # the settings hub (W1)
    if hub is not None:
        @hub.on(prefixes=["service_checks."], lists=["services"], name="services.checks")
        async def _services(change) -> None:
            for service_id in await checker.set_services():
                if notifier is not None:
                    notifier.forget_service(service_id)

    return Component("services", start=start, stop=stop)
