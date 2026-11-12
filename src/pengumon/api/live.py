"""The live stream's route: the /ws websocket. The broadcaster that feeds it is live/ws.py's ConnectionManager, started
by the live component (live/component.py)."""

import json

from fastapi import APIRouter, WebSocket

from pengumon.live.ws import ws_manager
from pengumon.security import can_edit

router = APIRouter()

MAX_CLIENT_TEXT = 64  # the only thing a page sends is {"resync": true}; anything longer is not read


def wants_full(text: str) -> bool:
    """True for the one message a page may send: {"resync": true}, 'send me the whole state' (it saw a gap in the
    deltas). Anything else is ignored: what a client sends changes nothing but what that connection is sent."""
    if len(text) > MAX_CLIENT_TEXT:
        return False
    try:
        return json.loads(text) == {"resync": True}
    except ValueError:
        return False


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint for real-time live telemetry."""
    # ?delta=1 (the dashboard): recent_latencies in full only when asked for, otherwise the probes since the last message
    # a connection that may not edit (a viewer, the read token) is sent the failed-poll messages in fixed words
    if not await ws_manager.connect(websocket, delta=websocket.query_params.get("delta") == "1",
                                    editor=can_edit(websocket)):
        return  # over the connection cap: closed with 1013
    try:
        while True:
            # Keep-alive receive loop; the one thing read is a request for the whole state
            if wants_full(await websocket.receive_text()):
                ws_manager.request_full(websocket)
    except Exception:
        pass
    finally:
        ws_manager.disconnect(websocket)
