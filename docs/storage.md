# Storage

## Storage layout and the 0.2.0 migration

The schema is versioned (`PRAGMA user_version`, one module per version in `db/migrations/`, named `v<NNN>_<what>.py` and found automatically; a new schema change is a new file, and a test checks the versions are unique and contiguous). Version 2, introduced in 0.2.0:

* `samples(ts, target, ok, latency_ms, jitter_ms, error)` - raw probes, `WITHOUT ROWID`, clustered by time,
  no secondary index. `targets(id, key)` maps the integer ids to target keys. Packet loss is derived from `ok`.
* `rollup_1m` - per-minute aggregates per target (see telemetry.md). Raw data answers history ranges up to 6 h
  (buckets under 60 s only up to 24 h); everything longer is served from the rollups.
* `incidents` gained `load_pct` and `expected`; `custom_targets` keeps targets added in the UI across restarts;
  `meta` holds one-time markers (backfills).

**The first start of 0.2.0 on an existing database is a one-time migration**: the old `probe_samples` table
(about 5 M rows) is copied into `samples`, the rollups are built from it, the old table is dropped and the file
is vacuumed. This takes about 15-30 s, during which the API does not answer; progress is in the container log
and `scripts/deploy.sh` waits up to 5 minutes (polling `/api/health`). The database shrinks from ~566 MB to
~200 MB. It runs in a transaction, so a crash half-way simply repeats it on the next start. Back up the data
volume before the first upgrade: going back to 0.1.x afterwards needs that backup.

**The probe path (schema v13).** `path_targets(id, position, config, deleted_at, updated)` holds the targets the
dashboard edits (one row per hop that is or was on the path; `config` is the target as JSON, so a new target field needs
no migration). A hop removed in the path editor keeps its row with `deleted_at` set: it is not probed, and its samples
and rollups stay but are left out of every per-target answer (history, report, uptime, dossier, loss grid) until
`DELETE /api/path/history/{id}` deletes them. `meta` holds the path's `path_revision` (every applied edit adds one; an
edit from an older revision is refused) and `path_initialised`, set by the one-time import of config.yaml's targets and
the old `custom_targets` (kept, no longer written) so that import never runs twice. The import runs at start
(`engine/path.py`), not in the migration, because it needs the config.

**Device readings (schema v9).** Readings are stored per device id and kind of reading; no table names a vendor:

* `device_samples(device_id, ts, status, error_kind, error_message, ...)` - one row per poll of a device, `WITHOUT
  ROWID` with the primary key `(device_id, ts)`, so each device's rows are clustered by time and no index is needed.
  Scalar readings have one nullable column each, grouped by capability: `wan_rx_bytes`, `wan_tx_bytes`,
  `wan_rx_mbps`, `wan_tx_mbps` (wan_counters; NULL rates were not measured), `cpu_pct`, `mem_pct`, `client_count`
  (router_system), and `model` (from config). A device without a capability leaves its columns NULL, which costs a
  byte each.
* `line_samples(device_id, ts, technology, status, error_kind, error_message, errors_total, errors_recent, summary,
  detail)` - one row per access-line reading, unique per `(device_id, ts)`. The error counters every line technology
  has are columns; `summary` (a few values) and `detail` (per channel) are compact JSON that only the technology's
  module writes and reads (`pengumon/lines/docsis.py`: channels as positional rows under a format version). A rowid
  table, because a row (about 1.8 KB for 36 DOCSIS channels) is too large for `WITHOUT ROWID`.
* The rule for new readings: a scalar reading gets a column in `device_samples`, a structured one (a list of
  channels, clients, ...) gets its own table. A second router or modem, or a new line technology, needs no new table.

**The first start on schema v9 is a one-time migration**: `router_metrics` becomes `device_samples` rows of device
`router` and `docsis_signals` becomes `line_samples` rows of device `modem` (technology `docsis`, channels re-encoded;
the devices in `config.yaml` keep these ids);
`router_metrics`, `docsis_signals` and the unused `docsis_logs` are dropped and their space is returned to the file
system right away. For a week of data (about 61,000 router and 680 modem rows) it takes under a second and the two
tables shrink from about 13 MB to about 7.6 MB (router rows 123 to 105 bytes, modem rows 8.2 KB to 1.9 KB). The log
says `Schema v9: copied N router rows and M modem rows into the device tables (X s)`. **Back up the data volume before
upgrading** (`scripts/deploy.sh` does): an older build cannot run against v9 (its startup fails with `no such table:
docsis_signals`), so going back needs that backup, and readings taken since the upgrade are lost with it.

## Upgrade note: the modem event log is no longer read

Pengumon used to read the gateway's event log to count T3/T4 timeouts and keep the events. On the supported gateway
that read took longer than the request timeout and, when it did return, the log was empty, so it never produced
anything. It was removed: status now uses the power and SNR averages, and the dossier and the incident inspector also
use the codeword errors. Schema v9 drops the `docsis_logs` table and the T3/T4 and `event_log_ok` columns with
`docsis_signals`.
