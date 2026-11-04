"""OpenTelemetry: pengumon's tracer and meter, its instruments, and the settings for FastAPI's own telemetry.

Only the OpenTelemetry API is used here. Until export.setup_telemetry installs exporting providers (when an OTLP
endpoint is set) every span and measurement is a no-op that costs a function call; instruments created here start
recording once it has. FastAPI's HTTP and WebSocket spans and metrics use the same providers.

Spans cover operations someone may need to explain later: device polls (with their HTTP exchanges), incident path
traces, migrations and retention, and (since 0.21) discovery runs and route-change checks, alert deliveries and
settings previews. The 1 s probe loop is measured with metrics only. Attributes are
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
db_samples_dropped = meter.create_counter(
    "pengumon.db.samples.dropped", unit="{row}",
    description="Probe samples thrown away because writes kept failing and the buffer was full (oldest first)")
db_flush_failures = meter.create_counter(
    "pengumon.db.flush.failures", unit="{flush}", description="Failed commits of the probe sample buffer, by error kind")
db_retention_rows = meter.create_counter(
    "pengumon.db.retention.rows", unit="{row}", description="Rows removed by the hourly retention, by table")
db_retention_duration = meter.create_histogram(
    "pengumon.db.retention.duration", unit="s", description="Time the hourly retention took, vacuum included",
    explicit_bucket_boundaries_advisory=[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0])
db_vacuum_pages = meter.create_counter(
    "pengumon.db.vacuum.pages", unit="{page}", description="Free pages the incremental vacuum returned to the file system")
api_answer_rows = meter.create_histogram(
    "pengumon.api.answer.rows", unit="{row}", description="Rows in a history answer, by endpoint",
    explicit_bucket_boundaries_advisory=[0, 10, 100, 400, 1000, 5000, 10000, 25000, 50000, 100000])
api_answer_size = meter.create_histogram(
    "pengumon.api.answer.size", unit="By", description="Bytes of a history answer's body, by endpoint",
    explicit_bucket_boundaries_advisory=[1e3, 1e4, 5e4, 1e5, 5e5, 1e6, 2e6, 5e6, 1e7, 3e7])
api_answer_capped = meter.create_counter(
    "pengumon.api.answer.capped", unit="{answer}",
    description="History answers cut short or coarsened by a cap, by endpoint and cap (rows: the row cap was reached; "
                "range: an unbucketed range was bucketed or shortened)")
api_rate_limited = meter.create_counter(
    "pengumon.api.rate_limited", unit="{request}", description="Requests refused with 429 by a rate limit, by bucket")
ws_message_size = meter.create_histogram(
    "pengumon.ws.message.size", unit="By", description="Bytes of one live message, as sent to every dashboard",
    explicit_bucket_boundaries_advisory=[1e3, 2.5e3, 5e3, 1e4, 2.5e4, 5e4, 1e5, 2.5e5, 5e5, 1e6])
ws_send_duration = meter.create_histogram(
    "pengumon.ws.send.duration", unit="s", description="Time to send one live message to one dashboard, by outcome",
    explicit_bucket_boundaries_advisory=[0.0001, 0.00025, 0.0005, 0.001, 0.0025, 0.005, 0.01, 0.05, 0.25, 1.0, 2.0])
ws_clients_dropped = meter.create_counter(
    "pengumon.ws.clients.dropped", unit="{client}",
    description="Dashboards disconnected because a send failed, by reason (timeout: too slow; error: gone)")
device_poll_duration = meter.create_histogram(
    "pengumon.device.poll.duration", unit="s", description="Time to poll one device",
    explicit_bucket_boundaries_advisory=[0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 7.5, 10.0, 15.0, 30.0])
device_polls = meter.create_counter(
    "pengumon.device.polls", unit="{poll}", description="Device polls by device, outcome and error kind")
device_polls_skipped = meter.create_counter(
    "pengumon.device.polls.skipped", unit="{poll}",
    description="Polls a device's gate held back, by device and reason (min_interval: a caller got the latest reading; "
                "credentials_floor: a credentials test came too soon; gate_wait: the schedule waited for the gate; "
                "auth_stopped: the schedule stopped on a refused login)")
device_rebuilds = meter.create_counter(
    "pengumon.device.rebuilds", unit="{device}",
    description="Devices the supervisor built or dropped after a saved change, by device and reason "
                "(added, changed, removed, failed)")
device_gate_handovers = meter.create_counter(
    "pengumon.device.gate.handovers", unit="{device}",
    description="Rebuilt devices that took over the previous one's last attempt and failures (Device.inherit_gate)")
task_exits = meter.create_counter(
    "pengumon.task.exits", unit="{task}",
    description="Background tasks that stopped on an exception, by task kind (the coroutine's name) and error type")
discovery_runs = meter.create_counter(
    "pengumon.discovery.runs", unit="{run}",
    description="Discovery runs, by trigger (manual, daily, incident, confirm) and outcome (done, no_answer: no trace "
                "reached a public server, failed, cancelled)")
discovery_duration = meter.create_histogram(
    "pengumon.discovery.duration", unit="s", description="Time one discovery run took, by trigger",
    explicit_bucket_boundaries_advisory=[1.0, 2.5, 5.0, 7.5, 10.0, 15.0, 20.0, 30.0, 60.0, 120.0])
drift_checks = meter.create_counter(
    "pengumon.drift.checks", unit="{check}",
    description="Route-change checks, by reason (daily, incident) and outcome (unchanged, changed, failed)")
route_changes = meter.create_counter(
    "pengumon.drift.route_changes", unit="{change}",
    description="Route changes a check saw, by kind (new, gone, changed) and stage (found: the first run; "
                "confirmed: the repeat run saw it too)")
traceroute_duration = meter.create_histogram(
    "pengumon.traceroute.duration", unit="s",
    description="Time one in-process traceroute took once it was its turn, by IP family",
    explicit_bucket_boundaries_advisory=[0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 7.5, 10.0, 20.0])
traceroute_hops = meter.create_histogram(
    "pengumon.traceroute.hops", unit="{hop}", description="Hops one traceroute found, by IP family",
    explicit_bucket_boundaries_advisory=[1, 2, 3, 4, 5, 6, 8, 10, 12, 15, 20, 30])
alerts = meter.create_counter(
    "pengumon.alerts", unit="{alert}",
    description="What became of each alert, by event, channel (ntfy, a webhook's id, or all for a decision before "
                "delivery) and outcome (sent, failed; held: quiet hours moved it; delayed: the delay setting; dropped: "
                "a rule refused it; suppressed: the repeat window)")
alert_delivery_duration = meter.create_histogram(
    "pengumon.alert.delivery.duration", unit="s", description="Time to deliver one alert to one channel, retries included",
    explicit_bucket_boundaries_advisory=[0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 15.0])
settings_saves = meter.create_counter(
    "pengumon.settings.saves", unit="{save}",
    description="Settings saves by source (save, restore, yaml_import) and outcome (saved, invalid, conflict, "
                "secrets_failed)")
settings_live_failures = meter.create_counter(
    "pengumon.settings.live_failures", unit="{applier}",
    description="Live appliers that raised after a save (the change then applies at the next start), by section and "
                "applier")
settings_imports = meter.create_counter(
    "pengumon.settings.imports", unit="{import}", description="Settings files read for import, by outcome (ok, invalid)")
preview_runs = meter.create_counter(
    "pengumon.preview.runs", unit="{preview}",
    description="Outage-rules previews by outcome (done, cap: stopped at the time limit, cancelled, failed, busy: "
                "refused because one was running)")
preview_duration = meter.create_histogram(
    "pengumon.preview.duration", unit="s", description="Time a preview ran before it ended, by outcome",
    explicit_bucket_boundaries_advisory=[0.5, 1.0, 2.5, 5.0, 10.0, 15.0, 20.0, 30.0, 45.0, 60.0])
preview_rounds = meter.create_histogram(
    "pengumon.preview.rounds", unit="{round}", description="Probe rounds a finished preview replayed",
    explicit_bucket_boundaries_advisory=[1e3, 1e4, 5e4, 1e5, 2.5e5, 5e5, 1e6, 2e6, 5e6])
service_up = meter.create_gauge(
    "pengumon.service.up", unit="{state}", description="Last health check of a host service: 1 answered, 0 did not")
service_check_duration = meter.create_histogram(
    "pengumon.service.check.duration", unit="s", description="Time a host service took to answer its health check",
    explicit_bucket_boundaries_advisory=[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0])
probe_round_work = meter.create_histogram(
    "pengumon.probe.round.work", unit="s",
    description="Time pengumon itself spent in one probe round after the probes were answered, by step (store, "
                "classify, publish): the probe waits and the deliberate stagger are not in it",
    explicit_bucket_boundaries_advisory=[0.0001, 0.00025, 0.0005, 0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25,
                                         0.5, 1.0])
probe_rounds_late = meter.create_counter(
    "pengumon.probe.rounds.late", unit="{round}",
    description="Probe rounds that started more than 1.5 intervals after the one before, by cause (overran: the "
                "round before took longer than the interval; starved: the event loop did not get to it)")
probe_rounds_skipped = meter.create_counter(
    "pengumon.probe.rounds.skipped", unit="{round}",
    description="Probe rounds that should have run and did not, counted when a late round starts")
loop_lag = meter.create_histogram(
    "pengumon.asyncio.loop.lag", unit="s",
    description="How much later than asked a 0.5 s sleep of the event loop woke up: the loop is busy or blocked",
    explicit_bucket_boundaries_advisory=[0.0005, 0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5])
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
