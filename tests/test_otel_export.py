"""OpenTelemetry export (pengumon.observability.export), the probe metrics, and the Grafana links."""

import json
import logging
import os
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from urllib.parse import unquote, urlsplit

import pytest
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter, SimpleLogRecordProcessor
from opentelemetry.sdk.trace import TracerProvider
from starlette.testclient import TestClient

from pengumon import __version__
from pengumon.models.probes import ProbeSample
from pengumon.observability import export, grafana, telemetry
from pengumon.observability.export import OtelLogHandler, observe_state, setup_telemetry
from pengumon.observability.gauges import GAUGES
from pengumon.observability.logs import get_logger

UNREACHABLE = "http://127.0.0.1:9"  # nothing listens: exports fail at once


# --- setup ----------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("env", [{}, {"OTEL_EXPORTER_OTLP_ENDPOINT": UNREACHABLE, "OTEL_SDK_DISABLED": "true"}])
def test_nothing_is_exported_without_an_endpoint_or_when_disabled(env):
    tel = setup_telemetry(env, set_global=False)
    assert not tel.enabled and tel.tracer_provider is None and tel.log_handler is None
    tel.shutdown()  # harmless


def test_an_endpoint_builds_all_three_exporters_named_pengumon(monkeypatch):
    monkeypatch.delenv("OTEL_SERVICE_NAME", raising=False)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TIMEOUT", "0.2")  # a failed export is not retried for long
    tel = setup_telemetry({"OTEL_EXPORTER_OTLP_ENDPOINT": UNREACHABLE}, set_global=False)
    try:
        assert tel.signals == ["traces", "metrics", "logs"]
        resource = tel.tracer_provider.resource.attributes
        assert resource["service.name"] == "pengumon" and resource["service.version"] == __version__
        assert all(tel.log_handler in logging.getLogger(n).handlers for n in export.LOGGERS)
        assert tel.log_handler not in logging.getLogger("uvicorn.access").handlers
        tel.tracer_provider.get_tracer("t").start_span("s").end()
    finally:
        tel.shutdown()
    assert all(tel.log_handler not in logging.getLogger(n).handlers for n in export.LOGGERS)


def test_shutdown_is_bounded_in_total_even_when_every_exporter_hangs(caplog):
    release = threading.Event()

    class Hanging:
        def force_flush(self, timeout_millis):
            release.wait(30)

        def shutdown(self):
            release.wait(30)

    tel = export.Telemetry(tracer_provider=Hanging(), meter_provider=Hanging(), logger_provider=Hanging(),
                           signals=["traces", "metrics", "logs"])
    started = time.monotonic()
    try:
        with caplog.at_level(logging.WARNING, logger="pengumon.telemetry"):
            tel.shutdown(deadline_seconds=0.3)
        assert time.monotonic() - started < 2
        assert "did not finish within 0.3 s" in caplog.text
    finally:
        release.set()
    assert export.SHUTDOWN_DEADLINE_SECONDS <= 3.0  # well inside podman's 10 s grace


def test_a_finished_shutdown_says_nothing(caplog):
    class Quick:
        def shutdown(self):
            pass

    with caplog.at_level(logging.WARNING, logger="pengumon.telemetry"):
        export.Telemetry(tracer_provider=Quick(), signals=["traces"]).shutdown()
    assert caplog.text == ""


def test_one_signal_can_be_exported_alone_and_the_service_name_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("OTEL_SERVICE_NAME", "netmon")
    tel = setup_telemetry({"OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": f"{UNREACHABLE}/v1/traces"}, set_global=False)
    try:
        assert tel.signals == ["traces"] and tel.meter_provider is None and tel.log_handler is None
        assert tel.tracer_provider.resource.attributes["service.name"] == "netmon"
    finally:
        tel.shutdown()


def test_signal_urls():
    env = {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://c:4318/", "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT": "http://l/x"}
    assert export._signal_url("traces", env) == "http://c:4318/v1/traces"
    assert export._signal_url("logs", env) == "http://l/x"


# --- logs -----------------------------------------------------------------------------------------------------------

@pytest.fixture
def otel_logs():
    exporter = InMemoryLogRecordExporter()
    provider = LoggerProvider()
    provider.add_log_record_processor(SimpleLogRecordProcessor(exporter))
    handler = OtelLogHandler(provider)
    logger = logging.getLogger("pengumon.test_otel")
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    yield exporter
    logger.removeHandler(handler)


def _records(exporter):
    return [getattr(r, "log_record", r) for r in exporter.get_finished_logs()]


def test_context_becomes_flat_attributes_with_severity_and_body(otel_logs):
    get_logger("pengumon.test_otel", device="router").warning("poll failed", error="ConnectTimeout", duration_ms=812,
                                                              missing=None)
    (record,) = _records(otel_logs)
    assert record.body == "poll failed" and record.severity_text == "WARNING"
    assert dict(record.attributes) == {"logger.name": "pengumon.test_otel", "device": "router",
                                       "error": "ConnectTimeout", "duration_ms": 812}


def test_a_record_carries_the_trace_of_the_span_it_was_logged_in(otel_logs):
    tracer = TracerProvider().get_tracer("t")
    with tracer.start_as_current_span("poll") as span:
        get_logger("pengumon.test_otel").info("inside")
    (record,) = _records(otel_logs)
    assert record.trace_id == span.get_span_context().trace_id and record.span_id == span.get_span_context().span_id


def test_uvicorn_info_lines_which_name_client_addresses_stay_out_but_its_errors_go_out():
    exporter = InMemoryLogRecordExporter()
    provider = LoggerProvider()
    provider.add_log_record_processor(SimpleLogRecordProcessor(exporter))
    logger = logging.getLogger("uvicorn.error")
    handler = OtelLogHandler(provider)
    logger.addHandler(handler)
    level, logger.level = logger.level, logging.INFO
    try:
        logger.info('192.0.2.7:52887 - "WebSocket /ws" [accepted]')
        logger.error("Exception in ASGI application")
    finally:
        logger.removeHandler(handler)
        logger.setLevel(level)
    assert [r.body for r in _records(exporter)] == ["Exception in ASGI application"]


def test_exceptions_keep_their_type_and_stack(otel_logs):
    try:
        raise ValueError("boom")
    except ValueError:
        get_logger("pengumon.test_otel").error("round failed", exc_info=True)
    (record,) = _records(otel_logs)
    assert record.attributes["exception.type"] == "ValueError"
    assert "boom" in record.attributes["exception.stacktrace"]


def test_odd_context_values_become_short_text(otel_logs):
    get_logger("pengumon.test_otel").info("x", targets=["a", "b"], long="y" * 1000)
    (record,) = _records(otel_logs)
    assert record.attributes["targets"] == "['a', 'b']" and len(record.attributes["long"]) == 256


# --- gauges from the app state --------------------------------------------------------------------------------------

def _observed(otel_name):
    (gauge,) = [g for g in GAUGES if g.otel == otel_name]
    return [(o.value, dict(o.attributes or {})) for o in export._guard(gauge)(None)]


def test_gauges_read_the_app_state_and_stay_quiet_without_one():
    observe_state(None)
    assert _observed("pengumon.target.status") == []
    stats = {"gw": SimpleNamespace(target_id="gw", name="Gateway", category="gateway", status="degraded")}
    device = SimpleNamespace(id="router", enabled=True, latest_snapshot=SimpleNamespace(status="warning"))
    off = SimpleNamespace(id="modem", enabled=False, latest_snapshot=SimpleNamespace(status="error"))
    observe_state(SimpleNamespace(
        prober=SimpleNamespace(stats=stats, last_round_ts=time.time() - 3),
        classifier=SimpleNamespace(last_diagnosis=SimpleNamespace(status="busy"), active_incident=object()),
        devices=SimpleNamespace(all=lambda: (device, off)),
    ))
    try:
        assert _observed("pengumon.target.status") == [(1, {"target": "gw", "category": "gateway"})]
        assert dict((a["status"], v) for v, a in _observed("pengumon.system.status")) == {
            "healthy": 0, "degraded": 0, "down": 0, "busy": 1}
        assert _observed("pengumon.incident.active") == [(1, {})]
        collectors = _observed("pengumon.device.status")
        assert {a["device"] for _, a in collectors} == {"router"}  # a disabled device reports nothing
        assert [a["status"] for v, a in collectors if v == 1] == ["warning"]
        ((age, _),) = _observed("pengumon.probe.loop.age")
        assert 2 < age < 10
    finally:
        observe_state(None)


def test_a_state_read_that_races_a_change_skips_the_collection():
    class Racing(dict):
        def values(self):
            raise RuntimeError("dictionary changed size during iteration")

    observe_state(SimpleNamespace(prober=SimpleNamespace(stats=Racing())))
    try:
        assert _observed("pengumon.target.status") == []
    finally:
        observe_state(None)


# --- probe metrics --------------------------------------------------------------------------------------------------

class Recorder:
    def __init__(self):
        self.calls = []

    def add(self, value, attributes):
        self.calls.append((value, attributes))

    record = add


def test_each_probe_counts_by_result_and_answered_ones_record_their_latency(monkeypatch):
    counter, histogram = Recorder(), Recorder()
    monkeypatch.setattr(telemetry, "probes", counter)
    monkeypatch.setattr(telemetry, "probe_latency", histogram)
    telemetry.record_probes([
        ProbeSample(target_id="gw", success=True, latency_ms=12.5),
        ProbeSample(target_id="dns", success=False, packet_loss_pct=100.0),
        ProbeSample(target_id="new", success=True, latency_ms=None),
    ], {"gw": "gateway", "dns": "dns"})
    assert counter.calls == [(1, {"target": "gw", "category": "gateway", "result": "answered"}),
                             (1, {"target": "dns", "category": "dns", "result": "lost"}),
                             (1, {"target": "new", "category": "unknown", "result": "lost"})]
    assert histogram.calls == [(0.0125, {"target": "gw", "category": "gateway"})]


def test_the_probe_attribute_names_the_method_and_a_lookup_is_dns(monkeypatch):
    counter, histogram = Recorder(), Recorder()
    monkeypatch.setattr(telemetry, "probes", counter)
    monkeypatch.setattr(telemetry, "probe_latency", histogram)
    telemetry.record_probes([ProbeSample(target_id="gw", success=True, latency_ms=2.0),
                             ProbeSample(target_id="web", success=True, latency_ms=40.0),
                             ProbeSample(target_id="dns", success=False, packet_loss_pct=100.0)],
                            {"gw": "gateway", "web": "custom", "dns": "dns"},
                            {"gw": "icmp", "web": "http"})
    assert counter.calls == [(1, {"target": "gw", "category": "gateway", "probe": "icmp", "result": "answered"}),
                             (1, {"target": "web", "category": "custom", "probe": "http", "result": "answered"}),
                             (1, {"target": "dns", "category": "dns", "probe": "dns", "result": "lost"})]
    assert histogram.calls == [(0.002, {"target": "gw", "category": "gateway", "probe": "icmp"}),
                               (0.04, {"target": "web", "category": "custom", "probe": "http"})]


# --- Grafana links --------------------------------------------------------------------------------------------------

def _pane(url):
    return next(iter(json.loads(unquote(url.split("panes=", 1)[1])).values()))


def test_links_go_to_the_dashboard_the_logs_and_the_traces():
    links = {l["label"]: l["url"] for l in grafana.configure({"PENGUMON_GRAFANA_URL": "https://grafana.lan:3000/"})}
    try:
        assert links["Dashboard"] == "https://grafana.lan:3000/d/pengumon/pengumon"
        logs, traces = _pane(links["Logs"]), _pane(links["Traces"])
        assert logs["datasource"] == "loki" and logs["queries"][0]["expr"] == '{service_name="pengumon"}'
        assert traces["datasource"] == "tempo" and traces["queries"][0]["query"] == '{resource.service.name="pengumon"}'
        assert all(urlsplit(u)[:2] == ("https", "grafana.lan:3000") for u in links.values())
    finally:
        grafana.configure({})


@pytest.mark.parametrize("url", ["javascript:alert(1)", "https://user:pw@grafana.lan", "https://g.lan/?x=1",
                                 "https://g.lan/#a", "ftp://g.lan", "https://g.lan/<x>"])
def test_anything_but_a_plain_base_url_is_refused(url):
    with pytest.raises(ValueError, match="PENGUMON_GRAFANA_URL"):
        grafana.configure({"PENGUMON_GRAFANA_URL": url})
    assert grafana.links() == []


def test_the_page_header_shows_the_links_only_when_configured():
    from pengumon.main import app
    client = TestClient(app)
    assert "telemetry-links" not in client.get("/").text
    grafana.configure({"PENGUMON_GRAFANA_URL": "https://grafana.lan:3000"})
    try:
        page = client.get("/").text
        assert 'href="https://grafana.lan:3000/d/pengumon/pengumon"' in page
        assert page.count('target="_blank" rel="noopener noreferrer"') >= 3
    finally:
        grafana.configure({})


def test_every_histogram_in_seconds_has_buckets_made_for_seconds():
    # In a child process: OpenTelemetry lets a process set its meter provider once. Each instrument records a value
    # typical for it; with the SDK's default boundaries (0, 5, 10, ...) every one would land in the first bucket.
    script = """
import json
from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
reader = InMemoryMetricReader()
metrics.set_meter_provider(MeterProvider(metric_readers=[reader]))
from pengumon.observability import telemetry as t
t.probe_round_duration.record(0.3); t.db_flush_duration.record(0.004); t.device_poll_duration.record(2.4)
t.service_check_duration.record(0.008); t.probe_latency.record(0.012)
out = {}
for rm in reader.get_metrics_data().resource_metrics:
    for sm in rm.scope_metrics:
        for m in sm.metrics:
            point = m.data.data_points[0]
            out[m.name] = [list(point.explicit_bounds), list(point.bucket_counts)]
print(json.dumps(out))
"""
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(p for p in sys.path if p)}  # pytest's path, with src/
    run = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60, env=env)
    assert run.returncode == 0, run.stderr
    histograms = json.loads(run.stdout)
    assert set(histograms) == {"pengumon.probe.round.duration", "pengumon.db.flush.duration",
                               "pengumon.device.poll.duration", "pengumon.service.check.duration",
                               "pengumon.probe.latency"}
    for name, (bounds, counts) in histograms.items():
        assert bounds[-1] <= 30 and counts.index(1) > 0, name  # not the first bucket, and no millisecond-sized bounds
