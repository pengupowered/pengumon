"""The reliability report, the loss-by-hour grid and the uptime strips (built in pengumon.reports)."""

from typing import Literal

from fastapi import APIRouter, HTTPException, Query

from pengumon.api.common import StateDep
from pengumon.engine.path_rules import ignoring_ping_ids
from pengumon.reports.loss_grid import build_loss_grid
from pengumon.reports.reliability import build_report
from pengumon.reports.uptime import build_uptime

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
