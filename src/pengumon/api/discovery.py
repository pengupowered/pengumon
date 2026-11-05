"""POST /api/discovery and GET /api/discovery/{id}: start a discovery run, and read how it is going and its draft.

Discovery never applies anything: the draft is for the path editor, which applies it with PUT /api/path. Starting a
run is rate limited and single flight (a second start while one runs returns the running id).
"""

from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Path
from pydantic import BaseModel, ConfigDict

from pengumon.api.common import StateDep
from pengumon.engine.discovery.engine import DiscoveryOptions
from pengumon.security import rate_limit

router = APIRouter(prefix="/api")


class DiscoveryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asn_lookup: bool = True  # ask Team Cymru who operates each public hop (sends those addresses; docs/discovery.md)
    snmp: bool = False  # SNMP "public" sysDescr of the private hops
    device_checks: bool = True  # web pages of the private hops and 192.168.100.1, UPnP IGD
    first_run: bool = False  # the dashboard's automatic run: once per server process, only while no path is applied


@router.post("/discovery", dependencies=[Depends(rate_limit("discovery", 6, 60))])
async def start_discovery(state: StateDep, req: Optional[DiscoveryRequest] = Body(default=None)):
    """Starts discovering the path (or returns the run in progress): `{"id": "..."}`. With `first_run` (the
    dashboard's own run, asked only while no path is applied) at most one run starts per server process; later calls
    return that run's id, or `{"id": null}` once it is gone."""
    if state.discovery is None:
        raise HTTPException(status_code=503, detail="Discovery not initialized")
    req = req or DiscoveryRequest()
    options = DiscoveryOptions(asn_lookup=req.asn_lookup, device_checks=req.device_checks, snmp=req.snmp)
    if req.first_run:
        return {"id": state.discovery.start_first_run(options)}
    return {"id": state.discovery.start(options)}


@router.get("/discovery/{job_id}")
async def get_discovery(state: StateDep, job_id: str = Path(..., max_length=64)):
    """A run's state (`running`, `done`, `failed`), its progress in words, its draft (`{"hops": [...]}`, empty until
    done) and notes. 404 for an unknown id, or one finished more than ten minutes ago."""
    if state.discovery is None:
        raise HTTPException(status_code=503, detail="Discovery not initialized")
    job = state.discovery.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown discovery")
    return job
