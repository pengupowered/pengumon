"""The client list and its history: GET /api/devices/{id}/clients and /api/devices/{id}/clients/history.

Both need a device offering CLIENT_INVENTORY (400 otherwise, 404 for an unknown id). The list is the one thing in
pengumon that names people's devices, so what it shows is decided here, on the server (`privacy.client_details`):
with the default "token" the names, IP and MAC addresses are sent only when an access token protects the API; without
one, or when "masked" is set, a client is its vendor and the last four hex digits of its MAC. The answer says which
applied. Client rows are never logged, and they are kept in memory only (the monitor's latest reading): not in
/metrics, reports, the dossier, the websocket or the database. The history and the live summary (devices.summary)
are counts only.
"""

import hashlib
import math
import secrets
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from pengumon.api.common import StateDep, device_or_404
from pengumon.api.state import AppState
from pengumon.capabilities import BAND_2G, BAND_5G, BAND_OTHER, Capability, Client, ClientsReading, MeshReading
from pengumon.devices.device import Device
from pengumon.devices.summary import mesh_nodes, summarize_clients
from pengumon.security import rate_limit

router = APIRouter(prefix="/api")
_RATE = rate_limit("clients", 30, 60)  # the open modal asks once a minute; one bucket for both routes

MAX_HISTORY_POINTS = 300
DEFAULT_HISTORY_SECONDS = 24 * 3600
_KEY_SALT = secrets.token_bytes(16)  # row keys are opaque and change on restart: a MAC cannot be recovered from one
HINT = ("Names, IP and MAC addresses are hidden. Set an access token (PENGUMON_ACCESS_TOKEN) or "
        "privacy.client_details: full in config.yaml to see them.")


def privacy_mode(request: Request, state: AppState) -> str:
    """"full" or "masked": what this installation shows of its clients."""
    configured = state.config.privacy.client_details if state.config is not None else "token"
    if configured == "token":
        guards = getattr(request.app.state, "security_config", None)
        return "full" if guards is not None and guards.access_token else "masked"
    return configured


# A Wi-Fi signal in dBm as a word, the number of bars (of 4) and a grade: the thresholds UniFi uses.
SIGNAL_LEVELS = ((-60, "Excellent", 4, "good"), (-67, "Good", 3, "good"), (-75, "Fair", 2, "warn"))
SIGNAL_WEAK = ("Weak", 1, "critical")


def signal_grade(dbm: Optional[int]) -> Optional[Dict[str, Any]]:
    """The word, bars and grade (good / warn / critical, as in pengumon.grades) of a signal; None without one."""
    if dbm is None:
        return None
    for above, word, bars, grade in SIGNAL_LEVELS:
        if dbm > above:
            return {"word": word, "bars": bars, "grade": grade}
    word, bars, grade = SIGNAL_WEAK
    return {"word": word, "bars": bars, "grade": grade}


def _suffix(mac: Optional[str]) -> str:
    digits = "".join(ch for ch in (mac or "") if ch in "0123456789abcdefABCDEF")
    return digits[-4:].upper()


def _row(client: Client, full: bool) -> Dict[str, Any]:
    suffix = _suffix(client.mac)
    vendor = client.vendor or "Device"
    masked_label = f"{vendor} {suffix}".strip()
    row: Dict[str, Any] = {
        "key": hashlib.sha256(_KEY_SALT + client.key.encode()).hexdigest()[:12],
        "label": (client.label_parts[0] if client.label_parts else masked_label) if full else masked_label,
        "vendor": client.vendor,
        "kind": client.kind,
        "connection": client.connection,
        "band": client.band,
        "node": client.node,
        "rssi_dbm": client.rssi_dbm,
        "signal": signal_grade(client.rssi_dbm),
        "link_tx_mbps": client.link_tx_mbps,
        "link_rx_mbps": client.link_rx_mbps,
        "connected_s": client.connected_s,
        "online": client.online,
    }
    if full:
        row["ip"], row["mac"] = client.ip, client.mac
    return row


def _monitor_readings(device: Device):
    monitor = device.monitor
    return (getattr(monitor, "latest_clients", None), getattr(monitor, "latest_mesh", None),
            getattr(monitor, "clients_at", None))


def _require_inventory(state: AppState, device_id: str) -> Device:
    device = device_or_404(state, device_id)
    if Capability.CLIENT_INVENTORY not in device.driver.offers:
        raise HTTPException(status_code=400, detail="This device does not report its clients")
    return device


@router.get("/devices/{device_id}/clients", dependencies=[Depends(_RATE)])
async def get_clients(device_id: str, request: Request, state: StateDep):
    """The device's connected clients as of its last client-list read, with its mesh nodes and a summary. Offline
    clients the device still remembers are included (`online: false`). `ts` is null until a list has been read."""
    device = _require_inventory(state, device_id)
    await device.current()  # waits for a poll in flight; never starts one
    reading, mesh, read_at = _monitor_readings(device)
    mode = privacy_mode(request, state)
    reading = reading if reading is not None else ClientsReading()
    body = {
        "ts": read_at,
        "nodes": mesh_nodes(mesh),
        "clients": [_row(c, mode == "full") for c in reading.clients],
        "summary": summarize_clients(reading),
        "privacy": {"configured": state.config.privacy.client_details if state.config else "token",
                    "applied": mode, "hint": HINT if mode == "masked" else None},
    }
    return JSONResponse(body)  # /api answers are never cached (the security headers)


def bucket_seconds(start: float, end: float) -> int:
    """The bucket for a range: at most MAX_HISTORY_POINTS of them even when the range starts and ends part way into a
    bucket, whole minutes once buckets are a minute or more."""
    raw = max(1, math.ceil((end - start) / (MAX_HISTORY_POINTS - 1)))
    return raw if raw < 60 else math.ceil(raw / 60) * 60


@router.get("/devices/{device_id}/clients/history", dependencies=[Depends(_RATE)])
async def get_clients_history(
    device_id: str,
    state: StateDep,
    start_time: Optional[float] = Query(None),
    end_time: Optional[float] = Query(None),
):
    """Client counts over time (default: the last 24 hours), bucketed on the server to at most 300 points: each point
    is the mean of the readings in its bucket. Counts only, no client. `nodes` lists the mesh nodes seen in the range
    (labelled as the device names them now) and each point has `by_node` counts."""
    device = _require_inventory(state, device_id)
    if not state.repository:
        raise HTTPException(status_code=503, detail="Repository not initialized")
    now = time.time()
    end = end_time if end_time is not None else now
    start = start_time if start_time is not None else end - DEFAULT_HISTORY_SECONDS
    if not (start < end):
        raise HTTPException(status_code=400, detail="start_time must be before end_time")
    bucket = bucket_seconds(start, end)
    rows = await state.repository.get_client_counts(device.id, start, end, bucket)
    node_rows = await state.repository.get_mesh_counts(device.id, start, end, bucket)
    points: List[Dict[str, Any]] = [
        {"ts": ts, "online": online, "wired": wired, "wifi": round(sum(v or 0 for v in bands), 1),
         "by_band": {BAND_2G: bands[0], BAND_5G: bands[1], BAND_OTHER: bands[2]}, "by_node": {}}
        for ts, online, wired, *bands in rows]
    index = {p["ts"]: p for p in points}
    seen: Dict[str, None] = {}
    for ts, node_id, clients in node_rows:
        seen[node_id] = None
        if ts in index:
            index[ts]["by_node"][node_id] = clients
    _, mesh, _ = _monitor_readings(device)
    labels = {n.id: n.label for n in (mesh.nodes if isinstance(mesh, MeshReading) else ())}
    return {"start_time": start, "end_time": end, "bucket_seconds": bucket,
            "nodes": [{"id": n, "label": labels.get(n, n)} for n in sorted(seen)], "points": points}
