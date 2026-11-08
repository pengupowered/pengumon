"""The host app checks as GET /api/services and the MCP `services` tool show them: each service's state, timings and
last outages. Only configuration the owner wrote for display is exposed: never the CA file path, and the check URL only
as host:port/path (services.checker.display_url: no credentials, no query)."""

from typing import Any, Dict, List, Optional

from pengumon.incidents.model import Incident
from pengumon.services.checker import ServiceChecker, display_url

HISTORY_PER_SERVICE = 5
_HISTORY_SCAN = 500  # newest service incidents scanned to find each service's last few


def _incident(incident: Incident) -> dict:
    return {
        "id": incident.id, "start_time": incident.start_time, "end_time": incident.end_time,
        "duration_seconds": incident.duration_seconds, "resolved": incident.resolved,
        "title": incident.title, "description": incident.description,
    }


async def service_list(checker: Optional[ServiceChecker], repository) -> List[Dict[str, Any]]:
    """Each enabled service, config order: name, how to open it, status (up, degraded, down, unknown), timings,
    the classified error when not up, and its last five outages newest first. [] without service_checks."""
    if checker is None:
        return []
    history: List[Incident] = []
    if repository is not None:
        history = await repository.service_incidents(limit=_HISTORY_SCAN)
    result: List[Dict[str, Any]] = []
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
    return result
