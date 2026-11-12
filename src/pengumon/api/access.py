"""The sign-in API (pengumon.access): who may open the dashboard, the first-run question, the password and the read token.

GET    /api/access                  {mode, from_environment, pending, https, read_token}: the rule in force, never the
                                    password or the read token (read_token: whether one exists)
POST   /api/access/setup            {choice: "open" | "password", password?, viewers?}: the first-run question, once
PUT    /api/access/password         {password, current_password?}: set or change the password (every other browser is
                                    signed out)
DELETE /api/access/password         {current_password?}: back to no password
POST   /api/access/read-token       {current_password?}: make (or replace) the read token: {read_token} (shown once)
DELETE /api/access/read-token       {current_password?}: remove the read token

The password is in no answer but the one that set it (shown once, with Copy); a lost one is replaced by setting a new
one from a signed-in browser, or by the recovery in docs/security.md. The same for the read token.

Threat model: a browser left signed in, or a copied cookie, cannot take the dashboard over, open it to everyone or plant
access that outlives it; after the owner changes the password, nothing made with the old sign-in still works. So:
* Changing or removing a password set on the page needs the current one in the body (`current_password`), whoever asks.
* So does making, replacing or removing the read token, and turning on viewing without the password (a Settings save of
  `access.allow_viewers`, api/settings.py), whenever a password is in force, the environment's included: the
  middleware holds that one as text and checks it the same way (the page can change neither it nor the stored one's
  source, only these). Only the person who knows the password can leave a read token or open the dashboard to viewers.
* A new password, and removing the password, remove the read token (pengumon.access): it may have been made by someone
  who knew the old one. Signing out everywhere ends the browsers' sign-ins only: the read token is no sign-in, and a
  cookie alone cannot make one. A new PENGUMON_ACCESS_TOKEN does not touch it either (the page cannot see that the
  variable changed): remove or replace it in Settings > Sign-in (docs/security.md).
Missing or wrong: 403 with a sentence; a wrong one counts as a failed sign-in of this address (homeguard's throttle:
after 5 in a minute, 429 without checking). Not asked for the first-run question (it sets the first password), nor
while there is no password.

Every non-GET needs the password once one is set (homeguard; the read token is refused). The setup and PUT answers sign
this browser in (a session cookie with the new password), so nobody is locked out by their own choice. Refusals:
{"detail": "<sentence>"}. A 422 never repeats what was sent (main._validation_error).
"""

from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from pengumon.access.manager import LOCKED, STORED, AccessManager, AccessRefused
from pengumon.api.common import StateDep
from pengumon.observability import get_logger
from pengumon.security import confirm_token, rate_limit, session_cookie
from pengumon.state import AppState

router = APIRouter(prefix="/api")
_log = get_logger("pengumon.access")

_RATE = rate_limit("access", 10, 60)
_LIMIT = 1024  # characters accepted in a password field before the manager's own check (128) says what is wrong
NEED_CURRENT = "Enter the current password to change or remove it."
NEED_CURRENT_TOKEN = "Enter the current password to make, replace or remove the read token."
NEED_CURRENT_VIEWERS = "Enter the current password to let others look without it."
WRONG_CURRENT = "The current password is not right."


class SetupBody(BaseModel):
    choice: Literal["open", "password"]
    password: Optional[str] = Field(None, max_length=_LIMIT)
    viewers: bool = False  # "Others may look but not change anything" (the access.allow_viewers setting)


class PasswordBody(BaseModel):
    password: str = Field(max_length=_LIMIT)
    current_password: Optional[str] = Field(None, max_length=_LIMIT)


class RemoveBody(BaseModel):
    current_password: Optional[str] = Field(None, max_length=_LIMIT)


ReadTokenBody = RemoveBody  # {current_password?}


def _access(state: AppState) -> AccessManager:
    if state.access is None:
        raise HTTPException(status_code=503, detail="The sign-in is not ready yet. Try again in a moment.")
    return state.access


def _refused(e: AccessRefused) -> JSONResponse:
    return JSONResponse(status_code=e.status, content={"detail": e.detail})


async def unconfirmed(request: Request, given: Optional[str], need: str) -> Optional[str]:
    """None when `given` is the password in force (homeguard's confirm_token: the stored hash or the environment's
    text), or there is none; otherwise why not, for a 403: `need` when nothing was given. A wrong one is counted by
    homeguard's throttle; while this address is throttled, confirm_token raises homeguard's 429 instead."""
    if not request.app.state.security_config.token_set:
        return None
    if not given:
        return need
    if not await confirm_token(request, given):
        return WRONG_CURRENT
    return None


async def _confirmed(request: Request, given: Optional[str], need: str) -> Optional[JSONResponse]:
    why = await unconfirmed(request, given, need)
    return None if why is None else JSONResponse(status_code=403, content={"detail": why})


async def _current_refused(request: Request, access: AccessManager, given: Optional[str]) -> Optional[JSONResponse]:
    """For changing or removing the password: asked when it was set on the page (none yet: setting the first; the
    environment's or a locked one: the manager refuses the change anyway, with a better sentence)."""
    if access.source != STORED:
        return None
    return await _confirmed(request, given, NEED_CURRENT)


def _signed_in_answer(request: Request, body: dict) -> JSONResponse:
    """`body` with a session cookie for the new password, so this browser stays signed in."""
    response = JSONResponse(body)
    response.headers["set-cookie"] = session_cookie(request.app.state.security_config)
    return response


@router.get("/access")
async def get_access(request: Request, state: StateDep):
    """The rule in force: `mode` "open" (no password), "password", or "locked" (secrets.json is damaged and the password
    cannot be read); `from_environment` when PENGUMON_ACCESS_TOKEN sets it; `pending` while the first-run question is
    unanswered; `read_token` whether a read token exists."""
    access = _access(state)
    mode = "open" if access.source is None else "locked" if access.source == LOCKED else "password"
    return {"mode": mode, "from_environment": access.source == "env", "pending": access.pending,
            "https": request.url.scheme == "https", "read_token": access.read_token_set}


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
    access = _access(state)
    refused = await _current_refused(request, access, body.current_password)
    if refused is not None:
        return refused
    had_token = access.read_token_set
    try:
        password = await access.set_password(body.password)
    except AccessRefused as e:
        return _refused(e)
    return _signed_in_answer(request, {"mode": "password", "password": password, "read_token_removed": had_token})


@router.delete("/access/password", dependencies=[Depends(_RATE)])
async def remove_password(request: Request, state: StateDep, body: Optional[RemoveBody] = None):
    access = _access(state)
    refused = await _current_refused(request, access, body.current_password if body else None)
    if refused is not None:
        return refused
    had_token = access.read_token_set
    try:
        await access.remove_password()
    except AccessRefused as e:
        return _refused(e)
    return {"mode": "open", "read_token_removed": had_token}


@router.post("/access/read-token", dependencies=[Depends(_RATE)])
async def new_read_token(request: Request, state: StateDep, body: Optional[ReadTokenBody] = None):
    """Makes the read token (or replaces the one there was: it stops working at once). Shown once."""
    access = _access(state)
    refused = await _confirmed(request, body.current_password if body else None, NEED_CURRENT_TOKEN)
    if refused is not None:
        return refused
    try:
        token = await access.new_read_token()
    except AccessRefused as e:
        return _refused(e)
    return {"read_token": token}


@router.delete("/access/read-token", dependencies=[Depends(_RATE)])
async def remove_read_token(request: Request, state: StateDep, body: Optional[ReadTokenBody] = None):
    access = _access(state)
    refused = await _confirmed(request, body.current_password if body else None, NEED_CURRENT_TOKEN)
    if refused is not None:
        return refused
    try:
        await access.remove_read_token()
    except AccessRefused as e:
        return _refused(e)
    return {"read_token": False}
