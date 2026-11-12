"""GET /api/history: latency and loss samples for charting."""

import asyncio
import json
import time
from typing import Any, List, Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response

from pengumon.api.common import StateDep, json_response, record_answer, record_capped
from pengumon.db.probes import HISTORY_ROW_CAP, HISTORY_THREAD_ROWS
from pengumon.security import rate_limit

router = APIRouter(prefix="/api")


# Without bucket_seconds the answer is every raw sample, about 10 rows a second: a 6 h range is 28 MB of JSON and
# pushes the process's memory up by well over 100 MB. Longer ranges are bucketed (10 s up to a day, a minute beyond).
HISTORY_UNBUCKETED_MAX_SECONDS = 3600


# json.dumps holds the GIL for a whole call, so a big answer is encoded in pieces of this many rows: the event loop gets
# its turn between them.
_ENCODE_CHUNK_ROWS = 2000


def _dumps(content: Any) -> str:
    return json.dumps(content, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _rows_json(rows: list) -> str:
    """json.dumps(rows) byte for byte, one piece at a time."""
    pieces = [_dumps(rows[i:i + _ENCODE_CHUNK_ROWS])[1:-1] for i in range(0, len(rows), _ENCODE_CHUNK_ROWS)]
    return "[" + ",".join(pieces) + "]"


def _encode(answer: Any, compact: bool) -> Response:
    """json_response(answer), for an answer of many rows (run in a thread)."""
    if compact:
        text = '{"fields":' + _dumps(answer["fields"]) + ',"rows":' + _rows_json(answer["rows"]) + "}"
    else:
        text = _rows_json(answer)
    return Response(text.encode("utf-8"), media_type="application/json")


@router.get("/history", dependencies=[Depends(rate_limit("history", 60, 60))])
async def get_history(
    state: StateDep,
    start_time: Optional[float] = Query(None, allow_inf_nan=False),
    end_time: Optional[float] = Query(None, allow_inf_nan=False),
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
            record_capped("history", "range")
    answer, count = await state.repository.get_history_answer(
        start_time=st,
        end_time=et,
        target_ids=target_ids,
        bucket_seconds=bucket_seconds,
        compact=compact,
    )
    if count >= HISTORY_ROW_CAP:
        record_capped("history", "rows")
    if count > HISTORY_THREAD_ROWS:
        response = await asyncio.to_thread(_encode, answer, compact)
    else:
        response = json_response(answer)
    record_answer("history", count, len(response.body))
    return response
