"""GET /api/targets, POST /api/targets and DELETE /api/targets/{id}: the probe targets and their statistics."""

from typing import List

from fastapi import APIRouter, Depends, HTTPException

from pengumon.api.common import StateDep
from pengumon.db.targets import MAX_CUSTOM_TARGETS
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
    """Adds a custom target to the probe list. It is stored in the database, so it survives restarts."""
    if not state.config or not state.prober or not state.repository:
        raise HTTPException(status_code=503, detail="Engine not ready")

    # Check if ID exists
    for t in state.config.targets:
        if t.id == target.id:
            raise HTTPException(status_code=400, detail="Target ID already exists")

    if not await state.repository.add_custom_target(target):
        if await state.repository.count_custom_targets() >= MAX_CUSTOM_TARGETS:
            raise HTTPException(status_code=400, detail=f"At most {MAX_CUSTOM_TARGETS} custom targets are allowed")
        raise HTTPException(status_code=400, detail="Target ID already exists")

    state.config.targets.append(target)
    state.prober.register_target(target)
    return target


@router.delete("/targets/{target_id}")
async def delete_target(target_id: str, state: StateDep):
    """Removes a custom target (and forgets it across restarts).

    Targets from config.yaml are refused with 400: they are the configured hop layout the classifier relies
    on, and the file would bring them back at the next start anyway. Set `enabled: false` there instead.
    """
    if not state.config or not state.prober or not state.repository:
        raise HTTPException(status_code=503, detail="Engine not ready")

    if not await state.repository.delete_custom_target(target_id):
        if any(t.id == target_id for t in state.config.targets):
            raise HTTPException(
                status_code=400,
                detail="Targets defined in config.yaml cannot be deleted here; set enabled: false in the file",
            )
        raise HTTPException(status_code=404, detail="Unknown target")

    state.config.targets[:] = [t for t in state.config.targets if t.id != target_id]  # in place: the classifier shares the list
    state.prober.stats.pop(target_id, None)
    return {"message": f"Target {target_id} removed"}
