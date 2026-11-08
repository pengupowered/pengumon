"""The speedtest component: the scheduler that runs a speed test when one is due (speedtest/runner.py), and the mask that
keeps the classifier from calling the test's own delay an outage.

Always registers, whatever the setting: the scheduler task reads `config.speedtest` at every tick, so turning the test on
in Settings needs no restart. Off, it does nothing. A failure here is contained (OPTIONAL): it costs the speed test, never
the start. A manual test still running at shutdown is canceled with the other tasks and records itself as interrupted.
"""

from pengumon.components import Component, Context
from pengumon.speedtest.runner import SpeedTestRunner

OPTIONAL = True


async def register(ctx: Context) -> Component:
    devices = ctx.state.devices  # the registry (rebuilt in place): its router's WAN counters, sampled during a test
    runner = SpeedTestRunner(ctx.config, ctx.state.repository, line_load=ctx.line_load, spawn=ctx.spawn,
                             notify_slow=ctx.notifier.notify_speed_slow if ctx.notifier is not None else None,
                             wan_reader=devices.read_wan_counters if devices is not None else None)
    ctx.state.speedtest = runner
    if ctx.state.classifier is not None:
        ctx.state.classifier.speedtest_active = runner.is_masking

    async def start() -> None:
        await runner.load_last()
        ctx.spawn(runner.run())

    async def stop() -> None:
        await runner.aclose()

    return Component("speedtest", start=start, stop=stop)
