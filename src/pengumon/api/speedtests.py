"""GET /api/speedtests: the speed test's state and its recent tests; POST /api/speedtests/run: start one now.

Both always answer, whether or not the test is on (docs/speedtest.md): off, `enabled` is false and the list holds
whatever was recorded while it was on. The page polls the GET; nothing is pushed.
"""

import time
from typing import Any, Dict

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse, Response

from pengumon.api.common import StateDep, json_response
from pengumon.security import rate_limit
from pengumon.speedtest.quality import confidence, headline
from pengumon.speedtest.runner import MB, grade_of

router = APIRouter(prefix="/api")

MAX_HOURS = 8760


def _test(row: Dict[str, Any]) -> Dict[str, Any]:
    level, note = confidence(row)
    best = headline(row)
    return {**row, "bufferbloat": grade_of(row), "confidence": level, "note": note,
            "headline_down_mbps": best["down_mbps"], "headline_up_mbps": best["up_mbps"],
            "down_measured_by": best["down_by"], "up_measured_by": best["up_by"], "measured_by": best["measured_by"],
            "traffic_note": best["note"]}


@router.get("/speedtests")
async def get_speedtests(state: StateDep, hours: int = Query(720, ge=1, le=MAX_HOURS),
                         limit: int = Query(100, ge=1, le=500)) -> Response:
    """`{enabled, provider, next_run, running, month_used_mb, monthly_cap_mb, plan: {down_mbps, up_mbps}, tests}`: the
    tests of the last `hours` (newest first, at most `limit`), each with its `bufferbloat` grade (A+ to F, null without
    latency data), its `confidence` ("ok", or "low" when a direction lasted too short to measure the line: `note` says
    which and how much data per test would do; null without a speed; speedtest/quality.py), and its headline speeds
    (`headline_down_mbps`, `headline_up_mbps`: the line's rate the router measured during the test when it gave one,
    else the test's own `down_mbps` / `up_mbps`; `down_measured_by`, `up_measured_by` and `measured_by`: "router" or
    "test"; `traffic_note`: when the test's own share was far below the line's, the household's traffic). `next_run`
    is when the next scheduled test may start (null while off); `plan` is Settings' "Your line" (null where unset)."""
    config, runner, repository = state.config, state.speedtest, state.repository
    cfg = config.speedtest if config is not None else None
    now = time.time()
    tests = await repository.get_speed_tests(now - hours * 3600.0, now, limit) if repository is not None else []
    return json_response({
        "enabled": bool(cfg and cfg.enabled),
        "provider": cfg.provider if cfg else None,
        "next_run": await runner.next_run() if runner is not None else None,
        "running": bool(runner and runner.running),
        "month_used_mb": round(await runner.month_used_bytes() / MB, 1) if runner is not None else 0,
        "monthly_cap_mb": cfg.monthly_cap_mb if cfg else 0,
        "plan": {"down_mbps": config.line.down_mbps if config else None, "up_mbps": config.line.up_mbps if config else None},
        "tests": [_test(t) for t in tests],
    })


@router.post("/speedtests/run", status_code=202, dependencies=[Depends(rate_limit("speedtest_run", 3, 600))])
async def run_speedtest(state: StateDep) -> Response:
    """Starts a manual test in the background: 202 `{"started": true}`. 409 `{"started": false, "reason": ...}` when it
    is refused: the test is off, one is already running, or this month's data cap is reached. 503 before the app has
    started."""
    if state.speedtest is None:
        return JSONResponse({"started": False, "reason": "the speed test is not running"}, status_code=503)
    reason = await state.speedtest.run_now()
    if reason is not None:
        return JSONResponse({"started": False, "reason": reason}, status_code=409)
    return JSONResponse({"started": True}, status_code=202)
