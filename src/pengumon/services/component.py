"""The services component: the health checks of the apps on this host, and the pattern for an optional feature.

`router` (GET /api/services) is always mounted; without an enabled service it answers []. `register` returns None
when service_checks lists no enabled service, so nothing is built, started or stopped for it. Otherwise it builds the
ServiceChecker, wired to the notifier when there is one, and polls it in its own task. A failure here is contained
(OPTIONAL): it costs the service checks, never the start. At shutdown the checker's open incidents are resolved and
its HTTP clients closed. Its gauges (pengumon_service_up, pengumon_service_check_seconds) are rows of
observability/gauges.py, which read state.service_checker.
"""

import asyncio
import time
from typing import Optional

from pengumon.components import Component, Context
from pengumon.observability import get_logger
from pengumon.services.api import router
from pengumon.services.checker import ServiceChecker

__all__ = ["OPTIONAL", "collector_background_worker", "register", "router"]

OPTIONAL = True
_log = get_logger("pengumon")  # the app's lifecycle lines keep the logger name they have always had


async def collector_background_worker(name: str, collector, interval: float):
    """Polls a slow, HTTP-based collector (the service health checks) on its own schedule. Devices (modem,
    router) have their own loop, Device.run.
    """
    _log.info(f"Starting {name} collector (interval {interval:g}s)...")
    while True:
        started = time.monotonic()
        try:
            await collector.poll(force=True)
        except asyncio.CancelledError:
            break
        except Exception as e:
            _log.error(f"{name} collector error: {e}", exc_info=True)
        await asyncio.sleep(max(0.1, interval - (time.monotonic() - started)))


async def register(ctx: Context) -> Optional[Component]:
    settings, notifier = ctx.config.service_checks, ctx.notifier
    if not any(s.enabled for s in settings.services):
        return None
    checker = ServiceChecker(
        settings, ctx.state.repository,
        on_service_down=notifier.notify_service_down if notifier else None,
        on_service_up=notifier.notify_service_up if notifier else None,
    )
    ctx.state.service_checker = checker

    async def start() -> None:
        ctx.spawn(collector_background_worker("service health", checker, settings.interval_seconds))

    async def stop() -> None:
        try:
            await checker.resolve_open()
        except Exception as e:
            _log.error(f"Error resolving service incidents on shutdown: {e}")
        await checker.aclose()

    return Component("services", start=start, stop=stop)
