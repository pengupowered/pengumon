"""Server-side alert channels (ntfy and webhooks): which are configured, and a test message through all of them."""

from fastapi import APIRouter, Depends, HTTPException, Request

from pengumon.api.common import StateDep
from pengumon.security import can_edit, rate_limit

router = APIRouter(prefix="/api")


@router.get("/notifications")
async def get_notifications(request: Request, state: StateDep):
    """The configured channels: name, kind (ntfy or webhook), format and host. Never a URL path, query or header. For
    someone who may not change settings (a viewer, the read token) the host is null, as GET /api/settings keeps the
    host of a secret address from them: the names and kinds say what is set up."""
    channels = state.notifier.channels() if state.notifier else []
    if not can_edit(request):
        channels = [{**c, "host": None} for c in channels]
    return {"channels": channels}


@router.post("/notifications/test", dependencies=[Depends(rate_limit("notify_test", 6, 60))])
async def send_test_notification(state: StateDep):
    """Sends a test message to every channel and returns how each went (the reason is a short word, not a URL)."""
    if state.notifier is None or not state.notifier.channels():
        raise HTTPException(status_code=409, detail="No ntfy topic or webhook is configured")
    return {"channels": await state.notifier.send_test()}
