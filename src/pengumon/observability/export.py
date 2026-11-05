"""OpenTelemetry export: the SDK providers, built from the standard OTEL_* environment, that send pengumon's spans,
metrics and logs to a collector over OTLP/HTTP.

Nothing is exported unless an endpoint is set (OTEL_EXPORTER_OTLP_ENDPOINT, or OTEL_EXPORTER_OTLP_<SIGNAL>_ENDPOINT
for one signal); OTEL_SDK_DISABLED=true turns export off. OTEL_SERVICE_NAME and OTEL_RESOURCE_ATTRIBUTES have the last
word over the resource defaults here. Without an endpoint the API stays a no-op, as before.

What goes out:
  * traces: the spans of telemetry.py's tracer (device polls with their HTTP exchanges, path traces, migrations,
    retention) and FastAPI's request and WebSocket spans, which use the same global provider;
  * metrics, every METRIC_EXPORT_INTERVAL_MS: the instruments of telemetry.py, FastAPI's HTTP metrics, and the gauges
    registered from the table in gauges.py (the ones /metrics shows that have an OTel name: target status, overall
    status, devices, incident, probe loop age), read from the app state when the metrics are collected;
  * logs: the records of the "pengumon" loggers (and uvicorn's errors), with their ContextLogger context as
    attributes and the trace and span id of the span they were logged in.

The content rule is the same as for the stdout log: identifiers, outcomes and measurements, never credentials,
client names or addresses (the client inventory never reaches a log, a span or a metric label).
"""

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional

from opentelemetry import _logs, metrics, trace
from opentelemetry._logs import LogRecord, SeverityNumber
from opentelemetry.metrics import CallbackOptions, Observation

from pengumon import __version__
from pengumon.observability.gauges import GAUGES, Gauge
from pengumon.observability.logs import CONTEXT_ATTR
from pengumon.observability.telemetry import meter

log = logging.getLogger("pengumon.telemetry")

SERVICE_NAME = "pengumon"
METRIC_EXPORT_INTERVAL_MS = 30000
SHUTDOWN_TIMEOUT_MS = 5000
# The whole of Telemetry.shutdown(): podman gives the container 10 s after SIGTERM, and the app's own shutdown (database,
# the other components) runs before this one, so a dead collector may cost no more than this.
SHUTDOWN_DEADLINE_SECONDS = 3.0
LOGGERS = ("pengumon", "uvicorn.error")  # not uvicorn.access: the request spans cover requests, without query strings
# uvicorn's INFO lines name the client's address and port ("192.0.2.7:52887 - WebSocket /ws"): only its warnings and
# errors are exported
_UVICORN_MIN_LEVEL = logging.WARNING
_ATTR_MAX = 256

_SEVERITY = {
    logging.DEBUG: SeverityNumber.DEBUG, logging.INFO: SeverityNumber.INFO, logging.WARNING: SeverityNumber.WARN,
    logging.ERROR: SeverityNumber.ERROR, logging.CRITICAL: SeverityNumber.FATAL,
}


def _endpoint(signal: str, env: Mapping[str, str]) -> Optional[str]:
    return env.get(f"OTEL_EXPORTER_OTLP_{signal.upper()}_ENDPOINT") or env.get("OTEL_EXPORTER_OTLP_ENDPOINT") or None


def _attribute(value: Any) -> Any:
    """A log attribute value: scalars as they are, anything else as short text."""
    if isinstance(value, (bool, int, float)):
        return value
    return str(value)[:_ATTR_MAX]


class OtelLogHandler(logging.Handler):
    """Sends log records to an OpenTelemetry logger provider. The ContextLogger context becomes flat attributes (the
    SDK's own handler would drop it, being a dict); the record is tied to the span that was current when it was
    logged, so Grafana can jump from a log line to its trace."""

    def __init__(self, provider: Any, level: int = logging.NOTSET):
        super().__init__(level)
        self._logger = provider.get_logger("pengumon", __version__)

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name.startswith("uvicorn") and record.levelno < _UVICORN_MIN_LEVEL:
            return False
        return super().filter(record)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            attributes: Dict[str, Any] = {"logger.name": record.name}
            for key, value in (getattr(record, CONTEXT_ATTR, None) or {}).items():
                if value is not None:
                    attributes[str(key)] = _attribute(value)
            if record.exc_info and record.exc_info[0] is not None:
                attributes["exception.type"] = record.exc_info[0].__name__
                attributes["exception.stacktrace"] = logging.Formatter().formatException(record.exc_info)
            self._logger.emit(LogRecord(
                timestamp=int(record.created * 1e9),
                observed_timestamp=time.time_ns(),
                severity_text=record.levelname,
                severity_number=_SEVERITY.get(record.levelno, SeverityNumber.UNSPECIFIED),
                body=record.getMessage(),
                attributes=attributes,
            ))
        except Exception:  # a telemetry problem must never break logging
            self.handleError(record)


@dataclass
class Telemetry:
    """The providers this process exports with (None: that signal is not exported)."""

    tracer_provider: Any = None
    meter_provider: Any = None
    logger_provider: Any = None
    log_handler: Optional[logging.Handler] = None
    signals: List[str] = field(default_factory=list)

    @property
    def enabled(self) -> bool:
        return bool(self.signals)

    def shutdown(self, deadline_seconds: float = SHUTDOWN_DEADLINE_SECONDS) -> None:
        """Flushes and stops every exporter, all of them within `deadline_seconds` in total, so a stop never hangs on
        an unreachable collector (the exporters retry with back-off). What is not done by then is abandoned: the work
        runs on a daemon thread that the process leaves behind."""
        if self.log_handler is not None:
            for name in LOGGERS:
                logging.getLogger(name).removeHandler(self.log_handler)
        end = time.monotonic() + deadline_seconds

        def stop_all() -> None:
            for provider in (self.tracer_provider, self.meter_provider, self.logger_provider):
                if provider is None:
                    continue
                try:
                    if hasattr(provider, "force_flush"):
                        provider.force_flush(int(max(0.0, min(SHUTDOWN_TIMEOUT_MS / 1000, end - time.monotonic())) * 1000))
                    provider.shutdown()
                except Exception as e:  # the process is stopping: say so and go on
                    log.warning(f"Telemetry shutdown: {type(provider).__name__} failed: {type(e).__name__}")

        worker = threading.Thread(target=stop_all, name="telemetry-shutdown", daemon=True)
        worker.start()
        worker.join(max(0.0, end - time.monotonic()))
        if worker.is_alive():
            log.warning(f"Telemetry shutdown did not finish within {deadline_seconds:g} s (is the collector down?); "
                        "leaving the rest")


def setup_telemetry(environ: Optional[Mapping[str, str]] = None, *, set_global: bool = True) -> Telemetry:
    """Builds the exporting providers for every signal with an endpoint and, with `set_global`, installs them as the
    process's providers (OpenTelemetry allows that once per process; tests pass False). Returns a disabled Telemetry
    when nothing is to be exported, or when the SDK is not installed."""
    env = os.environ if environ is None else environ
    if (env.get("OTEL_SDK_DISABLED") or "").strip().lower() == "true":
        return Telemetry()
    signals = [s for s in ("traces", "metrics", "logs") if _endpoint(s, env)]
    if not signals:
        return Telemetry()
    try:
        from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk._logs import LoggerProvider
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        log.warning("An OTLP endpoint is set but the OpenTelemetry SDK is not installed; nothing is exported")
        return Telemetry()

    # Resource.create adds OTEL_RESOURCE_ATTRIBUTES and OTEL_SERVICE_NAME from the process environment; without a
    # service name from there the SDK says "unknown_service", and pengumon is the better default
    resource = Resource.create({"service.version": __version__})
    if str(resource.attributes.get("service.name", "")).startswith("unknown_service"):
        resource = resource.merge(Resource({"service.name": SERVICE_NAME}))
    tel = Telemetry(signals=signals)
    # shutdown_on_exit=False: Telemetry.shutdown, from the lifespan, is the one bounded shutdown
    if "traces" in signals:
        tel.tracer_provider = TracerProvider(resource=resource, shutdown_on_exit=False)
        tel.tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=_signal_url("traces", env))))
    if "metrics" in signals:
        reader = PeriodicExportingMetricReader(OTLPMetricExporter(endpoint=_signal_url("metrics", env)),
                                               export_interval_millis=METRIC_EXPORT_INTERVAL_MS)
        tel.meter_provider = MeterProvider(resource=resource, metric_readers=[reader], shutdown_on_exit=False)
    if "logs" in signals:
        tel.logger_provider = LoggerProvider(resource=resource, shutdown_on_exit=False)
        tel.logger_provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter(endpoint=_signal_url("logs", env))))
        tel.log_handler = OtelLogHandler(tel.logger_provider)
        for name in LOGGERS:
            logging.getLogger(name).addHandler(tel.log_handler)
    if set_global:
        if tel.tracer_provider is not None:
            trace.set_tracer_provider(tel.tracer_provider)
        if tel.meter_provider is not None:
            metrics.set_meter_provider(tel.meter_provider)
        if tel.logger_provider is not None:
            _logs.set_logger_provider(tel.logger_provider)
    log.info(f"Exporting {', '.join(signals)} over OTLP to {_endpoint(signals[0], env)}",
             extra={CONTEXT_ATTR: {"signals": ",".join(signals), "service": resource.attributes.get("service.name")}})
    return tel


def _signal_url(signal: str, env: Mapping[str, str]) -> str:
    """The URL an exporter posts to: a per-signal endpoint as it is, the general one with /v1/<signal> appended."""
    specific = env.get(f"OTEL_EXPORTER_OTLP_{signal.upper()}_ENDPOINT")
    if specific:
        return specific
    return f"{env['OTEL_EXPORTER_OTLP_ENDPOINT'].rstrip('/')}/v1/{signal}"


# --- gauges read from the app state at collection time -----------------------------------------------------------

_state: Dict[str, Any] = {"app": None}


def _guard(gauge: Gauge) -> Callable[[CallbackOptions], List[Observation]]:
    """Callbacks run on the SDK's export thread while the event loop changes the state: a read that races a change
    (a target added from the page) skips this collection instead of failing it."""
    def observe(options: CallbackOptions) -> List[Observation]:
        if _state["app"] is None:
            return []
        try:
            return [Observation(value, {k: v for k, v in labels.items() if k not in gauge.otel_drop})
                    for labels, value in gauge.collect(_state["app"]) if value is not None]
        except (RuntimeError, AttributeError, KeyError):
            return []
    return observe


for _gauge in GAUGES:
    if _gauge.otel is not None:
        meter.create_observable_gauge(_gauge.otel, [_guard(_gauge)], unit=_gauge.unit, description=_gauge.help)


def observe_state(app_state: Any) -> None:
    """Points the gauges at the app state (pengumon.state.AppState); None stops them."""
    _state["app"] = app_state
