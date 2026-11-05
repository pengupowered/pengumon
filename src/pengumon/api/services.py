"""GET /api/services: the apps on this host whose health pengumon checks (services/checker.py), how to open them and
how they have been doing. Only configuration the owner wrote for display is exposed: never the CA file path."""

from typing import Any, List

from fastapi import APIRouter, Response

from pengumon.api.common import StateDep, json_response
from pengumon.services.checker import display_url
from pengumon.incidents.model import Incident

router = APIRouter(prefix="/api")

HISTORY_PER_SERVICE = 5
_HISTORY_SCAN = 500  # newest service incidents scanned to find each service's last few


def _incident(incident: Incident) -> dict:
    return {
        "id": incident.id, "start_time": incident.start_time, "end_time": incident.end_time,
        "duration_seconds": incident.duration_seconds, "resolved": incident.resolved,
        "title": incident.title, "description": incident.description,
    }


@router.get("/services")
async def get_services(state: StateDep) -> Response:
    """Each enabled service, config order: name, how to open it, status (up, degraded, down, unknown), timings,
    the classified error when not up, and its last five outages newest first. [] without service_checks."""
    checker = state.service_checker
    if checker is None:
        return json_response([])
    history: List[Incident] = []
    if state.repository is not None:
        history = await state.repository.service_incidents(limit=_HISTORY_SCAN)
    result: List[dict[str, Any]] = []
    for service in checker.services:
        st = checker.status.get(service.id)
        result.append({
            "id": service.id,
            "name": service.name,
            "description": service.description,
            "open_url": service.open_url,
            "check_url_display": display_url(service.url),
            "status": checker.state_of(service.id),
            "since": st.since if st else None,
            "last_checked": st.last_checked if st else None,
            "last_ok": st.last_ok if st else None,
            "latency_ms": st.latency_ms if st else None,
            "last_status_code": st.last_status_code if st else None,
            "consecutive_failures": st.consecutive_failures if st else 0,
            "error": st.error.as_dict() if st and st.error else None,
            "incidents": [_incident(i) for i in history if service.id in i.affected_targets][:HISTORY_PER_SERVICE],
        })
    return json_response(result)
