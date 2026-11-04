"""The line_load component: the line's capacity and the load on it (engine/load.py), and the wiring between the
devices and the probes that load awareness needs.

Live rates come from the enabled WAN device; stored ones (capacity learning, the backfill) from the device whose
history the views show, which may be switched off in config. A monitor that grades WAN rates against the capacity
offers `watch_load`; one that checks line errors against probe loss offers `watch_probes`; each gets what it asks for.
Learning the capacity and the one-time load annotation of incidents recorded before load awareness may fail: that
costs only the load awareness, never the start.
"""

from typing import Optional

from pengumon.capabilities import Capability
from pengumon.components import Component, Context, LivePart
from pengumon.engine.load import LineLoad, backfill_incident_load, backfill_ping_deprioritisation
from pengumon.observability import get_logger

_log = get_logger("pengumon")  # the app's lifecycle lines keep the logger name they have always had


def line_status(load, load_pct: Optional[float]) -> dict:
    """The live line part of the message: the load now (the diagnosis's) and the capacity it is a share of."""
    return {"load_pct": load_pct, "down_mbps": getattr(load, "down_mbps", None), "up_mbps": getattr(load, "up_mbps", None)}


def live_part(load) -> LivePart:
    return LivePart("line", lambda diagnosis: line_status(load, diagnosis.load_pct))


async def register(ctx: Context) -> Component:
    config, repository, devices = ctx.config, ctx.state.repository, ctx.state.devices
    wan_history = devices.primary(Capability.WAN_COUNTERS)
    line_load = LineLoad(config.line, rates_source=devices.wan_rates_source(), repository=repository,
                         device_id=wan_history.id if wan_history else None)
    async def probes_lost_packets(start: float, end: float) -> Optional[bool]:
        """Whether the probes to the gateway and the ISP hops lost any packet in the window; None without a reading.
        The roles are the path's as it is now (config.targets, which a path edit updates)."""
        probe_categories = {t.id: t.category for t in config.targets}
        stats = [r for r in await repository.get_sample_stats(start, end)
                 if probe_categories.get(r["target_id"]) in ("gateway", "isp")]
        return any(r["sent"] > r["received"] for r in stats) if stats else None

    def rewire() -> None:
        """Points the line load at the devices as they are now (also after the devices were rebuilt): the live rates
        source, the device whose stored history it learns from, and the load and probe-loss hooks of every monitor
        (a rebuilt device has a new monitor)."""
        history = devices.primary(Capability.WAN_COUNTERS)
        line_load.rates_source = devices.wan_rates_source()
        line_load.device_id = history.id if history else None
        for device in devices:
            if hasattr(device.monitor, "watch_probes"):
                device.monitor.watch_probes(probes_lost_packets)
            if hasattr(device.monitor, "watch_load"):
                device.monitor.watch_load(line_load, lambda: config.classifier.busy_load_pct)

    rewire()
    hub = getattr(ctx, "settings", None)
    if hub is not None:
        @hub.on(events=["devices_rebuilt"], name="line_load.devices")
        async def _devices_rebuilt(change) -> None:
            rewire()
            await line_load.refresh_capacity(force=True)
    try:
        await line_load.refresh_capacity(force=True)
        await backfill_incident_load(repository, line_load, config.classifier.busy_load_pct)
    except Exception as e:
        _log.error(f"Line load setup failed: {e}", exc_info=True)
    try:
        await backfill_ping_deprioritisation(repository, config.targets, config.classifier.degraded_loss_pct)
    except Exception as e:
        _log.error(f"Ping deprioritisation backfill failed: {e}", exc_info=True)
    ctx.line_load = line_load

    async def start() -> None:
        ctx.spawn(line_load.run())

    return Component("line_load", start=start, live_part=live_part(line_load))
