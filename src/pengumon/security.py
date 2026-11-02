"""Request guards for Pengumon: the shared homeguard guards plus this app's profile.

The guards themselves (Host allow-list, Origin/CSRF check, access token and session cookie, body caps, login
throttle, rate limits, websocket checks, security headers) live in the vendored `homeguard` package; edit them
upstream, not here. This module declares what is specific to Pengumon and re-exports the names the rest of the
app imports.
"""

from pengumon import paths
from pengumon._vendor.homeguard.security import (
    AppProfile,
    CacheRule,
    FileSessionStore,
    LoginThrottle,
    SecurityMiddleware,
    host_allowed,
    origin_allowed,
    rate_limit,
    reset_rate_limits,
    session_status,
)
from pengumon._vendor.homeguard.security import SecurityConfig as _BaseConfig

__all__ = ["CSP", "PROFILE", "PUBLIC_PATHS", "SESSION_COOKIE", "SESSIONS_PATH", "FileSessionStore", "LoginThrottle",
           "SecurityConfig", "SecurityMiddleware", "host_allowed", "origin_allowed", "rate_limit", "reset_rate_limits",
           "session_status"]

# Kept in one place. No inline scripts or handlers anywhere (the dashboard delegates events), so script-src
# stays strict; 'self' in connect-src covers the same-origin websocket in current browsers. The dashboard has
# no <object>, <base> or cross-origin forms, so those are shut off as well.
CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "font-src 'self'; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'self'"
)

# Open even when a token is configured: container health check, the login page and its icon (and /static/).
PUBLIC_PATHS = ("/api/health", "/login", "/favicon.ico")

PROFILE = AppProfile(
    slug="pengumon",
    title="Pengumon",
    env_prefix="PENGUMON",
    cookie_name="pg_session",
    session_purpose="pg-session-v1",  # part of the cookie's signing key: changing it signs every browser out
    csp=CSP,
    public_paths=PUBLIC_PATHS,
    referrer_policy="strict-origin-when-cross-origin",
    no_token_warning=(
        "No PENGUMON_ACCESS_TOKEN set: anyone who can reach this port can read your "
        "network data and the modem/router credential settings. Set a token if the port is reachable beyond this PC."
    ),
    cache_rules=(
        # content-addressed: the tree hash is part of the path, so the bytes behind it never change.
        # Errors are never cached: a 404 for an old hash must not outlive a rollback.
        CacheRule("/static/v/", "public, max-age=31536000, immutable", "no-store"),
    ),
    # Old ?v= URLs on /static/ are cached by the app's VersionedStaticFiles, so no other cache rules here.
)

SESSION_COOKIE = PROFILE.cookie_name

# "Sign out everywhere" (POST /logout?everywhere=1) is remembered here, next to secrets.json in the data directory
# (pengumon.paths): one time, after which earlier sign-ins are refused (homeguard's FileSessionStore).
SESSIONS_PATH = paths.sessions_path()


class SecurityConfig(_BaseConfig):
    PROFILE = PROFILE
