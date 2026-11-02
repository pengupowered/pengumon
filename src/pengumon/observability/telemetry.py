"""OpenTelemetry: pengumon's tracer and meter, its instruments, and the settings for FastAPI's own telemetry.

Only the OpenTelemetry API is used here. Until export.setup_telemetry installs exporting providers (when an OTLP
endpoint is set) every span and measurement is a no-op that costs a function call; instruments created here start
recording once it has. FastAPI's HTTP and WebSocket spans and metrics use the same providers.

Spans cover operations someone may need to explain later: device polls (with their HTTP exchanges), incident path
traces, migrations and retention. The 1 s probe loop is measured with metrics only. Attributes are
low-cardinality: device and target ids from the config, categories, outcomes, error kinds; never credentials,
addresses or query strings.
"""

from typing import Any, Iterable, Mapping, MutableMapping

from opentelemetry import metrics, trace

from pengumon import __version__

tracer = trace.get_tracer("pengumon", __version__)
meter = metrics.get_meter("pengumon", __version__)

# Instruments are created once on the API's meter; they start recording when an SDK meter provider is set. Every
# histogram in seconds names its buckets: the SDK's default boundaries (0, 5, 10, 25, ...) are meant for
# milliseconds, and would put every poll and round in the first bucket.
probe_round_duration = meter.create_histogram(
    "pengumon.probe.round.duration", unit="s", description="Time to run one probe round across every target",
    explicit_bucket_boundaries_advisory=[0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 5.0])
db_flush_duration = meter.create_histogram(
    "pengumon.db.flush.duration", unit="s", description="Time to commit one batch of probe samples",
    explicit_bucket_boundaries_advisory=[0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5])
db_flush_rows = meter.create_counter(
    "pengumon.db.flush.rows", unit="{row}", description="Probe samples committed")
device_poll_duration = meter.create_histogram(
    "pengumon.device.poll.duration", unit="s", description="Time to poll one device",
    explicit_bucket_boundaries_advisory=[0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 7.5, 10.0, 15.0, 30.0])
device_polls = meter.create_counter(
    "pengumon.device.polls", unit="{poll}", description="Device polls by device, outcome and error kind")
service_up = meter.create_gauge(
    "pengumon.service.up", unit="{state}", description="Last health check of a host service: 1 answered, 0 did not")
service_check_duration = meter.create_histogram(
    "pengumon.service.check.duration", unit="s", description="Time a host service took to answer its health check",
    explicit_bucket_boundaries_advisory=[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0])
probe_latency = meter.create_histogram(
    "pengumon.probe.latency", unit="s", description="Round-trip time of each answered probe, by target",
    explicit_bucket_boundaries_advisory=[0.001, 0.0025, 0.005, 0.01, 0.015, 0.02, 0.03, 0.05, 0.075, 0.1, 0.15, 0.25,
                                         0.5, 1.0, 2.5])
probes = meter.create_counter(
    "pengumon.probes", unit="{probe}", description="Probes sent, by target and result (answered or lost)")
incidents_started = meter.create_counter(
    "pengumon.incidents.started", unit="{incident}", description="Network incidents opened, by kind and severity")


def record_probes(samples: Iterable[Any], categories: Mapping[str, str]) -> None:
    """One probe round: each sample's result and, when answered, its latency. `categories` maps target id to its
    category (the prober's stats)."""
    for sample in samples:
        attributes = {"target": sample.target_id, "category": categories.get(sample.target_id, "unknown")}
        answered = sample.success and sample.latency_ms is not None
        probes.add(1, {**attributes, "result": "answered" if answered else "lost"})
        if answered:
            probe_latency.record(sample.latency_ms / 1000.0, attributes)

# Paths FastAPI's telemetry skips. Credential and login requests carry secrets in their bodies, and FastAPI
# records validation failures with the input values. Static assets, the health check and the Prometheus scrape
# are high-volume and say nothing about a fault.
_EXCLUDED_PREFIXES = ("/static/",)
_EXCLUDED_PATHS = frozenset({"/api/health", "/metrics", "/login", "/favicon.ico"})
_EXCLUDED_SUFFIXES = ("/credentials",)


def exclude_from_telemetry(scope: MutableMapping[str, Any]) -> bool:
    path = scope.get("path") or ""
    return (
        path in _EXCLUDED_PATHS
        or path.startswith(_EXCLUDED_PREFIXES)
        or path.rstrip("/").endswith(_EXCLUDED_SUFFIXES)
    )


def fastapi_telemetry() -> dict:
    """The `telemetry=` argument for FastAPI()."""
    return {"exclude": exclude_from_telemetry}
