"""The router's traffic statistics and the device events: GET /api/devices/{id}/traffic,
/api/devices/{id}/traffic/clients/{client} and /api/devices/{id}/events.

The traffic routes need a device that offers TRAFFIC_USAGE (400 otherwise, 404 for an unknown id). They name clients
by the rule of /clients (api/clients.py `privacy_mode`): with the names and MACs to a request that may see them, else
as the client's maker and the last four hex digits of its MAC. A client is addressed by the opaque `key` /clients
gives it (the same key in both answers until the next start); a request that may see MACs may also use the MAC.
The figures are computed on read (devices/traffic.py); nothing here asks the router for anything.

PUT /api/devices/{id}/clients/{client}/name gives a client a name in Pengumon, shown instead of the router's wherever
names are shown in full (clients/names.py), or takes it away; only to a request that may see names.
"""

import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from pengumon.api.clients import client_key, privacy_mode
from pengumon.api.common import StateDep, device_or_404
from pengumon.capabilities import Capability
from pengumon.clients.names import NAME_MAX, clean_name
from pengumon.devices import traffic
from pengumon.devices.device import Device
from pengumon.devices.events import events_between
from pengumon.drivers.common import normal_mac
from pengumon.security import rate_limit
from pengumon.state import AppState
from pengumon.timeofday import viewer_zone

router = APIRouter(prefix="/api")
_RATE = rate_limit("traffic", 60, 60)  # the panel asks once a minute at most; one bucket for both traffic routes
DEFAULT_EVENT_SECONDS = 7 * 86400


def _require_traffic(state: AppState, device_id: str) -> Device:
    device = device_or_404(state, device_id)
    if Capability.TRAFFIC_USAGE not in type(device.driver).offers:
        raise HTTPException(status_code=400, detail="This device does not report traffic per client")
    if not state.repository:
        raise HTTPException(status_code=503, detail="Repository not initialized")
    return device


async def _namer(request: Request, state: AppState, device: Device) -> traffic.Namer:
    clients = getattr(device.monitor, "latest_clients", None)
    mode = privacy_mode(request, state)
    names = await state.client_names.load(state.repository) if mode == "full" else None
    return traffic.Namer(mode, clients, key=client_key, names=names)


def _status(device: Device) -> dict:
    s = traffic.status_of(device)
    return {"available": s.available, "reason": s.reason, "read_at": s.read_at, "interval_seconds": s.interval_seconds}


@router.get("/devices/{device_id}/traffic", dependencies=[Depends(_RATE)])
async def get_traffic(
    device_id: str,
    request: Request,
    state: StateDep,
    hours: int = Query(traffic.DEFAULT_HOURS, ge=1, le=traffic.MAX_HOURS),
    days: Optional[int] = Query(None, ge=1, le=traffic.MAX_DAYS),
    top: Optional[int] = Query(None, ge=1, le=500),
):
    """The last `hours` complete clock hours of the router's traffic, or (with `days`, which `hours` then yields to) the
    last `days` whole days up to the last midnight, in the zone set in Settings, with the WAN's totals per day: hourly
    WAN totals, clients ranked by download (with their top apps and hint) and the apps of every client together ranked
    by download. The apps are the router's latest 24-hour window over a day or less, and the sum of the whole days
    in the range over more (`apps_days`). `available`/`reason` say whether the router gives these figures at all."""
    device = _require_traffic(state, device_id)
    namer = await _namer(request, state, device)
    body = await traffic.summary(state.repository, device.id, hours, namer, top=top, days=days,
                                 tz=viewer_zone(state.config)[0])
    return {**_status(device), **body, "privacy": namer.mode}


async def _resolve(state: AppState, device: Device, client: str, full: bool) -> str:
    """The MAC of `client`: an opaque key from /clients or /traffic, or (to a request that may see MACs) a MAC."""
    mac = normal_mac(client)
    if mac is not None:
        if not full:
            raise HTTPException(status_code=404, detail="Unknown client")
        return mac
    for known in await traffic.stored_clients(state.repository, device.id):
        if client_key(known) == client:
            return known
    clients = getattr(device.monitor, "latest_clients", None)
    for c in (clients.clients if clients is not None else ()):
        if c.mac and client_key(c.key) == client:
            return c.mac
    raise HTTPException(status_code=404, detail="Unknown client")


@router.get("/devices/{device_id}/traffic/clients/{client}", dependencies=[Depends(_RATE)])
async def get_client_traffic(
    device_id: str,
    client: str,
    request: Request,
    state: StateDep,
    hours: int = Query(168, ge=1, le=traffic.MAX_HOURS),
    days: Optional[int] = Query(None, ge=1, le=traffic.MAX_DAYS),
):
    """One client's hourly series over the last `hours` complete hours, or the last `days` whole days (as /traffic, with
    `daily`), its apps over the range as /traffic counts them and its apps by whole day. 404 for a client with no stored traffic and not in the client list."""
    device = _require_traffic(state, device_id)
    namer = await _namer(request, state, device)
    mac = await _resolve(state, device, client, namer.mode == "full")
    body = await traffic.client_detail(state.repository, device.id, mac, hours, namer, tz=viewer_zone(state.config)[0],
                                       days=days)
    return {**_status(device), **body, "privacy": namer.mode}


class ClientName(BaseModel):
    """The name to give a client; null or empty takes the name given before away (the router's is shown again)."""
    name: Optional[str] = Field(None, max_length=NAME_MAX)


@router.put("/devices/{device_id}/clients/{client}/name", dependencies=[Depends(rate_limit("client_name", 30, 60))])
async def put_client_name(device_id: str, client: str, body: ClientName, request: Request, state: StateDep):
    """Gives the client (its key, as /clients and /traffic give it) a name in Pengumon, or takes it away. 403 to a
    request that may not see names (it would not see the name it gave), 404 for an unknown client, 409 when the most
    names Pengumon keeps are kept. Answers the client's `key`, `label` and `router_label` as the traffic routes show
    them."""
    device = device_or_404(state, device_id)
    if not {Capability.CLIENT_INVENTORY, Capability.TRAFFIC_USAGE} & set(type(device.driver).offers):
        raise HTTPException(status_code=400, detail="This device does not list connected clients")
    if not state.repository:
        raise HTTPException(status_code=503, detail="Repository not initialized")
    if privacy_mode(request, state) != "full":
        raise HTTPException(status_code=403, detail="Device names can be given only where names and addresses are "
                                                    "shown (Settings > Privacy, and signed in when a password is set)")
    mac = await _resolve(state, device, client, True)
    try:
        await state.client_names.set(state.repository, mac, clean_name(body.name))
    except ValueError as ex:
        raise HTTPException(status_code=409, detail=str(ex))
    row = (await _namer(request, state, device)).row(mac)
    return {"key": row["key"], "label": row["label"], "router_label": row["router_label"]}


@router.get("/devices/{device_id}/events")
async def get_device_events(
    device_id: str,
    state: StateDep,
    start_time: Optional[float] = Query(None, allow_inf_nan=False),
    end_time: Optional[float] = Query(None, allow_inf_nan=False),
):
    """The device's events (devices/events.py: `restarted`, `wan_link`) in [start_time, end_time] (default: the last
    7 days), oldest first, each with a short `text` in the zone set in Settings."""
    device = device_or_404(state, device_id)
    if not state.repository:
        raise HTTPException(status_code=503, detail="Repository not initialized")
    now = time.time()
    end = end_time if end_time is not None else now
    start = start_time if start_time is not None else end - DEFAULT_EVENT_SECONDS
    if not start <= end:
        raise HTTPException(status_code=400, detail="start_time must not be after end_time")
    tz = viewer_zone(state.config)[0]
    events = await events_between(state.repository, device.id, start, end)
    return {"start_time": start, "end_time": end, "events": [e.as_dict(tz) for e in events]}
