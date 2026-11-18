"""GET /api/oldest: where each kind of data starts, so a chart at "All" asks once."""

from typing import Dict, Optional

from fastapi import APIRouter, Request

from pengumon.api.common import StateDep
from pengumon.capabilities import Capability
from pengumon.db.oldest import TABLES, oldest_stored
from pengumon.preview import from_request, viewer_registry

# The kinds of data a device brings, by the capabilities that store them: under a preview without that device
# (pengumon.preview), null as on an installation that never had it.
DEVICE_KINDS = {"router": (Capability.WAN_COUNTERS, Capability.ROUTER_SYSTEM), "mesh": (Capability.MESH,),
                "line": (Capability.ACCESS_LINE,), "traffic": (Capability.TRAFFIC_USAGE,)}

router = APIRouter(prefix="/api")


@router.get("/oldest")
async def get_oldest(state: StateDep, request: Request) -> Dict[str, Optional[float]]:
    """The oldest stored time (epoch seconds) of each kind of data, by the names of the retention settings:
    `probes` (latency, targets; samples and their rollups), `router` (router history, client counts), `mesh`, `line`
    (the cable line's channels), `traffic` (Internet use) and `speedtest`; `null` where nothing is stored. A rollup
    row counts from the start of its minute or quarter hour. Cheap (the first row of each table), so a page may ask at
    load and when a retention changes."""
    if not state.repository:
        return {kind: None for kind in TABLES}
    oldest = await oldest_stored(state.repository)
    if from_request(request).active and state.devices is not None:
        devices = viewer_registry(state, request)
        for kind, capabilities in DEVICE_KINDS.items():
            if kind in oldest and not any(devices.offering(c) for c in capabilities):
                oldest[kind] = None
    return oldest
