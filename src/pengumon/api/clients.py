"""The client list and its history: GET /api/devices/{id}/clients and /api/devices/{id}/clients/history.

Both need a device offering CLIENT_INVENTORY (400 otherwise, 404 for an unknown id). The list (and the router's traffic
per client, api/traffic.py, which applies the same rule) is what in pengumon names people's devices, so what it
shows is decided here, on the server (`privacy.client_details`): with the default "token" the names, IP and MAC
addresses are sent only to a request that carries the access token (so not without one, and not to a viewer without
the token: PENGUMON_VIEW_WITHOUT_TOKEN); otherwise, or when "masked" is set, a client is its vendor and the last four
hex digits of its MAC. The answer says which applied. Client rows are never logged, and they are kept in memory only (the monitor's latest reading): not in
/metrics, reports, the dossier, the websocket or the database. The history and the live summary (devices.summary)
are counts only. The exceptions are the router's traffic per client (schema v19), stored by MAC so its history
exists, and the names the owner gives devices (v20, clients/names.py; shown, like the router's names, only in full);
the dossier and the AI pack describe a client only by its kind ("a game console"), never by name or MAC.

PUT /api/devices/{id}/clients/{client}/name gives a client a name in Pengumon (or takes it away): only to a request
that may see names (privacy "full"), which also means one that may edit when a password is set.
"""

import hashlib
import secrets
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from pengumon.api.common import StateDep, device_or_404
from pengumon.state import AppState
from pengumon.capabilities import BAND_2G, BAND_5G, BAND_OTHER, Capability, Client, ClientsReading, MeshReading
from pengumon.devices.device import Device
from pengumon.grades import signal_grade
from pengumon.devices.samples import bucket_width
from pengumon.devices.summary import mesh_nodes, summarize_clients
from pengumon.devices.traffic import traffic_hints
from pengumon.security import can_edit, rate_limit

router = APIRouter(prefix="/api")
_RATE = rate_limit("clients", 30, 60)  # the open modal asks once a minute; one bucket for both routes

MAX_HISTORY_POINTS = 300
DEFAULT_HISTORY_SECONDS = 24 * 3600
_KEY_SALT = secrets.token_bytes(16)  # row keys are opaque and change on restart: a MAC cannot be recovered from one
HINT = ("Names, IP and MAC addresses are hidden because no password is set. Set one under Settings > Sign-in, or choose "
        "Always for Connected clients' names and addresses under Settings > Privacy.")
MASKED_HINT = ("Names, IP and MAC addresses are hidden by a setting. To see them, change Connected clients' names and "
               "addresses under Settings > Privacy.")
VIEWER_HINT = "Names, IP and MAC addresses are hidden. Sign in with the password to see them."


def _token_set(request: Request) -> bool:
    guards = getattr(request.app.state, "security_config", None)
    return guards is not None and bool(getattr(guards, "token_set", False))  # as text or as a hash


def privacy_mode(request: Request, state: AppState) -> str:
    """"full" or "masked": what this request is shown of the clients. With "token", "full" needs a token to be set and
    this request to carry it (a viewer without the token, PENGUMON_VIEW_WITHOUT_TOKEN, gets "masked")."""
    configured = getattr(getattr(state.config, "privacy", None), "client_details", "token")
    if configured == "token":
        return "full" if _token_set(request) and can_edit(request) else "masked"
    return configured


def privacy_hint(request: Request, state: AppState, mode: str) -> Optional[str]:
    """Why the details are hidden, and how to see them; None when they are shown. A viewer without the token, under
    the default "token", is told to sign in."""
    if mode != "masked":
        return None
    configured = state.config.privacy.client_details if state.config is not None else "token"
    if configured == "masked":
        return MASKED_HINT
    return VIEWER_HINT if _token_set(request) and not can_edit(request) else HINT


def _suffix(mac: Optional[str]) -> str:
    digits = "".join(ch for ch in (mac or "") if ch in "0123456789abcdefABCDEF")
    return digits[-4:].upper()


def client_key(key: str) -> str:
    """A client's opaque row key: the same for the same client (its driver key, a MAC in normal form for a router)
    until the next start, and no way back to the MAC. The traffic routes key their rows the same way."""
    return hashlib.sha256(_KEY_SALT + key.encode()).hexdigest()[:12]


def _row(client: Client, full: bool, hint: Optional[str] = None, names: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    suffix = _suffix(client.mac)
    vendor = client.vendor or "Device"
    masked_label = f"{vendor} {suffix}".strip()
    router_label = client.label_parts[0] if client.label_parts else masked_label
    given = (names or {}).get(client.mac or "") if full else None
    row: Dict[str, Any] = {
        "key": client_key(client.key),
        "label": (given or router_label) if full else masked_label,
        # with a name given in Pengumon: the router's own name for it (full only), else null
        "router_label": router_label if given else None,
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
        # the app the router counted at least 80 % of its last 24 hours' traffic as (devices/traffic.py), or null
        "traffic_hint": hint,
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
    hints: Dict[str, str] = {}
    if Capability.TRAFFIC_USAGE in device.driver.offers and state.repository is not None:
        hints = await traffic_hints(state.repository, device.id)
    names = await state.client_names.load(state.repository) if mode == "full" else {}
    body = {
        "ts": read_at,
        "nodes": mesh_nodes(mesh),
        "clients": [_row(c, mode == "full", hints.get(c.mac or ""), names) for c in reading.clients],
        "summary": summarize_clients(reading),
        "privacy": {"configured": state.config.privacy.client_details if state.config else "token",
                    "applied": mode, "hint": privacy_hint(request, state, mode)},
    }
    return JSONResponse(body)  # /api answers are never cached (the security headers)


def bucket_seconds(start: float, end: float) -> int:
    """The bucket for a range: at most MAX_HISTORY_POINTS of them even when the range starts and ends part way into a
    bucket (one point to spare), whole seconds below a minute, whole minutes from there (the router history's rule)."""
    return bucket_width(end - start, max_points=MAX_HISTORY_POINTS - 1, sub_minute_unit=1)


@router.get("/devices/{device_id}/clients/history", dependencies=[Depends(_RATE)])
async def get_clients_history(
    device_id: str,
    state: StateDep,
    start_time: Optional[float] = Query(None, allow_inf_nan=False),
    end_time: Optional[float] = Query(None, allow_inf_nan=False),
):
    """Client counts over time (default: the last 24 hours), bucketed on the server to at most 300 points: each point
    is the mean of the readings in its bucket. Counts only, no client. `nodes` lists the mesh nodes seen in the range
    (labeled as the device names them now) and each point has `by_node` counts."""
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
