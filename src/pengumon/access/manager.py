"""The sign-in password, the read token and the first-run question (state.access).

The password is homeguard's access token. Sources, highest first:
  1. PENGUMON_ACCESS_TOKEN (containers and scripts): read at start; the page says so and cannot change it. Kept as text
     in memory (it is the operator's choice where it lives).
  2. secrets.json `access_hash`, set on the page (the first-run question, or Settings > Sign-in), through the secrets
     store (never written over a damaged file): an scrypt hash of the password (homeguard's hash_token), never the text.
     The text is shown once, by the answer that set it.
Neither: no password; anyone who can reach the port may open the dashboard and change everything.

secrets.json also keeps `session_secret` (random, made the first time a password is in force): it keys the session
cookies, so a copied cookie is no offline check of the password. Without a usable secrets.json (a damaged file under an
environment password) it is made anew at each start and kept in memory only: sign-ins then end at every restart.

An `access_token` with the text, as versions before this one stored it, is replaced by its hash at the next start (one
way; logged once). Next to a hash, `access_token` holds `retired_token(<hash>)`: an older version rolled back onto this
file then finds a password nobody knows (locked, until the file is fixed: docs/security.md) instead of none (open).
Any other `access_token` beside a hash was written by such an older version (or by hand) and wins: it is converted.

The read token (`read_token_hash` in secrets.json, its SHA-256): a second, random credential that only reads what a
viewer may, plus POST /mcp (pengumon.security.VIEWER_POLICY). Made, replaced or removed from Settings > Sign-in (with the
current password: api/access.py); shown once, by the answer that made it. A new password, and removing the password,
remove it too (in the same write of secrets.json): someone who knew the old password may have made it, and changing the
password must leave nothing of theirs working. Not removed by signing out everywhere (it is no sign-in), nor at a start
with a different PENGUMON_ACCESS_TOKEN (nothing records which one it was made under: docs/security.md says to replace
it).

Applied without a restart: the app has one SecurityConfig (main.create_app), shared by the middleware and
app.state.security_config, and the middleware reads the password, the read token and `viewers_allowed` on every request
and every new websocket. A change edits that one object in place, then closes the live connections: they were let in
under the old rule and are not checked again (only cookie-signed ones are, by homeguard). The new rule is in force before
the closing starts, so a page that connects meanwhile is checked by it; each closed page reconnects and is checked again
(a browser without the new password lands on the sign-in page). The password is part of the session cookies' key, so a
new password signs every other browser out; the browser that set it gets a new cookie in the same answer.

The first-run question is asked while there is no password from either source and the database meta key
`setup.access_done` is missing. Its value is the last choice: "open", "password", or "env" (a token from the
environment answers it, so such an install never sees the question). A damaged or missing secrets.json while the last
choice was "password" keeps the dashboard locked behind an unknown password instead of opening it (fix or restore the
file, or set PENGUMON_ACCESS_TOKEN): a lost data volume or a wrong data folder must not open a dashboard its owner
closed, and asking the first-run question again would leave it open to whoever answers first. So does an `access_hash`
that is not a hash record. A readable secrets.json without `access_hash` or `access_token` after "password" means they
were removed by hand, the documented way back in (docs/security.md): the dashboard is open again.
"""

import asyncio
import hashlib
import json
import re
import secrets as random_source
from typing import Any, Awaitable, Callable, Mapping, Optional

from pengumon.observability import get_logger
from pengumon.secrets import SECRETS_FILE_DAMAGED, SecretsFileUnusable, SecretStore
from pengumon.security import check_token_hash, hash_token, read_token_digest
from pengumon.settings.env import env_value

_log = get_logger("pengumon.access")

SETUP_KEY = "setup.access_done"
TOKEN_ENV = "PENGUMON_ACCESS_TOKEN"
STORED_PATH = ("access_token",)  # in secrets.json: the text, as older versions kept it (converted), or retired_token()
HASH_PATH = ("access_hash",)  # the password as an scrypt record
SECRET_PATH = ("session_secret",)  # keys the session cookies
READ_TOKEN_PATH = ("read_token_hash",)  # read_token_digest(<the read token>)
READ_TOKEN_PREFIX = "pgr_"  # so a read token is recognizable where it is pasted (an MCP client's config)
MIN_LENGTH, MAX_LENGTH = 8, 128

ENV, STORED, LOCKED = "env", "stored", "locked"  # where the password in force comes from (None: there is none)
_HEX64 = re.compile(r"[0-9a-f]{64}")
_RETIRED = re.compile(r"hashed:[0-9a-f]{32}")  # what retired_token() writes


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


def retired_token(record: Mapping[str, Any]) -> str:
    """What `access_token` holds next to a hash: derived from the hash record, so this version recognizes it, and
    no password anyone knows, so a version from before the hash, rolled back onto the file, stays locked."""
    canonical = json.dumps({k: record[k] for k in ("salt", "n", "r", "p", "hash")}, sort_keys=True)
    return "hashed:" + hashlib.sha256(canonical.encode()).hexdigest()[:32]


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

    @property
    def read_token_set(self) -> bool:
        return bool(self.security.read_token_hash)

    # --- start ---------------------------------------------------------------------------------------------------------

    async def load(self) -> None:
        """Puts the password and the read token in force at start (see the module docstring) and logs which rule
        applies."""
        self.choice = await self.repository.get_meta(SETUP_KEY)
        self.source = None
        self._load_read_token()
        raw_hash = self.secrets.data().get(HASH_PATH[0])
        record = self._valid_record(raw_hash)
        broken = raw_hash is not None and record is None  # an access_hash that is no hash record: nothing to check
        text = self.secrets.get(STORED_PATH)
        # the placeholder beside a hash, with the hash removed by hand (docs/security.md says to remove both): no
        # password anyone knows, so it stays locked, but it is said so, and never taken for a password and hashed
        orphan = raw_hash is None and text is not None and _RETIRED.fullmatch(text) is not None
        unconverted = None  # the text, when it could not be replaced by its hash (an unwritable file)
        # never converted over a broken hash (the text beside it may be the retired token); converted even when the
        # environment's token wins, so the text does not stay on disk
        if not broken and not orphan and text is not None and (record is None or text != retired_token(record)):
            record, unconverted = await self._convert(text)
        env = env_value(TOKEN_ENV, self._environ)
        if env is not None:
            self._apply_text(env, ENV, self._session_secret())
            if self.choice is None:
                await self._remember("env")
            _log.info("Sign-in: the password from PENGUMON_ACCESS_TOKEN is required (it wins over the Settings page)")
            return
        if record is not None:
            self._apply_hash(record, STORED, self._session_secret())
        elif unconverted is not None:
            self._apply_text(unconverted, STORED, self._session_secret())
        if self.source == STORED:
            if self.choice != "password":
                await self._remember("password")
            _log.info("Sign-in: a password is required (set on the Settings page, kept hashed in secrets.json)")
            return
        if broken or orphan or (self.choice == "password" and (self.secrets.problem is not None
                                                               or not self.secrets.path.exists())):
            self._apply_text(random_source.token_urlsafe(32), LOCKED, "")
            why = ("has an access_hash that is not a password hash" if broken else
                   "has the access_token placeholder of a hashed password without its access_hash (remove access_token "
                   "too)" if orphan else self.secrets.problem or "is missing")
            _log.error(f"Sign-in: secrets.json {why}, so the dashboard's password cannot be read. Nobody can sign in "
                       "until the file is fixed or restored (or PENGUMON_ACCESS_TOKEN is set) and Pengumon is restarted; "
                       "it is not opened to everyone instead", path=str(self.secrets.path))
            return
        self._apply_none()
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

    @staticmethod
    def _valid_record(raw) -> Optional[dict]:
        if raw is None:
            return None
        try:
            return check_token_hash(raw)
        except ValueError:
            return None

    async def _convert(self, text: str):
        """The text of a password in secrets.json (an older version's) replaced by its hash, once: (the record, None),
        or (None, the text) when the file could not be written (tried again at the next start)."""
        record = await asyncio.to_thread(hash_token, text)
        secret = self.secrets.get(SECRET_PATH)
        if not secret or len(secret) < 32:
            secret = random_source.token_urlsafe(32)
        try:
            self.secrets.change({HASH_PATH[0]: record, STORED_PATH[0]: retired_token(record), SECRET_PATH[0]: secret})
        except (SecretsFileUnusable, OSError) as e:
            _log.warning(f"Sign-in: the password in secrets.json could not be replaced by its hash ({type(e).__name__}); "
                         "it stays as it is and is tried again at the next start", path=str(self.secrets.path))
            return None, text
        _log.info("Sign-in: the password in secrets.json was replaced by its hash (access_hash); the text is gone",
                  path=str(self.secrets.path))
        return record, None

    def _session_secret(self) -> str:
        """The secret that keys the session cookies: from secrets.json, or made and stored there now. Kept in memory
        only (sign-ins end at the next start) when the file cannot be written."""
        found = self.secrets.get(SECRET_PATH)
        if found and len(found) >= 32:
            return found
        secret = random_source.token_urlsafe(32)
        try:
            self.secrets.change({SECRET_PATH[0]: secret})
        except (SecretsFileUnusable, OSError) as e:
            _log.warning(f"Sign-in: the session secret could not be kept in secrets.json ({type(e).__name__}): sign-ins "
                         "end when Pengumon restarts", path=str(self.secrets.path))
        return secret

    def _load_read_token(self) -> None:
        digest = self.secrets.get(READ_TOKEN_PATH)
        if digest is not None and not _HEX64.fullmatch(digest):
            _log.warning("Sign-in: read_token_hash in secrets.json is not a read token's hash; it is ignored (make a "
                         "new read token in Settings > Sign-in)", path=str(self.secrets.path))
            digest = None
        self.security.read_token_hash = digest or ""

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
        """Sets or changes the stored password, in force at once; every other browser is signed out and the read token
        is removed. The route has checked the current password (api/access.py)."""
        async with self._lock:
            self._refuse_when_pinned()
            changed = self.source == STORED
            kept = await self._store(check_password(password))
            _log.info("Sign-in: the password was changed; other browsers are signed out" if changed
                      else "Sign-in: a password is now required")
            return kept

    async def remove_password(self) -> None:
        """Removes the stored password, and the read token with it: anyone who can reach the port may open the
        dashboard again (a password set later must not bring back a read token made under this one)."""
        async with self._lock:
            self._refuse_when_pinned()
            if self.source is None:
                return
            had_token = self._has_read_token()
            self._write(lambda: self.secrets.change({}, [STORED_PATH, HASH_PATH, READ_TOKEN_PATH]))
            self._apply_none()
            self.security.read_token_hash = ""
            await self._close_live()
            await self._remember("open")
            _log.warning("Sign-in: the password was removed; anyone who can reach this port can open the dashboard"
                         + ("; the read token was removed with it" if had_token else ""))

    async def new_read_token(self) -> str:
        """Makes a read token (replacing one there was), in force at once; returns it (shown once). Needs a password:
        without one everybody may read everything anyway."""
        async with self._lock:
            if self.source == LOCKED:
                raise AccessRefused(409, SECRETS_FILE_DAMAGED)
            if self.source is None:
                raise AccessRefused(409, "Set a password first: without one, anyone on your network can read everything "
                                         "anyway.")
            token = READ_TOKEN_PREFIX + random_source.token_urlsafe(32)
            digest = read_token_digest(token)
            self._write(lambda: self.secrets.change({READ_TOKEN_PATH[0]: digest}))
            replaced = self.read_token_set
            self.security.read_token_hash = digest
            if replaced:  # a live connection opened with the old one is not checked again
                await self._close_live()
            _log.info("Sign-in: the read token was replaced; the old one no longer works" if replaced
                      else "Sign-in: a read token was made (it reads, and calls the MCP endpoint)")
            return token

    async def remove_read_token(self) -> None:
        """Removes the read token, in force at once (its live connections are closed)."""
        async with self._lock:
            if self.source == LOCKED:
                raise AccessRefused(409, SECRETS_FILE_DAMAGED)
            if not self.read_token_set and self.secrets.get(READ_TOKEN_PATH) is None:
                return
            self._write(lambda: self.secrets.remove(READ_TOKEN_PATH))
            self.security.read_token_hash = ""
            await self._close_live()
            _log.info("Sign-in: the read token was removed")

    def _refuse_when_pinned(self) -> None:
        if self.source == ENV:
            raise AccessRefused(409, "The password comes from the environment variable PENGUMON_ACCESS_TOKEN, which "
                                     "wins over this page. Change it where Pengumon is started.")
        if self.source == LOCKED:
            raise AccessRefused(409, SECRETS_FILE_DAMAGED)

    def _has_read_token(self) -> bool:
        return self.read_token_set or self.secrets.get(READ_TOKEN_PATH) is not None

    async def _store(self, password: str) -> str:
        """A new password in force, and the read token removed (in the same write): it may have been made by someone who
        knew the old password, or, after a removal, under an earlier one."""
        record = await asyncio.to_thread(hash_token, password)  # tens of milliseconds: not on the event loop
        secret = self.secrets.get(SECRET_PATH) or ""
        if len(secret) < 32:
            secret = random_source.token_urlsafe(32)
        had_token = self._has_read_token()
        self._write(lambda: self.secrets.change({HASH_PATH[0]: record, STORED_PATH[0]: retired_token(record),
                                                 SECRET_PATH[0]: secret}, [READ_TOKEN_PATH]))
        # first, with no await in between: a page that connects while the others are closed is checked by the new rule
        self._apply_hash(record, STORED, secret)
        self.security.read_token_hash = ""
        await self._close_live()
        await self._remember("password")
        if had_token:
            _log.info("Sign-in: the read token was removed with the old password; make a new one in Settings > Sign-in")
        return password

    def _write(self, change: Callable[[], None]) -> None:
        try:
            change()
        except SecretsFileUnusable:  # logged by the store, with what to do
            raise AccessRefused(500, SECRETS_FILE_DAMAGED) from None
        except OSError as e:
            _log.error(f"secrets.json could not be written: {type(e).__name__}; the sign-in was not changed")
            raise AccessRefused(500, "The secrets file could not be written, so nothing was changed. See the log.") from None

    # Each sets every field the middleware reads, with no await in between: a request never sees half of a change.
    def _apply_text(self, token: str, source: Optional[str], secret: str) -> None:
        self.security.access_hash = None
        self.security.session_secret = secret
        self.security.access_token = token
        self.source = source

    def _apply_hash(self, record: dict, source: str, secret: str) -> None:
        self.security.session_secret = secret
        self.security.access_hash = record
        self.security.access_token = ""
        self.source = source

    def _apply_none(self) -> None:
        self.security.access_token = ""
        self.security.access_hash = None
        self.source = None

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
