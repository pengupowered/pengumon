"""GET /api/storage: what the database costs on disk, by kind of data (Settings > Retention shows it).

Only for someone who may edit, as the settings are (a viewer and the read token are refused twice: the path is in
VIEWER_POLICY.deny, and the handler asks `can_edit`). The answer is built by db/storage_stats.py and kept for a minute.
"""

from fastapi import APIRouter, HTTPException, Request

from pengumon.api.common import StateDep
from pengumon.db.storage_stats import storage_answer
from pengumon.security import can_edit

router = APIRouter(prefix="/api")


@router.get("/storage")
async def get_storage(request: Request, state: StateDep) -> dict:
    """{generated_at, took_seconds, estimate, fixed_days, kinds: [{id, label, setting, tables, bytes, days_stored,
    per_day, fixed, tiered, estimate}], total_bytes, free_bytes, file_bytes, wal_bytes, disk: {total, used, free}}.
    `kinds` are the seven kinds of data with a retention setting, "Full detail" (the fixed tiers) and "Other";
    `per_day` is the kind's bytes a day (measured over its last 7 days of rows), `fixed` the part a retention does not
    change, `estimate` true where the figure is rows times a sampled row size (a SQLite build without dbstat)."""
    if not can_edit(request):
        raise HTTPException(status_code=403, detail="Sign in with the password to see the storage")
    if not state.repository:
        raise HTTPException(status_code=503, detail="Repository not initialized")
    return await storage_answer(state.repository)
