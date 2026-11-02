"""The probing component: the probe path (engine/path.py: stored in the database, imported once from config.yaml and
the custom targets), the prober with every target and the probe loop (engine/loop.py), which it starts once every
component has registered. It owns the app's ICMP backend (engine/icmp_select.shared_backend), which traceroute and
the incident path trace use too."""

from pengumon.components import Component, Context
from pengumon.engine.icmp_base import IcmpUnavailable
from pengumon.engine.icmp_select import close_shared_backend
from pengumon.engine.loop import check_live_parts, probe_background_worker
from pengumon.engine.path import PathManager
from pengumon.engine.prober import NetworkProber


async def register(ctx: Context) -> Component:
    config, repository = ctx.config, ctx.state.repository

    path = PathManager(config, repository)
    await path.load()  # config.targets is the stored path from here on

    prober = NetworkProber(
        ping_timeout=config.prober.timeout_seconds,
        history_window=60,
        status_thresholds=config.prober.hop_status,
    )
    for t in config.targets:
        prober.register_target(t)
    for d in config.dns_probes:
        prober.register_dns_target(d)
    path.prober = prober
    ctx.state.prober = prober
    ctx.state.path = path

    async def start() -> None:
        try:
            prober.backend()  # opened (and its name and resolution logged, or why not) at startup, not mid-round
        except IcmpUnavailable:
            pass  # logged by icmp_select; every probe records the reason, and opening is retried
        parts = ctx.live_parts()
        check_live_parts(parts)
        ctx.spawn(probe_background_worker(config, prober, ctx.state.classifier, repository, live_parts=parts))

    async def stop() -> None:
        close_shared_backend()

    return Component("probing", start=start, stop=stop)
