"""Incidents: the list, the post-mortem inspection of one, and the evidence dossier for an ISP complaint."""

import asyncio
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from pengumon.analysis.incident import inspect_incident as run_inspection
from pengumon.api.clients import client_key, privacy_mode
from pengumon.api.common import StateDep
from pengumon.api.reports import TzName, TzOffset
from pengumon.capabilities import Capability
from pengumon.devices.samples import line_history, router_history
from pengumon.devices.traffic import traffic_source
from pengumon.engine.path_rules import ignoring_ping_ids
from pengumon.incidents.model import Incident
from pengumon.incidents.outage_read import with_outage_ids
from pengumon.incidents.outages import Rules
from pengumon.observability.telemetry import api_rate_limited
from pengumon.reports.dossier import build_dossier
from pengumon.preview import basic_snapshot, event_devices, from_request, is_basic, preview_config, viewer_registry
from pengumon.security import rate_limit
from pengumon.timeofday import viewer_zone

router = APIRouter(prefix="/api")

# One dossier at a time, whoever asks (as the AI pack has): a build reads up to 720 hours of evidence. A caller that
# finds it locked is answered 429 rather than queued.
dossier_building = asyncio.Lock()


# The most incidents one range request answers (about 1.8 KB each in full, a third of that compact); a range with more
# answers its newest and the caller pages on with `offset`.
RANGE_MAX = 5000


@router.get("/incidents", response_model=List[Incident])
async def get_incidents(
    state: StateDep,
    limit: Optional[int] = Query(None, ge=1, le=RANGE_MAX,
                                 description=f"Page size: 100 by default; with `since` up to {RANGE_MAX} (default {RANGE_MAX})"),
    offset: int = Query(0, ge=0, le=RANGE_MAX, description="Skip this many of the newest (the next page of an earlier answer)"),
    include_expected: bool = Query(True, description="Include incidents explained by heavy line load"),
    scope: Literal["network", "service", "all"] = Query(
        "network", description="network: network outages (default); service: host app outages; all: both"),
    since: Optional[float] = Query(None, ge=0, description="Epoch seconds: every incident that overlaps [since, until] (a range)"),
    until: Optional[float] = Query(None, ge=0, description="Epoch seconds, with `since`: the end of the range (default now)"),
    fields: Literal["full", "list"] = Query("full", description="list: leave out path_trace (what the incident list and timeline do not use; "
                                                              "GET /api/incidents/{id}/inspect has it)"),
):
    """Returns historical network outages and incidents. App health incidents (kind service_down) are listed
    only with scope=service or scope=all; GET /api/services shows them per service.

    Newest first. `limit` is the page size and `offset` how many newer incidents come before it, so the dashboard loads
    a first page and the older ones on demand (offset + limit is what the repository reads, and caches, per incident change).
    With `since` the answer is the range: every incident that overlaps [since, until], up to `limit` (at most 5000; a
    full page means there may be older ones: ask again with `offset`)."""
    if not state.repository:
        return []
    if since is None:
        if until is not None:
            raise HTTPException(status_code=422, detail="until goes with since")
        limit = 100 if limit is None else limit
        if limit > 1000 or offset > 1000:
            raise HTTPException(status_code=422, detail="without since, limit and offset are at most 1000")
        rows = await state.repository.get_recent_incidents(limit=offset + limit, include_expected=include_expected, scope=scope)
        page = rows[offset:offset + limit]
        if fields == "list":
            page = [i.model_copy(update={"path_trace": None}) for i in page]
    else:
        page = await state.repository.get_incidents_overlapping(
            since, until, include_expected, scope, RANGE_MAX if limit is None else limit, offset, compact=fields == "list")
    if scope == "service":
        return page
    # outage_id: an outage that straddles the page edge has the same id on both pages (the grouping reads by time)
    return await with_outage_ids(state.repository, Rules.from_config(state.config), page)


@router.get("/incidents/count")
async def count_incidents(
    state: StateDep,
    scope: Literal["network", "service", "all"] = Query("network", description="Same meaning as for GET /api/incidents"),
):
    """How many incidents are recorded: `total` (the expected, busy-line ones included) and `expected`. The list loads
    a page at a time, so the dashboard asks here for the real figure instead of counting what it has loaded."""
    if not state.repository:
        return {"total": 0, "expected": 0}
    total = await state.repository.count_incidents(True, scope)
    return {"total": total, "expected": total - await state.repository.count_incidents(False, scope)}


# The inspector reads every probe sample around an incident, the dossier up to 720 hours of them: neither is free to repeat.
@router.get("/incidents/{incident_id}/inspect", dependencies=[Depends(rate_limit("incident_inspect", 60, 60))])
async def inspect_incident(incident_id: int, state: StateDep, request: Request):
    """Provides high-resolution post-mortem inspection data for an incident, with the device events around it and the
    router's traffic in the hours it covers (clients named as /clients names them for this request)."""
    if not state.repository:
        raise HTTPException(status_code=503, detail="Repository not ready")

    incident = await state.repository.get_incident_by_id(incident_id)
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found")

    # Target configurations in canonical order: ICMP targets, then DNS probes
    cfg = state.config
    targets = [*cfg.targets, *cfg.dns_probes] if cfg else []
    devices = viewer_registry(state, request)  # as this browser's preview shows them (pengumon.preview)
    line = devices.primary(Capability.ACCESS_LINE) if devices is not None else None
    poll = line.interval_seconds if line else None  # a line reading this close to the window still counts
    mode = privacy_mode(request, state)
    reach = {"snapshot_reach_seconds": poll} if poll else {}
    inspection = await run_inspection(state.repository, targets, incident, **reach,
                                      line=line_history(devices, state.repository),
                                      router=router_history(devices, state.repository),
                                      classifier=getattr(cfg, "classifier", None),
                                      traffic=traffic_source(devices, mode, client_key,
                                                             names=await state.client_names.load(state.repository)
                                                             if mode == "full" else None),
                                      tz=viewer_zone(cfg)[0])
    out = inspection.to_response()
    preview = from_request(request)
    if preview.active and state.devices is not None:
        # events of a device previewed as absent or basic (a basic router tells neither restarts nor link changes), and
        # a basic router's figures beyond its WAN rates
        out["device_events"] = [e for e in out["device_events"] if e.get("device_id") in event_devices(devices)]
        # the router snapshots are those of the primary WAN device (devices.samples.router_history): cut only when
        # that one is seen as a basic router
        if is_basic(devices.primary(Capability.WAN_COUNTERS)):
            out["router_snapshots"] = [basic_snapshot(s) for s in out["router_snapshots"]]
    return out


@router.get("/incidents/dossier", dependencies=[Depends(rate_limit("dossier", 20, 60))])
async def get_escalation_dossier(request: Request, state: StateDep, hours: int = Query(24, ge=1, le=720), tz: TzName = None,
                                 tz_offset_minutes: TzOffset = 0):
    """Evidence report for an ISP support ticket or complaint; every statement is derived from the data."""
    if not state.repository:
        raise HTTPException(status_code=503, detail="Repository not initialized")
    if dossier_building.locked():
        api_rate_limited.add(1, {"bucket": "dossier_busy"})
        raise HTTPException(status_code=429, detail="Another dossier is being built; try again in a few seconds",
                            headers={"Retry-After": "5"})
    async with dossier_building:
        devices = viewer_registry(state, request)  # as this browser's preview shows them (pengumon.preview)
        preview = from_request(request)
        return await build_dossier(state.repository, preview_config(state.config, preview), hours,
                                   line=line_history(devices, state.repository),
                                   traffic=traffic_source(devices, "generic"),
                                   ignoring=ignoring_ping_ids(state.prober), tz_name=tz,
                                   tz_offset_minutes=tz_offset_minutes,
                                   event_devices=event_devices(devices) if preview.active and devices is not None else None)
