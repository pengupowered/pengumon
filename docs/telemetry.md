# Telemetry, logging and notifications

## Telemetry budget (what it stores, sends and writes)

- Raw probe samples, device readings (`device_samples`, `line_samples`): Kept for `database.retention_days` (7). Pruned at startup and hourly; the log line says how many rows went, and an incremental vacuum then hands the space back to the OS Where: `cleanup_old_samples`
- Per-minute rollups (`rollup_1m`): One row per target and minute (count, ok, latency sum/min/max, jitter), upserted on every flush. Kept for `database.rollup_retention_days` (400). They feed every history chart, the report, the dossier and the incident inspector (its hop table and charts, at minute resolution) beyond the raw window Where: `cleanup_old_samples`
- Speed tests (`speed_tests`, off unless the speed test is on): One row per test, a few a day at most with the default interval (24 hours; one an hour at the shortest). Kept as long as the rollups (at least 32 days), so this month's data use stays whole ([speedtest.md](speedtest.md)) Where: `cleanup_old_samples`
- Incidents: Kept (hundreds of rows, a few hundred KB). They are the outage record, including the ones classified as expected (busy line)
- Dashboard live stream: The incident history (up to 500 entries, ~0.5 MB) is sent once per dashboard and again only when an incident changes; every other second's message is ~12 KB. With no dashboard open nothing is built or queried Where: `live/ws.py`
- Disk writes: Probe samples (and their rollup upserts) are committed in one transaction every 10 s (`sample_flush_seconds`); reads and shutdown flush first. A crash can lose at most 10 s of samples. One idle connection stays open so SQLite does not checkpoint and delete the WAL after every write; every connection uses `synchronous=NORMAL` Where: `db/probes.py`

Measured 2026-10-10 on the live container (before 0.2.0): disk writes 93 → 16 KB/s (~8 GB/day → ~1.4 GB/day), network out
~55 → ~5 KB/s with no dashboard open and ~537 → ~12 KB/s per open dashboard, CPU 1.9% → 0.6%.
Diagnostics: `scripts/diagnostics/pengumon-db-report.py` (table sizes, ages, free pages, schema version) and
`pengumon-ws-size.py` (live message size), both run inside the container: `... | podman exec -i pengumon python -`.

## 🪵 Logging and tracing

Logs go to stdout (journald, through Podman). Each line keeps the `time [LEVEL] logger: message` form and adds
`key=value` context, e.g. `... pengumon.devices: Polling router (asuswrt at 192.168.1.1) failing: timeout: ...
device=router driver=asuswrt failures=1`. A device that keeps failing logs when it starts failing, when the error
changes, every 15 minutes while it lasts, and when it recovers (with the outage length), not on every poll.

- `PENGUMON_LOG_LEVEL`: Root level (otherwise `server.log_level` in the settings, default `info`)
- `PENGUMON_LOG_LEVELS`: Per-logger levels, e.g. `pengumon.drivers=debug` (every request to the modem and router: method, path, status, duration, size; never bodies or query strings)
- `PENGUMON_LOG_FORMAT`: `json` for one JSON object per line

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
  network status, device status, open incident, probe loop age (grows when the 1 s loop stalls). The metrics
  reference below lists everything else: the process and event loop, storage, dashboard clients, devices, discovery,
  alerts, settings and previews.
* **Logs**: the `pengumon` loggers and uvicorn's warnings and errors (its INFO lines name client addresses), with the
  `key=value` context as attributes and the trace and span id of the span they were logged in.

The gauges, here and in the Prometheus text at `/metrics`, come from one table, `observability/gauges.py` (`GAUGES`:
Prometheus name, OTel name, unit, help, a function of the app state); a new metric is one entry. The device status
gauge is labeled `device="<device id>"` in both (`pengumon_collector_status` in the text, `pengumon.device.status` over
OTLP); older builds labeled the text `collector="docsis"` / `collector="router"`.

Labels are device and target ids from the config, categories, outcomes and error kinds: never credentials, addresses,
client names or query strings. Credential and login requests, static files, `/api/health` and `/metrics` are excluded
from FastAPI's telemetry. `PENGUMON_GRAFANA_URL` (a plain http(s) base URL, checked at start) adds **Grafana:
Dashboard / Logs / Traces** links under the header; the dashboard is expected under the uid `pengumon`.

### Metrics reference

Every instrument pengumon exports, with the name Prometheus shows (the OTel name has dots; a unit of `s` or `By` adds
`_seconds` / `_bytes`, a counter adds `_total`, a histogram `_bucket`, `_sum`, `_count`). The first column says what
you are looking at; **g** is a gauge read when the metrics are collected, **c** a counter, **h** a histogram.
Attributes are the whole label set: ids from the config (`device`, `device.id` shown as `device_id`, `target`,
`service`), categories, outcomes and error kinds, never an address, a name, a URL, a topic, a message or an incident
id. Nothing on the 1 s probe path costs more than a few microseconds a round (measured in the table's last section).

**The process and the event loop** (`observability/runtime.py`; the loop monitor is a small task on the event loop)

| Metric | Type | Attributes | What it tells you |
|---|---|---|---|
| `pengumon_process_memory_rss_bytes` | g | | Resident memory (Linux only; left out elsewhere). A line that climbs and never falls back is a leak |
| `pengumon_process_cpu_time_seconds` | g | `mode` user, system | CPU the process used since it started; graph `rate()` for cores |
| `pengumon_process_threads` | g | | Threads of the process |
| `pengumon_process_open_fds` | g | | Open file descriptors (Linux, macOS) |
| `pengumon_asyncio_tasks` | g | | Asyncio tasks alive at the loop monitor's last sample (counted on the loop, not on the export thread) |
| `pengumon_asyncio_loop_lag_seconds` | h | | How much later than asked a 0.5 s sleep woke up. Over 0.25 s for 2 s logs a WARNING once ("Event loop running late"), and one line when it is over |
| `pengumon_probe_round_work_seconds` | h | `step` store, classify, publish | What pengumon itself spends in a round after the probes were answered; apart from probe round-trip time |
| `pengumon_probe_rounds_late_total` | c | `cause` overran, starved | Rounds that started over 1.5 intervals after the one before |
| `pengumon_probe_rounds_skipped_total` | c | | Rounds that should have run in that gap and did not |
| `pengumon_dns_lookups_stuck` | g | | System-resolver lookups still running past their timeout; each holds a thread |

**Storage**

| Metric | Type | Attributes | What it tells you |
|---|---|---|---|
| `pengumon_db_sample_buffer` | g | | Probe samples waiting for the next commit. Grows past a flush's worth only while writes fail |
| `pengumon_db_samples_dropped_total` | c | | Samples thrown away because writes kept failing and the buffer (10 000) was full |
| `pengumon_db_flush_failures_total` | c | `error` exception type | Failed commits |
| `pengumon_db_file_size_bytes` | g | `file` main, wal | Size of the SQLite file and its write-ahead log |
| `pengumon_db_retention_rows_total` | c | `table` | Rows the hourly retention removed |
| `pengumon_db_retention_duration_seconds` | h | | Time one retention run took, vacuum included |
| `pengumon_db_vacuum_pages_total` | c | | Free pages the incremental vacuum returned to the file system |

Already there: `pengumon_db_flush_duration_seconds`, `pengumon_db_flush_rows_total`.

**Dashboard clients**

| Metric | Type | Attributes | What it tells you |
|---|---|---|---|
| `pengumon_api_answer_rows` | h | `endpoint` history, device_history, device_history_bucketed | Rows in a history answer |
| `pengumon_api_answer_size_bytes` | h | `endpoint` | Body bytes of a history answer (not recorded for the bucketed device answer, which FastAPI encodes) |
| `pengumon_api_answer_capped_total` | c | `endpoint`, `cap` rows, range | Answers that reached the 50 000-row cap, or whose unbucketed range was bucketed (`history`) or cut to 48 h (`device_history`) |
| `pengumon_api_rate_limited_total` | c | `bucket` | Requests answered 429 by a rate limit |
| `pengumon_ai_pack_exports_total` | c | `level` full, vague, anonymized; `scope` window, incident | AI troubleshooting packs built through `GET /api/ai-pack` |
| `pengumon_mcp_calls_total` | c | `tool` (a tool name, or `unknown`), `outcome` ok, error, refused | MCP tool calls through `POST /mcp` ([mcp.md](mcp.md)): error is a tool's own failure, refused an unknown tool or bad arguments |
| `pengumon_ws_clients` | g | | Dashboards connected to the live stream |
| `pengumon_ws_message_size_bytes` | h | | Bytes of one live message (once per message, not per client) |
| `pengumon_ws_send_duration_seconds` | h | `outcome` sent, failed | Time to send one message to one dashboard |
| `pengumon_ws_clients_dropped_total` | c | `reason` timeout, error | Dashboards disconnected because a send failed (INFO log "Dashboard client dropped") |

**Devices and background tasks**

| Metric | Type | Attributes | What it tells you |
|---|---|---|---|
| `pengumon_device_polls_skipped_total` | c | `device_id`, `reason` min_interval, credentials_floor, gate_wait, auth_stopped | Polls a device's gate held back: a caller got the latest reading, a credentials test came too soon, the schedule waited for the gate, a refused login stopped the schedule |
| `pengumon_device_failures` | g | `device` | Consecutive failed polls |
| `pengumon_device_backoff_seconds` | g | `device` | How much longer than its interval a device with a minimum interval is waited for after failures |
| `pengumon_device_rebuilds_total` | c | `device_id`, `reason` added, changed, removed, failed | Devices the supervisor built or dropped after a saved change |
| `pengumon_device_gate_handovers_total` | c | `device_id` | Rebuilt devices that took over the previous one's gate (`Device.inherit_gate`) |
| `pengumon_task_exits_total` | c | `kind` the coroutine's name, `error` type | Background tasks that died of an exception (any is a bug; the ERROR log has the traceback) |

Already there: `pengumon_device_polls_total`, `pengumon_device_poll_duration_seconds`, `pengumon_device_status`.

**Device modules** (`observability/module_gauges.py`; counters in `devices/monitors.py`). What the cable line, the line
load, the router and the Wi-Fi and mesh report, per device, so a dashboard shows the readings Pengumon's own pages show.
A device without a reading yet, or whose last poll failed, adds no sample (the series is absent, not zero), and a service
without the module exports none of these. Attributes are the device id and small fixed sets: never a channel id, a
client or node name or an address (a series per channel would be about 120 per modem, and channel ids move when the
modem re-ranges). The gauges read the monitors' latest state every 30 s; nothing is added to the 1 s probe path. The
names carry the unit as Prometheus translates it (`dBmV`, `dB`, `dBm`, `Mbit_per_second`, `percent`).

| Metric | Type | Attributes | What it tells you |
|---|---|---|---|
| `pengumon_line_docsis_power_dBmV` | g | `device`, `direction` downstream, upstream, `stat` min, avg, max | Channel power of the cable line |
| `pengumon_line_docsis_snr_dB` | g | `device`, `stat` min, avg | Downstream channel SNR |
| `pengumon_line_docsis_channels` | g | `device`, `direction`, `grade` good, warn, critical | Channels per grade (a channel counts at its worst grade) |
| `pengumon_line_errors_total` | c | `device`, `kind` uncorrectable, corrected | New codeword errors between two polls (graph `increase()`); the first poll after a start only sets the baseline, and corrected errors are not counted for the first poll after a restart of pengumon |
| `pengumon_line_restarts_total` | c | `device` | Polls that found an error counter lower than before: the modem restarted |
| `pengumon_line_load_percent` | g | | Load as a percent of the line capacity (also `pengumon.line.load` over OTLP since 0.22) |
| `pengumon_line_capacity_mbps` | g | `direction` down, up | Line capacity the load is measured against. In the Prometheus text at `/metrics` it keeps this name; over OTLP it is `pengumon.line.capacity` and Prometheus shows `pengumon_line_capacity_Mbit_per_second` |
| `pengumon_wan_rate_Mbit_per_second` | g | `device`, `direction` down, up | WAN rate of the router |
| `pengumon_router_restarts_total` | c | `device` | Polls that found a WAN byte counter lower than before without wrapping: the router restarted |
| `pengumon_router_cpu_percent`, `pengumon_router_memory_percent` | g | `device` | Router CPU and memory use |
| `pengumon_clients` | g | `device`, `connection` wired, wifi, `band` 2g, 5g, 6g, other, none (wired) | Online clients |
| `pengumon_clients_wifi_signal_dBm` | g | `device`, `band`, `stat` p10, median | Signal of the online Wi-Fi clients (p10: the weakest tenth, nearest rank) |
| `pengumon_clients_wifi_weak` | g | `device`, `band` | Online Wi-Fi clients at or below -75 dBm |
| `pengumon_mesh_nodes` | g | `device`, `state` online, offline, unknown | Mesh nodes by state |
| `pengumon_mesh_node_clients` | g | `device`, `stat` min, max | The fewest and the most clients on one node |
| `pengumon_mesh_backhaul_signal_dBm` | g | `device`, `stat` min | The weakest Wi-Fi backhaul link |

The first four of the gauges and the counters appear only for a modem or router that offers the capability; the client and
mesh gauges only after the router has read its client list. The Prometheus text at `/metrics` has all of them under the
same names.

**Discovery, route changes, traceroute, alerts**

| Metric | Type | Attributes | What it tells you |
|---|---|---|---|
| `pengumon_discovery_runs_total` | c | `trigger` manual, daily, incident, confirm; `outcome` done, no_answer, failed, canceled | Discovery runs (`no_answer`: no trace reached a public server) |
| `pengumon_discovery_duration_seconds` | h | `trigger` | Time of a run |
| `pengumon_drift_checks_total` | c | `reason` daily, incident; `outcome` unchanged, changed, failed | Route-change checks |
| `pengumon_drift_route_changes_total` | c | `kind` new, gone, changed; `stage` found, confirmed | Changes the first run saw, and the ones the repeat run confirmed |
| `pengumon_traceroute_duration_seconds` | h | `family` 4, 6 | Time of one in-process traceroute once it was its turn (the wait for another trace is not in it) |
| `pengumon_traceroute_hops` | h | `family` | Hops a traceroute found |
| `pengumon_alerts_total` | c | `event`, `channel`, `outcome` | What became of each alert. Before any channel (`channel="all"`): `held` (quiet hours), `delayed`, `dropped` (a rule), `suppressed` (repeat window); per channel (`ntfy` or the webhook's id): `sent`, `failed` |
| `pengumon_alerts_held` | g | | Alerts waiting for their time |
| `pengumon_alert_delivery_duration_seconds` | h | `channel` | Delivery time to one channel, retries included |

**Speed test** (`speedtest/runner.py`, `speedtest/gauges.py`; [speedtest.md](speedtest.md))

| Metric | Type | Attributes | What it tells you |
|---|---|---|---|
| `pengumon_speedtest_runs_total` | c | `status` ok, partial, failed, skipped; `trigger` scheduled, manual; `provider` cloudflare, mlab, librespeed | Tests recorded, whatever came of them (a skip for the data cap or a busy line counts as `skipped`) |
| `pengumon_speedtest_bytes_total` | c | `direction` download, upload | Bytes the tests moved: the data the feature uses (graph `increase()`; the unit is `{byte}`, so no `_bytes` is added) |
| `pengumon_speedtest_download_mbps`, `pengumon_speedtest_upload_mbps` | g | | Speed of the last test that worked (status ok): the line's rate the router measured during it, else the test's own (the headline, [speedtest.md](speedtest.md)); no series until one has. In the Prometheus text at `/metrics` they keep these names; over OTLP they are `pengumon.speedtest.download` and `.upload` and Prometheus shows `pengumon_speedtest_download_Mbit_per_second` and `pengumon_speedtest_upload_Mbit_per_second` |
| `pengumon_speedtest_loaded_latency_ms` | g | `phase` idle, download, upload | Median latency of the public probe targets before the last good test (idle) and during its download and upload. Over OTLP it is `pengumon.speedtest.loaded_latency` with the unit `{ms}`, so Prometheus shows `pengumon_speedtest_loaded_latency` (milliseconds) |

The gauges read the last good test from memory (loaded from the database at the start), so a restart does not empty
them. Labels are fixed words: no server name, no URL.

**Settings and preview**

| Metric | Type | Attributes | What it tells you |
|---|---|---|---|
| `pengumon_settings_saves_total` | c | `source` save, restore, yaml_import; `outcome` saved, invalid, conflict, secrets_failed | Saves |
| `pengumon_settings_live_failures_total` | c | `section`, `applier` | Live appliers that raised after a save (the change then applies at the next start). The ERROR log names the applier, the setting keys, list names and secret slot names, and where it was raised: never the message, a traceback or a value (the traceback is at DEBUG) |
| `pengumon_settings_imports_total` | c | `outcome` ok, invalid | Settings files read for import |
| `pengumon_preview_runs_total` | c | `outcome` done, cap, canceled, failed, busy | Outage-rules previews (`cap`: stopped at the 60 s limit; `busy`: refused because one was running) |
| `pengumon_preview_duration_seconds` | h | `outcome` | Time a preview ran |
| `pengumon_preview_rounds` | h | | Probe rounds a finished preview replayed |

**Spans** added: `discovery.run` (attributes `discovery.trigger`, `discovery.outcome`) with children `discovery.traces`,
`discovery.trace` (one per server) and `discovery.checks`; `discovery.drift_check` (`drift.reason`, `drift.outcome`)
around its runs; `alert.deliver` (`alert.channel`, `alert.event`, `alert.outcome`); `settings.preview`
(`preview.outcome`, `preview.days`, `preview.rounds`). Their errors set the span status like the older ones.

**Logs** added: `Event loop running late` (WARNING, through StateLog); `Probe round started late` (DEBUG);
`Dashboard client dropped` (INFO); `Request refused by a rate limit` (DEBUG, the bucket only); `Settings preview ended`
(INFO, outcome, seconds, rounds); the failing live applier (ERROR, above); a failed ntfy post names the error type and
status, never the topic URL.

### Grafana dashboard

`docs/grafana/pengumon.json` is the dashboard behind the **Grafana: Dashboard** link (uid `pengumon`), for a Grafana with
Prometheus, Loki and Tempo datasources whose uids are `prometheus`, `loki` and `tempo` (the Grafana LGTM stack's
defaults). Import it (Dashboards > New > Import), or provision it from a file. It holds no address or name of your
network. Rows: the network probes, modem and router polling, host services, **pengumon itself** (round time, memory,
CPU, threads, descriptors, tasks, loop lag, round work, late rounds, stuck DNS lookups), **Storage**, **Dashboard
clients**, **Devices**, **Discovery and alerts**, **Settings and preview**, then logs and traces. Four more rows show the
device modules (**Cable line**, **WAN and line load**, **Router**, **Wi-Fi and mesh**, before "pengumon itself"): each
is driven by a hidden variable that lists the devices reporting the module and repeats once per device, so a service
without the module shows no row. Modem restarts are marked on every time-series panel. Add a link to your
pengumon page under Dashboard settings > Links.

The queries use only plain PromQL, so they run on a stock Prometheus with no feature flag (an earlier version used the
experimental `anchored` range modifier, which a stock Prometheus refuses). Counters are shown with `increase()` over the
panel's interval. Prometheus extrapolates `increase()` to the edges of the window, and it cannot see a counter's rise from
nothing to its first value (a counter that shows up already at 1 has not "increased" yet). For large counts the effect is
negligible. For small counts, such as "Incidents opened" with two incidents in a day, the number can read 1.8 or 2.3
instead of 2, and the first event of a counter that was never seen before can be missed. Read small numbers as
"about", and use the incident list in Pengumon itself for the exact count.

The **Service** selector at the top picks one instance by its `OTEL_SERVICE_NAME` (default `pengumon`), and every
panel, log and trace query is limited to it. A second instance, or a test run exporting under another name, never
mixes into the panels.

Older instruments, for completeness: `pengumon_probe_latency_seconds`, `pengumon_probes_total`,
`pengumon_probe_round_duration_seconds`, `pengumon_incidents_started_total`, `pengumon_service_check_duration_seconds`,
`pengumon_service_up`, and the gauges `pengumon_target_status`, `pengumon_system_status`, `pengumon_incident_active`,
`pengumon_device_status` and `pengumon_probe_loop_age_seconds`.

The new gauges have the same name in the Prometheus text at `/metrics` and over OTLP (older ones keep the names they
had: `pengumon_collector_status` is `pengumon.device.status`).

### Cost of the instruments

Measured on the probe loop alone (12 targets, a fake prober, classifier and store, the real OTLP exporters on, the
loop's pacing sleep replaced by a yield, process CPU time over 100 000 rounds, median of four alternating runs against
the code before the instruments): 308 us a round before, 314 us after without a dashboard open (+6 us, 2 %); 383 us
before, 395 us after with one open (+12 us; the run-to-run spread is about 15 us). The new per-round work is three
`perf_counter` pairs, three histogram records with fixed attributes (a fourth while a dashboard is open) and one
subtraction for the late-round check. Everything else is recorded where a rare event happens, or read when the metrics
are collected every 30 s.

## 🔔 Notifications

Enter an [ntfy](https://ntfy.sh) topic URL (and a token for a protected server) on the Settings page, Alerts (or set
`PENGUMON_NOTIFY_URL` and `PENGUMON_NOTIFY_TOKEN`, which win), to be told when an incident starts and when it resolves. Expected and `busy` conditions never notify,
one cause is not repeated within the repeat window ([webhooks.md](webhooks.md#when-alerts-go-out)), and delivery runs in a background task so a slow ntfy
server cannot delay the probes. Webhooks (Discord, Slack, Home Assistant, any URL) get the same alerts after the same
rules: [webhooks.md](webhooks.md).
