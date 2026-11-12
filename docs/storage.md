# Storage

## Backups

Three ways to get a consistent copy of the database, all made with `VACUUM INTO` on a read-only connection, so they are
safe while Pengumon runs and never hold the live file longer than a read transaction:

* **The button:** Settings > About this install > **Download a backup of the database** (shown to whoever may change
  settings; a viewer and the read token cannot, [security.md](security.md#downloading-a-backup)). The browser saves
  `pengumon-<version>-<UTC stamp>.db`: the history and the settings, no passwords, tokens or webhook addresses (those
  are in `secrets.json`, which the download never includes). Two in 10 minutes, one at a time. The copy is made in
  `<data directory>/.backup-tmp/` (the container's root is read-only and `/tmp` is memory), sent in chunks and deleted
  when the download ends or the browser gives up; it needs as much free disk space as the database (507 otherwise), and
  a crash's leftovers are removed by the next backup.
* **`pengumon backup [--to DIR]`:** the same copy, plus `secrets.json` and `sessions.json`, into a timestamped folder
  ([configuration.md](configuration.md)).
* **`scripts/deploy.sh`:** one before each deploy, for the maintainer.

**Restore:** stop Pengumon, replace `pengumon.db` in the data directory (`pengumon paths` prints it) with the copy,
delete `pengumon.db-wal` and `pengumon.db-shm` next to it, and start it. The copy holds the settings, so the
dashboard comes back as it was; `secrets.json` (device passwords, webhook addresses) comes from the folder
`pengumon backup` made, or has to be entered again. **A backup from a newer version cannot be read by an older one:**
the schema only moves forward (below), so restore into the same version or a newer one.

## Schema versions

The schema is versioned (`PRAGMA user_version`, one module per version in `db/migrations/`, named `v<NNN>_<what>.py`
and found automatically; a new schema change is a new file, and a test checks the versions are unique and contiguous).
Back up the data before upgrading ([install.md](install.md#upgrade) does it for the install scripts); an older build
cannot read a newer schema, and going back needs that backup.

| Version | What it adds |
| --- | --- |
| v1 | The pre-versioning schema (the baseline) |
| v2 | `samples` and `rollup_1m` instead of `probe_samples`; incident load columns (the 0.2.0 migration, below) |
| v3 | Targets added from the dashboard (`custom_targets`) |
| v4 | `meta`, key/value state such as the one-time backfill markers |
| v5 | Whether each DOCSIS snapshot read the event log; event-log rows stored once |
| v6 | The incident `kind`, filled once from the old titles ([diagnosis.md](diagnosis.md)); new incidents written by an older build would have no `kind` |
| v7 | A nullable `error_kind` on the modem and router snapshots (older builds work against it) |
| v8 | The vendor-neutral `cause_code`; stored `xfinity_last_mile_coax_drop` incidents become `access_line_drop` |
| v9 | Device readings keyed by device id (`device_samples`, `line_samples`); a one-time copy (below) |
| v10 | Client counts over time ([clients.md](clients.md)) |
| v11 | Per-minute latency percentiles in `rollup_1m` |
| v12 | The loss span of an incident (`loss_start`, `loss_seconds`), filled at each start for resolved incidents whose raw samples are kept |
| v13 | The probe path in the database (`path_targets`) |
| v14 | The applied versions of the path (`path_versions`) and its events (`path_events`) |
| v15 | The settings store (`settings_values`, `settings_items`, `settings_versions`) |
| v16 | Stored names take the US spelling (incident kinds and titles, meta markers, the card colors setting) |
| v17 | The speed tests (`speed_tests`, below) |
| v18 | 10-second and quarter-hour rollups (`rollup_10s`, `rollup_15m`), built from what is stored; the per-minute retention's new default ([below](#rollups-and-retention-schema-v18)) |
| v19 | The router's uptime, CPU temperature and WAN link speed (`device_samples` columns), device events (`device_events`) and the router's traffic per client and hour (`traffic_hourly`, `traffic_apps`), below |
| v20 | The names given to connected devices in Pengumon (`client_names`), below |

## Rollups and retention (schema v18)

Probe history is kept at four widths, each written in the same transaction as the samples (every flush, about every
10 s) and each read where it is enough:

| Table | One row per target and | Kept for | Read by |
| --- | --- | --- | --- |
| `samples` | probe (about 1 a second) | `database.retention_days` (7) | raw history up to 6 h, buckets that are not whole tens of seconds, the incident inspector |
| `rollup_10s` | 10 s (`slot` = ts // 10) | `database.slot_retention_days` (7; never shorter than the raw samples) | history buckets of whole tens of seconds, up to a day (the latency chart's 10 s buckets, with the band) |
| `rollup_1m` | minute | `database.rollup_retention_days` (90) | history buckets of whole minutes, the AI pack, incident windows, hop totals of windows under 6 h, the edges of quarter-hour reads |
| `rollup_15m` | quarter hour (`quarter` = ts // 900) | `database.long_rollup_retention_days` (400) | the report, the loss grid, the uptime strips, hop totals of windows of 6 h or more, history buckets of whole quarters, every range older than the per-minute rows |

`rollup_10s` has the columns of `rollup_1m`: n, ok, the latency sum, min and max of the answered probes, the jitter sum
and count, and the slot's own 10th and 90th percentile (nearest rank). The flush recomputes the slots, minutes and
quarters it touched from their source (raw samples, raw samples, `rollup_1m`), so a duplicate sample never counts
twice. `rollup_15m` sums `rollup_1m`: n, ok, the latency sum, min and max, the jitter sum and count, the band as
weighted sums (`p10_sum` = the sum of `lat_p10 * ok`, `p10_ok` = the `ok` of the minutes that have a `lat_p10`; p90
alike) and `dead_minutes` (minutes with every probe of at least 10 lost, what the uptime strips count).

**What a reader gets.** A window is read as its whole quarters from `rollup_15m` and the minutes of its partial quarters
from `rollup_1m` (`db/rollups.py`), so the counts are exactly the per-minute ones while `rollup_1m` covers the window
(every UTC offset and daylight-saving change is a whole number of quarter hours, so a local hour or day is whole
quarters; a fixed offset that is not, such as an odd `tz_offset_minutes`, is read from `rollup_1m` alone). Older than
`rollup_1m`'s first stored minute, a window edge that is not on a quarter is rounded out to its whole quarter: a
365-day report counts up to 14 minutes more at its start. History buckets of exactly 10 s carry each slot's exact
percentiles (what the raw query gives); wider buckets of whole tens of seconds carry the slots' percentiles averaged,
weighted by their answered probes, the way per-minute buckets average the minutes' (and they have the band at any range
up to a day, where the raw query left it out past 6 h). Latency and jitter means are sums divided by counts at every
width; summed in another order they can round to the other side of a last-digit tie (0.01 ms).

**Retention.** The hourly cleanup deletes `rollup_10s` rows older than `slot_retention_days` (7; at least
`retention_days` and at most `rollup_retention_days`; left unset it is 7 or `retention_days`, the longer),
`rollup_1m` rows older than `rollup_retention_days` and `rollup_15m` rows (and speed tests and device events) older
than `long_rollup_retention_days`, which is at least `rollup_retention_days` (left unset it is 400 or
`rollup_retention_days`, the longer). The 10-second rollups no longer follow the raw samples, so a shorter
`retention_days` keeps the latency chart's 10 s buckets for `slot_retention_days` (the 10 s buckets of a window older
than the raw samples miss at most the partial slot at either end). Resolved incidents are deleted only when
`database.incident_retention_days` is set: 0 (the default) keeps them forever; otherwise they go that many days after
they ended (at least `long_rollup_retention_days`). An open incident is never deleted, and the incident list refreshes
when rows go.

**Why raw samples stay at 7 days.** They are most of the file (about 85 %), and with `rollup_10s` the charts no longer
read them beyond a day, but two readers still need the week: line-capacity learning (the 99.9th percentile of seven days
of the router's WAN rates in `device_samples`, which the same `retention_days` prunes; Settings warns under 7 days) and
the rules preview (`engine/replay.py` replays the stored raw samples, one probe round at a time, for up to
`retention_days` days; the 10-second rollups have no per-probe detail to replay). The default therefore stays 7; an
install that does not use those two can set `retention_days` lower in Settings.

**The upgrade.** The v18 migration builds `rollup_15m` from every `rollup_1m` row and `rollup_10s` from every raw sample
(about 35 s for 9.7 M samples and 37 days of 16 targets; 60 s with a year of per-minute rows), during which the API does
not answer. A per-minute retention stored at the old default, 400 days (which the one-time import of config.yaml stored
when the file was copied from config.example.yaml), goes back to the new default, 90 days, as a settings version of its
own (source `upgrade`); any other stored value stays. The storage component migrates before the retention component's
start-up cleanup, so the per-minute rows that cleanup then deletes are already in `rollup_15m`.

**Speed** (`scripts/bench-storage.py`, 7 days of samples of 16 targets, 9.7 M rows, and 37 days of per-minute rows, warm,
medians): latency chart 6 h with its band 1.31 s -> 0.24 s, 12 h 0.72 -> 0.28 s, 1 h 0.22 -> 0.06 s; `/api/report`
7 d 297 -> 27 ms, 30 d 1.36 s -> 78 ms, 365 d 1.70 s -> 0.10 s (with a full year of per-minute rows: 20.4 s -> 1.0 s
once pruned to 90 days); loss grid 7 d 178 -> 15 ms; uptime 30 d 415 -> 28 ms. A flush of 160 samples takes about
1.5 ms more (median 3.5-4.9 -> 4.9-6.4 ms). The dashboard's three slow aggregates are also kept for the rest of the
minute (`api/reports.py`), dropped at once when the path, a setting or an incident changes.

## The speed tests (schema v17)

`speed_tests(id, ts, duration_s, trigger, provider, server, status, reason, down_mbps, up_mbps, idle_ms,
loaded_down_ms, loaded_up_ms, bytes_down, bytes_up, wan_down_mbps, wan_up_mbps)`, indexed on `ts`: one row per test,
scheduled or manual, whatever came of it (`status` is `ok`, `partial`, `skipped` or `failed`; a skipped row has a
`reason` and no measurement). `down_mbps` / `up_mbps` are the test's own rates; `wan_down_mbps` / `wan_up_mbps` the line's
peak rate the router's counters showed during each direction (read once a second since 0.25.1; the peak of the 10 s polls
in 0.25.0). Which of the two a test's headline is, is worked out when read (speedtest/quality.py `headline`). One row per test: one a day with the default interval, at most one an hour. They are kept as long as the quarter-hour rollups
(`database.long_rollup_retention_days`, 400 by default, and at least 32 days): the hourly cleanup deletes older ones. The monthly data cap sums
`bytes_down + bytes_up` of the calendar month's rows. The install's random schedule offset is in `meta`
(`speedtest_offset_s`).

## Router readings, device events and traffic (schema v19)

- `device_samples` gains `uptime_s` (INTEGER), `cpu_temp_c` (REAL) and `wan_link_mbps` (INTEGER, 0 = no link): set on
  the polls that read them (AsusWRT: with the client list, every 60 s; SNMP: `uptime_s` every poll), NULL on the others.
  Three NULLs cost a byte each in the row header.
- `device_events(id, device_id, ts, kind, detail)`, indexed on `(device_id, ts)`: `restarted` (the device's uptime went
  back; `detail` has `boot_at`, `previous_boot_at`, `uptime_s`) and `wan_link` (the WAN port's link speed changed, went
  down or came back; `old`, `new` in Mb/s). `detail` is compact JSON. A few rows a month, and they explain old
  incidents: kept as long as the quarter-hour rollups (`database.long_rollup_retention_days`, 400 days by default).
- `traffic_hourly(device_id, hour, client, up, down, read_at)`, primary key `(device_id, hour, client)`, WITHOUT ROWID:
  bytes per client (a MAC in upper case with `:`; `''` is the whole WAN) and clock hour (`hour` is its start, epoch
  seconds), as the router counted them. A later read of an hour replaces it (the router publishes an hour a minute or so
  after it ends, and may correct it). About 30 bytes a row: 10 active clients are about 8 MB a year.
- `traffic_apps(device_id, client, day, window_end, read_at, apps)`, unique on `(device_id, client, day)`: bytes per
  application over the 24 hours that end at `window_end` (the router keeps app figures only as such windows). `day`
  `''` is the latest window read for the client (replaced at each read); a date (`YYYY-MM-DD`, in the zone set in
  Settings) is the window that ends at that day's midnight (the whole day), kept so app history exists by day. `apps` is compact JSON
  `[[name, up, down], ...]` in the router's order.
- Both traffic tables are pruned by the hourly cleanup after `database.traffic_retention_days` (400 by default; the
  latest app window too, by its age).
- `client_names(mac, name, updated_at)`, primary key `mac`, WITHOUT ROWID (v20): the name given to a connected device
  in its traffic dialog, shown instead of the router's wherever names are shown in full; at most 1000, 40 characters
  each, never pruned (removing a name deletes its row).

## The 0.2.0 migration (schema v2)

Version 2, introduced in 0.2.0:

* `samples(ts, target, ok, latency_ms, jitter_ms, error)` - raw probes, `WITHOUT ROWID`, clustered by time,
  no secondary index. `targets(id, key)` maps the integer ids to target keys. Packet loss is derived from `ok`.
* `rollup_1m` - per-minute aggregates per target (see telemetry.md). Raw data answers history ranges up to 6 h
  (buckets under 60 s only up to 24 h); everything longer is served from the rollups. (Since v18 there are 10-second
  and quarter-hour rollups too: above.)
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
edit from an older revision is refused) and `path_initialized`, set by the one-time import of config.yaml's targets and
the old `custom_targets` (kept, no longer written) so that import never runs twice. The import runs at start
(`engine/path.py`), not in the migration, because it needs the config.

**The settings store (schema v15).** What the Settings page edits (pengumon.settings, `db/settings.py`):

* `settings_values(key, value, updated)` - the single values the owner set (registry key, JSON value); a missing key
  means the default.
* `settings_items(list, id, position, config, updated)` - the items of the four lists (`devices`, `services`,
  `webhooks`, `dns_probes`), one row per item, the item as JSON (so a new field needs no migration).
* `settings_versions(revision, applied_at, source, summary, document)` - the whole document after each save (revision
  1 is the one-time import; source `import`, `save`, `restore`, `yaml_import` or `upgrade`, a change made by a schema
  migration); the last 50 are kept.
* `meta`: `settings_revision`, `settings_initialized` (the import ran; never cleared), `settings_lists_set` (the lists
  that are set, not at their default; a set list may be empty), `settings_import_report` (what the import left out and
  why, never a value) and `settings_imported_at`.

The migration creates the tables only; the one-time import of `config.yaml` runs at start (it needs the config), in
one transaction with revision 1. A save is one write transaction too: the conflict check, the rows, the new revision
and its version. Rows of keys or lists a newer build wrote are kept and ignored (logged once at start). None of these
tables ever holds a secret: the ntfy topic and token, webhook URLs and header secrets are in `secrets.json`. A v15
database opened by an older build logs "newer than this build; not migrating" and runs on `config.yaml`; the deploy's
rollback restores the pre-deploy backup in any case.

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
