"""The discovery component: the job store behind POST /api/discovery (api/discovery.py), and the background
route-change checks (drift.py).

The routes are always mounted. `register` makes the job store, whose runs diff their draft against the applied probe
path (engine/path.py) and the hops removed from it; the configured devices' hosts get one front-page request each. A
run the owner starts begins when the path editor asks for one. With `discovery.background` on (the default) it also
makes the DriftChecker: started with the app, it checks the route once a day, and the classifier's "incident ended"
callback asks it for a check soon after an incident (the notifier's callback is kept and runs first). The checker is
always made and reads those settings live, so switching background checks or the ASN lookup on the Settings page, or
changing the daily time or the time zone, applies at once (the settings hub calls its `wake`). At shutdown a run still
going is stopped. Optional: a failure here costs discovery, never the start.
"""

import asyncio
import socket
from typing import FrozenSet, List, Tuple
from urllib.parse import urlsplit

from pengumon.components import Component, Context
from pengumon.engine.discovery.addresses import parse
from pengumon.engine.discovery.drift import DriftChecker
from pengumon.engine.discovery.engine import Deps, live_path_from_targets, run_discovery
from pengumon.engine.discovery.jobs import DiscoveryJobs
from pengumon.engine.path import path_manager

__all__ = ["OPTIONAL", "register"]

OPTIONAL = True


async def applied_paths(state) -> Tuple[List[dict], List[dict]]:
    """The path a rediscover is compared with: the applied one (engine/path.py, the database's), and the hops removed
    from it whose history is kept. A removed hop is not proposed again unless discovery finds its address; then it
    comes back with its id (and history) as a new hop."""
    manager = path_manager(state)
    if manager is None:
        return live_path_from_targets(state.config.targets if state.config else []), []
    _, hops = await manager.current()
    return live_path_from_targets(hops), live_path_from_targets(await manager.repository.removed_path_hops())


async def drift_paths(state) -> Tuple[int, List[dict], List[dict]]:
    """What a route-change check compares with: the revision, the applied path and the hops removed from it."""
    manager = path_manager(state)
    if manager is None:
        return 0, [], []
    revision, hops = await manager.current()
    return revision, live_path_from_targets(hops), live_path_from_targets(await manager.repository.removed_path_hops())


def chain_incident_ended(classifier, after) -> None:
    """Calls `after(incident)` when the classifier resolves an incident, after the callback it already has."""
    before = classifier.on_incident_resolved

    def resolved(incident) -> None:
        try:
            if before is not None:
                before(incident)
        finally:
            after(incident)

    classifier.on_incident_resolved = resolved


def device_hosts(config) -> FrozenSet[str]:
    """The hosts of the configured devices: pengumon reads them with their own driver, so discovery asks each for its
    front page once and nothing more (Deps.limited_hosts)."""
    hosts = set()
    for device in getattr(config, "devices", None) or []:
        host = urlsplit(device.url if "//" in device.url else f"http://{device.url}").hostname
        if host:
            hosts.add(host)
    return frozenset(hosts)


RESOLVE_TIMEOUT_S = 2.0


async def limited_hosts(config) -> FrozenSet[str]:
    """The configured devices' hosts, and the addresses (IPv4 and IPv6) a host name resolves to, looked up once per
    run: discovery meets a device by the address a trace found, not by the name in config.yaml. A name that does not
    resolve stays in the set as it is."""
    hosts = device_hosts(config)
    loop = asyncio.get_running_loop()

    async def resolve(host: str) -> List[str]:
        if parse(host) is not None:
            return []
        try:
            infos = await asyncio.wait_for(loop.getaddrinfo(host, None, type=socket.SOCK_STREAM), RESOLVE_TIMEOUT_S)
        except (OSError, asyncio.TimeoutError):
            return []
        return [info[4][0].split("%", 1)[0] for info in infos]

    found = await asyncio.gather(*(resolve(h) for h in sorted(hosts)))
    return frozenset(hosts.union(*map(set, found)))


async def register(ctx: Context) -> Component:
    state = ctx.state

    async def run(options, progress):
        return await run_discovery(options, lambda: applied_paths(state), progress=progress,
                                   deps=Deps(limited_hosts=await limited_hosts(ctx.config)))

    jobs = DiscoveryJobs(run)
    state.discovery = jobs

    settings = getattr(ctx.config, "discovery", None)
    drift = None
    if settings is not None and state.repository is not None:
        general = getattr(ctx.config, "general", None)
        drift = DriftChecker(run=lambda options: run(options, lambda text: None), paths=lambda: drift_paths(state),
                             repository=state.repository, jobs=jobs,
                             notify=ctx.notifier.notify_path_changed if ctx.notifier is not None else None,
                             settings=settings, timezone=lambda: general.timezone if general is not None else "UTC")
        state.drift = drift
        if state.classifier is not None:
            chain_incident_ended(state.classifier, drift.incident_ended)
        hub = getattr(ctx, "settings", None)  # the settings hub (W1)
        if hub is not None:
            @hub.on(keys=["discovery.background", "discovery.asn_lookup", "discovery.daily_check_time",
                          "general.timezone"], name="discovery.drift")
            def _drift(change) -> None:
                drift.wake()

    async def start() -> None:
        if drift is not None:
            await drift.load()
            ctx.spawn(drift.run_daily())

    async def stop() -> None:
        if drift is not None:
            await drift.close()
        await jobs.close()

    return Component("discovery", start=start, stop=stop)
