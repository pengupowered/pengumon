"""The probing component: the prober with every target (config.yaml's, then those added from the dashboard in
earlier runs) and the probe loop (engine/loop.py), which it starts once every component has registered."""

from pengumon.components import Component, Context
from pengumon.engine.loop import check_live_parts, probe_background_worker
from pengumon.engine.prober import NetworkProber


async def register(ctx: Context) -> Component:
    config, repository = ctx.config, ctx.state.repository

    # Targets added from the dashboard in earlier runs. A config.yaml target with the same id wins.
    configured_ids = {t.id for t in config.targets}
    for custom in await repository.list_custom_targets():
        if custom.id not in configured_ids:
            config.targets.append(custom)

    prober = NetworkProber(
        ping_timeout=config.prober.timeout_seconds,
        history_window=60,
        status_thresholds=config.prober.hop_status,
    )
    for t in config.targets:
        prober.register_target(t)
    for d in config.dns_probes:
        prober.register_dns_target(d)
    ctx.state.prober = prober

    async def start() -> None:
        parts = ctx.live_parts()
        check_live_parts(parts)
        ctx.spawn(probe_background_worker(config, prober, ctx.state.classifier, repository, live_parts=parts))

    return Component("probing", start=start)
