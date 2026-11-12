"""Request guards for a LAN-exposed app that has no user accounts.

Threats this addresses:

* **CSRF** - any web page the user opens could POST to http://<pc>:<port>/api/... because the API has no
  auth and no Origin check. Browsers always send `Origin` on cross-site POSTs, so a same-origin rule on
  state-changing requests stops that without any UI change.
* **DNS rebinding** - an attacker domain that re-resolves to the LAN IP passes a same-origin check, so the
  `Host` header is validated too (IP literals, localhost, single-label and .local names by default; extra
  names via <PREFIX>_ALLOWED_HOSTS).
* **Unauthenticated LAN access** - optional shared access token (<PREFIX>_ACCESS_TOKEN), sent as a Bearer
  header or an HttpOnly session cookie obtained from /login. Websockets are checked the same way.
* **Copied or forgotten sign-ins** - the session cookie carries a signed issue time: the server refuses it after
  <PREFIX>_SESSION_MAX_AGE_DAYS (default 30) and whenever the token changes. With a `SessionStore` from the app,
  POST /logout refuses that cookie from then on, and POST /logout?everywhere=1 every cookie issued until then.
  Without one, POST /logout only clears the cookie of the browser that sends it.
* **Guessing the token** - every failed credential (Bearer, /login, a signed link's key) is counted per client
  address (an IPv6 address by its /64) and for all addresses together (`AuthThrottle`); past either limit the
  credential is not even compared. A valid session cookie and the read token are never throttled, an address that
  has signed in recently is exempt from the all-addresses ceiling, so guesses from elsewhere cannot lock the owner
  out, and a POST to /login must be same-origin, so another site's page cannot submit guesses to drive the count.
* **Oversized bodies** and **abuse of expensive endpoints** (`rate_limit`).

Optionally (`<PREFIX>_VIEW_WITHOUT_TOKEN`, off by default, and only for an app whose profile declares a
`ViewerPolicy`), someone without the token may read what the policy allows (GET/HEAD of its paths, receive-only
websockets) while every state-changing request, and every path the policy keeps private, still needs the token.

Also optional, each off unless the app sets it: the token kept as an scrypt hash (`access_hash`), a random
`session_secret` that signs the cookies (so a cookie is no offline check of the token), and a read token
(`read_token_hash`) that reads what the viewer policy allows plus its `reader_post` paths.

Everything that differs between apps lives in an `AppProfile`; an app declares one and subclasses
`SecurityConfig` to attach it. No third-party dependencies beyond FastAPI/Starlette.
"""

import asyncio
import hashlib
import hmac
import html
import ipaddress
import json
import logging
import math
import os
import re
import tempfile
import threading
import time
import weakref
from collections import OrderedDict, defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Deque, Dict, List, Mapping, Optional, Protocol, Tuple, Union
from urllib.parse import parse_qs, urlsplit

from fastapi import HTTPException, Request

try:  # POSIX: FileSessionStore serialises its read-modify-write between processes with flock
    import fcntl
except ImportError:  # Windows: no cross-process lock (see FileSessionStore)
    fcntl = None

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
PERMISSIONS_POLICY = "camera=(), microphone=(), geolocation=()"
HSTS = "max-age=31536000"  # with cookie_secure only; no includeSubDomains (other hosts of the domain), no preload

# Session cookie: "<format>.<issued, ms since the epoch>.<hex HMAC-SHA256 of "<format>|<issued>">".
# Format 2 (no session_secret): the key is derived from the access token under a label of its own, never from a value
# that has been handed out (the old cookie was HMAC(token, session_purpose) itself, so it must not be the key now).
# Format 3 (the app gave a session_secret): the key is derived from that secret, the session purpose, the app's token
# generation and an id of the credential (so a new token still signs every browser out), never from the token alone:
# a cookie is then no offline check of the token. Each config accepts only its own format.
SESSION_FORMAT = "2"
SESSION_FORMAT_SECRET = "3"
_SESSION_RE = re.compile(r"([23])\.(0|[1-9][0-9]{0,15})\.([0-9a-f]{64})")
SESSION_KEY_LABEL = "homeguard-session-key-v2|"
SESSION_KEY_LABEL_SECRET = "homeguard-session-key-v3|"
CAPABILITY_LABEL_SECRET = "homeguard-capability-v3|"
TOKEN_ID_LABEL = "homeguard-token-id|"
MIN_SESSION_SECRET_LENGTH = 16
RESERVED_PURPOSE_PREFIX = "homeguard-"

# The token kept as a hash (SecurityConfig.access_hash): {"salt": hex, "n": int, "r": int, "p": int, "hash": hex},
# scrypt from the standard library. N=2**15, r=8 is 32 MiB and some tens of milliseconds per check; a check runs in a
# small thread pool of its own, never on the event loop, and a success is remembered (see _HashedTokenCheck).
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 15, 8, 1
SCRYPT_SALT_BYTES, SCRYPT_HASH_BYTES = 16, 32
_SCRYPT_MAX_N, _SCRYPT_MAX_R, _SCRYPT_MAX_P = 2 ** 20, 32, 16
MAX_PRESENTED_TOKEN = 4096  # characters: a longer "token" is a failure without being hashed
_HEX64 = re.compile(r"[0-9a-f]{64}")
DEFAULT_SESSION_MAX_AGE_DAYS = 30.0
MAX_SESSION_MAX_AGE_DAYS = 400.0  # browsers keep a cookie at most 400 days whatever Max-Age says
MIN_SESSION_MAX_AGE_DAYS = 1 / 24  # one hour: shorter would make Max-Age round to nothing and loop the sign-in
SESSION_CLOCK_SKEW_MS = 5 * 60 * 1000  # a cookie dated further ahead than this (clock set back) is refused
SESSIONS_ALL_REVOKED = 2 ** 63  # not-before value that refuses every cookie (an unreadable store fails closed)
MAX_REVOKED_SESSIONS = 1000  # FileSessionStore: more single sign-outs within the maximum age sign out everyone
_SESSION_ID_RE = re.compile(r"[0-9a-f]{32}")


def session_id(value: str) -> str:
    """The name a store keeps for one cookie: a hash, so the store never holds a usable cookie."""
    return hashlib.sha256(_bytes(value)).hexdigest()[:32]


def _now_ms() -> int:
    return int(time.time() * 1000)


# --- the token as a hash, and the read token ---------------------------------------------------------------------

def _scrypt_maxmem(n: int, r: int, p: int) -> int:
    return 128 * r * (n + p + 2) + 65536  # what OpenSSL needs for these parameters, plus room


def check_token_hash(record: Any) -> Dict[str, Any]:
    """The record, checked: {"salt": hex, "n": power of two, "r": int, "p": int, "hash": hex}. ValueError otherwise
    (also for parameters so large that one check would take a long time or much memory)."""
    if not isinstance(record, Mapping) or set(record) != {"salt", "n", "r", "p", "hash"}:
        raise ValueError("a token hash is an object with exactly salt, n, r, p and hash")
    salt, n, r, p, digest = (record[k] for k in ("salt", "n", "r", "p", "hash"))
    for name, value in (("salt", salt), ("hash", digest)):
        if not isinstance(value, str) or not re.fullmatch(r"(?:[0-9a-f]{2}){16,64}", value):
            raise ValueError(f"a token hash's {name} is 16 to 64 bytes of lowercase hex")
    for name, value, top in (("n", n, _SCRYPT_MAX_N), ("r", r, _SCRYPT_MAX_R), ("p", p, _SCRYPT_MAX_P)):
        if type(value) is not int or not 1 <= value <= top:
            raise ValueError(f"a token hash's {name} is an integer from 1 to {top}")
    if n < 2 or n & (n - 1):
        raise ValueError("a token hash's n is a power of two")
    return {"salt": salt, "n": n, "r": r, "p": p, "hash": digest}


def hash_token(token: str, *, n: int = SCRYPT_N, r: int = SCRYPT_R, p: int = SCRYPT_P) -> Dict[str, Any]:
    """A record for SecurityConfig.access_hash: scrypt of the token with a new random salt. Slow on purpose (run it
    in a thread from async code)."""
    if not isinstance(token, str) or not token:
        raise ValueError("there is no token to hash")
    salt = os.urandom(SCRYPT_SALT_BYTES)
    digest = hashlib.scrypt(_bytes(token), salt=salt, n=n, r=r, p=p, maxmem=_scrypt_maxmem(n, r, p),
                            dklen=SCRYPT_HASH_BYTES)
    return check_token_hash({"salt": salt.hex(), "n": n, "r": r, "p": p, "hash": digest.hex()})


def verify_token_hash(record: Mapping[str, Any], token: str) -> bool:
    """True when `token` is the one `record` was made from (constant-time comparison). Slow on purpose: never call
    it on an event loop. ValueError for a record that is not one (check_token_hash)."""
    rec = check_token_hash(record)
    if not isinstance(token, str) or not token or len(token) > MAX_PRESENTED_TOKEN:
        return False
    expected = bytes.fromhex(rec["hash"])
    got = hashlib.scrypt(_bytes(token), salt=bytes.fromhex(rec["salt"]), n=rec["n"], r=rec["r"], p=rec["p"],
                         maxmem=_scrypt_maxmem(rec["n"], rec["r"], rec["p"]), dklen=len(expected))
    return hmac.compare_digest(got, expected)


def _record_id(record: Mapping[str, Any]) -> str:
    """A short name for one token hash record (it changes with every new hash: a new salt)."""
    canonical = json.dumps({k: record.get(k) for k in ("salt", "n", "r", "p", "hash")}, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def read_token_digest(token: str) -> str:
    """What SecurityConfig.read_token_hash holds for a read token: its SHA-256, hex. A fast hash is enough: a read
    token is random (an app makes it with `secrets`), so it cannot be guessed from the digest, and the digest itself
    is no credential."""
    return hashlib.sha256(_bytes(token)).hexdigest()


def client_key(host: Optional[str]) -> str:
    """The key failed credentials are counted under: an IPv4 address as it is, an IPv6 address by its /64 (one
    household or one attacker gets a whole /64, and changes addresses inside it freely), an IPv4-mapped IPv6 address
    as the IPv4 address. Anything else (a test client's name) as it is, shortened."""
    text = (host or "?").strip()
    try:
        ip = ipaddress.ip_address(text.split("%", 1)[0])
    except ValueError:
        return text[:64] or "?"
    if ip.version == 6:
        if ip.ipv4_mapped is not None:
            return str(ip.ipv4_mapped)
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return str(ip)


@dataclass(frozen=True)
class CacheRule:
    """Cache-Control for responses whose path starts with `prefix`: `ok` for 2xx/304, `error` otherwise
    (defaults to `ok`). Rules are tried in order; the first matching prefix wins."""

    prefix: str
    ok: str
    error: Optional[str] = None


_SEGMENT = "[^/]+"


def _pattern(text: str, flags: int = 0) -> "re.Pattern[str]":
    """A path pattern: `*` is one path segment (at least one character, no `/`), `**` anything (slashes included);
    every other character is literal. The whole path must match."""
    if not isinstance(text, str) or not text.startswith("/"):
        raise ValueError(f"a viewer path pattern must start with '/': {text!r}")
    parts = re.split(r"(\*\*|\*)", text)
    regex = "".join(".*" if part == "**" else _SEGMENT if part == "*" else re.escape(part) for part in parts)
    return re.compile(regex, flags)


def _normal_path(path: str) -> Optional[str]:
    """The path a viewer rule is matched against: runs of `/` made one and a trailing `/` dropped, so `//x` and `x/`
    are judged as `/x`. None for a path with a `.` or `..` segment or a backslash (never a viewer's)."""
    if not path.startswith("/") or "\\" in path:
        return None
    segments = [s for s in path.split("/") if s]
    if any(s in (".", "..") for s in segments):
        return None
    return "/" + "/".join(segments)


@dataclass(frozen=True)
class ViewerPolicy:
    """What someone without the token may do when the deployment turns viewing on (`<PREFIX>_VIEW_WITHOUT_TOKEN`).
    A request without the token passes only when its method is one of `methods` (safe methods only: GET, HEAD,
    OPTIONS; GET and HEAD by default), its path matches an `allow` pattern and no `deny` pattern. `deny` wins over
    `allow` and is matched without regard to case; patterns are those of `_pattern` (`*` one segment, `**` anything).
    A websocket without the token is accepted only on a path in `websockets` (exact paths), and is receive-only: what
    the client sends never reaches the app (it gets only connect and disconnect). Everything else still needs the
    token: a state-changing method can never be allowed.

    The same policy says what the read token (SecurityConfig.read_token_hash) may do, whether or not viewing without
    the token is on: everything a viewer may, plus POST to the exact paths in `reader_post` (a read-only API that is a
    POST, like an MCP endpoint). Empty by default."""

    allow: Tuple[str, ...]
    deny: Tuple[str, ...] = ()
    websockets: Tuple[str, ...] = ()
    methods: Tuple[str, ...] = ("GET", "HEAD")
    reader_post: Tuple[str, ...] = ()

    def __post_init__(self):
        for name in ("allow", "deny", "websockets", "methods", "reader_post"):
            if isinstance(getattr(self, name), str):  # ("/x") instead of ("/x",): one pattern per character
                raise ValueError(f"ViewerPolicy.{name} must be a tuple of strings, not a string")
        unsafe = [m for m in self.methods if m not in SAFE_METHODS]
        if unsafe:
            raise ValueError(f"a viewer may only use safe methods ({', '.join(sorted(SAFE_METHODS))}), not {unsafe}")
        for path in self.websockets:
            if not isinstance(path, str) or not path.startswith("/") or "*" in path:
                raise ValueError(f"a viewer websocket is an exact path starting with '/': {path!r}")
        for path in self.reader_post:
            if not isinstance(path, str) or not path.startswith("/") or "*" in path or "\\" in path:
                raise ValueError(f"a reader POST path is an exact path starting with '/': {path!r}")
        object.__setattr__(self, "_allow", tuple(_pattern(p) for p in self.allow))
        object.__setattr__(self, "_deny", tuple(_pattern(p, re.IGNORECASE) for p in self.deny))

    def denies(self, path: str) -> bool:
        """True when the path is kept private (a `deny` pattern matches it, or it is not a plain path)."""
        normal = _normal_path(path)
        return normal is None or any(p.fullmatch(normal) for p in self._deny)

    def permits(self, method: str, path: str) -> bool:
        """True when someone without the token may make this request."""
        if method not in self.methods or self.denies(path):
            return False
        normal = _normal_path(path)
        return any(p.fullmatch(normal) for p in self._allow)

    def permits_websocket(self, path: str) -> bool:
        return path in self.websockets and not self.denies(path)

    def permits_reader(self, method: str, path: str) -> bool:
        """True when a request carrying the read token may make this request: what a viewer may, or a POST to one of
        the exact `reader_post` paths."""
        if self.permits(method, path):
            return True
        return method == "POST" and path in self.reader_post and not self.denies(path)


@dataclass(frozen=True)
class AppProfile:
    slug: str  # logger "<slug>.security" and the Bearer realm
    title: str  # shown on the sign-in page
    env_prefix: str  # <PREFIX>_ACCESS_TOKEN, _ALLOWED_HOSTS, _ALLOWED_ORIGINS, _MAX_BODY_BYTES, _COOKIE_SECURE
    cookie_name: str
    session_purpose: str  # HMAC purpose for the session cookie; changing it signs everybody out
    csp: str
    public_paths: Tuple[str, ...]  # reachable without a token (health check, login, installable-app files)
    referrer_policy: str
    no_token_warning: str  # logged once at start-up when no access token is configured
    public_prefixes: Tuple[str, ...] = ("/static/",)
    login_head_extra: str = ""  # raw HTML appended to the sign-in page <head>
    # Capability URLs: path -> HMAC purpose. A request to that path may carry ?key=<capability_key(purpose)>
    # instead of a header, for clients that cannot send one (calendar apps).
    capabilities: Mapping[str, str] = field(default_factory=dict)
    cache_rules: Tuple[CacheRule, ...] = ()
    # What someone without the token may read when <PREFIX>_VIEW_WITHOUT_TOKEN is on; None: the app offers no viewing
    # (setting the variable then stops the start).
    viewer_policy: Optional[ViewerPolicy] = None


def _csv(name: str) -> List[str]:
    return [x.strip().lower() for x in os.getenv(name, "").split(",") if x.strip()]


def _bytes(value: str) -> bytes:
    return value.encode("utf-8", "surrogatepass")


def _same(a: str, b: str) -> bool:
    """Constant-time comparison that accepts any text. hmac.compare_digest on str raises TypeError for
    non-ASCII input, which would turn a garbage credential into a 500 instead of a 401."""
    return hmac.compare_digest(_bytes(a), _bytes(b))


def _check_session_days(days: float, name: str = "session_max_age_days") -> float:
    if not math.isfinite(days) or not MIN_SESSION_MAX_AGE_DAYS <= days <= MAX_SESSION_MAX_AGE_DAYS:
        raise ValueError(f"{name} must be at least 1 hour ({MIN_SESSION_MAX_AGE_DAYS:.4f} days) and at most "
                         f"{MAX_SESSION_MAX_AGE_DAYS:g} days, not {days!r}")
    return days


def _session_days(name: str) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return DEFAULT_SESSION_MAX_AGE_DAYS
    try:
        days = float(raw)
    except ValueError:
        raise ValueError(f"{name} must be a number of days, not {raw[:40]!r}") from None
    return _check_session_days(days, name)


def _flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes", "on")


def _body_bytes(name: str) -> int:
    raw = os.getenv(name, "").strip()
    return int(raw) if raw else 256 * 1024  # empty (a compose file's ${VAR:-}) means the default


class SessionStore(Protocol):
    """Where sign-outs are remembered (homeguard itself keeps no state). `FileSessionStore` is the ready-made one; an
    app may pass its own (its database). Any method that raises refuses the cookie being checked (fails closed)."""

    def not_before_ms(self, max_age_ms: int, now_ms: int) -> int:
        """Cookies issued at or before this time (ms since the epoch) are refused: "sign out everywhere"; 0: never.
        `max_age_ms` is the configured maximum age: a store that forgets single sign-outs once their cookie has
        expired must make sure a longer maximum age cannot bring one back (FileSessionStore signs everyone out)."""

    def is_revoked(self, session_id: str) -> bool:
        """True for a cookie signed out on its own (POST /logout); `session_id` is `session_id(cookie value)`."""

    def revoke(self, session_id: str, expires_ms: int, now_ms: int, max_age_ms: int) -> None:
        """Refuses this one cookie from now on. It may be forgotten after `expires_ms` (its issue time plus
        `max_age_ms` plus the clock skew: it has expired by then)."""

    def revoke_all(self, now_ms: int) -> int:
        """Refuses every cookie issued until `now_ms` (and forgets single sign-outs); returns the stored time."""


class FileSessionStore:
    """`SessionStore` in one small JSON file, {"not_before_ms": <int>, "revoked": {<session id>: <expires ms>},
    "max_age_ms": <int or null>}, written atomically with mode 0600.

    * Read again whenever the file changes (one stat per check), so several worker processes agree. A missing file
      means nobody signed out yet. A file that cannot be read or parsed refuses the cookie being checked (fails
      closed) and is tried again on the next check; a broken file keeps refusing every cookie until it is fixed or
      deleted, or someone signed in with the Bearer token signs out everywhere (which rewrites it).
    * Every change is a read-modify-replace under an exclusive `flock` on `<file>.lock` next to it, so two processes
      cannot lose each other's sign-outs. Where `fcntl` does not exist (Windows), there is no such lock: it works,
      but run one process (a sign-out made at the same moment in another process could be lost); it logs this once.
    * A single sign-out is kept until its cookie would have expired anyway (issue time + maximum age + 5 minutes).
      The file records the shortest maximum age those times were computed with; when the configured one is later
      longer, a forgotten sign-out could come back, so everyone is signed out once instead. Past `max_revoked` entries (sign-
      outs within one maximum age), a sign-out signs out everyone instead, so the file stays small.
    * "Sign out everywhere" sets the time to now, even when the stored one is later (a clock that was ahead)."""

    def __init__(self, path: Union[str, "os.PathLike[str]"], logger: Optional[logging.Logger] = None,
                 max_revoked: int = MAX_REVOKED_SESSIONS):
        self.path = Path(path)
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self.logger = logger or logging.getLogger("homeguard.sessions")
        self.max_revoked = max_revoked
        self._seen: Optional[Tuple[int, int, int]] = None
        self._state: Tuple[int, Dict[str, int], Optional[int]] = (0, {}, None)
        self._failed: Optional[Tuple[int, int, int]] = None  # the file state whose read failed (logged once)
        self._warned_no_lock = False

    # -- reading ---------------------------------------------------------------------------------
    def _read(self) -> Tuple[int, Dict[str, int], Optional[int]]:
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("not a JSON object")
        not_before = data.get("not_before_ms")
        revoked = data.get("revoked", {})
        max_age = data.get("max_age_ms")
        if type(not_before) is not int or not_before < 0:
            raise ValueError("not_before_ms is not a non-negative integer")
        if not isinstance(revoked, dict) or not all(
                isinstance(k, str) and _SESSION_ID_RE.fullmatch(k) and type(v) is int for k, v in revoked.items()):
            raise ValueError("revoked is not an object of session ids and times")
        if max_age is not None and (type(max_age) is not int or max_age <= 0):
            raise ValueError("max_age_ms is not a positive integer")
        return not_before, revoked, max_age

    def _current(self) -> Optional[Tuple[int, Dict[str, int], Optional[int]]]:
        """The file's content (cached by its stat), or None when it cannot be read now (not cached: tried again)."""
        try:
            st = os.stat(self.path)
        except FileNotFoundError:
            self._seen, self._state = None, (0, {}, None)
            return self._state
        except OSError as exc:
            self.logger.error(f"Cannot read {str(self.path)!r} ({exc.__class__.__name__}); refusing this session cookie")
            return None
        seen = (st.st_mtime_ns, st.st_size, st.st_ino)
        if seen != self._seen:
            try:
                state = self._read()
            except (OSError, ValueError) as exc:  # json.JSONDecodeError and UnicodeDecodeError are ValueErrors
                if self._failed != seen:  # once per file state, though every check tries again
                    self.logger.error(f"Cannot use {str(self.path)!r} ({exc.__class__.__name__}): refusing session "
                                      "cookies until it can be read (fix or delete it, or sign out everywhere)")
                    self._failed = seen
                return None
            self._seen, self._state, self._failed = seen, state, None
        return self._state

    def not_before_ms(self, max_age_ms: Optional[int] = None, now_ms: Optional[int] = None) -> int:
        """See SessionStore. Without `max_age_ms` (a look from outside the guards) nothing is checked or changed."""
        state = self._current()
        if state is None:
            return SESSIONS_ALL_REVOKED
        not_before, _, recorded = state
        if max_age_ms is not None and recorded is not None and max_age_ms > recorded:
            now_ms = _now_ms() if now_ms is None else now_ms
            self.logger.warning("The session maximum age is longer than when single sign-outs were recorded: "
                                "signing out everyone once, so none of those sign-ins can come back")
            return self._change(lambda nb, rv, rec: (now_ms, {}, None) if rec is not None and max_age_ms > rec
                                else (nb, rv, rec))[0]
        return not_before

    def is_revoked(self, session_id: str) -> bool:
        state = self._current()
        return True if state is None else session_id in state[1]

    # -- writing ---------------------------------------------------------------------------------
    def _locked(self):
        """An exclusive lock between processes for one read-modify-replace (a no-op without fcntl)."""
        store = self

        class _Lock:
            def __enter__(self):
                store.path.parent.mkdir(parents=True, exist_ok=True)
                if fcntl is None:
                    if not store._warned_no_lock:
                        store.logger.warning("No file locking on this system: run a single process, or a sign-out "
                                             "made at the same moment in another process could be lost")
                        store._warned_no_lock = True
                    self.fd = None
                    return self
                self.fd = os.open(store.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
                try:
                    fcntl.flock(self.fd, fcntl.LOCK_EX)
                except BaseException:
                    os.close(self.fd)
                    raise
                return self

            def __exit__(self, *exc):
                if self.fd is not None:
                    try:
                        fcntl.flock(self.fd, fcntl.LOCK_UN)
                    finally:
                        os.close(self.fd)
                return False

        return _Lock()

    def _write(self, not_before: int, revoked: Dict[str, int], max_age: Optional[int]) -> None:
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=f".{self.path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:  # mkstemp creates it 0600
                json.dump({"not_before_ms": not_before, "revoked": revoked, "max_age_ms": max_age}, fh, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        self._seen = None  # re-read (and re-stat) on the next check

    def _change(self, update, *, repair: bool = False) -> Tuple[int, Dict[str, int], Optional[int]]:
        """Read, apply `update(not_before, revoked, max_age) -> new state`, replace; all under the lock. An unreadable
        file raises, unless `repair` (sign out everywhere, whose result does not depend on what was there)."""
        with self._locked():
            try:
                current = self._read()
            except FileNotFoundError:
                current = (0, {}, None)
            except (OSError, ValueError):
                if not repair:
                    raise  # overwriting it here could forget a "sign out everywhere"
                current = (0, {}, None)
            new = update(*current)
            if new != current:
                self._write(*new)
            return new

    def revoke(self, session_id: str, expires_ms: int, now_ms: int, max_age_ms: int) -> None:
        if not _SESSION_ID_RE.fullmatch(session_id):
            raise ValueError("not a session id")

        def update(not_before, revoked, recorded):
            if recorded is not None and max_age_ms > recorded:  # forgotten entries could come back: everyone out
                self.logger.warning("Maximum age grew since single sign-outs were recorded: signing out everyone")
                return int(now_ms), {}, None
            kept = {k: v for k, v in revoked.items() if v >= now_ms}
            kept[session_id] = int(expires_ms)
            if len(kept) > self.max_revoked:
                self.logger.warning(f"More than {self.max_revoked} single sign-outs are remembered: signing out everyone")
                return max(int(now_ms), not_before), {}, None
            # entries written under a longer maximum age outlive this one's: recording the shorter age is safe
            return not_before, kept, int(max_age_ms) if recorded is None else min(recorded, int(max_age_ms))

        self._change(update)

    def revoke_all(self, now_ms: int) -> int:
        # now, not max(now, stored): a time set while the clock was ahead must be undoable. Every single sign-out so
        # far is of a cookie issued before now, so they and the recorded maximum age can go.
        return self._change(lambda nb, rv, rec: (int(now_ms), {}, None), repair=True)[0]


@dataclass
class SecurityConfig:
    PROFILE: ClassVar[AppProfile]  # set by the app's subclass

    access_token: str = ""
    allowed_hosts: List[str] = field(default_factory=list)
    allowed_origins: List[str] = field(default_factory=list)
    max_body_bytes: int = 256 * 1024
    cookie_secure: bool = False
    session_max_age_days: float = DEFAULT_SESSION_MAX_AGE_DAYS
    session_store: Optional[SessionStore] = None  # enables POST /logout?everywhere=1
    view_without_token: bool = False  # with a token: viewers may read what the profile's viewer_policy allows
    # The token as an scrypt hash (hash_token) instead of access_token; needs session_secret. When both are set, the
    # plain access_token is the one checked.
    access_hash: Optional[Mapping[str, Any]] = None
    # A random secret of the app's (at least 16 characters, kept with its other secrets): when set, session cookies
    # (format 3) and signed links are keyed with it instead of with the token. Empty: as before (format 2).
    session_secret: str = ""
    # Mixed into the cookie key with a session_secret: change it to sign every browser out. A new token (or hash) signs
    # them out anyway: an id of the credential is part of the key too.
    token_generation: str = ""
    # read_token_digest(<the read token>): a Bearer with that token reads what the viewer policy allows (auth "reader").
    read_token_hash: str = ""

    def __post_init__(self):
        _check_session_days(self.session_max_age_days)
        profile = getattr(type(self), "PROFILE", None)
        if self.view_without_token and (profile is None or profile.viewer_policy is None):
            raise ValueError("view_without_token is on, but this app declares no viewer policy "
                             "(AppProfile.viewer_policy): it cannot offer viewing without the token")
        if self.access_hash is not None:
            self.access_hash = check_token_hash(self.access_hash)
            if not self.session_secret:
                raise ValueError("access_hash needs a session_secret: without the token itself nothing else could key "
                                 "the session cookies")
        if self.session_secret and len(self.session_secret) < MIN_SESSION_SECRET_LENGTH:
            raise ValueError(f"session_secret must have at least {MIN_SESSION_SECRET_LENGTH} characters")
        if self.read_token_hash:
            if not _HEX64.fullmatch(self.read_token_hash):
                raise ValueError("read_token_hash is read_token_digest(<token>): 64 lowercase hex characters")
            if profile is None or profile.viewer_policy is None:
                raise ValueError("a read token is set, but this app declares no viewer policy (AppProfile.viewer_policy) "
                                 "to say what it may read")

    @property
    def token_set(self) -> bool:
        """True when signing in is required: a token is set, as text or as a hash."""
        return bool(self.access_token) or bool(self.access_hash)

    @property
    def viewers_allowed(self) -> bool:
        """True when someone without the token may read what the viewer policy allows (a token is set, and so is
        view_without_token). Without a token everyone may do everything anyway."""
        return self.token_set and self.view_without_token

    @classmethod
    def from_env(cls, session_store: Optional[SessionStore] = None):
        prefix = cls.PROFILE.env_prefix
        return cls(
            access_token=os.getenv(f"{prefix}_ACCESS_TOKEN", "").strip(),
            allowed_hosts=_csv(f"{prefix}_ALLOWED_HOSTS"),
            allowed_origins=_csv(f"{prefix}_ALLOWED_ORIGINS"),
            max_body_bytes=_body_bytes(f"{prefix}_MAX_BODY_BYTES"),
            cookie_secure=os.getenv(f"{prefix}_COOKIE_SECURE", "").lower() in ("1", "true", "yes"),
            session_max_age_days=_session_days(f"{prefix}_SESSION_MAX_AGE_DAYS"),
            session_store=session_store,
            view_without_token=_flag(f"{prefix}_VIEW_WITHOUT_TOKEN"),
        )

    # -- derived secrets: rotating the token invalidates cookies and capability links ------------
    def _derive(self, purpose: str) -> str:
        return hmac.new(_bytes(self.access_token), purpose.encode(), hashlib.sha256).hexdigest()

    def _credential_id(self) -> str:
        """Names the token in force without being a check of it (it needs the session secret): part of the keys
        derived from the session secret, so a new token signs everyone out and voids the signed links."""
        if self.access_token:
            return "t:" + hmac.new(_bytes(self.session_secret), _bytes(TOKEN_ID_LABEL + self.access_token),
                                   hashlib.sha256).hexdigest()
        return "h:" + _record_id(self.access_hash or {})

    def _secret_key(self, label: str, purpose: str) -> bytes:
        material = "|".join((label + purpose, self.token_generation, self._credential_id()))
        return hmac.new(_bytes(self.session_secret), _bytes(material), hashlib.sha256).digest()

    def capability_key(self, purpose: str) -> str:
        """The key of a signed link for `purpose`: derived from the session secret when there is one (and a token),
        else from the token as before. ValueError for a hashed token without a session secret."""
        if self.session_secret and self.token_set:
            return self._secret_key(CAPABILITY_LABEL_SECRET, purpose).hex()[:32]
        if not self.access_token and self.access_hash:
            raise ValueError("access_hash needs a session_secret")
        return self._derive(purpose)[:32]

    # -- session cookie ----------------------------------------------------------------------------
    @property
    def session_max_age_seconds(self) -> int:
        return int(self.session_max_age_days * 86400)

    @property
    def _session_format(self) -> str:
        return SESSION_FORMAT_SECRET if self.session_secret else SESSION_FORMAT

    def _session_signature(self, issued_ms: int) -> Optional[str]:
        """The signature of a cookie issued at `issued_ms`, or None when nothing can key it (a hashed token without a
        session secret)."""
        fmt = self._session_format
        if self.session_secret:
            if not self.token_set:
                return None
            key = self._secret_key(SESSION_KEY_LABEL_SECRET, self.PROFILE.session_purpose)
        elif self.access_hash and not self.access_token:
            return None
        else:
            key = hmac.new(_bytes(self.access_token), (SESSION_KEY_LABEL + self.PROFILE.session_purpose).encode(),
                           hashlib.sha256).digest()
        return hmac.new(key, f"{fmt}|{issued_ms}".encode(), hashlib.sha256).hexdigest()

    def issue_session(self, now_ms: Optional[int] = None) -> str:
        """A new session cookie value, issued now (or at `now_ms`). ValueError when nothing can key it."""
        issued = _now_ms() if now_ms is None else int(now_ms)
        signature = self._session_signature(issued)
        if signature is None:
            raise ValueError("no session cookie can be issued: a hashed token needs a session_secret")
        return f"{self._session_format}.{issued}.{signature}"

    def _signed_issue_time(self, value: str) -> Optional[int]:
        """The issue time of a cookie this server signed with the current token (and session secret, in its format),
        else None. Nothing else checked."""
        if not self.token_set or not isinstance(value, str):
            return None
        match = _SESSION_RE.fullmatch(value)
        if not match or match.group(1) != self._session_format:
            return None
        issued = int(match.group(2))
        expected = self._session_signature(issued)
        if expected is None or not _same(match.group(3), expected):
            return None
        return issued

    def _store_accepts(self, issued_ms: int, value: str, now_ms: int) -> bool:
        store = self.session_store
        if store is None:
            return True
        try:
            not_before = int(store.not_before_ms(self.session_max_age_seconds * 1000, now_ms))
            return issued_ms > not_before and not store.is_revoked(session_id(value))
        except Exception as exc:  # an app's own store failing must not turn into a 500, nor let cookies through
            logging.getLogger(f"{self.PROFILE.slug}.security").error(
                f"Session store failed ({exc.__class__.__name__}); refusing every session cookie")
            return False

    def session_valid(self, value: str, now_ms: Optional[int] = None) -> bool:
        """True for a cookie value this server issued with the current token, not older than the maximum age,
        not dated in the future, issued after the last "sign out everywhere" and not signed out on its own. Any
        other text is False."""
        issued = self._signed_issue_time(value)
        if issued is None:
            return False
        now = _now_ms() if now_ms is None else int(now_ms)
        if issued > now + SESSION_CLOCK_SKEW_MS:
            return False
        if now - issued > self.session_max_age_seconds * 1000:
            return False
        return self._store_accepts(issued, value, now)

    def end_session(self, value: str, now_ms: Optional[int] = None) -> None:
        """Refuses this one cookie from now on (POST /logout), when there is a store and the cookie is valid now.
        Raises when the store cannot record it."""
        if self.session_store is None or not self.session_valid(value, now_ms):
            return
        now = _now_ms() if now_ms is None else int(now_ms)
        max_age = self.session_max_age_seconds * 1000
        expires = self._signed_issue_time(value) + max_age + SESSION_CLOCK_SKEW_MS  # it has expired by then anyway
        self.session_store.revoke(session_id(value), expires, now, max_age)

    def describe(self) -> Dict[str, object]:
        return {
            "auth_required": self.token_set,
            "origin_check": True,
            "host_check": True,
            "extra_allowed_hosts": self.allowed_hosts,
            "max_body_bytes": self.max_body_bytes,
            "session_max_age_days": self.session_max_age_days,
            "sign_out_everywhere": self.session_store is not None,
            "view_without_token": self.viewers_allowed,
            "read_token": self.token_set and bool(self.read_token_hash),
        }


def host_allowed(host_header: str, cfg: SecurityConfig) -> bool:
    host = (host_header or "").strip().lower()
    if not host:
        return False
    if host.startswith("["):  # [::1]:8080
        host = host[1: host.find("]")] if "]" in host else host
    elif host.count(":") == 1:
        host = host.split(":", 1)[0]
    try:
        ipaddress.ip_address(host)
        return True  # DNS rebinding needs a hostname; IP literals can't be rebound
    except ValueError:
        pass
    if host == "localhost" or host.endswith(".localhost") or "." not in host:
        return True
    if host.endswith((".local", ".lan", ".home.arpa", ".internal")):
        return True
    for extra in cfg.allowed_hosts:
        if host == extra or (extra.startswith(".") and host.endswith(extra)):
            return True
    return False


def origin_allowed(origin: str, host_header: str, cfg: SecurityConfig) -> bool:
    if not origin or origin == "null":
        return False
    if origin.lower() in cfg.allowed_origins:
        return True
    return urlsplit(origin).netloc.lower() == (host_header or "").lower()


LOGIN_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">{head_extra}<title>{title} - sign in</title>
<style>body{{margin:0;min-height:100vh;display:grid;place-items:center;background:#0b1220;color:#e5e7eb;
font:16px system-ui,sans-serif}}form{{background:#111827;border:1px solid #1f2937;border-radius:12px;
padding:28px;width:min(92vw,360px)}}h1{{font-size:20px;margin:0 0 4px}}p{{color:#94a3b8;margin:0 0 18px;font-size:14px}}
input{{width:100%;box-sizing:border-box;padding:11px 12px;border-radius:8px;border:1px solid #334155;background:#0b1220;
color:inherit;font:inherit}}button{{margin-top:14px;width:100%;padding:11px;border:0;border-radius:8px;background:#3b82f6;
color:#fff;font:inherit;font-weight:600;cursor:pointer}}.err{{color:#f87171;font-size:14px;margin-top:10px}}</style></head>
<body><form method="post" action="/login"><h1>{title}</h1><p>Enter the access token to continue.</p>
<input type="password" name="token" placeholder="Access token" autocomplete="current-password" autofocus required>
<button type="submit">Sign in</button>{error}</form></body></html>"""


class _BodyTooLarge(Exception):
    pass


MAX_THROTTLE_KEYS = 4096  # client keys a throttle or rate limit remembers at most


def _prune(q: Deque[float], now: float, window: float) -> None:
    while q and now - q[0] > window:
        q.popleft()


def _make_room(table: Dict[Any, Deque[float]], window_of, now: float, cap: int) -> None:
    """Before a new key goes into `table`: drop every key whose entries have all expired; if that is not enough, the
    oldest keys (insertion order) until there is room. `window_of(key)` is that key's window."""
    if len(table) < cap:
        return
    for key in list(table):
        _prune(table[key], now, window_of(key))
        if not table[key]:
            del table[key]
    while len(table) >= cap:
        del table[next(iter(table))]


class LoginThrottle:
    """`limit` attempts per `window` seconds per key (5 a minute). Remembers at most `max_keys` keys: keys whose
    attempts have all expired are dropped first, then the oldest. (The middleware itself now uses AuthThrottle.)"""

    def __init__(self, limit: int = 5, window: float = 60.0, max_keys: int = MAX_THROTTLE_KEYS):
        self.limit, self.window, self.max_keys = limit, window, max_keys
        self.hits: Dict[str, Deque[float]] = {}

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        q = self.hits.get(key)
        if q is None:
            _make_room(self.hits, lambda _k: self.window, now, self.max_keys)
            q = self.hits[key] = deque()
        _prune(q, now, self.window)
        if len(q) >= self.limit:
            return False
        q.append(now)
        return True


class AuthThrottle:
    """Failed credentials (a wrong Bearer token, a wrong token on /login, a wrong signed-link key), counted per client
    key (`client_key`: an IPv6 address by its /64) and for all clients together.

    * Per client: after `limit` failures within `window` seconds, that client's credentials are not compared at all
      until the oldest failure is `window` old (429). Someone at another address is not affected.
    * All clients together: after `global_limit` failures within `window` (an attacker who changes addresses), a
      client that has not signed in recently is refused the same way. A client that has (a correct token, the read
      token or a valid session cookie within `known_seconds`) is exempt from this ceiling, though not from its own
      limit: so guesses from many addresses cannot lock the owner's devices out. A new device of the owner's has to
      wait while such an attack goes on (or use a signed-in browser); that is the price of the ceiling.
    * Session cookies are never counted or refused here: one cannot be guessed (an HMAC-SHA256), and every browser
      holds an invalid one after a new token, its maximum age or a sign-out everywhere. Nor is a correct read token
      (random, so as unguessable): the middleware compares it before it asks `refused`.

    Memory is bounded: at most `max_keys` client keys with failures and `max_known` known clients."""

    def __init__(self, limit: int = 5, window: float = 60.0, global_limit: int = 20, known_seconds: float = 86400.0,
                 max_keys: int = MAX_THROTTLE_KEYS, max_known: int = 1024, clock=time.monotonic):
        self.limit, self.window, self.global_limit = limit, window, global_limit
        self.known_seconds, self.max_keys, self.max_known = known_seconds, max_keys, max_known
        self.clock = clock
        self.failures: Dict[str, Deque[float]] = {}
        self.all_failures: Deque[float] = deque()
        self.known: "OrderedDict[str, float]" = OrderedDict()  # client key -> last success (clock)

    def clear(self) -> None:
        self.failures.clear()
        self.all_failures.clear()
        self.known.clear()

    def _is_known(self, key: str, now: float) -> bool:
        seen = self.known.get(key)
        if seen is None:
            return False
        if now - seen > self.known_seconds:
            del self.known[key]
            return False
        return True

    def refused(self, key: str) -> Optional[str]:
        """None when a credential from this client may be compared now; "client" when this client has failed too
        often, "server" when all clients together have and this one has not signed in recently."""
        now = self.clock()
        q = self.failures.get(key)
        if q is not None:
            _prune(q, now, self.window)
            if not q:
                del self.failures[key]
            elif len(q) >= self.limit:
                return "client"
        _prune(self.all_failures, now, self.window)
        if len(self.all_failures) >= self.global_limit and not self._is_known(key, now):
            return "server"
        return None

    def retry_after(self, key: str) -> int:
        """Seconds until this client's credentials are compared again (at least 1)."""
        now = self.clock()
        q = self.failures.get(key)
        oldest = q[0] if q and len(q) >= self.limit else (self.all_failures[0] if self.all_failures else now)
        return max(1, math.ceil(self.window - (now - oldest)))

    def fail(self, key: str) -> float:
        """Records a failure; returns its time (for `forgive`)."""
        now = self.clock()
        q = self.failures.get(key)
        if q is None:
            _make_room(self.failures, lambda _k: self.window, now, self.max_keys)
            q = self.failures[key] = deque()
        q.append(now)
        self.all_failures.append(now)
        return now

    def forgive(self, key: str, stamp: float) -> None:
        """Takes back a failure recorded with `fail` before a slow check that then succeeded."""
        for q in (self.failures.get(key), self.all_failures):
            if q is not None:
                try:
                    q.remove(stamp)
                except ValueError:  # already expired
                    pass
        if key in self.failures and not self.failures[key]:
            del self.failures[key]

    def succeed(self, key: str) -> None:
        """This client proved it has a credential: exempt from the all-clients ceiling for `known_seconds`."""
        now = self.clock()
        if key in self.known:
            self.known.move_to_end(key)
        elif len(self.known) >= self.max_known:
            self.known.popitem(last=False)
        self.known[key] = now


_SCRYPT_POOL: Optional[ThreadPoolExecutor] = None
_SCRYPT_POOL_LOCK = threading.Lock()


def _scrypt_pool() -> ThreadPoolExecutor:
    """Two threads of their own for token hash checks: never the event loop, and never every thread of the default
    pool (each check holds 32 MiB for some tens of milliseconds)."""
    global _SCRYPT_POOL
    with _SCRYPT_POOL_LOCK:
        if _SCRYPT_POOL is None:
            _SCRYPT_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="homeguard-scrypt")
        return _SCRYPT_POOL


class _HashedTokenCheck:
    """Checks a presented token against SecurityConfig.access_hash. A success is remembered, so the slow hash runs
    once per correct token and process, not on every request: the cache holds HMAC(<random key of this process>,
    <record id> + token) of successes only (at most `max_entries`), so neither it nor a memory dump of it is a fast
    check of guesses without the key, and a new hash (a new token) never matches an old entry. Failures are not
    cached: the AuthThrottle in front of this bounds how many slow checks a guesser can cause."""

    def __init__(self, max_entries: int = 64):
        self._key = os.urandom(32)
        self._ok: "OrderedDict[bytes, None]" = OrderedDict()
        self._record: Optional[str] = None
        self.max_entries = max_entries

    def _digest(self, record_id: str, presented: str) -> bytes:
        return hmac.new(self._key, _bytes(record_id + "\x00" + presented), hashlib.sha256).digest()

    def cached(self, record: Mapping[str, Any], presented: str) -> bool:
        record_id = _record_id(record)
        if record_id != self._record:  # a new token: nothing remembered applies any more
            self._ok.clear()
            self._record = record_id
            return False
        digest = self._digest(record_id, presented)
        if digest in self._ok:
            self._ok.move_to_end(digest)
            return True
        return False

    async def verify(self, record: Mapping[str, Any], presented: str) -> bool:
        """The slow check, in the scrypt pool. ValueError for a record that is not one."""
        record_id = _record_id(record)
        loop = asyncio.get_running_loop()
        ok = await loop.run_in_executor(_scrypt_pool(), verify_token_hash, dict(record), presented)
        if ok and record_id == self._record:
            self._ok[self._digest(record_id, presented)] = None
            while len(self._ok) > self.max_entries:
                self._ok.popitem(last=False)
        return ok


@dataclass
class Rejection:
    status: int
    detail: str
    redirect: Optional[str] = None
    challenge: bool = False
    retry_after: Optional[int] = None


THROTTLED_DETAIL = {
    "client": "Too many failed sign-in attempts from this address. Wait a minute and try again.",
    "server": "Too many failed sign-in attempts on this server. Wait a minute and try again, or use a browser "
              "that is still signed in.",
}
READER_REFUSED = "The read token can only read. This needs the password."


class _Request:
    """The few facts every guard needs, parsed once."""

    def __init__(self, scope, headers: Dict[str, str]):
        self.scope = scope
        self.headers = headers
        self.method: str = scope["method"]
        self.path: str = scope["path"]
        self.host: str = headers.get("host", "")
        # set by _guard_auth: "bearer", "cookie", "link", "open" (no token), "reader" (the read token) or "viewer" (no
        # token sent, viewing allowed)
        self.auth: Optional[str] = None


class _SessionEnded(OSError):
    """Raised from a websocket's send after the middleware closed it because its session cookie stopped being
    valid. Starlette turns an OSError from send into WebSocketDisconnect, as for a client that went away."""


def session_status(request_or_scope: Any) -> Dict[str, bool]:
    """What a page should offer, for an app's info endpoint: `sign_out` when this request was signed in by the
    session cookie (not a Bearer token, not a signed link, not an app without a token), `sign_out_everywhere`
    when it also may (the app gave the config a `SessionStore`). Both False on public paths."""
    scope = getattr(request_or_scope, "scope", request_or_scope)
    info = scope.get("homeguard") or {}
    return {"sign_out": bool(info.get("sign_out")), "sign_out_everywhere": bool(info.get("sign_out_everywhere"))}


FULL_ACCESS = ("open", "bearer", "cookie")  # the ways of reaching an app that may change it


def can_edit(request_or_scope: Any) -> bool:
    """True when this request may change state: it carries the token (Bearer or the session cookie), or the app has no
    token. False for a viewer (`<PREFIX>_VIEW_WITHOUT_TOKEN`), the read token, a signed link, a public path, or a
    request the middleware did not see. For an app's info endpoint, so the page can offer or disable its edit controls;
    the middleware enforces it whatever the page shows."""
    scope = getattr(request_or_scope, "scope", request_or_scope)
    return (scope.get("homeguard") or {}).get("auth") in FULL_ACCESS


async def confirm_token(request_or_scope: Any, presented: Any) -> bool:
    """For an app route that asks for the token again before a sensitive change (a new token, say): True when
    `presented` is the token in force (plain or hashed; never the read token). A wrong one counts as a failed attempt
    of this client in the middleware's throttle; while the client is throttled the token is not compared and this
    raises HTTPException(429) with Retry-After. True when the app has no token (everyone may change everything).
    RuntimeError for a request the middleware did not see."""
    scope = getattr(request_or_scope, "scope", request_or_scope)
    check = (scope.get("homeguard") or {}).get("confirm")
    if check is None:
        raise RuntimeError("this request did not pass through SecurityMiddleware")
    return await check(scope, presented)


class SecurityMiddleware:
    def __init__(self, app, cfg: SecurityConfig):
        self.app = app
        self.cfg = cfg
        self.profile: AppProfile = cfg.PROFILE
        self.logger = logging.getLogger(f"{self.profile.slug}.security")
        self.throttle = AuthThrottle()
        _THROTTLES.add(self.throttle)
        self._hashed = _HashedTokenCheck()
        for purpose in self.profile.capabilities.values():
            # a link key is a published HMAC of its purpose: it must never equal a session purpose or key label
            if purpose == self.profile.session_purpose or purpose.startswith(RESERVED_PURPOSE_PREFIX):
                raise ValueError(f"capability purpose {purpose!r} is reserved")
        if self.cfg.view_without_token and self.profile.viewer_policy is None:
            raise ValueError("view_without_token is on, but the profile declares no viewer_policy")
        # a signed link's path is private by intent: never a viewer's, whatever the policy allows
        self._link_paths = {(_normal_path(p) or p).lower() for p in self.profile.capabilities}
        if not self.cfg.token_set:
            self.logger.warning(self.profile.no_token_warning)
        elif self.cfg.viewers_allowed:
            self.logger.warning("Viewing without the token is on: anyone who can reach this port can read what the "
                                "viewer policy allows; changing anything still needs the token")

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def _headers(scope) -> Dict[str, str]:
        return {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers") or []}

    async def _respond(self, send, status: int, body: bytes, path: str, content_type: str = "application/json",
                       extra: Optional[List[Tuple[bytes, bytes]]] = None):
        """Every response the guard itself produces: security headers plus exactly one no-store."""
        headers = ([(b"content-type", content_type.encode()), (b"content-length", str(len(body)).encode()),
                    (b"cache-control", b"no-store")]
                   + self._security_headers(path, status, cache=False) + (extra or []))
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})

    async def _json(self, send, status: int, detail: str, path: str, extra=None):
        await self._respond(send, status, json.dumps({"detail": detail}).encode(), path, extra=extra)

    def _cookie(self, headers: Dict[str, str]) -> str:
        for part in headers.get("cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == self.profile.cookie_name:
                return v
        return ""

    @staticmethod
    def _client(scope) -> str:
        return client_key((scope.get("client") or ("?", 0))[0])

    async def _token_matches(self, key: str, presented: str) -> bool:
        """Whether `presented` is the token in force, recording a failure (or success) for this client. The caller has
        just checked `self.throttle.refused(key)`, with no await in between: so concurrent guesses cannot all pass
        that check before the first failure is recorded (the slow path records it before it waits, and takes it back
        on success)."""
        cfg = self.cfg
        if cfg.access_token:
            if isinstance(presented, str) and _same(presented, cfg.access_token):
                self.throttle.succeed(key)
                return True
            self.throttle.fail(key)
            return False
        record = cfg.access_hash
        if not record:
            return False
        if not isinstance(presented, str) or not presented or len(presented) > MAX_PRESENTED_TOKEN:
            self.throttle.fail(key)
            return False
        if self._hashed.cached(record, presented):
            self.throttle.succeed(key)
            return True
        stamp = self.throttle.fail(key)
        try:
            ok = await self._hashed.verify(record, presented)
        except ValueError as exc:
            self.logger.error(f"The access token hash cannot be used ({exc}); refusing the token")
            return False
        current = cfg.access_hash
        if ok and (cfg.access_token or not current or _record_id(current) != _record_id(record)):
            ok = False  # the token changed while this one was being checked
        if ok:
            self.throttle.forgive(key, stamp)
            self.throttle.succeed(key)
        return ok

    def _read_token_matches(self, presented: str) -> bool:
        digest = self.cfg.read_token_hash
        return bool(digest) and _same(read_token_digest(presented), digest)

    async def _authenticate(self, scope, headers: Dict[str, str], path: str) -> Tuple[Optional[str], Optional[str]]:
        """(how this request is signed in, or None; why its credential was not compared, or None). The first is "open"
        when no token is configured, else "bearer", "reader", "cookie" or "link". The second is "client" or "server"
        (AuthThrottle.refused) when it carries a Bearer token or a link key that was not compared because of failed
        attempts; a valid session cookie or read token is never refused."""
        cfg = self.cfg
        if not cfg.token_set:
            return "open", None
        key = self._client(scope)
        cookie_ok = None  # checked once, when needed

        def by_cookie() -> bool:
            nonlocal cookie_ok
            if cookie_ok is None:
                cookie_ok = cfg.session_valid(self._cookie(headers))
                if cookie_ok:
                    self.throttle.succeed(key)
            return cookie_ok

        bearer = headers.get("authorization", "")
        if bearer.lower().startswith("bearer "):
            presented = bearer[7:].strip()
            # The read token is compared before the throttle is asked, as a cookie is never refused: it is random (as
            # unguessable as a cookie), so failed attempts elsewhere must not lock out the clients that use it.
            if self._read_token_matches(presented):
                self.throttle.succeed(key)
                return "reader", None
            refused = self.throttle.refused(key)
            if refused:
                return ("cookie", None) if by_cookie() else (None, refused)
            if await self._token_matches(key, presented):  # a wrong one is counted there
                return "bearer", None
            self.logger.warning(f"Wrong Bearer token from {key}")
        if by_cookie():
            return "cookie", None
        purpose = self.profile.capabilities.get(path)
        if purpose:  # capability URL: the client cannot send headers
            query = scope.get("query_string", b"").decode("utf-8", "replace")
            link_key = parse_qs(query).get("key", [""])[0]
            if link_key:
                refused = self.throttle.refused(key)
                if refused:
                    return None, refused
                try:
                    expected = cfg.capability_key(purpose)
                except ValueError:
                    return None, None
                if _same(link_key, expected):
                    return "link", None
                self.throttle.fail(key)
        return None, None

    async def _confirm(self, scope, presented: Any) -> bool:
        """confirm_token: the token in force only (never the read token), throttled like a Bearer token."""
        if not self.cfg.token_set:
            return True
        key = self._client(scope)
        refused = self.throttle.refused(key)
        if refused:
            raise HTTPException(status_code=429, detail=THROTTLED_DETAIL[refused],
                                headers={"Retry-After": str(self.throttle.retry_after(key))})
        ok = await self._token_matches(key, presented if isinstance(presented, str) else "")
        if not ok:
            self.logger.warning(f"Wrong token given to confirm a change, from {key}")
        return ok

    def _viewer_may(self, method: str, path: str) -> bool:
        """True when a request without the token may still be made: viewing is on and the policy allows it."""
        if not self.cfg.viewers_allowed or (_normal_path(path) or path).lower() in self._link_paths:
            return False
        return self.profile.viewer_policy.permits(method, path)

    def _reader_may(self, method: str, path: str) -> bool:
        """True when a request with the read token may be made: what a viewer may (viewing on or not), or a POST to
        one of the policy's reader_post paths. Never a signed link's path."""
        policy = self.profile.viewer_policy
        if policy is None or (_normal_path(path) or path).lower() in self._link_paths:
            return False
        return policy.permits_reader(method, path)

    def _clear_cookie(self) -> Tuple[bytes, bytes]:
        cookie = (f"{self.profile.cookie_name}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"
                  + ("; Secure" if self.cfg.cookie_secure else ""))
        return (b"set-cookie", cookie.encode())

    def _annotate(self, scope, auth: Optional[str]) -> None:
        """What the app may read with session_status(request)."""
        signed_in = auth == "cookie"
        scope["homeguard"] = {"auth": auth, "sign_out": signed_in,
                              "sign_out_everywhere": signed_in and self.cfg.session_store is not None,
                              "confirm": self._confirm}

    # -- guards: each returns a Rejection, or None to let the request through --------------------
    def _guard_host(self, ctx: "_Request") -> Optional[Rejection]:
        if host_allowed(ctx.host, self.cfg):
            return None
        self.logger.warning(f"Rejected request with unexpected Host header: {ctx.host[:80]!r}")
        return Rejection(421, f"Host not allowed. Add it to {self.profile.env_prefix}_ALLOWED_HOSTS.")

    def _guard_origin(self, ctx: "_Request") -> Optional[Rejection]:
        """Cross-site requests can't change state, nor sign in: a POST to /login is checked too, so another site's
        page cannot submit wrong tokens to drive the throttle of failed sign-ins (and lock out the owner's address).
        The sign-in page's own form sends its Origin, which matches. Non-browser clients send neither header and
        pass."""
        if ctx.method in SAFE_METHODS:
            return None
        origin = ctx.headers.get("origin")
        if origin is not None:
            if origin_allowed(origin, ctx.host, self.cfg):
                return None
            if origin == "null" and ctx.headers.get("sec-fetch-site") == "same-origin":
                # a form of this origin's own page under Referrer-Policy: no-referrer, for which browsers send
                # "Origin: null"; Sec-Fetch-Site cannot be set by a page, and another site's page gets "cross-site"
                return None
            self.logger.warning(f"Blocked cross-origin {ctx.method} {ctx.path} from origin {origin[:80]!r}")
            return Rejection(403, "Cross-origin request blocked.")
        if ctx.headers.get("sec-fetch-site", "same-origin") not in ("same-origin", "none"):
            return Rejection(403, "Cross-site request blocked.")
        return None

    def _throttled(self, ctx: "_Request", refused: str) -> Rejection:
        key = self._client(ctx.scope)
        return Rejection(429, THROTTLED_DETAIL[refused], retry_after=self.throttle.retry_after(key))

    async def _guard_auth(self, ctx: "_Request") -> Optional[Rejection]:
        p = self.profile
        if not self.cfg.token_set:
            ctx.auth = "open"
            return None
        if ctx.path in p.public_paths or ctx.path.startswith(tuple(p.public_prefixes)):
            return None
        auth, refused = await self._authenticate(ctx.scope, ctx.headers, ctx.path)
        if refused:
            return self._throttled(ctx, refused)
        if auth == "reader":
            if not self._reader_may(ctx.method, ctx.path):
                return Rejection(403, READER_REFUSED)
            ctx.auth = auth
            return None
        ctx.auth = auth
        if ctx.auth:
            return None
        if self._viewer_may(ctx.method, ctx.path):
            ctx.auth = "viewer"
            return None
        wants_page = ctx.path == "/" or (ctx.method == "GET" and "text/html" in ctx.headers.get("accept", ""))
        if wants_page:
            return Rejection(303, "", redirect="/login")
        return Rejection(401, "Authentication required.", challenge=True)

    def _guard_declared_size(self, ctx: "_Request") -> Optional[Rejection]:
        length = ctx.headers.get("content-length")
        if length and length.isdigit() and int(length) > self.cfg.max_body_bytes:
            return Rejection(413, "Request body too large.")
        return None

    async def _reject(self, send, r: "Rejection", path: str) -> None:
        if r.redirect:
            return await self._respond(send, r.status, b"", path, extra=[(b"location", r.redirect.encode())])
        extra = [(b"www-authenticate", f'Bearer realm="{self.profile.slug}"'.encode())] if r.challenge else []
        if r.retry_after is not None:
            extra.append((b"retry-after", str(r.retry_after).encode()))
        await self._json(send, r.status, r.detail, path, extra=extra or None)

    # -- response decoration ---------------------------------------------------------------------
    def _cache_control(self, path: str, status: int) -> Optional[str]:
        if path.startswith("/api/"):
            return "no-store"
        for rule in self.profile.cache_rules:
            if path.startswith(rule.prefix):
                if 200 <= status < 300 or status == 304:
                    return rule.ok
                return rule.ok if rule.error is None else rule.error
        return None

    def _security_headers(self, path: str, status: int = 200, cache: bool = True) -> List[Tuple[bytes, bytes]]:
        """The headers every response carries. `cache=False` leaves Cache-Control out (the caller sets it)."""
        hdrs = [
            (b"x-content-type-options", b"nosniff"),
            (b"x-frame-options", b"SAMEORIGIN"),
            (b"referrer-policy", self.profile.referrer_policy.encode()),
            (b"content-security-policy", self.profile.csp.encode()),
            (b"permissions-policy", PERMISSIONS_POLICY.encode()),
        ]
        if self.cfg.cookie_secure:  # served over HTTPS: browsers must not fall back to plain HTTP for a year
            hdrs.append((b"strict-transport-security", HSTS.encode()))
        if cache:
            value = self._cache_control(path, status)
            if value:
                hdrs.append((b"cache-control", value.encode()))
        return hdrs

    # -- websocket ---------------------------------------------------------------------------------
    async def _websocket_auth(self, scope, headers: Dict[str, str]) -> Tuple[Optional[str], Optional[str]]:
        """(problem, auth method). The HTTP guards never see websocket scopes. Browsers don't apply the
        same-origin policy to websockets, so any page could otherwise open one: Host, Origin and the token are
        checked here."""
        host = headers.get("host", "")
        if not host_allowed(host, self.cfg):
            return f"Host not allowed: {host[:80]!r}", None
        origin = headers.get("origin")
        if origin is None or not origin_allowed(origin, host, self.cfg):
            return f"Origin not allowed: {(origin or '')[:80]!r}", None
        # Header or cookie (or a signed link, on a capability path that is a websocket).
        path = scope.get("path", "")
        auth, refused = await self._authenticate(scope, headers, path)
        if refused:
            return THROTTLED_DETAIL[refused], None
        policy = self.profile.viewer_policy
        if auth == "reader" and (policy is None or not policy.permits_websocket(path)):
            return "The read token cannot open this websocket", None
        if auth is None and self.cfg.viewers_allowed and self.profile.viewer_policy.permits_websocket(path):
            auth = "viewer"
        if auth is None:
            return "Authentication required", None
        return None, auth

    async def _websocket(self, scope, receive, send):
        headers = self._headers(scope)
        problem, auth = await self._websocket_auth(scope, headers)
        if problem is not None:
            self.logger.warning(f"Rejected websocket {scope.get('path', '')}: {problem}")
            await receive()  # websocket.connect
            await send({"type": "websocket.close", "code": 1008})
            return
        self._annotate(scope, auth)
        if auth in ("viewer", "reader"):
            # Receive-only: what a viewer (or the read token) sends is read and dropped here, so the app never acts on
            # it. It still hears connect and disconnect (and its own close), so its receive loop ends as usual.
            async def viewer_receive():
                while True:
                    message = await receive()
                    if message["type"] != "websocket.receive":
                        return message

            return await self.app(scope, viewer_receive, send)
        if auth != "cookie":
            return await self.app(scope, receive, send)

        # Signed in by cookie: the connection may outlive the session (maximum age, sign out everywhere), so the
        # cookie is checked again on every message, and the socket is closed (1008) once it is no longer valid.
        cookie = self._cookie(headers)
        closed = False

        async def end_session():
            nonlocal closed
            closed = True
            self.logger.info(f"Closed websocket {scope.get('path', '')[:80]!r}: its session ended")
            await send({"type": "websocket.close", "code": 1008})

        async def checked_receive():
            message = await receive()
            if message["type"] == "websocket.receive" and not closed and not self.cfg.session_valid(cookie):
                await end_session()
                return {"type": "websocket.disconnect", "code": 1008}
            return message

        async def checked_send(message):
            if closed:
                if message["type"] == "websocket.close":
                    return
                raise _SessionEnded("websocket closed: session ended")
            if message["type"] == "websocket.send" and not self.cfg.session_valid(cookie):
                await end_session()
                raise _SessionEnded("websocket closed: session ended")
            await send(message)

        await self.app(scope, checked_receive, checked_send)

    # -- ASGI --------------------------------------------------------------------------------------
    async def __call__(self, scope, receive, send):
        if scope["type"] == "websocket":
            return await self._websocket(scope, receive, send)
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        ctx = _Request(scope, self._headers(scope))
        if ctx.path == "/login":
            # Host, and for a POST the Origin rule, before the body is read or anything is counted
            for guard in (self._guard_host, self._guard_origin):
                rejection = guard(ctx)
                if rejection:
                    return await self._reject(send, rejection, ctx.path)
            return await self._login(scope, receive, send, ctx.headers, ctx.method)
        if ctx.path == "/logout":
            # Same Host and Origin rules as any state-changing request: another site cannot sign you out (a plain
            # same-origin <form method="post"> sends a matching Origin, so the app's own button works).
            for guard in (self._guard_host, self._guard_origin):
                rejection = guard(ctx)
                if rejection:
                    return await self._reject(send, rejection, ctx.path)
            return await self._logout(ctx, send)

        for guard in (self._guard_host, self._guard_origin, self._guard_auth, self._guard_declared_size):
            rejection = guard(ctx)
            if asyncio.iscoroutine(rejection):
                rejection = await rejection
            if rejection:
                return await self._reject(send, rejection, ctx.path)
        self._annotate(scope, ctx.auth)

        received = 0
        overflowed = False
        started = False

        async def limited_receive():
            nonlocal received, overflowed
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.cfg.max_body_bytes:  # chunked bodies have no Content-Length to check up front
                    overflowed = True
                    raise _BodyTooLarge()
            return message

        async def guarded_send(message):
            nonlocal started
            if overflowed and not started:
                return  # the app is failing because of the oversized body (probably with its own 500): replace it
            if message["type"] == "http.response.start":
                started = True
                extra = self._security_headers(ctx.path, message.get("status", 200))
                message = {**message, "headers": list(message.get("headers") or []) + extra}
            await send(message)

        try:
            await self.app(scope, limited_receive, guarded_send)
        except Exception:
            # The app usually turns the _BodyTooLarge raised in limited_receive into its own 500 (and re-raises);
            # only an overflow is ours to answer.
            if not overflowed:
                raise
        if overflowed and not started:
            await self._json(send, 413, "Request body too large.", ctx.path)

    def _login_page(self, error: str = "") -> bytes:
        p = self.profile
        err = f'<div class="err">{html.escape(error)}</div>' if error else ""
        return LOGIN_PAGE.format(title=html.escape(p.title), head_extra=p.login_head_extra, error=err).encode()

    async def _login(self, scope, receive, send, headers, method):
        path = "/login"
        html_type = "text/html; charset=utf-8"
        if not self.cfg.token_set:
            return await self._respond(send, 303, b"", path, extra=[(b"location", b"/")])
        if method == "GET":
            return await self._respond(send, 200, self._login_page(), path, html_type)
        if method != "POST":
            return await self._json(send, 405, "Method not allowed.", path)
        body = b""
        while True:
            msg = await receive()
            body += msg.get("body", b"")
            if not msg.get("more_body") or len(body) > 4096:
                break
        client = self._client(scope)
        # checked after the body is read, right before the comparison (no await between): concurrent attempts cannot
        # all pass the check before the first failure is counted
        refused = self.throttle.refused(client)
        if refused:
            text = "Too many attempts. Wait a minute." if refused == "client" else THROTTLED_DETAIL["server"]
            return await self._respond(send, 429, self._login_page(text), path, html_type,
                                       extra=[(b"retry-after", str(self.throttle.retry_after(client)).encode())])
        supplied = parse_qs(body.decode("utf-8", "ignore")).get("token", [""])[0]
        if await self._token_matches(client, supplied):
            cookie = (f"{self.profile.cookie_name}={self.cfg.issue_session()}; Path=/; HttpOnly; SameSite=Strict; "
                      f"Max-Age={self.cfg.session_max_age_seconds}" + ("; Secure" if self.cfg.cookie_secure else ""))
            return await self._respond(send, 303, b"", path,
                                       extra=[(b"location", b"/"), (b"set-cookie", cookie.encode())])
        self.logger.warning(f"Failed login from {client}")
        return await self._respond(send, 401, self._login_page("That token is not right."), path, html_type)

    async def _logout(self, ctx: "_Request", send):
        """POST /logout clears this browser's cookie and, when the app gave a SessionStore, refuses that cookie from
        now on (a copy of it too). POST /logout?everywhere=1 instead refuses every session cookie issued until now
        (all browsers; needs a store); it needs a valid sign-in (cookie or Bearer token; not the read token), so nobody
        on the network can sign everyone out. Both redirect to /login (to / without a token)."""
        path = "/logout"
        if ctx.method != "POST":
            return await self._json(send, 405, "Method not allowed.", path, extra=[(b"allow", b"POST")])
        query = ctx.scope.get("query_string", b"").decode("utf-8", "replace")
        values = parse_qs(query, keep_blank_values=True).get("everywhere")
        if values is not None and values != ["1"]:
            return await self._json(send, 400, "everywhere must be 1.", path)
        if values and self.cfg.token_set:
            if self.cfg.session_store is None:
                return await self._json(send, 501, "Signing out everywhere is not enabled in this app.", path)
            auth, refused = await self._authenticate(ctx.scope, ctx.headers, path)
            if refused:
                return await self._reject(send, self._throttled(ctx, refused), path)
            if auth == "reader":
                return await self._reject(send, Rejection(403, READER_REFUSED), path)
            if auth not in FULL_ACCESS:
                return await self._reject(send, Rejection(401, "Authentication required.", challenge=True), path)
            try:
                self.cfg.session_store.revoke_all(_now_ms())
            except Exception as exc:
                self.logger.error(f"Signing out everywhere failed: {exc.__class__.__name__}")
                return await self._json(send, 500, "Could not sign out everywhere. See the server log.", path)
            self.logger.warning("Signed out everywhere: every session cookie issued until now is refused")
        elif self.cfg.token_set:
            try:  # so that a copy of this cookie stops working too, not only this browser's
                self.cfg.end_session(self._cookie(ctx.headers))
            except Exception as exc:
                self.logger.error(f"Signing out failed: {exc.__class__.__name__}")
                return await self._json(send, 500, "Signed out in this browser, but the server could not record it, so "
                                        "a copy of the sign-in would still work. Sign in and sign out everywhere, "
                                        "or see the server log.", path, extra=[self._clear_cookie()])
        location = b"/login" if self.cfg.token_set else b"/"
        return await self._respond(send, 303, b"", path, extra=[(b"location", location), self._clear_cookie()])


# ---------------------------------------------------------------------------
# Per-endpoint rate limit (protects upstream quotas and expensive endpoints from a loop or a misbehaving client)
# ---------------------------------------------------------------------------

_buckets: Dict[Tuple[str, str], Deque[float]] = defaultdict(deque)
_windows: Dict[str, float] = {}  # rate limit name -> its window (the longest, if a name is used twice)
_THROTTLES: "weakref.WeakSet[AuthThrottle]" = weakref.WeakSet()  # every middleware's, for reset_rate_limits


def reset_rate_limits() -> None:
    """Forget every rate-limit counter and every middleware's failed sign-in attempts (process-wide). For tests, which
    make more calls than a limit allows."""
    _buckets.clear()
    for throttle in list(_THROTTLES):
        throttle.clear()


def rate_limit(name: str, calls: int, per_seconds: float):
    """FastAPI dependency: at most `calls` requests per `per_seconds` per client address. At most MAX_THROTTLE_KEYS
    (name, address) pairs are remembered: pairs whose requests have all expired are dropped first, then the oldest
    (so a flood from more addresses than that can make the limit forget some of them, never grow memory)."""
    _windows[name] = max(_windows.get(name, 0.0), per_seconds)

    async def dependency(request: Request):
        key = (name, (request.client.host if request.client else "?"))
        now = time.monotonic()
        q = _buckets.get(key)
        if q is None:
            _make_room(_buckets, lambda k: _windows.get(k[0], per_seconds), now, MAX_THROTTLE_KEYS)
            q = _buckets[key] = deque()
        _prune(q, now, per_seconds)
        if len(q) >= calls:
            retry = max(1, int(per_seconds - (now - q[0])))
            raise HTTPException(status_code=429, detail=f"Too many requests; try again in {retry}s.",
                                headers={"Retry-After": str(retry)})
        q.append(now)

    return dependency
