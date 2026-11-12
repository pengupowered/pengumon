"""Logging and OpenTelemetry setup (pengumon.observability)."""

import io
import json
import logging

import pytest
from opentelemetry import trace
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags

from pengumon.observability import logs
from pengumon.observability.logs import (
    JsonFormatter, StateLog, TextFormatter, TraceContextFilter, apply_config_level, get_logger, parse_levels,
    setup_logging,
)
from pengumon.observability.telemetry import exclude_from_telemetry, fastapi_telemetry

from device_helpers import Clock


class Capture(logging.Handler):
    """Keeps the records and formats them with the formatter under test."""

    def __init__(self, formatter):
        super().__init__(logging.DEBUG)
        self.setFormatter(formatter)
        self.addFilter(TraceContextFilter())
        self.records = []

    def emit(self, record):
        self.records.append(record)

    def lines(self):
        return [self.format(r) for r in self.records]


@pytest.fixture
def capture(request):
    """A logger 'pengumon.test.<test>' writing to a Capture with the text formatter (or the one parametrised)."""
    formatter = getattr(request, "param", None) or TextFormatter()
    handler = Capture(formatter)
    logger = logging.getLogger(f"pengumon.test.{request.node.name}")
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    yield logger, handler
    logger.removeHandler(handler)


@pytest.fixture
def clean_root():
    """setup_logging changes the root logger and a few named loggers; restore them afterwards."""
    root = logging.getLogger()
    saved = (list(root.handlers), root.level)
    named = {n: (logging.getLogger(n).level, list(logging.getLogger(n).handlers), logging.getLogger(n).propagate)
             for n in ("uvicorn", "uvicorn.error", "uvicorn.access", "httpx", "httpcore", "pengumon.devices")}
    yield root
    root.handlers[:] = saved[0]
    root.setLevel(saved[1])
    for name, (level, handlers, propagate) in named.items():
        lg = logging.getLogger(name)
        lg.setLevel(level)
        lg.handlers[:] = handlers
        lg.propagate = propagate


def _span(trace_id=0x1234, span_id=0x56):
    return NonRecordingSpan(SpanContext(trace_id=trace_id, span_id=span_id, is_remote=False,
                                        trace_flags=TraceFlags(TraceFlags.SAMPLED)))


# --- formatting -------------------------------------------------------------------------------------------

def test_text_line_keeps_the_familiar_prefix_and_appends_context(capture):
    logger, handler = capture
    log = get_logger(logger.name, device="router")
    log.warning("poll failed", error="ConnectTimeout", duration_ms=812.25, retried=False, detail=None)
    line = handler.lines()[0]
    assert line.endswith(f"[WARNING] {logger.name}: poll failed device=router error=ConnectTimeout "
                         "duration_ms=812.25 retried=false detail=-")


def test_text_values_with_spaces_quotes_or_newlines_are_quoted(capture):
    logger, handler = capture
    get_logger(logger.name).info("x", reason='timed out "twice"\nthen gave up', empty="")
    assert handler.lines()[0].endswith(r'x reason="timed out \"twice\"\nthen gave up" empty=""')


def test_plain_logging_calls_are_unchanged(capture):
    logger, handler = capture
    logger.info("Database initialized at %s", "data/pengumon.db")
    assert handler.lines()[0].endswith(f"[INFO] {logger.name}: Database initialized at data/pengumon.db")


def test_traceback_stays_after_the_context(capture):
    logger, handler = capture
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        get_logger(logger.name).exception("poll crashed", device="modem")
    first, *rest = handler.lines()[0].splitlines()
    assert first.endswith("poll crashed device=modem")
    assert rest[0] == "Traceback (most recent call last):" and rest[-1] == "RuntimeError: boom"


def test_bind_adds_context_and_call_context_wins(capture):
    logger, handler = capture
    log = get_logger(logger.name, device="router", op="poll").bind(driver="asuswrt")
    log.info("done", op="login")
    assert handler.lines()[0].endswith("done device=router op=login driver=asuswrt")


def test_extra_from_stdlib_callers_is_kept(capture):
    logger, handler = capture
    get_logger(logger.name, device="router").info("done", extra={"custom": 1})
    assert handler.records[0].custom == 1


@pytest.mark.parametrize("capture", [JsonFormatter()], indirect=True)
def test_json_line_has_the_fields_and_context(capture):
    logger, handler = capture
    get_logger(logger.name, device="router").warning("poll failed", error="Timeout", message="clash")
    entry = json.loads(handler.lines()[0])
    assert entry["level"] == "WARNING" and entry["logger"] == logger.name
    assert entry["message"] == "poll failed" and entry["device"] == "router" and entry["error"] == "Timeout"
    assert entry["ctx.message"] == "clash"  # a context key never overwrites a fixed field
    assert entry["time"].endswith("Z") and "T" in entry["time"]


@pytest.mark.parametrize("capture", [JsonFormatter()], indirect=True)
def test_json_line_carries_the_traceback(capture):
    logger, handler = capture
    try:
        raise ValueError("bad")
    except ValueError:
        logger.exception("failed")
    assert json.loads(handler.lines()[0])["exception"].endswith("ValueError: bad")


# --- trace context ----------------------------------------------------------------------------------------

def test_trace_and_span_id_are_added_while_a_span_is_current(capture):
    logger, handler = capture
    with trace.use_span(_span(), end_on_exit=False):
        logger.info("inside")
    logger.info("outside")
    inside, outside = handler.lines()
    assert inside.endswith(f"inside trace_id={0x1234:032x} span_id={0x56:016x}")
    assert outside.endswith("outside")


def test_no_ids_without_a_valid_span_context(capture):
    """The API without an SDK hands out spans with an invalid context: nothing is added."""
    logger, handler = capture
    with trace.get_tracer("t").start_as_current_span("s"):
        logger.info("plain")
    assert handler.lines()[0].endswith("plain")


# --- setup ------------------------------------------------------------------------------------------------

def test_setup_installs_one_handler_and_is_idempotent(clean_root, monkeypatch):
    for var in ("PENGUMON_LOG_LEVEL", "PENGUMON_LOG_LEVELS", "PENGUMON_LOG_FORMAT"):
        monkeypatch.delenv(var, raising=False)
    first = setup_logging(stream=io.StringIO())
    second = setup_logging(stream=io.StringIO())
    ours = [h for h in clean_root.handlers if h.get_name() == logs.HANDLER_NAME]
    assert ours == [second] and first not in clean_root.handlers
    assert clean_root.level == logging.INFO
    assert logging.getLogger("httpx").level == logging.WARNING


def test_setup_routes_uvicorn_through_the_handler(clean_root):
    logging.getLogger("uvicorn.error").addHandler(logging.NullHandler())
    logging.getLogger("uvicorn.access").propagate = False
    stream = io.StringIO()
    setup_logging("info", levels="", fmt="text", stream=stream)
    logging.getLogger("uvicorn.access").info('%s - "%s %s HTTP/%s" %d', "203.0.113.4:5", "GET", "/", "1.1", 200)
    assert logging.getLogger("uvicorn.error").handlers == []
    assert stream.getvalue().strip().endswith('[INFO] uvicorn.access: 203.0.113.4:5 - "GET / HTTP/1.1" 200')


def test_environment_wins_over_arguments(clean_root, monkeypatch):
    monkeypatch.setenv("PENGUMON_LOG_LEVEL", "warning")
    monkeypatch.setenv("PENGUMON_LOG_LEVELS", "pengumon.devices=debug")
    monkeypatch.setenv("PENGUMON_LOG_FORMAT", "json")
    stream = io.StringIO()
    setup_logging("debug", levels="", fmt="text", stream=stream)
    assert clean_root.level == logging.WARNING
    assert logging.getLogger("pengumon.devices").level == logging.DEBUG
    logging.getLogger("pengumon.devices").debug("visible")
    assert json.loads(stream.getvalue())["message"] == "visible"


def test_config_level_applies_unless_the_environment_sets_one(clean_root, monkeypatch):
    monkeypatch.delenv("PENGUMON_LOG_LEVEL", raising=False)
    apply_config_level("debug")
    assert clean_root.level == logging.DEBUG
    monkeypatch.setenv("PENGUMON_LOG_LEVEL", "error")
    apply_config_level("info")
    assert clean_root.level == logging.DEBUG


def test_parse_levels():
    assert parse_levels(" a=debug, b.c=WARNING ,") == {"a": "DEBUG", "b.c": "WARNING"}
    assert parse_levels(None) == {}
    for bad in ("a", "a=loud", "=info"):
        with pytest.raises(ValueError):
            parse_levels(bad)


@pytest.mark.parametrize("kwargs", [{"level": "loud"}, {"fmt": "xml"}])
def test_invalid_settings_are_refused(clean_root, monkeypatch, kwargs):
    for var in ("PENGUMON_LOG_LEVEL", "PENGUMON_LOG_LEVELS", "PENGUMON_LOG_FORMAT"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(ValueError):
        setup_logging(stream=io.StringIO(), **kwargs)


# --- StateLog ---------------------------------------------------------------------------------------------

def _state_log(capture):
    logger, handler = capture
    clock = Clock()
    return StateLog(get_logger(logger.name, device="router"), "Router poll", clock=clock), clock, handler


def _visible(handler):
    return [(r.levelname, r.getMessage()) for r in handler.records if r.levelno > logging.DEBUG]


def test_state_log_warns_once_then_reminds_every_15_minutes(capture):
    state, clock, handler = _state_log(capture)
    assert state.failure("ConnectTimeout") is True
    for _ in range(89):  # 10 s polls for almost 15 minutes
        clock.now += 10
        assert state.failure("ConnectTimeout") is False
    clock.now += 10
    assert state.failure("ConnectTimeout") is True
    assert _visible(handler) == [("WARNING", "Router poll failing: ConnectTimeout"),
                                 ("WARNING", "Router poll still failing: ConnectTimeout")]
    reminder = handler.records[-1]
    assert getattr(reminder, logs.CONTEXT_ATTR) == {
        "device": "router", "error": "ConnectTimeout", "failures": 91, "failing_for_s": 900}


def test_state_log_reports_a_different_error_at_once(capture):
    state, clock, handler = _state_log(capture)
    state.failure("ConnectTimeout")
    clock.now += 10
    assert state.failure("HTTP 401: login refused") is True
    clock.now += 10
    assert state.failure("HTTP 401: login refused") is False
    assert _visible(handler)[-1] == ("WARNING", "Router poll still failing, now: HTTP 401: login refused")


def test_state_log_recovery_names_the_outage_and_resets(capture):
    state, clock, handler = _state_log(capture)
    assert state.success() is False  # nothing to recover from: silent
    state.failure("ConnectTimeout")
    clock.now += 300
    state.failure("ConnectTimeout")
    assert state.failing
    assert state.success() is True
    assert _visible(handler)[-1] == ("INFO", "Router poll recovered after 5 min (2 failed attempts)")
    assert not state.failing and state.failures == 0
    state.failure("ConnectTimeout")
    assert _visible(handler)[-1] == ("WARNING", "Router poll failing: ConnectTimeout")


def test_state_log_logs_every_repeat_at_debug(capture):
    state, clock, handler = _state_log(capture)
    state.failure("x")
    clock.now += 1
    state.failure("x")
    assert [r.levelname for r in handler.records] == ["WARNING", "DEBUG"]


# --- telemetry --------------------------------------------------------------------------------------------

@pytest.mark.parametrize("path, excluded", [
    ("/api/modem/credentials", True), ("/api/router/credentials/", True), ("/api/devices/modem/credentials", True),
    ("/login", True), ("/static/v/abc/js/main.js", True), ("/api/health", True), ("/metrics", True),
    ("/api/status", False), ("/api/incidents/3/inspect", False), ("/ws", False), ("/", False),
])
def test_exclusions(path, excluded):
    assert exclude_from_telemetry({"type": "http", "path": path}) is excluded


def test_the_app_uses_the_exclusions():
    from pengumon.main import app
    assert fastapi_telemetry()["exclude"] is exclude_from_telemetry
    assert app.router is not None  # constructed with telemetry=fastapi_telemetry() without error


def test_credential_requests_never_reach_telemetry(monkeypatch):
    """FastAPI records validation failures with the input values; the credentials route must not get there.

    FastAPI only runs its telemetry when a provider is configured, so the test passes one explicitly (a no-op
    provider: no SDK is needed to see which requests FastAPI would observe)."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from pydantic import BaseModel

    seen = []

    def exclude(scope):
        excluded = exclude_from_telemetry(scope)
        if scope.get("type") == "http":
            seen.append((scope["path"], excluded))
        return excluded

    class Body(BaseModel):
        password: str

    app = FastAPI(telemetry={"exclude": exclude, "tracer_provider": trace.NoOpTracerProvider()})

    @app.post("/api/modem/credentials")
    async def creds(body: Body):
        return {}

    @app.get("/api/status")
    async def status():
        return {}

    with TestClient(app) as client:
        assert client.post("/api/modem/credentials", json={"password": 5}).status_code == 422
        assert client.get("/api/status").status_code == 200
    assert seen == [("/api/modem/credentials", True), ("/api/status", False)]


def test_state_log_with_a_key_treats_messages_of_the_same_key_as_the_same_error(capture):
    state, clock, handler = _state_log(capture)
    state.failure("timeout: connect timed out", key="timeout")
    clock.now += 10
    assert state.failure("timeout: read timed out", key="timeout") is False  # not "a different error"
    clock.now += 10
    assert state.failure("unreachable: refused", key="unreachable") is True
    assert _visible(handler) == [("WARNING", "Router poll failing: timeout: connect timed out"),
                                 ("WARNING", "Router poll still failing, now: unreachable: refused")]


def test_state_log_without_a_key_treats_every_new_message_as_a_different_error(capture):
    state, clock, handler = _state_log(capture)
    state.failure("timeout: connect timed out")
    clock.now += 10
    assert state.failure("timeout: read timed out") is True
    assert _visible(handler)[-1] == ("WARNING", "Router poll still failing, now: timeout: read timed out")


# --- credentials in a websocket handshake ------------------------------------------------------------------------

COOKIE_SECRET, BEARER_SECRET = "cookie-canary-5f1e2d", "bearer-canary-9a8b7c"


@pytest.mark.filterwarnings("ignore")
@pytest.mark.parametrize("ws", ["auto", "websockets-sansio", "websockets"])
async def test_a_websocket_handshake_never_logs_its_credentials(clean_root, monkeypatch, ws):
    """uvicorn hands its `uvicorn.error` logger to `websockets`, which logs every handshake header at DEBUG: with the log
    level at debug, each /ws connection put the browser's session cookie (or the bearer token) into the log. Served
    by uvicorn on loopback, the real app, every implementation uvicorn can pick here (wsproto is not installed)."""
    import asyncio
    import socket

    import uvicorn
    from websockets.asyncio.client import connect

    from pengumon.main import app, security_config

    for name, value in (("access_token", ""), ("allowed_hosts", []), ("allowed_origins", []), ("view_without_token", False)):
        monkeypatch.setattr(security_config, name, value)
    for var in ("PENGUMON_LOG_LEVEL", "PENGUMON_LOG_LEVELS", "PENGUMON_LOG_FORMAT"):
        monkeypatch.delenv(var, raising=False)
    stream = io.StringIO()
    setup_logging("debug", levels="", fmt="text", stream=stream)
    records = Capture(TextFormatter())
    clean_root.addHandler(records)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, lifespan="off", log_config=None, ws=ws))
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(200):
            if server.started:
                break
            await asyncio.sleep(0.05)
        assert server.started
        silent = logging.getLogger("pengumon.test.ws-client")       # (the client's own debug lines are not the app's)
        silent.setLevel(logging.WARNING)
        headers = {"Cookie": f"pg_session={COOKIE_SECRET}", "Authorization": f"Bearer {BEARER_SECRET}"}
        async with connect(f"ws://127.0.0.1:{port}/ws", additional_headers=headers, logger=silent,
                           origin=f"http://127.0.0.1:{port}") as client:
            await client.send("ping")
        await asyncio.sleep(0.1)
    finally:
        server.should_exit = True
        await task
    text = "\n".join(records.lines()) + stream.getvalue()
    assert any(r.name == "uvicorn.error" and r.levelno == logging.DEBUG for r in records.records)   # debug was on
    assert COOKIE_SECRET not in text and BEARER_SECRET not in text
    assert "< cookie: [redacted]" in text and "< authorization: [redacted]" in text    # (the line stays, its value not)
    assert "< user-agent: " in text                                                    # (other headers are untouched)


# --- addresses with a token in the path or query ----------------------------------------------------------

async def test_httpx_request_lines_never_log_a_path_or_query(caplog):
    """`server.log_levels = httpx=info` makes httpx log "HTTP Request: POST <url>" for every alert: a webhook's token
    sits in that path, the ntfy topic is the path, a host-app check may carry an API key in the query."""
    import httpx
    secret_path, secret_query = "T000/path-canary-77aa", "apikey=query-canary-55bb"
    handler = httpx.MockTransport(lambda request: httpx.Response(200))
    caplog.set_level(logging.INFO, logger="httpx")
    async with httpx.AsyncClient(transport=handler) as client:
        await client.post(f"https://hooks.example.test:8443/{secret_path}?{secret_query}", content=b"x")
        await client.get(f"http://192.0.2.10:8080/health?{secret_query}")
    lines = [r.getMessage() for r in caplog.records if r.name == "httpx"]
    assert len(lines) == 2
    text = "\n".join(lines)
    assert "path-canary" not in text and "query-canary" not in text
    assert "https://hooks.example.test:8443/[path hidden]" in text and "http://192.0.2.10:8080/[path hidden]" in text
    assert "POST" in text and "200" in text
