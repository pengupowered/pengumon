"""GET /api/history: latency and loss samples for charting."""

import time
from typing import List, Optional

from fastapi import APIRouter, Query

from pengumon.api.common import StateDep, json_response

router = APIRouter(prefix="/api")


# Without bucket_seconds the answer is every raw sample, about 10 rows a second: a 6 h range is 28 MB of JSON and
# pushes the process's memory up by well over 100 MB. Longer ranges are bucketed (10 s up to a day, a minute beyond).
HISTORY_UNBUCKETED_MAX_SECONDS = 3600


@router.get("/history")
async def get_history(
    state: StateDep,
    start_time: Optional[float] = None,
    end_time: Optional[float] = None,
    target_ids: Optional[List[str]] = Query(None),
    bucket_seconds: Optional[int] = Query(None),
    compact: bool = Query(False),
):
    """Fetches time-series latency and loss samples for charting.

    compact=true returns {"fields": [...], "rows": [[...], ...]} (each row's values in field order) instead of
    a list of objects: about a third of the bytes, and less to encode and parse.

    A range longer than an hour with no bucket_seconds is bucketed anyway (10 s buckets up to a day, 60 s beyond),
    so one request can never ask for every raw sample of a long range."""
    if not state.repository:
        return []
    now = time.time()
    st = start_time if start_time is not None else now - 3600  # Default last 1 hour
    et = end_time if end_time is not None else now
    if not bucket_seconds or bucket_seconds <= 1:
        if et - st > HISTORY_UNBUCKETED_MAX_SECONDS:
            bucket_seconds = 10 if et - st <= 86400 else 60
    rows = await state.repository.get_history(
        start_time=st,
        end_time=et,
        target_ids=target_ids,
        bucket_seconds=bucket_seconds,
    )
    if compact:
        fields = list(rows[0]) if rows else []
        return json_response({"fields": fields, "rows": [list(r.values()) for r in rows]})
    return json_response(rows)
