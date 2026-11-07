"""The sign-in password and the first-run question (state.access).

The password is homeguard's access token. Sources, highest first:
  1. PENGUMON_ACCESS_TOKEN (containers and scripts): read at start; the page says so and cannot change it.
  2. secrets.json `access_token`, set on the page (the first-run question, or Settings > Sign-in), through the secrets
     store (never written over a damaged file).
Neither: no password; anyone who can reach the port may open the dashboard and change everything.

Applied without a restart: the app has one SecurityConfig (main.create_app), shared by the middleware and
app.state.security_config, and the middleware reads `access_token` and `viewers_allowed` on every request and every new
websocket. A change edits that one object in place, then closes the live connections: they were let in under the old
rule and are not checked again (only cookie-signed ones are, by homeguard). The new rule is in force before the closing
starts, so a page that connects meanwhile is checked by it; each closed page reconnects and is checked again (a browser
without the new password lands on the sign-in page). The token keys the session cookies, so a new password signs every
other browser out; the browser that set it gets a new cookie in the same answer.

The first-run question is asked while there is no password from either source and the database meta key
`setup.access_done` is missing. Its value is the last choice: "open", "password", or "env" (a token from the
environment answers it, so such an install never sees the question). A damaged or missing secrets.json while the last
choice was "password" keeps the dashboard locked behind an unknown password instead of opening it (fix or restore the
file, or set PENGUMON_ACCESS_TOKEN): a lost data volume or a wrong data folder must not open a dashboard its owner
closed, and asking the first-run question again would leave it open to whoever answers first. A readable secrets.json
without `access_token` after "password" means it was removed by hand, the documented way back in (docs/security.md):
the dashboard is open again.
"""

import asyncio
import secrets as random_source
from typing import Awaitable, Callable, Mapping, Optional

from pengumon.observability import get_logger
from pengumon.secrets import SECRETS_FILE_DAMAGED, SecretsFileUnusable, SecretStore
from pengumon.settings.env import env_value

_log = get_logger("pengumon.access")

SETUP_KEY = "setup.access_done"
TOKEN_ENV = "PENGUMON_ACCESS_TOKEN"
STORED_PATH = ("access_token",)  # in secrets.json
MIN_LENGTH, MAX_LENGTH = 8, 128

ENV, STORED, LOCKED = "env", "stored", "locked"  # where the password in force comes from (None: there is none)


class AccessRefused(Exception):
    """A change the sign-in cannot make; `detail` is a sentence for the page, `status` the HTTP status."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


def check_password(text) -> str:
    """The password to keep (surrounding spaces dropped), or AccessRefused(422) saying what is wrong with it."""
    if not isinstance(text, str) or not text.strip():
        raise AccessRefused(422, "Enter a password.")
    text = text.strip()
    if len(text) < MIN_LENGTH:
        raise AccessRefused(422, f"A password needs at least {MIN_LENGTH} characters.")
    if len(text) > MAX_LENGTH:
        raise AccessRefused(422, f"A password can have at most {MAX_LENGTH} characters.")
    if any(ord(c) < 32 or ord(c) == 127 for c in text):
        raise AccessRefused(422, "A password cannot hold line breaks or other control characters.")
    return text


async def _close_live() -> None:
    from pengumon.live.ws import ws_manager
    await ws_manager.close_all()


class AccessManager:
    def __init__(self, security, secrets: SecretStore, repository, *, environ: Optional[Mapping[str, str]] = None,
                 close_live: Callable[[], Awaitable[None]] = _close_live):
        """`security`: the app's SecurityConfig (changed in place). `close_live`: ends every live connection."""
        self.security = security
        self.secrets = secrets
        self.repository = repository
        self._environ = environ
        self._close_live = close_live
        self.source: Optional[str] = None
        self.choice: Optional[str] = None  # the meta key's value
        self._lock = asyncio.Lock()

    # --- reading -----------------------------------------------------------------------------------------------------

    @property
    def pending(self) -> bool:
        """True while the first-run question has not been answered (and no password is in force)."""
        return self.source is None and self.choice is None

    # --- start ---------------------------------------------------------------------------------------------------------

    async def load(self) -> None:
        """Puts the password in force at start (see the module docstring) and logs which rule applies."""
        self.choice = await self.repository.get_meta(SETUP_KEY)
        env = env_value(TOKEN_ENV, self._environ)
        if env is not None:
            self._apply(env, ENV)
            if self.choice is None:
                await self._remember("env")
            _log.info("Sign-in: the password from PENGUMON_ACCESS_TOKEN is required (it wins over the Settings page)")
            return
        stored = self.secrets.get(STORED_PATH)
        if stored is not None:
            self._apply(stored, STORED)
            if self.choice != "password":
                await self._remember("password")
            _log.info("Sign-in: a password is required (set on the Settings page, kept in secrets.json)")
            return
        if self.choice == "password" and (self.secrets.problem is not None or not self.secrets.path.exists()):
            self._apply(random_source.token_urlsafe(32), LOCKED)
            _log.error(f"Sign-in: secrets.json {self.secrets.problem or 'is missing'}, so the dashboard's password "
                       "cannot be read. Nobody can sign in until the file is fixed or restored (or "
                       "PENGUMON_ACCESS_TOKEN is set) and Pengumon is restarted; it is not opened to everyone instead",
                       path=str(self.secrets.path))
            return
        self._apply("", None)
        if self.choice == "password":
            await self._remember("open")
            _log.warning("Sign-in: the password was removed from secrets.json, so anyone who can reach this port can "
                         "open the dashboard. Set a new one in Settings > Sign-in")
        elif self.choice is None:
            _log.warning("Sign-in: not chosen yet. The first person to open the dashboard is asked who may open it; "
                         "until then anyone who can reach this port can read your network data and change settings")
        else:
            _log.warning("Sign-in: no password. Anyone who can reach this port can read your network data and change "
                         "settings (Settings > Sign-in sets a password)")

    # --- changing ------------------------------------------------------------------------------------------------------

    async def answer_setup(self, choice: str, password: Optional[str] = None) -> Optional[str]:
        """The first-run question: "open" (no password) or "password" (stored, in force at once). Returns the password
        kept, or None. AccessRefused(409) once the question has been answered: it is asked once."""
        async with self._lock:
            if not self.pending:
                raise AccessRefused(409, "Who can open this dashboard has already been chosen. Change it in "
                                         "Settings > Sign-in.")
            if choice == "open":
                await self._remember("open")
                _log.info("Sign-in: chosen at the first start: no password")
                return None
            if choice != "password":
                raise AccessRefused(422, "Choose who can open this dashboard.")
            kept = await self._store(check_password(password))
            _log.info("Sign-in: chosen at the first start: a password is required")
            return kept

    async def set_password(self, password) -> str:
        """Sets or changes the stored password, in force at once; every other browser is signed out."""
        async with self._lock:
            self._refuse_when_pinned()
            changed = self.source == STORED
            kept = await self._store(check_password(password))
            _log.info("Sign-in: the password was changed; other browsers are signed out" if changed
                      else "Sign-in: a password is now required")
            return kept

    async def remove_password(self) -> None:
        """Removes the stored password: anyone who can reach the port may open the dashboard again."""
        async with self._lock:
            self._refuse_when_pinned()
            if self.source is None:
                return
            self._write(lambda: self.secrets.remove(STORED_PATH))
            self._apply("", None)
            await self._close_live()
            await self._remember("open")
            _log.warning("Sign-in: the password was removed; anyone who can reach this port can open the dashboard")

    def _refuse_when_pinned(self) -> None:
        if self.source == ENV:
            raise AccessRefused(409, "The password comes from the environment variable PENGUMON_ACCESS_TOKEN, which "
                                     "wins over this page. Change it where Pengumon is started.")
        if self.source == LOCKED:
            raise AccessRefused(409, SECRETS_FILE_DAMAGED)

    async def _store(self, password: str) -> str:
        self._write(lambda: self.secrets.change({STORED_PATH[0]: password}))
        self._apply(password, STORED)  # first: a page that connects while the others are closed is checked by it
        await self._close_live()
        await self._remember("password")
        return password

    def _write(self, change: Callable[[], None]) -> None:
        try:
            change()
        except SecretsFileUnusable:  # logged by the store, with what to do
            raise AccessRefused(500, SECRETS_FILE_DAMAGED) from None
        except OSError as e:
            _log.error(f"secrets.json could not be written: {type(e).__name__}; the sign-in was not changed")
            raise AccessRefused(500, "The secrets file could not be written, so nothing was changed. See the log.") from None

    def _apply(self, token: str, source: Optional[str]) -> None:
        self.security.access_token = token
        self.source = source

    async def _remember(self, choice: str) -> None:
        await self.repository.set_meta(SETUP_KEY, choice)
        self.choice = choice

    async def apply_viewers(self, on: bool) -> None:
        """The viewers setting (access.allow_viewers), in force at once. Turning it off then closes the live
        connections: a viewer's would go on receiving otherwise (a page that connects meanwhile is checked by the new
        rule)."""
        on = bool(on)
        if self.security.view_without_token == on:
            return
        self.security.view_without_token = on
        if not on:
            await self._close_live()
