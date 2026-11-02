# Telemetry, logging and notifications

## Telemetry budget (what it stores, sends and writes)

| What | Policy | Where |
|---|---|---|
| Raw probe samples, device readings (`device_samples`, `line_samples`) | Kept for `database.retention_days` (7). Pruned at startup and hourly; the log line says how many rows went, and an incremental vacuum then hands the space back to the OS | `cleanup_old_samples` |
| Per-minute rollups (`rollup_1m`) | One row per target and minute (count, ok, latency sum/min/max, jitter), upserted on every flush. Kept for `database.rollup_retention_days` (400). They feed every history chart, the report, the dossier and the incident inspector (its hop table and charts, at minute resolution) beyond the raw window | `cleanup_old_samples` |
| Incidents | Kept (hundreds of rows, a few hundred KB). They are the outage record, including the ones classified as expected (busy line) | |
| Dashboard live stream | The incident history (up to 500 entries, ~0.5 MB) is sent once per dashboard and again only when an incident changes; every other second's message is ~12 KB. With no dashboard open nothing is built or queried | `api/ws.py` |
| Disk writes | Probe samples (and their rollup upserts) are committed in one transaction every 10 s (`sample_flush_seconds`); reads and shutdown flush first. A crash can lose at most 10 s of samples. One idle connection stays open so SQLite does not checkpoint and delete the WAL after every write; every connection uses `synchronous=NORMAL` | `db/probes.py` |

Measured 2026-10-10 on the live container (before 0.2.0): disk writes 93 → 16 KB/s (~8 GB/day → ~1.4 GB/day), network out
~55 → ~5 KB/s with no dashboard open and ~537 → ~12 KB/s per open dashboard, CPU 1.9% → 0.6%.
Diagnostics: `scripts/diagnostics/pengumon-db-report.py` (table sizes, ages, free pages, schema version) and
`pengumon-ws-size.py` (live message size), both run inside the container: `... | podman exec -i pengumon python -`.

## 🪵 Logging and tracing

Logs go to stdout (journald, through Podman). Each line keeps the `time [LEVEL] logger: message` form and adds
`key=value` context, e.g. `... pengumon.devices: Polling router (asuswrt at 192.168.1.1) failing: timeout: ...
device=router driver=asuswrt failures=1`. A device that keeps failing logs when it starts failing, when the error
changes, every 15 minutes while it lasts, and when it recovers (with the outage length), not on every poll.

| Variable | Purpose |
|---|---|
| `PENGUMON_LOG_LEVEL` | Root level (otherwise `server.log_level` in `config.yaml`, default `info`) |
| `PENGUMON_LOG_LEVELS` | Per-logger levels, e.g. `pengumon.drivers=debug` (every request to the modem and router: method, path, status, duration, size; never bodies or query strings) |
| `PENGUMON_LOG_FORMAT` | `json` for one JSON object per line |

### OpenTelemetry export

Set `OTEL_EXPORTER_OTLP_ENDPOINT` (in `.env`; the container uses the host network, so a collector on the same host is
`http://127.0.0.1:4318`) and pengumon exports over OTLP/HTTP (`observability/export.py`). Without it nothing is
exported and every span and measurement costs a function call. The standard variables apply: a per-signal
`OTEL_EXPORTER_OTLP_<SIGNAL>_ENDPOINT` exports only that signal, `OTEL_SERVICE_NAME` (default `pengumon`) and
`OTEL_RESOURCE_ATTRIBUTES` set the resource, `OTEL_SDK_DISABLED=true` turns export off. A stop flushes every exporter,
bounded to 5 s each.

* **Traces**: device polls with their HTTP requests to the modem and router, incident path traces, migrations,
  retention, and FastAPI's request and WebSocket spans.
* **Metrics** (every 30 s): per-probe latency (`pengumon.probe.latency`, histogram) and probes by result
  (`pengumon.probes`, answered / lost) per target; device poll duration and outcome (ok, partial, error with its
  type, unconfigured); incidents opened by kind and severity; host service checks; probe round time; database
  flushes; FastAPI's HTTP request duration; and gauges read when the metrics are collected: target status, overall
  network status, device status, open incident, probe loop age (grows when the 1 s loop stalls).
* **Logs**: the `pengumon` loggers and uvicorn's warnings and errors (its INFO lines name client addresses), with the
  `key=value` context as attributes and the trace and span id of the span they were logged in.

The gauges, here and in the Prometheus text at `/metrics`, come from one table, `observability/gauges.py` (`GAUGES`:
Prometheus name, OTel name, unit, help, a function of the app state); a new metric is one entry. The device status
gauge is labelled `device="<device id>"` in both (`pengumon_collector_status` in the text, `pengumon.device.status` over
OTLP); older builds labelled the text `collector="docsis"` / `collector="router"`.

Labels are device and target ids from the config, categories, outcomes and error kinds: never credentials, addresses,
client names or query strings. Credential and login requests, static files, `/api/health` and `/metrics` are excluded
from FastAPI's telemetry. `PENGUMON_GRAFANA_URL` (a plain http(s) base URL, checked at start) adds **Grafana:
Dashboard / Logs / Traces** links under the header; the dashboard is expected under the uid `pengumon`.

## 🔔 Notifications

Set `PENGUMON_NOTIFY_URL` to an [ntfy](https://ntfy.sh) topic URL (and `PENGUMON_NOTIFY_TOKEN` for a protected
server) to be told when an incident starts and when it resolves. Expected and `busy` conditions never notify,
there is at most one message per cause every 5 minutes, and delivery runs in a background task so a slow ntfy
server cannot delay the probes. Webhooks (Discord, Slack, Home Assistant, any URL) get the same alerts after the same
rules: [webhooks.md](webhooks.md).
