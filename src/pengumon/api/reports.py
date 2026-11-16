"""The reliability report, the loss-by-hour grid, the uptime strips and the latency spikes (built in pengumon.reports).

Every route that puts times in days or hours takes `tz` (the viewer's IANA zone) and `tz_offset_minutes` (minutes east
of UTC, the fallback of older pages); the zone used is the one set in Settings, else `tz`, else the offset, else UTC
(timeofday.viewer_zone), and days and hours follow its daylight saving.

The report, the loss grid, the uptime strips and the latency spikes are kept for the rest of the minute
(reports/cache.py): the dashboard asks for them at every page load, and they change slowly. A kept answer is dropped as
soon as the path is applied (or a removed hop's history deleted), a setting is saved or an incident (or a latency spike)
changes. Only a few are built at once (the
cache's MAX_BUILDS), so that answers asked for with ever-changing parameters cannot take every database reader; and the
answers that are not kept count against a rate limit per client (REPORTS_RATE; a kept one is free)."""

import time
from typing import Annotated, Any, Awaitable, Callable, Hashable, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from pengumon.api.common import StateDep, json_response
from pengumon.engine.path_rules import ignoring_ping_ids
from pengumon.observability.telemetry import ai_pack_exports, api_rate_limited
from pengumon.reports.common import pack_building
from pengumon.reports.loss_grid import build_loss_grid
from pengumon.reports.reliability import build_report
from pengumon.reports.spikes import spike_summary
from pengumon.reports.uptime import build_uptime
from pengumon.security import rate_limit
from pengumon.timeofday import viewer_zone

router = APIRouter(prefix="/api")

# Answers built (not kept) for one client, in one bucket for the three routes: a page load asks for three, a minute
# later three again, and once more after each saved setting or new incident.
REPORTS_RATE = rate_limit("reports", 60, 60)


async def _kept(state, request: Request, key: Hashable, build: Callable[[], Awaitable[Any]]) -> Any:
    """state.answers.get with the stamp of what these answers depend on besides the clock and their parameters, and
    the rate limit on a miss."""
    repository, config = state.repository, state.config
    stamp = (id(repository), repository.path_changes, repository.incidents_version,
             getattr(state.settings, "revision", None),
             tuple((t.id, t.name) for t in getattr(config, "targets", None) or ()))
    return await state.answers.get(key, stamp, build, admit=lambda: REPORTS_RATE(request))


TzOffset = Annotated[int, Query(ge=-840, le=840,
                                description="minutes east of UTC: the fallback when Settings and `tz` name no zone")]
TzName = Annotated[Optional[str], Query(max_length=64, pattern=r"^[A-Za-z0-9_+\-/]+$",
                                        description="the viewer's IANA zone (America/New_York), when Settings has "
                                                    "none; one the server does not know is ignored")]


@router.get("/report")
async def get_reliability_report(
    state: StateDep,
    request: Request,
    days: int = Query(30, ge=1, le=400),
    tz_offset_minutes: TzOffset = 0,
    tz: TzName = None,
):
    """Per-day availability per hop, incidents by cause (expected ones excluded) and the worst hours of the day (the
    hops that ignore ping and the hops removed from the path left out, as in the loss grid)."""
    if not state.repository:
        raise HTTPException(status_code=503, detail="Repository not initialized")
    zone, ignoring = viewer_zone(state.config, tz, tz_offset_minutes)[0], ignoring_ping_ids(state.prober)
    return await _kept(state, request, ("report", days, zone, frozenset(ignoring)),
                       lambda: build_report(state.repository, state.config, days, tz=zone, ignoring=ignoring))


@router.get("/report/loss-grid")
async def get_loss_grid(
    state: StateDep,
    request: Request,
    days: int = Query(7, ge=1, le=3650),
    tz_offset_minutes: TzOffset = 0,
    tz: TzName = None,
):
    """Probe loss per calendar day and hour of the last `days` days, for the "when it breaks" heatmap: rows are days
    (oldest first), columns the 24 hours; sent, lost and loss_pct per cell (loss_pct null where nothing was sent);
    when each cell begins (day_starts, hour_starts) and the zone used. Up to 31 days only; more are folded into 7 rows,
    Monday to Sunday (`fold: "weekday"`, `days` the number asked), each hour the sum of that weekday-hour over the window
    (up to the probe retention; 3650 at most)."""
    if not state.repository:
        raise HTTPException(status_code=503, detail="Repository not initialized")
    zone, ignoring = viewer_zone(state.config, tz, tz_offset_minutes)[0], ignoring_ping_ids(state.prober)
    return await _kept(state, request, ("loss-grid", days, zone, frozenset(ignoring)),
                       lambda: build_loss_grid(state.repository, days, tz=zone, ignoring=ignoring))


@router.get("/uptime")
async def get_uptime(
    state: StateDep,
    request: Request,
    window: Literal["24h", "7d", "30d", "90d", "all"] = Query("24h"),
    tz_offset_minutes: TzOffset = 0,
    tz: TzName = None,
):
    """Availability strips for every target in one answer: one status per block (24h: 96 blocks of 15 min; 7d, 30d,
    90d: calendar days; all: the whole probe retention, from the oldest stored probe, in calendar days up to 90 days
    and in local weeks beginning Monday beyond; `cell` says which: "15m", "day" or "week") of ok, degraded, outage, busy (explained by line load) or no_data, plus the availability over the
    window. From the rollups; each block's start and end (bounds) and the zone used."""
    if not state.repository:
        raise HTTPException(status_code=503, detail="Repository not initialized")
    zone = viewer_zone(state.config, tz, tz_offset_minutes)[0]
    return await _kept(state, request, ("uptime", window, zone),
                       lambda: build_uptime(state.repository, state.config, window, tz=zone))


SPIKE_MAX_DAYS = 3650  # incidents kept forever: the longest range a route answers (js/ranges.js FOREVER_DAYS)


@router.get("/latency-spikes")
async def get_latency_spikes(
    state: StateDep,
    request: Request,
    hours: int = Query(168, ge=1, le=SPIKE_MAX_DAYS * 24),
    days: Optional[int] = Query(None, ge=1, le=SPIKE_MAX_DAYS, description="`days` N means 24 * N hours and wins"),
    tz_offset_minutes: TzOffset = 0,
    tz: TzName = None,
):
    """Brief latency spikes (latency or jitter with no loss that ended before
    classifier.latency_incident_after_seconds, so not incidents) over the rolling window of the last `hours` hours
    (`days` N: 24 * N hours, and it wins), ending now: the count (expected ones left out, counted in `expected`), the
    split by stage, and the count per local calendar day (the first and last `partial`, with the `hours` they hold).
    Days in the zone of /api/report. Kept and rate limited like the report (docs/api.md)."""
    if not state.repository:
        raise HTTPException(status_code=503, detail="Repository not initialized")
    seconds = (days * 24 if days is not None else hours) * 3600
    zone = viewer_zone(state.config, tz, tz_offset_minutes)[0]

    async def build():
        end = time.time()
        return await spike_summary(state.repository, state.config, end - seconds, end, zone)

    return await _kept(state, request, ("latency-spikes", seconds, zone), build)


@router.get("/ai-pack", dependencies=[Depends(rate_limit("ai_pack", 6, 60))])
async def get_ai_pack(
    state: StateDep,
    scope: Literal["window", "incident"] = Query("window"),
    hours: int = Query(24, ge=1, le=720, description="the window for scope=window: 1, 24, 168 or 720 in the dashboard"),
    incident_id: Optional[int] = Query(None, ge=1, description="required for scope=incident, refused otherwise"),
    level: Literal["full", "vague", "anonymized"] = Query("full"),
    detail: Literal["short", "detailed"] = Query("short"),
    tz_offset_minutes: TzOffset = 0,
    tz: TzName = None,
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
    if pack_building.locked():
        api_rate_limited.add(1, {"bucket": "ai_pack_busy"})
        raise HTTPException(status_code=429, detail="Another pack is being built; try again in a few seconds")
    from pengumon.reports.contributions import build_ai_pack  # imported when asked: nothing loads without it

    async with pack_building:
        pack = await build_ai_pack(
            state.repository, state.config, scope=scope, hours=hours, incident_id=incident_id, level=level,
            detail=detail, devices=tuple(state.config.devices) if state.config else (), live=state.devices,
            ignoring=ignoring_ping_ids(state.prober), tz_offset_minutes=tz_offset_minutes, prober=state.prober,
            tz_name=tz)
    ai_pack_exports.add(1, {"level": level, "scope": scope})
    return json_response(pack)
