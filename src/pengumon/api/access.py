"""The sign-in API (pengumon.access): who may open the dashboard, the first-run question and the password.

GET    /api/access                  {mode, from_environment, pending, https}: the rule in force, never the password
POST   /api/access/setup            {choice: "open" | "password", password?, viewers?}: the first-run question, once
PUT    /api/access/password         {password}: set or change the password (every other browser is signed out)
DELETE /api/access/password         back to no password

The password is in no answer but the one that set it (shown once, with Copy); a lost one is replaced by setting a new
one from a signed-in browser, or by the recovery in docs/security.md.

Every non-GET needs the password once one is set (homeguard). The setup and PUT answers sign this browser in (a session
cookie with the new password), so nobody is locked out by their own choice. Refusals: {"detail": "<sentence>"}. A 422
never repeats what was sent (main._validation_error).
"""

from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from pengumon.access.manager import LOCKED, AccessManager, AccessRefused
from pengumon.api.common import StateDep
from pengumon.observability import get_logger
from pengumon.security import rate_limit, session_cookie
from pengumon.state import AppState

router = APIRouter(prefix="/api")
_log = get_logger("pengumon.access")

_RATE = rate_limit("access", 10, 60)
_LIMIT = 1024  # characters accepted in a password field before the manager's own check (128) says what is wrong


class SetupBody(BaseModel):
    choice: Literal["open", "password"]
    password: Optional[str] = Field(None, max_length=_LIMIT)
    viewers: bool = False  # "Others may look but not change anything" (the access.allow_viewers setting)


class PasswordBody(BaseModel):
    password: str = Field(max_length=_LIMIT)


def _access(state: AppState) -> AccessManager:
    if state.access is None:
        raise HTTPException(status_code=503, detail="The sign-in is not ready yet. Try again in a moment.")
    return state.access


def _refused(e: AccessRefused) -> JSONResponse:
    return JSONResponse(status_code=e.status, content={"detail": e.detail})


def _signed_in_answer(request: Request, body: dict) -> JSONResponse:
    """`body` with a session cookie for the new password, so this browser stays signed in."""
    response = JSONResponse(body)
    response.headers["set-cookie"] = session_cookie(request.app.state.security_config)
    return response


@router.get("/access")
async def get_access(request: Request, state: StateDep):
    """The rule in force: `mode` "open" (no password), "password", or "locked" (secrets.json is damaged and the password
    cannot be read); `from_environment` when PENGUMON_ACCESS_TOKEN sets it; `pending` while the first-run question is
    unanswered."""
    access = _access(state)
    mode = "open" if access.source is None else "locked" if access.source == LOCKED else "password"
    return {"mode": mode, "from_environment": access.source == "env", "pending": access.pending,
            "https": request.url.scheme == "https"}


@router.post("/access/setup", dependencies=[Depends(_RATE)])
async def answer_setup(body: SetupBody, request: Request, state: StateDep):
    """The first-run question. 409 once it has been answered (by anyone: it is asked once)."""
    access = _access(state)
    try:
        password = await access.answer_setup(body.choice, body.password)
    except AccessRefused as e:
        return _refused(e)
    if password is None:
        return {"mode": "open"}
    answer = {"mode": "password", "password": password, "viewers_saved": not body.viewers}
    if body.viewers and state.settings is not None:
        try:  # a settings revision of its own, as if saved on the Settings page
            await state.settings.apply(state.settings.revision, {"access.allow_viewers": True}, {})
            answer["viewers_saved"] = True
        except Exception as e:  # the password is in force; only the viewers part is missing
            _log.warning(f"Sign-in: viewing without the password could not be turned on: {type(e).__name__}")
    return _signed_in_answer(request, answer)


@router.put("/access/password", dependencies=[Depends(_RATE)])
async def set_password(body: PasswordBody, request: Request, state: StateDep):
    try:
        password = await _access(state).set_password(body.password)
    except AccessRefused as e:
        return _refused(e)
    return _signed_in_answer(request, {"mode": "password", "password": password})


@router.delete("/access/password", dependencies=[Depends(_RATE)])
async def remove_password(state: StateDep):
    try:
        await _access(state).remove_password()
    except AccessRefused as e:
        return _refused(e)
    return {"mode": "open"}

