"""The live stream's route: the /ws websocket. The broadcaster that feeds it is live/ws.py's ConnectionManager, started
by the live component (live/component.py)."""

from fastapi import APIRouter, WebSocket

from pengumon.live.ws import ws_manager

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
