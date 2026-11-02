"""GET /api/targets, POST /api/targets and DELETE /api/targets/{id}: the probe targets and their statistics.

POST and DELETE edit the stored probe path (engine/path.py) one hop at a time, for the add-target dialog and the
trace dialog's Monitor button; the path editor (api/path.py) edits the whole path."""

from typing import List

from fastapi import APIRouter, Depends, HTTPException

from pengumon.api.common import StateDep
from pengumon.engine.path import PathError, path_manager
from pengumon.models.probes import TargetConfig, TargetStats
from pengumon.security import rate_limit

router = APIRouter(prefix="/api")


@router.get("/targets", response_model=List[TargetStats])
async def get_targets(state: StateDep):
    """Returns current WinMTR-style statistics for all monitored targets."""
    if not state.prober:
        raise HTTPException(status_code=503, detail="Prober engine initializing")
    return list(state.prober.stats.values())


@router.post("/targets", response_model=TargetConfig, dependencies=[Depends(rate_limit("targets", 20, 60))])
async def add_target(target: TargetConfig, state: StateDep):
    """Adds a target at the end of the probe path. It is stored in the database and probed from the next round."""
    manager = path_manager(state)
    if manager is None:
        raise HTTPException(status_code=503, detail="Engine not ready")

    def append(hops: List[TargetConfig]) -> List[TargetConfig]:
        if any(t.id == target.id for t in hops):
            raise PathError("Target ID already exists")
        return [*hops, target]

    try:
        await manager.edit(append)
    except PathError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None
    return target


@router.delete("/targets/{target_id}", dependencies=[Depends(rate_limit("targets", 20, 60))])
async def delete_target(target_id: str, state: StateDep):
    """Removes a target from the probe path: it is no longer probed, and its history is kept but hidden (the path
    editor can delete it)."""
    manager = path_manager(state)
    if manager is None:
        raise HTTPException(status_code=503, detail="Engine not ready")

    def remove(hops: List[TargetConfig]) -> List[TargetConfig]:
        if not any(t.id == target_id for t in hops):
            raise LookupError(target_id)
        return [t for t in hops if t.id != target_id]

    try:
        await manager.edit(remove)
    except LookupError:
        raise HTTPException(status_code=404, detail="Unknown target") from None
    return {"message": f"Target {target_id} removed"}
