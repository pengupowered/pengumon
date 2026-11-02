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

from pengumon import __version__, notify, paths
from pengumon.api.router import metrics_router, router as api_router
from pengumon.api.state import AppState
from pengumon.clients import component as clients
from pengumon.components import run_components
from pengumon.config import load_config, load_server_config
from pengumon.db import retention, storage
from pengumon.devices import component as devices
from pengumon.engine import line_load, probing
from pengumon.engine.classifier import component as classifier
from pengumon.live import component as live
from pengumon.observability import apply_config_level, get_logger, grafana, setup_logging
from pengumon.observability.export import setup_telemetry
from pengumon.observability.telemetry import fastapi_telemetry
from pengumon.security import SESSIONS_PATH, FileSessionStore, SecurityConfig, SecurityMiddleware
from pengumon.services import component as services
from pengumon.web import assets, pages

# Logging is set up by main() (observability.setup_logging), not at import, so importing the app in tests or tools
# leaves the host process's logging alone.
logger = get_logger("pengumon")

# Registered and started in this order, stopped in the reverse order (docs/architecture.md says why it is this one).
# Adding a feature is a module with register(ctx) and one line here.
COMPONENTS = [
    devices,        # first: a device config that cannot run stops the start before the database opens
    storage,        # opens (migrates) the database; closes it after every later component has stopped
    probing,        # the prober with the stored custom targets; starts the probe loop
    retention,      # the start-up cleanup, before the load backfills read the history
    line_load,      # line capacity, the load backfills, the monitors' load and probe-loss hooks
    notify,         # ntfy, for the classifier and the service checks
    classifier,     # needs the line load and the notifier; its path traces end before the database closes
    services,       # optional: host service checks
    clients,        # optional: the client list
    live,           # the websocket broadcaster
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    grafana.configure(os.environ)  # an invalid PENGUMON_GRAFANA_URL stops the start here
    telemetry = setup_telemetry()  # exports only when an OTLP endpoint is set (OTEL_EXPORTER_OTLP_ENDPOINT)
    paths.ensure_data_dir()  # a first start on a fresh system: the database, secrets and sessions live in it
    config = load_config()
    apply_config_level(config.server.log_level)
    logger.info(f"Loaded config with {len(config.targets)} ICMP targets and {len(config.dns_probes)} DNS probes.")

    state = AppState(config=config, repository=storage.repository_for(config))
    async with run_components(app, config, COMPONENTS, state):
        yield

    logger.info("Pengumon shut down successfully.")
    telemetry.shutdown()  # last, so the shutdown's own log lines and spans go out too


async def _validation_error(request: Request, exc: RequestValidationError):
    """FastAPI's 422 answer, except that on credential routes the errors leave out the submitted values: FastAPI
    echoes each invalid `input`, which there is a password (pydantic's hide_input_in_errors does not reach it)."""
    if request.url.path.rstrip("/").endswith("/credentials"):
        errors = [{k: v for k, v in error.items() if k != "input"} for error in exc.errors()]
        return JSONResponse(status_code=422, content={"detail": jsonable_encoder(errors)})
    return await request_validation_exception_handler(request, exc)


def create_app() -> FastAPI:
    """The app: middleware, the static files, the core API, each component's router, metrics and the page."""
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

    # Outermost: Host/Origin/token guards, body limit and the security headers (HTTP and websocket).
    security_config = SecurityConfig.from_env(session_store=FileSessionStore(SESSIONS_PATH))
    app.add_middleware(SecurityMiddleware, cfg=security_config)
    app.state.security_config = security_config  # the clients API reads whether a token is set (privacy.client_details)

    assets.mount_static(app)
    app.include_router(api_router)
    # Mounted whether or not the feature is configured: the routes and the OpenAPI schema never depend on config.yaml.
    for component in COMPONENTS:
        if getattr(component, "router", None) is not None:
            app.include_router(component.router)
    app.include_router(metrics_router)
    app.include_router(pages.router)
    return app


app = create_app()
security_config: SecurityConfig = app.state.security_config  # tests adjust the guards of the app through it


def main():
    """Starts the server on the configured address (pengumon.paths.listen_address: PENGUMON_HOST/PORT, then the
    config's server section, then 127.0.0.1:8000). `pengumon run` and `python -m pengumon.main` both end here."""
    host, port, _, _ = paths.listen_address(load_server_config())
    setup_logging()
    # log_config=None: uvicorn keeps its hands off logging; its loggers go through the pengumon handler.
    uvicorn.run("pengumon.main:app", host=host, port=port, reload=False, log_config=None)


if __name__ == "__main__":
    main()
