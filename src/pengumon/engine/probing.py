"""The probing component: the probe path (engine/path.py: stored in the database, imported once from config.yaml and
the custom targets), the prober with every target and the probe loop (engine/loop.py), which it starts once every
component has registered. It owns the app's ICMP backend (engine/icmp_select.shared_backend), which traceroute and
the incident path trace use too.

Saved settings it applies without a restart (`subscribe`): the probe timeouts and the DNS lookups list (the prober
keeps copies). Everything else the probe round reads (the hop colours, the outage rules, the busy level) is the live
config object itself, which the settings manager updates in place before the appliers run; so are the line and router
levels the device monitors grade by (devices/monitors.py watch_levels, wired by the devices component)."""

from pengumon.components import Component, Context
from pengumon.engine.icmp_base import IcmpUnavailable
from pengumon.engine.icmp_select import close_shared_backend
from pengumon.engine.loop import check_live_parts, probe_background_worker
from pengumon.engine.path import PathManager
from pengumon.engine.prober import NetworkProber


def subscribe(hub, config, prober: NetworkProber) -> None:
    """The appliers of the settings this component owns. Each reads the live config, which the settings manager has
    already updated in place when it publishes the change."""

    @hub.on(keys=["prober.timeout_seconds", "prober.dns_timeout_seconds"], name="probing.timeouts")
    def _timeouts(change) -> None:
        prober.ping_timeout = config.prober.timeout_seconds
        prober.dns_timeout = config.prober.dns_timeout_seconds

    @hub.on(lists=["dns_probes"], name="probing.dns_probes")
    def _dns_probes(change) -> None:
        prober.set_dns_targets(list(config.dns_probes))


async def register(ctx: Context) -> Component:
    config, repository = ctx.config, ctx.state.repository

    path = PathManager(config, repository)
    await path.load()  # config.targets is the stored path from here on

    prober = NetworkProber(
        ping_timeout=config.prober.timeout_seconds,
        history_window=60,
        status_thresholds=config.prober.hop_status,
        dns_timeout=config.prober.dns_timeout_seconds,
    )
    for t in config.targets:
        prober.register_target(t)
    for d in config.dns_probes:
        prober.register_dns_target(d)
    path.prober = prober
    ctx.state.prober = prober
    ctx.state.path = path
    hub = getattr(ctx, "settings", None)  # the settings hub (Context.settings); absent before the settings component
    if hub is not None:
        subscribe(hub, config, prober)

    async def start() -> None:
        try:
            prober.backend()  # opened (and its name and resolution logged, or why not) at startup, not mid-round
        except IcmpUnavailable:
            pass  # logged by icmp_select; every probe records the reason, and opening is retried
        parts = ctx.live_parts()
        check_live_parts(parts)
        ctx.spawn(probe_background_worker(config, prober, ctx.state.classifier, repository, live_parts=parts))

    async def stop() -> None:
        await path.previews.close()  # draft previews probe through the shared backend: stopped before it closes
        close_shared_backend()

    return Component("probing", start=start, stop=stop)
