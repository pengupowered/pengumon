"""GET /api/oldest: where each kind of data starts, so a chart at "All" asks once."""

from typing import Dict, Optional

from fastapi import APIRouter

from pengumon.api.common import StateDep
from pengumon.db.oldest import TABLES, oldest_stored

router = APIRouter(prefix="/api")


@router.get("/oldest")
async def get_oldest(state: StateDep) -> Dict[str, Optional[float]]:
    """The oldest stored time (epoch seconds) of each kind of data, by the names of the retention settings:
    `probes` (latency, targets; samples and their rollups), `router` (router history, client counts), `mesh`, `line`
    (the cable line's channels), `traffic` (Internet use) and `speedtest`; `null` where nothing is stored. A rollup
    row counts from the start of its minute or quarter hour. Cheap (the first row of each table), so a page may ask at
    load and when a retention changes."""
    if not state.repository:
        return {kind: None for kind in TABLES}
    return await oldest_stored(state.repository)
