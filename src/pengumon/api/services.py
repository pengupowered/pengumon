"""GET /api/services: the apps on this host whose health pengumon checks (services/checker.py), how to open them and
how they have been doing (built in services/summary.py, which the MCP `services` tool shares)."""

from fastapi import APIRouter, Response

from pengumon.api.common import StateDep, json_response
from pengumon.services.summary import service_list

router = APIRouter(prefix="/api")


@router.get("/services")
async def get_services(state: StateDep) -> Response:
    """Each enabled service, config order: name, how to open it, status (up, degraded, down, unknown), timings,
    the classified error when not up, and its last five outages newest first. [] without service_checks."""
    return json_response(await service_list(state.service_checker, state.repository))
