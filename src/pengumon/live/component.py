"""The live stream to the dashboards: the /ws websocket and the task that sends what the probe loop publishes
(api/ws.py's ConnectionManager). The message's fields come from the components' live parts (engine/loop.py)."""

from fastapi import APIRouter, WebSocket

from pengumon.api.ws import ws_manager
from pengumon.components import Component, Context

router = APIRouter()


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint for real-time live telemetry."""
    await ws_manager.connect(websocket)
    try:
        while True:
            # Keep-alive receive loop
            await websocket.receive_text()
    except Exception:
        pass
    finally:
        ws_manager.disconnect(websocket)


async def register(ctx: Context) -> Component:
    async def start() -> None:
        ctx.spawn(ws_manager.run_broadcaster())

    return Component("live", start=start)
