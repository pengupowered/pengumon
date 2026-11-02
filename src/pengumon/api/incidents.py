"""Incidents: the list, the post-mortem inspection of one, and the evidence dossier for an ISP complaint."""

from typing import List, Literal

from fastapi import APIRouter, HTTPException, Query

from pengumon.analysis.incident import inspect_incident as run_inspection
from pengumon.api.common import StateDep
from pengumon.capabilities import Capability
from pengumon.engine.path_rules import ignoring_ping_ids
from pengumon.incidents.model import Incident
from pengumon.reports.dossier import build_dossier

router = APIRouter(prefix="/api")


@router.get("/incidents", response_model=List[Incident])
async def get_incidents(
    state: StateDep,
    limit: int = Query(100, ge=1, le=1000),
    include_expected: bool = Query(True, description="Include incidents explained by heavy line load"),
    scope: Literal["network", "service", "all"] = Query(
        "network", description="network: network outages (default); service: host app outages; all: both"),
):
    """Returns historical network outages and incidents. App health incidents (kind service_down) are listed
    only with scope=service or scope=all; GET /api/services shows them per service."""
    if not state.repository:
        return []
    return await state.repository.get_recent_incidents(limit=limit, include_expected=include_expected, scope=scope)



@router.get("/incidents/{incident_id}/inspect")
async def inspect_incident(incident_id: int, state: StateDep):
    """Provides high-resolution post-mortem inspection data for an incident."""
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
    reach = {"snapshot_reach_seconds": poll} if poll else {}
    inspection = await run_inspection(state.repository, targets, incident, **reach, line=state.line_history(),
                                      router=state.router_history())
    return inspection.to_response()


@router.get("/incidents/dossier")
async def get_escalation_dossier(state: StateDep, hours: int = Query(24, ge=1, le=720)):
    """Evidence report for an ISP support ticket or complaint; every statement is derived from the data."""
    if not state.repository:
        raise HTTPException(status_code=503, detail="Repository not initialized")
    return await build_dossier(state.repository, state.config, hours, line=state.line_history(),
                              ignoring=ignoring_ping_ids(state.prober))
