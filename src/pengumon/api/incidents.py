"""Incidents: the list, the post-mortem inspection of one, and the evidence dossier for an ISP complaint."""

import asyncio
from typing import List, Literal

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
from pengumon.security import rate_limit
from pengumon.timeofday import viewer_zone

router = APIRouter(prefix="/api")

# One dossier at a time, whoever asks (as the AI pack has): a build reads up to 720 hours of evidence. A caller that
# finds it locked is answered 429 rather than queued.
dossier_building = asyncio.Lock()


@router.get("/incidents", response_model=List[Incident])
async def get_incidents(
    state: StateDep,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0, le=1000, description="Skip this many of the newest (the next page of an earlier answer)"),
    include_expected: bool = Query(True, description="Include incidents explained by heavy line load"),
    scope: Literal["network", "service", "all"] = Query(
        "network", description="network: network outages (default); service: host app outages; all: both"),
):
    """Returns historical network outages and incidents. App health incidents (kind service_down) are listed
    only with scope=service or scope=all; GET /api/services shows them per service.

    Newest first. `limit` is the page size and `offset` how many newer incidents come before it, so the dashboard loads
    a first page and the older ones on demand (offset + limit is what the repository reads, and caches, per incident change)."""
    if not state.repository:
        return []
    rows = await state.repository.get_recent_incidents(limit=offset + limit, include_expected=include_expected, scope=scope)
    page = rows[offset:offset + limit]
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
    line = state.devices.primary(Capability.ACCESS_LINE) if state.devices is not None else None
    poll = line.interval_seconds if line else None  # a line reading this close to the window still counts
    mode = privacy_mode(request, state)
    reach = {"snapshot_reach_seconds": poll} if poll else {}
    inspection = await run_inspection(state.repository, targets, incident, **reach,
                                      line=line_history(state.devices, state.repository),
                                      router=router_history(state.devices, state.repository),
                                      classifier=getattr(cfg, "classifier", None),
                                      traffic=traffic_source(state.devices, mode, client_key,
                                                             names=await state.client_names.load(state.repository)
                                                             if mode == "full" else None),
                                      tz=viewer_zone(cfg)[0])
    return inspection.to_response()


@router.get("/incidents/dossier", dependencies=[Depends(rate_limit("dossier", 20, 60))])
async def get_escalation_dossier(state: StateDep, hours: int = Query(24, ge=1, le=720), tz: TzName = None,
                                 tz_offset_minutes: TzOffset = 0):
    """Evidence report for an ISP support ticket or complaint; every statement is derived from the data."""
    if not state.repository:
        raise HTTPException(status_code=503, detail="Repository not initialized")
    if dossier_building.locked():
        api_rate_limited.add(1, {"bucket": "dossier_busy"})
        raise HTTPException(status_code=429, detail="Another dossier is being built; try again in a few seconds",
                            headers={"Retry-After": "5"})
    async with dossier_building:
        return await build_dossier(state.repository, state.config, hours,
                                   line=line_history(state.devices, state.repository),
                                   traffic=traffic_source(state.devices, "generic"),
                                   ignoring=ignoring_ping_ids(state.prober), tz_name=tz,
                                   tz_offset_minutes=tz_offset_minutes)
