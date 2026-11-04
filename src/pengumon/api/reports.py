"""The reliability report, the loss-by-hour grid and the uptime strips (built in pengumon.reports)."""

import asyncio
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from pengumon.api.common import StateDep, json_response
from pengumon.engine.path_rules import ignoring_ping_ids
from pengumon.observability.telemetry import ai_pack_exports, api_rate_limited
from pengumon.reports.loss_grid import build_loss_grid
from pengumon.reports.reliability import build_report
from pengumon.reports.uptime import build_uptime
from pengumon.security import rate_limit

router = APIRouter(prefix="/api")


@router.get("/report")
async def get_reliability_report(
    state: StateDep,
    days: int = Query(30, ge=1, le=400),
    tz_offset_minutes: int = Query(0, ge=-840, le=840),
):
    """Per-day availability per hop, incidents by cause (expected ones excluded) and the worst hours of the day."""
    if not state.repository:
        raise HTTPException(status_code=503, detail="Repository not initialized")
    return await build_report(state.repository, state.config, days, tz_offset_minutes=tz_offset_minutes)


@router.get("/report/loss-grid")
async def get_loss_grid(
    state: StateDep,
    days: int = Query(7, ge=1, le=31),
    tz_offset_minutes: int = Query(0, ge=-840, le=840),
):
    """Probe loss per calendar day and hour of the last `days` days, for the "when it breaks" heatmap: rows are days
    (oldest first), columns the 24 hours; sent, lost and loss_pct per cell (loss_pct null where nothing was sent)."""
    if not state.repository:
        raise HTTPException(status_code=503, detail="Repository not initialized")
    return await build_loss_grid(state.repository, days, tz_offset_minutes=tz_offset_minutes,
                                 ignoring=ignoring_ping_ids(state.prober))


@router.get("/uptime")
async def get_uptime(
    state: StateDep,
    window: Literal["24h", "7d", "30d"] = Query("24h"),
    tz_offset_minutes: int = Query(0, ge=-840, le=840),
):
    """Availability strips for every target in one answer: one status per block (24h: 96 blocks of 15 min; 7d, 30d:
    calendar days) of ok, degraded, outage, busy (explained by line load) or no_data, plus the availability over the
    window. From the per-minute rollups."""
    if not state.repository:
        raise HTTPException(status_code=503, detail="Repository not initialized")
    return await build_uptime(state.repository, state.config, window, tz_offset_minutes=tz_offset_minutes)


_pack_building = asyncio.Lock()  # one pack at a time: a build reads the whole window and every module's history


@router.get("/ai-pack", dependencies=[Depends(rate_limit("ai_pack", 6, 60))])
async def get_ai_pack(
    state: StateDep,
    scope: Literal["window", "incident"] = Query("window"),
    hours: int = Query(24, ge=1, le=168, description="the window for scope=window: 1, 24 or 168 in the dashboard"),
    incident_id: Optional[int] = Query(None, ge=1, description="required for scope=incident, refused otherwise"),
    level: Literal["full", "vague", "anonymized"] = Query("full"),
    detail: Literal["short", "detailed"] = Query("short"),
    tz_offset_minutes: int = Query(0, ge=-840, le=840),
    tz: Optional[str] = Query(None, max_length=64, pattern=r"^[A-Za-z0-9_+\-/]+$",
                              description="the viewer's IANA zone (America/New_York), when Settings has none"),
):
    """The AI troubleshooting pack: one Markdown document to paste into a chat model, built from the stored history.
    `markdown` is the exact text; `modules` says which module sections it includes (id, title, chars, budget_chars,
    trimmed) and `roles` is the "who is who" key (never part of the markdown). Anyone who may view the dashboard may
    ask for any level, Full included. Rate limited (6 a minute) and one build at a time (429 while another runs).
    Times are in the zone set in Settings (general.timezone), else in `tz`, else at `tz_offset_minutes`, else UTC; a
    `tz` the server does not know is ignored."""
    if not state.repository:
        raise HTTPException(status_code=503, detail="Repository not initialized")
    if (scope == "incident") != (incident_id is not None):
        raise HTTPException(status_code=422, detail="incident_id goes with scope=incident, and only with it")
    if incident_id is not None and await state.repository.get_incident_by_id(incident_id) is None:
        raise HTTPException(status_code=404, detail="Incident not found")
    if _pack_building.locked():
        api_rate_limited.add(1, {"bucket": "ai_pack_busy"})
        raise HTTPException(status_code=429, detail="Another pack is being built; try again in a few seconds")
    from pengumon.reports.ai_pack import build_ai_pack  # A2's module, imported when asked: nothing loads without it

    async with _pack_building:
        pack = await build_ai_pack(
            state.repository, state.config, scope=scope, hours=hours, incident_id=incident_id, level=level,
            detail=detail, devices=tuple(state.config.devices) if state.config else (), live=state.devices,
            ignoring=ignoring_ping_ids(state.prober), tz_offset_minutes=tz_offset_minutes, prober=state.prober,
            tz_name=tz)
    ai_pack_exports.add(1, {"level": level, "scope": scope})
    return json_response(pack)
