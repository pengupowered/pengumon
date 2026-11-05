"""Logging: one stdout handler, a readable line with key=value context (or one JSON object per line), the trace and
span id when a span is recording, and StateLog for dependencies that keep failing.

Context goes in as keyword arguments of a ContextLogger, per call or bound once:

    log = get_logger("pengumon.devices", device="router")
    log.warning("poll failed", error="ConnectTimeout", duration_ms=812)

    2026-10-31 14:20:01,123 [WARNING] pengumon.devices: poll failed device=router error=ConnectTimeout duration_ms=812

Context values are identifiers, outcomes and measurements: never credentials, and never URLs with a query string.

Configuration (environment wins over the setting):
  PENGUMON_LOG_LEVEL   root level (the server.log_level setting; default info)
  PENGUMON_LOG_LEVELS  per-logger levels, "pengumon.devices=debug,uvicorn.access=warning"
  PENGUMON_LOG_FORMAT  "text" (default) or "json"
"""

import json
import logging
import os
import sys
import time
from typing import Any, Callable, Dict, Mapping, Optional, TextIO

from opentelemetry import trace

from pengumon.settings.env import env_value

HANDLER_NAME = "pengumon"
CONTEXT_ATTR = "pengumon_context"
TEXT_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
# Loggers that are too chatty at INFO for a service that runs for months: httpx logs every request.
DEFAULT_LEVELS = {"httpx": "WARNING", "httpcore": "WARNING"}
_ADAPTER_KWARGS = frozenset({"exc_info", "stack_info", "stacklevel", "extra"})
# Attributes of every LogRecord; a context key with one of these names would be ambiguous in JSON output.
_RECORD_ATTRS = frozenset(vars(logging.LogRecord("", 0, "", 0, "", (), None))) | {"message", "asctime"}


class ContextLogger(logging.LoggerAdapter):
    """A logger that takes context as keyword arguments. `bind` returns a logger with more context."""

    def __init__(self, logger: logging.Logger, context: Optional[Mapping[str, Any]] = None):
        super().__init__(logger, dict(context or {}))

    def bind(self, **context: Any) -> "ContextLogger":
        return ContextLogger(self.logger, {**self.extra, **context})

    def process(self, msg, kwargs):
        context = dict(self.extra)
        for key in [k for k in kwargs if k not in _ADAPTER_KWARGS]:
            context[key] = kwargs.pop(key)
        extra = dict(kwargs.get("extra") or {})
        extra[CONTEXT_ATTR] = {**extra.get(CONTEXT_ATTR, {}), **context}
        kwargs["extra"] = extra
        return msg, kwargs


def get_logger(name: str, **context: Any) -> ContextLogger:
    return ContextLogger(logging.getLogger(name), context)


def _text_value(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:.6g}"
    text = str(value)
    if not text or any(c in text for c in ' "=\n\r\t'):
        return json.dumps(text, ensure_ascii=False)
    return text


def _context(record: logging.LogRecord) -> Dict[str, Any]:
    context = dict(getattr(record, CONTEXT_ATTR, None) or {})
    trace_id = getattr(record, "otel_trace_id", None)
    if trace_id:
        context["trace_id"] = trace_id
        context["span_id"] = record.otel_span_id
    return context


class TextFormatter(logging.Formatter):
    """The familiar `time [LEVEL] logger: message` line with the context appended as key=value pairs (before any
    traceback, which stays on the following lines)."""

    def __init__(self):
        super().__init__(TEXT_FORMAT)

    def formatMessage(self, record: logging.LogRecord) -> str:
        line = super().formatMessage(record)
        context = _context(record)
        if not context:
            return line
        return line + " " + " ".join(f"{key}={_text_value(value)}" for key, value in context.items())


class JsonFormatter(logging.Formatter):
    """One JSON object per line: time (UTC, ISO 8601), level, logger, message, the context, and the traceback."""

    def format(self, record: logging.LogRecord) -> str:
        created = time.gmtime(record.created)
        entry: Dict[str, Any] = {
            "time": time.strftime("%Y-%m-%dT%H:%M:%S", created) + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in _context(record).items():
            entry[f"ctx.{key}" if key in entry or key in _RECORD_ATTRS else key] = value
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            entry["stack"] = self.formatStack(record.stack_info)
        return json.dumps(entry, ensure_ascii=False, default=str)


class TraceContextFilter(logging.Filter):
    """Puts the current span's trace and span id on the record when a span is recording (with the OpenTelemetry
    SDK configured); without one there is no valid span context and nothing is added."""

    def filter(self, record: logging.LogRecord) -> bool:
        span_context = trace.get_current_span().get_span_context()
        if span_context.is_valid:
            record.otel_trace_id = format(span_context.trace_id, "032x")
            record.otel_span_id = format(span_context.span_id, "016x")
        return True


def parse_levels(spec: Optional[str]) -> Dict[str, str]:
    """"a=debug,b.c=warning" -> {"a": "DEBUG", "b.c": "WARNING"}. Raises ValueError for an entry it can't use."""
    levels: Dict[str, str] = {}
    for item in (spec or "").split(","):
        item = item.strip()
        if not item:
            continue
        name, sep, level = item.partition("=")
        name, level = name.strip(), level.strip().upper()
        if not sep or not name or level not in logging.getLevelNamesMapping():
            raise ValueError(f"invalid logger level {item!r}: expected name=level with a standard level name")
        levels[name] = level
    return levels


def _level(name: Optional[str]) -> str:
    level = (name or "info").strip().upper()
    if level not in logging.getLevelNamesMapping():
        raise ValueError(f"invalid log level {name!r}")
    return level


def setup_logging(level: Optional[str] = None, *, levels: Optional[str] = None, fmt: Optional[str] = None,
                  stream: Optional[TextIO] = None) -> logging.Handler:
    """Installs the pengumon handler on the root logger (replacing an earlier one, so calling it again is safe)
    and routes uvicorn's loggers through it. Other handlers, such as pytest's, are left alone."""
    level = _level(env_value("PENGUMON_LOG_LEVEL") or level)
    overrides = parse_levels(env_value("PENGUMON_LOG_LEVELS") or levels)
    fmt = (os.getenv("PENGUMON_LOG_FORMAT") or fmt or "text").strip().lower()
    if fmt not in ("text", "json"):
        raise ValueError(f"invalid log format {fmt!r}: expected text or json")

    handler = logging.StreamHandler(stream or sys.stdout)
    handler.set_name(HANDLER_NAME)
    handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())
    handler.addFilter(TraceContextFilter())

    root = logging.getLogger()
    for existing in [h for h in root.handlers if h.get_name() == HANDLER_NAME]:
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True
    for name, name_level in {**DEFAULT_LEVELS, **overrides}.items():
        logging.getLogger(name).setLevel(name_level)
    _applied[:] = list(overrides)
    return handler


_applied: list = []  # the logger names the last per-logger spec set


def apply_levels(level: Optional[str], levels: Optional[str]) -> None:
    """Applies the effective server.log_level and server.log_levels (the Settings, PENGUMON_LOG_LEVEL /
    PENGUMON_LOG_LEVELS already folded in): the root level, then each named logger. A logger the previous spec named
    and this one does not goes back to its default (DEFAULT_LEVELS, else inherited)."""
    if level:
        logging.getLogger().setLevel(_level(level))
    spec = parse_levels(levels)
    for name in _applied:
        if name not in spec:
            logging.getLogger(name).setLevel(DEFAULT_LEVELS.get(name, logging.NOTSET))
    for name, name_level in spec.items():
        logging.getLogger(name).setLevel(name_level)
    _applied[:] = list(spec)


def apply_config_level(level: Optional[str]) -> None:
    """Applies the server.log_level setting once it is loaded; PENGUMON_LOG_LEVEL, if set, still wins."""
    if not env_value("PENGUMON_LOG_LEVEL") and level:
        logging.getLogger().setLevel(_level(level))


def _duration_text(seconds: float) -> str:
    seconds = int(round(seconds))
    if seconds < 120:
        return f"{seconds} s"
    if seconds < 7200:
        return f"{seconds // 60} min"
    return f"{seconds / 3600:.1f} h"


class StateLog:
    """Logs a dependency that fails repeatedly by state, not by attempt.

    The first failure is a warning with the error; while it keeps failing, a different error is a warning
    again and the same error is repeated as a warning at most every `reminder_seconds` (with the count and how
    long it has been failing) and otherwise only at debug. The first success afterwards is one info line with
    the outage length. A dependency that polls every 10 s therefore logs a handful of lines per outage instead
    of one per poll.
    """

    def __init__(self, log: ContextLogger, what: str, *, reminder_seconds: float = 900.0,
                 clock: Callable[[], float] = time.monotonic):
        self.log = log
        self.what = what
        self.reminder_seconds = reminder_seconds
        self.clock = clock
        self.failures = 0
        self.failing_since: Optional[float] = None
        self.error: Optional[str] = None
        self._key: Optional[str] = None
        self._last_logged: float = 0.0

    @property
    def failing(self) -> bool:
        return self.failing_since is not None

    def failure(self, error: str, *, key: Optional[str] = None, **context: Any) -> bool:
        """Records one failed attempt; True if it was logged above debug. `key` (default: the error text) decides
        whether this is "a different error": a timeout and a connection refusal alternating during one outage are
        the same kind of failure when keyed by kind."""
        now = self.clock()
        self.failures += 1
        key = error if key is None else key
        if self.failing_since is None:
            self.failing_since = now
            message = f"{self.what} failing: {error}"
        elif key != self._key:
            message = f"{self.what} still failing, now: {error}"
        elif now - self._last_logged >= self.reminder_seconds:
            message = f"{self.what} still failing: {error}"
        else:
            self.log.debug(f"{self.what} failed again: {error}", error=error, failures=self.failures, **context)
            return False
        self.error, self._key = error, key
        self._last_logged = now
        self.log.warning(message, error=error, failures=self.failures,
                         failing_for_s=round(now - self.failing_since), **context)
        return True

    def success(self, **context: Any) -> bool:
        """Records one successful attempt; True if it ended a failure (and logged the recovery)."""
        if self.failing_since is None:
            return False
        duration = self.clock() - self.failing_since
        failures = self.failures
        self.failures, self.failing_since, self.error, self._key = 0, None, None, None
        self.log.info(f"{self.what} recovered after {_duration_text(duration)} ({failures} failed attempts)",
                      failures=failures, failing_for_s=round(duration), **context)
        return True
