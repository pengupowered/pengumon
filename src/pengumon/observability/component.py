"""The runtime component: starts the event-loop monitor (observability/runtime.py), whose lag histogram and task count
tell whether pengumon itself is keeping up."""

from pengumon.components import Component, Context
from pengumon.observability.runtime import loop_monitor


async def register(ctx: Context) -> Component:
    async def start() -> None:
        ctx.spawn(loop_monitor.run())

    return Component("runtime", start=start)
