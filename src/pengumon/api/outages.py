"""Outages: the drops of the line that belong together (incidents.outages), computed on every read."""

import time
from typing import List, Optional

from fastapi import APIRouter, Query
from pydantic import BaseModel

from pengumon.api.common import StateDep
from pengumon.incidents.outage_read import outages_since
from pengumon.incidents.outages import Rules

router = APIRouter(prefix="/api")


class OutageOut(BaseModel):
    id: int  # the first member's incident id
    start_time: float
    end_time: Optional[float] = None  # null while a member is open
    ongoing: bool
    settling: bool  # ended less than the gap ago: it may still grow
    duration_seconds: float
    down_seconds: float  # the critical members' time, overlaps counted once
    affected_seconds: float  # all members' time, overlaps counted once
    drops: int
    critical_drops: int
    severity: str
    places: List[str]  # path order
    title: str
    cause_code: str
    likely_maintenance: bool
    incident_ids: List[int]


class OutagesOut(BaseModel):
    gap_minutes: int
    outages: List[OutageOut]


@router.get("/outages", response_model=OutagesOut)
async def get_outages(
    state: StateDep,
    hours: int = Query(168, ge=1, le=10000),
    limit: int = Query(100, ge=1, le=500),
):
    """The outages that overlap the last `hours`, newest first (at most `limit`). An outage joins incidents of
    neighboring places of the path that follow each other within `incidents.group_gap_minutes`; the incidents stay as
    recorded. `gap_minutes` 0: grouping is off and every drop is an outage of one. Network incidents explained by load,
    and causes that are not a place of the path (load, unknown, host apps), are in no outage."""
    rules = Rules.from_config(state.config)
    gap_minutes = int(round(rules.gap_seconds / 60))
    if not state.repository:
        return {"gap_minutes": gap_minutes, "outages": []}
    now = time.time()
    found = await outages_since(state.repository, rules, now - hours * 3600, now)
    found.reverse()
    return {"gap_minutes": gap_minutes,
            "outages": [o.summary(now, rules) for o in found[:limit]]}
