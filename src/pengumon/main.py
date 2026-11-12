"""FastAPI application entrypoint for Pengumon: the app factory, its middleware, and the ordered list of the
components that make up the running app (docs/architecture.md)."""

import os
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse

from pengumon import __version__, paths
from pengumon.api.router import metrics_router, router as api_router
from pengumon.state import AppState
from pengumon.clients import component as clients
from pengumon.components import run_components
from pengumon.config import load_config, load_server_config
from pengumon.db import retention, storage
from pengumon.devices import component as devices
from pengumon.engine import line_load, probing
from pengumon.engine.classifier import component as classifier
from pengumon.engine.discovery import component as discovery
from pengumon.live import component as live
from pengumon.notify import component as notify
from pengumon.observability import apply_config_level, get_logger, grafana, setup_logging
from pengumon.observability import component as runtime
from pengumon.observability.export import setup_telemetry
from pengumon.observability.telemetry import fastapi_telemetry
from pengumon.security import (SESSIONS_PATH, FileSessionStore, SecurityConfig, SecurityMiddleware, tls_files,
                               tls_requested)
from pengumon.services import component as services
from pengumon.settings import component as settings
from pengumon.settings import preview_component as settings_preview
from pengumon.speedtest import component as speedtest
from pengumon.web import assets, pages

# Logging is set up by main() (observability.setup_logging), not at import, so importing the app in tests or tools
# leaves the host process's logging alone.
logger = get_logger("pengumon")

# The only message a page sends over /ws is {"resync": true} (api/live.py reads at most 64 characters); uvicorn refuses a
# bigger frame (close 1009) instead of buffering it.
WS_MAX_MESSAGE_BYTES = 4096

# Registered and started in this order, stopped in the reverse order (docs/architecture.md says why it is this one).
# Adding a feature is a module with register(ctx) and one line here.
COMPONENTS = [
    storage,        # first: opens (migrates) the database; closes it after every later component has stopped
    settings,       # the settings store: the one-time import of config.yaml, then the stored settings into the config
    devices,        # built from the stored devices; a device that cannot run is reported, the start goes on
    probing,        # the probe path (stored in the database), the prober; starts the probe loop
    retention,      # the start-up cleanup, before the load backfills read the history
    line_load,      # line capacity, the load backfills, the monitors' load and probe-loss hooks
    notify,         # ntfy, for the classifier and the service checks
    classifier,     # needs the line load and the notifier; its path traces end before the database closes
    speedtest,      # optional: the scheduled speed test; needs the line load and the classifier (it masks its verdicts)
    settings_preview,  # the outage-rules preview jobs; a running replay is killed at shutdown
    services,       # optional: host service checks
    clients,        # optional: the client list
    live,           # the websocket broadcaster
    discovery,      # optional: network discovery on request (POST /api/discovery); stops a running one first
    runtime,        # the event-loop monitor (lag and task count for the telemetry)
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    grafana.configure(os.environ)  # an invalid PENGUMON_GRAFANA_URL stops the start here
    telemetry = setup_telemetry()  # exports only when an OTLP endpoint is set (OTEL_EXPORTER_OTLP_ENDPOINT)
    paths.ensure_data_dir()  # a first start on a fresh system: the database, secrets and sessions live in it
    config = load_config(lenient_rest=True)  # the bootstrap keys strictly; the settings come from the store
    apply_config_level(config.server.log_level)
    logger.info(f"Loaded config with {len(config.dns_probes)} DNS probes (the probe path is loaded from the database).")

    state = AppState(config=config, repository=storage.repository_for(config))
    async with run_components(app, config, COMPONENTS, state):
        yield

    logger.info("Pengumon shut down successfully.")
    telemetry.shutdown()  # last, so the shutdown's own log lines and spans go out too


async def _validation_error(request: Request, exc: RequestValidationError):
    """FastAPI's 422 answer, except that on credential routes (a device's, the sign-in's, a settings save, which may
    carry secrets and the current password) the errors leave out the submitted values: FastAPI echoes each invalid
    `input`, which there is a password (pydantic's hide_input_in_errors does not reach it)."""
    path = request.url.path.rstrip("/")
    if (path.endswith("/credentials") or path == "/api/access" or path.startswith("/api/access/")
            or path == "/api/settings"):
        errors = [{k: v for k, v in error.items() if k != "input"} for error in exc.errors()]
        return JSONResponse(status_code=422, content={"detail": jsonable_encoder(errors)})
    return await request_validation_exception_handler(request, exc)


def create_app() -> FastAPI:
    """The app: middleware, the static files, the API, metrics and the page."""
    app = FastAPI(
        title="Pengumon",
        description="Network Diagnostics & Outage Root-Cause Analyzer",
        version=__version__,
        lifespan=lifespan,
        telemetry=fastapi_telemetry(),
    )
    app.add_exception_handler(RequestValidationError, _validation_error)

    # Responses over 1 KiB (incident list, history) are gzipped; websocket frames are not touched.
    # Level 5, not the default 9: on the largest responses it is ~2.5x faster for a few percent more bytes.
    app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=5)

    # Outermost: Host/Origin/token guards, body limit and the security headers (HTTP and websocket). One instance, shared
    # by the middleware and app.state: the sign-in (pengumon.access) puts the password set on the page into it at start
    # and changes it in place, and the middleware reads it on every request.
    security_config = SecurityConfig.from_env(session_store=FileSessionStore(SESSIONS_PATH))
    if tls_requested():  # HTTPS with your own certificate (main() checks the files): the session cookie is Secure
        security_config.cookie_secure = True
    app.add_middleware(SecurityMiddleware, cfg=security_config)
    app.state.security_config = security_config  # the clients API reads whether a token is set (privacy.client_details)

    assets.mount_static(app)
    app.include_router(api_router)  # every feature's routes (api/router.py)
    app.include_router(metrics_router)
    app.include_router(pages.router)
    return app


app = create_app()
security_config: SecurityConfig = app.state.security_config  # tests adjust the guards of the app through it


def tls_options() -> dict:
    """uvicorn's ssl options from PENGUMON_TLS_CERT / PENGUMON_TLS_KEY ({} for plain HTTP). SystemExit with the reason
    when only one is set or the pair cannot be loaded: the start stops rather than serve plain HTTP by mistake."""
    try:
        files = tls_files()
    except ValueError as e:
        raise SystemExit(f"Pengumon cannot start with HTTPS: {e}") from None
    return {"ssl_certfile": files[0], "ssl_keyfile": files[1]} if files else {}


TRUSTED_PROXIES_ENV = "PENGUMON_TRUSTED_PROXIES"


def proxy_options(environ=None) -> dict:
    """uvicorn's options for X-Forwarded-For and X-Forwarded-Proto. Off unless PENGUMON_TRUSTED_PROXIES names the
    reverse proxy's addresses (comma separated; networks such as 10.0.0.0/8 too): uvicorn's own default trusts them
    from 127.0.0.1, and every process on this host connects from there (the container uses the host's network), so any
    of them could choose the address the failed sign-ins and the rate limits are counted under. SystemExit for "*",
    which would let every client choose it."""
    value = (os.environ if environ is None else environ).get(TRUSTED_PROXIES_ENV, "").strip()
    if not value:
        return {"proxy_headers": False}
    if "*" in value:
        raise SystemExit(f"Pengumon cannot start: {TRUSTED_PROXIES_ENV}=* would let every client choose the address its "
                         "sign-in attempts are counted under. Name the reverse proxy's address instead.")
    return {"proxy_headers": True, "forwarded_allow_ips": value}


def main():
    """Starts the server on the configured address (pengumon.paths.listen_address: PENGUMON_HOST/PORT, then the
    config's server section, then 127.0.0.1:8000), over HTTPS when a certificate is given (tls_options).
    `pengumon run` and `python -m pengumon.main` both end here."""
    host, port, _, _ = paths.listen_address(load_server_config())
    ssl = tls_options()
    proxies = proxy_options()
    setup_logging()
    if ssl:
        logger.info(f"Serving HTTPS with the certificate {ssl['ssl_certfile']}")
    if proxies["proxy_headers"]:
        logger.info(f"Trusting X-Forwarded-For from {proxies['forwarded_allow_ips']} ({TRUSTED_PROXIES_ENV})")
    # log_config=None: uvicorn keeps its hands off logging; its loggers go through the pengumon handler.
    uvicorn.run("pengumon.main:app", host=host, port=port, reload=False, log_config=None, ws_max_size=WS_MAX_MESSAGE_BYTES,
                **proxies, **ssl)


if __name__ == "__main__":
    main()
