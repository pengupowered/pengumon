"""Operations endpoints: host info, overall status, liveness, on-demand traceroute and the Prometheus /metrics."""

import asyncio
import time
from typing import List

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response
from fastapi.responses import PlainTextResponse

from pengumon import __version__
from pengumon.api.common import StateDep
from pengumon.engine.mtr import MtrEngine, TracerouteHop
from pengumon.engine.netinfo import get_lan_ips
from pengumon.metrics import render_metrics
from pengumon.incidents.model import SystemDiagnosis
from pengumon.models.probes import TracerouteRequest
from pengumon.security import can_edit, rate_limit, session_status

HEALTH_MAX_ROUND_AGE = 10.0

router = APIRouter(prefix="/api")
metrics_router = APIRouter()  # /metrics is outside /api


@router.get("/info")
async def get_network_info(request: Request, state: StateDep):
    """Returns the host machine's LAN IP addresses and access URLs, whether this browser may sign out (`session`:
    signed in with the access token's cookie; and "everywhere", which ends every browser's sign-in), whether this
    browser may change anything (`can_edit`: false for a viewer without the token, PENGUMON_VIEW_WITHOUT_TOKEN), and how
    probes are sent (`probe`: the ICMP backend and its RTT resolution in ms)."""
    port = state.config.server.port if state.config else 8000
    lan_ips = await asyncio.to_thread(get_lan_ips)  # a host name lookup: never on the event loop
    lan_urls = [f"http://{ip}:{port}" for ip in lan_ips]
    return {
        "port": port,
        "lan_ips": lan_ips,
        "primary_lan_ip": lan_ips[0] if lan_ips else "127.0.0.1",
        "lan_urls": lan_urls,
        "primary_lan_url": lan_urls[0] if lan_urls else f"http://127.0.0.1:{port}",
        "isp_name": state.config.line.isp_name if state.config else None,
        "session": session_status(request),
        "can_edit": can_edit(request),
        # How probes are sent: {"backend", "resolution_ms", "error", "fix"} (NetworkProber.probe_method)
        "probe": state.prober.probe_method() if state.prober else None,
    }


@router.get("/status", response_model=SystemDiagnosis)
async def get_system_status(state: StateDep):
    """Returns current network health, root-cause diagnosis, and active incident."""
    if not state.classifier:
        raise HTTPException(status_code=503, detail="Classifier engine initializing")
    return state.classifier.last_diagnosis


@router.post("/mtr/trace", response_model=List[TracerouteHop], dependencies=[Depends(rate_limit("mtr", 6, 60))])
async def run_traceroute(state: StateDep, req: TracerouteRequest = Body(...)):
    """Triggers an on-demand hop-by-hop traceroute to discover path routers with validation."""
    known_targets = state.config.targets if state.config else None
    engine = MtrEngine(target_host=req.target_host, known_targets=known_targets, family=req.family)
    return await engine.run_trace()


@router.get("/health")
async def get_health(response: Response, state: StateDep):
    """Liveness for the container HEALTHCHECK: 200 while the probe loop keeps finishing rounds."""
    last = getattr(state.prober, "last_round_ts", None) if state.prober else None
    if last is None:
        reason = "no probe round has finished yet"
    elif time.time() - last >= HEALTH_MAX_ROUND_AGE:
        reason = f"last probe round finished {time.time() - last:.0f} s ago"
    else:
        return {"status": "ok", "version": __version__}
    response.status_code = 503
    return {"status": "unhealthy", "reason": reason, "version": __version__}


@metrics_router.get("/metrics")
async def get_metrics(state: StateDep):
    """Prometheus text exposition."""
    return PlainTextResponse(await render_metrics(state), media_type="text/plain; version=0.0.4; charset=utf-8")
