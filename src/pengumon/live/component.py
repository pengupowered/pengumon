"""The live stream to the dashboards: the task that sends what the probe loop publishes (live/ws.py's
ConnectionManager) to the /ws websockets (api/live.py). The message's fields come from the components' live parts
(engine/loop.py)."""

from pengumon.components import Component, Context
from pengumon.live.ws import ws_manager


async def register(ctx: Context) -> Component:
    async def start() -> None:
        ctx.spawn(ws_manager.run_broadcaster())

    return Component("live", start=start)
