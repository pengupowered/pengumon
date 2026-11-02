"""What the route modules share: the app-state dependency, the plain-JSON response and the device lookup."""

import json
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Response

from pengumon.api.state import AppState, get_state
from pengumon.devices.device import Device

StateDep = Annotated[AppState, Depends(get_state)]


def json_response(content: Any) -> Response:
    """Plain JSON data (dicts, lists, numbers, strings, None) serialized the way JSONResponse does it, but
    without FastAPI's jsonable_encoder walk, which costs ~8x more than json.dumps on the history payloads."""
    return Response(
        json.dumps(content, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8"),
        media_type="application/json",
    )


def device_or_404(state: AppState, device_id: str) -> Device:
    """The configured device with this id; 503 before the registry exists, 404 for an unknown id."""
    if state.devices is None:
        raise HTTPException(status_code=503, detail="Devices not initialized")
    device = state.devices.get(device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Unknown device")
    return device
