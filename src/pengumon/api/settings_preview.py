"""The outage-rules preview API (design.md section 5): start a replay of the stored samples with draft rules, poll it,
cancel it. The work is done by settings/preview.py (the job store and its limits) and engine/replay.py (the process).

POST   /api/settings/preview       {values, days}  ->  {"id": ...}   422 for a key the preview can't replay or a value
                                                                      out of range, 429 (Retry-After 30) while another
                                                                      replay runs, 503 before the app is up
GET    /api/settings/preview/{id}  ->  PreviewJob (state running | done | failed | canceled); polling keeps the job alive
DELETE /api/settings/preview/{id}  ->  PreviewJob as it was left; the job's process is gone when this answers
"""

import time
from typing import Dict

from fastapi import APIRouter, Depends, HTTPException, Path
from fastapi.responses import JSONResponse

from pengumon.api.common import StateDep
from pengumon.api.settings_models import PreviewRequest
from pengumon.security import rate_limit
from pengumon.settings.finite import MESSAGE, non_finite
from pengumon.settings.preview import PreviewBusy, PreviewInvalid, PreviewJob, build_spec

router = APIRouter(prefix="/api")

_ID = Path(..., max_length=64, pattern=r"^[A-Za-z0-9_-]+$")


def _jobs(state):
    if state.config is None or state.repository is None or state.preview is None:
        raise HTTPException(status_code=503, detail="Settings not initialized")
    return state.preview


@router.post("/settings/preview", dependencies=[Depends(rate_limit("settings_preview", 6, 60))])
async def start_preview(body: PreviewRequest, state: StateDep) -> Dict[str, str]:
    jobs = _jobs(state)
    where = non_finite(body.values)  # (a model validator's 422 would have to print the value: JSON cannot)
    if where:
        return JSONResponse(status_code=422, content={"detail": f"{where} {MESSAGE}",
                                                      "errors": [{"where": f"values.{where}", "message": MESSAGE}]})
    try:
        spec, key = build_spec(state, body.values, body.days, time.time())
    except PreviewInvalid as e:
        return JSONResponse(status_code=422, content={"detail": e.message, "errors": e.errors})
    await state.repository.flush_samples()  # the replay reads the file: what the probe loop still buffers is not in it
    try:
        job = jobs.start(key, spec)
    except PreviewBusy as e:
        raise HTTPException(status_code=429, detail="A preview is running already; try again in half a minute.",
                            headers={"Retry-After": str(e.retry_after)}) from None
    return {"id": job.id}


@router.get("/settings/preview/{preview_id}", response_model=PreviewJob)
async def get_preview(state: StateDep, preview_id: str = _ID):
    jobs = _jobs(state)
    job = jobs.get(preview_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown preview")
    return jobs.describe(job)


@router.delete("/settings/preview/{preview_id}", response_model=PreviewJob)
async def cancel_preview(state: StateDep, preview_id: str = _ID):
    jobs = _jobs(state)
    job = await jobs.cancel(preview_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown preview")
    return jobs.describe(job)
