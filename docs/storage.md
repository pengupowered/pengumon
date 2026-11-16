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
| v21 | The router's and mesh's storage tiers (`device_rollup_1m`, `device_rollup_15m`, `mesh_rollup_15m`) and the hourly app lists (`traffic_app_hours`), [below](#router-and-mesh-tiers-and-hourly-app-lists-schema-v21) |
| v22 | The evidence each incident keeps (`incident_evidence`, `incident_evidence_probes`, `incident_evidence_readings`), filled for the resolved incidents when the schema is upgraded ([below](#incident-evidence-schema-v22)) |
| v23 | Retention by kind of data: a stored `long_rollup_retention_days` becomes `latency_retention_days` ([above](#retention-by-kind-of-data)) |
| v24 | Latency spikes (`latency_spikes`); the stored latency incidents under 2 minutes without lost probes become spikes ([below](#latency-spikes-schema-v24)) |
| v25 | Slimmer raw probe rows: `samples` keeps latency and jitter as integer hundredths of a millisecond (`latency_cms`, `jitter_cms`), a one-time copy ([below](#slimmer-raw-probe-rows-schema-v25)) |

## Rollups and retention (schema v18)

Probe history is kept at four widths, each written in the same transaction as the samples (every flush, about every
10 s) and each read where it is enough:

| Table | One row per target and | Kept for | Read by |
| --- | --- | --- | --- |
| `samples` | probe (one per target per probe round: every second by default, up to every 60 s; Settings > Probing) | `database.retention_days` (7) | raw history up to 6 h, buckets that are not whole tens of seconds, the incident inspector |
| `rollup_10s` | 10 s (`slot` = ts // 10) | `database.slot_retention_days` (7; never shorter than the raw samples) | history buckets of whole tens of seconds, up to a day (the latency chart's 10 s buckets, with the band) |
| `rollup_1m` | minute | `database.rollup_retention_days` (30) | history buckets of whole minutes, the AI pack, incident windows, hop totals of windows under 6 h, the edges of quarter-hour reads |
| `rollup_15m` | quarter hour (`quarter` = ts // 900) | `database.latency_retention_days` (180) | the report, the loss grid, the uptime strips, hop totals of windows of 6 h or more, history buckets of whole quarters, every range older than the per-minute rows |

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

**Retention.** What each table is kept for, and the setting that says so, is under [Retention by kind of
data](#retention-by-kind-of-data). The 10-second rollups do not follow the raw samples, so a shorter `retention_days`
keeps the latency chart's 10 s buckets for `slot_retention_days` (the 10 s buckets of a window older than the raw
samples miss at most the partial slot at either end). An open incident is never deleted, and the incident list
refreshes when rows go.

**The probe interval scales the raw samples only.** A row per target per round, about 26 bytes each (measured since
schema v25: 213.5 MB over 8.46 million rows): at 1 s a 14-target path writes about 31 MB a day, at 2 s 16 MB, at 5 s
6 MB, at 10 s 3 MB. `rollup_10s`,
`rollup_1m` and `rollup_15m` have one row per bucket whatever the interval, so a long interval shrinks only the 7-day
raw part (`rollup_10s` is still written at 10 s and longer, where it is about the size of the raw samples). Settings >
Probing shows the figure for the interval chosen.

**Why raw samples stay at 7 days.** They are most of the file (about 60 %; about 70 % before schema v24), and with `rollup_10s` the charts no longer
read them beyond a day, but two readers still need the week: line-capacity learning (the 99.9th percentile of seven days
of the router's WAN rates in `device_samples`, which the same `retention_days` prunes; Settings warns under 7 days) and
the rules preview (`engine/replay.py` replays the stored raw samples, one probe round at a time, for up to
`retention_days` days; the 10-second rollups have no per-probe detail to replay). The value is fixed at 7 by default and lives under Settings > Advanced; an
install that does not use those two can set `retention_days` lower there (at least 1: an incident's evidence is written
about 16 minutes after it).

**The upgrade.** The v18 migration builds `rollup_15m` from every `rollup_1m` row and `rollup_10s` from every raw sample
(about 35 s for 9.7 M samples and 37 days of 16 targets; 60 s with a year of per-minute rows), during which the API does
not answer. A per-minute retention stored at the old default, 400 days (which the one-time import of config.yaml stored
when the file was copied from config.example.yaml), goes back to the default as a settings version of its
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
in 0.25.0). Which of the two a test's headline is, is worked out when read (speedtest/quality.py `headline`). One row per test: one a day with the default interval, at most one an hour. They are kept for `database.speedtest_retention_days`
(180 by default, never less than 32 days): the hourly cleanup deletes older ones. The monthly data cap sums
`bytes_down + bytes_up` of the calendar month's rows. The install's random schedule offset is in `meta`
(`speedtest_offset_s`).

## Router readings, device events and traffic (schema v19)

- `device_samples` gains `uptime_s` (INTEGER), `cpu_temp_c` (REAL) and `wan_link_mbps` (INTEGER, 0 = no link): set on
  the polls that read them (AsusWRT: with the client list, every 60 s; SNMP: `uptime_s` every poll), NULL on the others.
  Three NULLs cost a byte each in the row header.
- `device_events(id, device_id, ts, kind, detail)`, indexed on `(device_id, ts)`: `restarted` (the device's uptime went
  back; `detail` has `boot_at`, `previous_boot_at`, `uptime_s`) and `wan_link` (the WAN port's link speed changed, went
  down or came back; `old`, `new` in Mb/s). `detail` is compact JSON. A few rows a month, and they explain old
  incidents: kept for `database.speedtest_retention_days` (180 days by default).
- `traffic_hourly(device_id, hour, client, up, down, read_at)`, primary key `(device_id, hour, client)`, WITHOUT ROWID:
  bytes per client (a MAC in upper case with `:`; `''` is the whole WAN) and clock hour (`hour` is its start, epoch
  seconds), as the router counted them. A later read of an hour replaces it (the router publishes an hour a minute or so
  after it ends, and may correct it). About 30 bytes a row: 10 active clients are about 8 MB a year.
- `traffic_apps(device_id, client, day, window_end, read_at, apps)`, unique on `(device_id, client, day)`: bytes per
  application over the 24 hours that end at `window_end` (the router keeps app figures only as such windows). `day`
  `''` is the latest window read for the client (replaced at each read); a date (`YYYY-MM-DD`, in the zone set in
  Settings) is the window that ends at that day's midnight (the whole day), kept so app history exists by day. `apps` is compact JSON
  `[[name, up, down], ...]` in the router's order.
- Both traffic tables are pruned by the hourly cleanup after `database.traffic_retention_days` (180 by default; the
  latest app window too, by its age).
- `client_names(mac, name, updated_at)`, primary key `mac`, WITHOUT ROWID (v20): the name given to a connected device
  in its traffic dialog, shown instead of the router's wherever names are shown in full; at most 1000, 40 characters
  each, never pruned (removing a name deletes its row).

## Incident evidence (schema v22)

What the incident inspector shows (the probes around the incident, the hop table, the router's polls and the modem's
readings) used to be rebuilt on every open from the shared tables, which lose raw probes and device readings after
`database.retention_days` (7). Now each resolved incident keeps its own copy, so the inspector shows an incident a year
old as it showed it on the day, and opens fast (one primary-key range per incident, no scan of `samples`).

- **Written** by the incident-evidence component (`incidents/evidence.py`, code in `db/evidence.py`) at start and then
  every minute: a few resolved incidents without evidence per pass, once nothing that belongs to them can still arrive
  (their end, plus 45 s of window, plus the longer of 2 minutes and the modem's poll interval for the readings, plus a
  margin of 30 s). Each is one write transaction; the `incident_evidence` row (`resolution`, window, `written_at`)
  marks it done, so it is never written twice. An open incident gets nothing until it closes: the inspector reads the
  shared tables for it. The upgrade (v22) writes the evidence of every resolved incident from what is stored then,
  before the shorter retentions can remove it.
- **What is kept**, for the inspector's window (the incident from 45 s before its start to 45 s after its end):
  - `incident_evidence_probes`, one row per probe or bucket, primary key `(incident_id, target, ts)`, WITHOUT ROWID:
    every raw probe (`width` 0); for a window longer than 30 minutes the raw probes of its first and last 15 minutes
    and 10-second buckets between (`width` 10: probes, answered, latency sum/min/max, jitter sum and count, the
    columns of `rollup_10s`), which give the same hop table. Where the raw probes are already gone, the per-minute
    rollups of the window (`width` 60). `resolution` of the incident is `raw`, `mixed`, `minute` or `none` (nothing was
    stored then: the inspector reads the shared tables, as before).
  - `incident_evidence_readings`, `(incident_id, kind, device_id, ts, payload)`: `router` (a `device_samples` row) and
    `line` (a `line_samples` row) as compact JSON, the router's polls within 2 minutes of the window and the modem's
    readings within 2 minutes, or, when there is none, those within its poll interval (the nearest is shown).
- **Read**: the inspector takes probes, hop table, router polls and modem readings from the evidence when the incident
  has some (resolution other than `none`). Device events, the router's traffic in the hours and the path trace still
  come from where they are. For a window of 30 minutes or less the answer is the one the shared tables give; for a longer
  one the chart buckets are a multiple of 10 s wide (the hop table is the same).
- **Size** (measured on the live data): about 53 bytes a probe row; a typical incident (a few minutes, 14 targets)
  about 75 KB with its readings, the longest seen (9 minutes) about 290 KB; an incident older than the raw probes
  that is kept as per-minute rollups about 1.6 KB.
- **Kept and deleted with the incident**: the hourly cleanup deletes the evidence of incidents that no longer exist
  (`database.incident_retention_days`); no other retention touches it.

## Latency spikes (schema v24)

A latency or jitter problem with no loss that ends within `classifier.latency_incident_after_seconds` (2 minutes by
default) is a latency spike, not an incident ([api.md](api.md#latency-spikes), [diagnosis.md](diagnosis.md)).

- **Table** `latency_spikes`: one row per spike, `id`, `start_time` and `end_time` (its first and last slow round),
  `kind` (the incident kind it would have had: `upstream_latency`, `local_latency`, `gateway_latency`, `busy_latency`,
  `legacy_latency`), `stage` (that kind's cause category: `router`, `gateway`, `line`, `isp`, `internet`, `load`),
  `expected` (a busy line explained it) and `peak_ms` (the slowest answered probe of the stage's hops during it; NULL
  when not known). Indexed by `start_time`. Written by the classifier's incident tracker (`db/spikes.py`), a few
  dozen bytes a row.
- **The upgrade** converts the stored history to the same rule, in one transaction: every resolved incident of a
  latency-only kind (`local_latency`, `gateway_latency`, `upstream_latency`, `busy_latency`, `legacy_latency`) with no
  lost probe (`loss_seconds` NULL or 0) that lasted under 120 s becomes a spike row (`expected` copied; `peak_ms` from
  its evidence, raw probes or a bucket's highest, when it has any) and is deleted with its evidence
  (`incident_evidence*`). Longer ones, those that lost probes, open ones and every other kind stay incidents. Its log
  line says how many it converted, by kind.
- **Kept** like the incidents: `database.incident_retention_days` prunes spikes that ended before it (0 keeps them
  forever), and `/api/storage` counts the table under the incidents' kind.

## Slimmer raw probe rows (schema v25)

The raw probes are most of the database, so v25 stores them in fewer bytes, still one row per probe (plain SQL keeps
working, and a new per-probe field stays a column away):

```
samples(ts REAL, target INTEGER, ok INTEGER, latency_cms INTEGER, jitter_cms INTEGER, error TEXT,
        PRIMARY KEY (ts, target)) WITHOUT ROWID
```

* `ts` is the probe's time in epoch seconds, exactly as before (the primary key and every time range are unchanged).
* `latency_cms` and `jitter_cms` are hundredths of a millisecond (`12.34 ms` is `1234`), NULL when there is none (a
  lost probe, no jitter yet). The prober reports both rounded to 0.01 ms (`engine/prober.py`), so nothing is lost;
  SQLite stores such an integer in 1 to 3 bytes where a REAL takes 8. A value with more decimals is rounded once,
  when it is written, to the nearest hundredth (halves away from zero, as SQLite's `ROUND(x * 100)`; `schema.sample_cms`).
* Reading it by hand: `SELECT ts, target, ok, latency_cms / 100.0 AS latency_ms, jitter_cms / 100.0 AS jitter_ms,
  error FROM samples`. The app's readers convert each row the same way and aggregate the milliseconds as before, so
  every answer (history, rollups, totals, reports, the inspector, the rules preview) is what it was, bit for bit.
* The rollup tables and the incident evidence (`incident_evidence_probes`) keep milliseconds as REAL: they are a small
  part of the file, and the evidence is read as it was written.

Measured on a copy of a live database (7 days of 14 targets, 8.46 million rows): the table went from 335.8 MB to
213.5 MB (39.7 to 25.2 bytes a row), the file from 466.9 MB to 344.4 MB.

**The first start on v25 is a one-time migration**: the rows are copied a day at a time, in time order, into a new
table, the old one is dropped and the new one takes its name, all in one transaction (an interrupted start leaves the old
table as it was and copies again on the next one); then the freed pages go back to the file system (incremental
vacuum). On that copy: 5 to 7 s to copy, 7 to 9 s for the migration, 3 s to return 336 MB, about 10 to 13 s in all,
during which the API does not answer. While it runs the file and its write-ahead log need room: the file grew to
681 MB and the log to 553 MB at the peak; when the migration's connection closed the log was gone and the file
344 MB.
The migrations that ran on the old table (v2, v11, v18, v22) read and write it with a frozen copy of its SQL
(`db/legacy_samples.py`), so a database of any age upgrades through them.

## The 0.2.0 migration (schema v2)

Version 2, introduced in 0.2.0:

* `samples(ts, target, ok, latency_ms, jitter_ms, error)` - raw probes, `WITHOUT ROWID`, clustered by time,
  no secondary index. `targets(id, key)` maps the integer ids to target keys. Packet loss is derived from `ok`.
  (Since v24 latency and jitter are integer hundredths of a millisecond: [below](#slimmer-raw-probe-rows-schema-v24).)
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

## Retention by kind of data

Every range up to 30 days is drawn from full detail, which is the same for every kind of data and fixed: 1 s and 10 s
for 7 days (`retention_days` and `slot_retention_days`) and 1 minute for 30 days (`rollup_retention_days`). They live
under Settings > Advanced for people who know why, and nowhere else. Past 30 days, what is kept is the owner's choice
**per kind of data**, one "keep for" number each, at least 30 days, under Settings > Retention:

| Setting | Keeps | Tables | Default |
| --- | --- | --- | --- |
| `database.latency_retention_days` | the probes' 15-minute history (was `long_rollup_retention_days`) | `rollup_15m` | 180 |
| `database.router_retention_days` | the router's 15-minute history | `device_rollup_15m` | 180 |
| `database.mesh_retention_days` | clients per mesh node, 15-minute | `mesh_rollup_15m` | 180 |
| `database.line_retention_days` | the cable line's readings, as they were read (about 0.1 MB a day) | `line_samples` | 180 |
| `database.traffic_retention_days` | Internet use per hour and the daily app lists | `traffic_hourly`, `traffic_apps` | 180 |
| `database.speedtest_retention_days` | speed tests (never under 32 days) and the router's events | `speed_tests`, `device_events` | 180 |
| `database.incident_retention_days` | resolved incidents and their evidence, and the latency spikes; 0 keeps them forever, else at least 30 | `incidents`, `incident_evidence*`, `latency_spikes` | 180 |

Full detail is `samples`, `rollup_10s`, `rollup_1m`, `device_samples`, `device_rollup_1m`, `mesh_samples` and
`traffic_app_hours` (31 days, `TRAFFIC_APP_HOURS_DAYS`). The only ordering rule is raw <= 10 s <= per-minute: a kind's
number has no tie to the tiers (an incident keeps its own evidence, so it does not need the quarter-hour tier). An open
incident is never deleted. Every value a person set is kept on upgrade; the ones never set take the new defaults, and no
install holds data older than 180 days yet, so the lower defaults delete nothing. A stored `long_rollup_retention_days`
becomes `latency_retention_days` (migration v23, a settings version with source `upgrade`; config.yaml and Settings >
Import read the old key as the new one).

**The hourly cleanup** (`db/core.py` `_prune`) deletes each table's old rows in batches by its kind's number, and logs
one line that names the kinds, for example `Retention (full detail 7 / 7 / 30 days; kept for, in days: latency 180,
router 180, ...): removed ...`. Lowering a number deletes what is older at the next cleanup; the file is shrunk by the
incremental vacuum that follows.

**The disk table.** `GET /api/storage` (owner only; [api.md](api.md)) says what each kind holds on disk, read from
SQLite's `dbstat` (the bytes of each table and its indexes; every table has a kind in `db/storage_stats.py`, and a test
fails for a table that has none). Settings > Retention shows it as a table, with the arithmetic done in the page as a
number is typed:

| Column | Meaning |
| --- | --- |
| Keep | the number as typed; "41 of 180 days stored" while the history is shorter |
| Now | the bytes the kind holds now |
| When full | `fixed + per_day x max(0, days - 30)` for a kind with a 15-minute tier (its first 30 days are `fixed`), `per_day x days` for the others, "at today's rate"; 0 days (incidents) is "grows ~N MB a year" |
| Change | what lowering the number frees at the next cleanup (`per_day x` the days stored beyond it), else the difference when full |

`per_day` is measured over the kind's rows of the last 7 days (or of what is stored, if less). Saving a lowered number
asks first: "Deletes about 380 MB of router history older than 90 days at the next hourly cleanup. This can't be
undone." The answer also has the database file and WAL sizes and the free space of the disk (`shutil.disk_usage`),
shown as "Disk: X now, Y free". Without `dbstat` (some SQLite builds) each table and its indexes are estimated as
their rows times the average stored cell of a sample (values at their size in SQLite's record format) over a 90% page
fill, and the answer says `estimate: true` (on a live 330 MB database: within 1% in all, 3% for the probe samples).
The answer is cached for a minute: `dbstat` reads every page (0.6 s on a 450 MB database).

## Router and mesh tiers and hourly app lists (schema v21)

The router's and the mesh's history follow the probes' pattern: full detail recently, consolidated further back, so
router history is not limited to the 7 days of raw polls (how long it reaches is [by kind](#retention-by-kind-of-data)).

| Table | One row per | Kept for | Holds |
| --- | --- | --- | --- |
| `device_samples` | poll (about every 10 s) | `database.retention_days` (7) | the raw polls, as before |
| `device_rollup_1m` | device and minute (`minute` = ts // 60) | `database.rollup_retention_days` (30) | per figure (WAN rx and tx Mbps, CPU %, memory %, client count, CPU temperature): sum, count of polls that measured it, max; the lowest WAN link speed; the five client counts (online, wired, per band): sum and count |
| `device_rollup_15m` | device and quarter hour (`quarter` = ts // 900) | `database.router_retention_days` (180) | the same columns, summed from the minutes |
| `mesh_rollup_15m` | device, quarter hour and mesh node | `database.mesh_retention_days` (180) | clients on the node: sum, count of readings, max |
| `traffic_app_hours` | device, client and window end | 31 days (`TRAFFIC_APP_HOURS_DAYS`, db/core.py) | the router's 24-hour app list of every hourly round, the compact JSON of `traffic_apps`; the reader for it is `get_traffic_app_windows` |

Sums and counts, not averages, so a bucket of any width merges raw polls, minutes and quarters exactly (an average is
`SUM(sum) / SUM(n)`). Every poll stored recomputes its minute and its quarter in the same transaction, from what is
stored; a mesh reading recomputes its quarters. The migration fills the tiers from the 7 days of raw rows present (its
oldest minute and quarter can be partial, so their averages are of the polls that were there); the app lists start
empty and fill from the next round. (On a copy of a live database, 447 MB, the migration took half a second and added
1.6 MB; the tiers grow about 0.2 MB a day for the minutes, 15 KB for the quarters, 10 KB for the mesh, and the app lists about 0.4 MB a day.)

**What a reader gets** (`db/device_tiers.py`; `get_device_samples_bucketed`, `get_client_counts`, `get_mesh_counts`). A
range the raw polls cover is read from them alone, by the same query as before the tiers. A range that starts before the
first whole minute (quarter, for the mesh) raw still holds is read in parts: raw from there on, whole quarters from the
quarter tier, and the minutes from the minute tier for the partial quarters at the edges; the parts are summed into the
same epoch-aligned buckets. Where a tier is read, a bucket narrower than the tier is widened to it (60 s for minutes, 900 s
for quarters; a wider bucket stays) and the start of the range is rounded out to the whole minute (quarter) it lies in; a
range edge older than the minute tier is read as its whole quarter. The router and client-count history routes clip a range
to `router_retention_days` (the longer of it and `rollup_retention_days`), and the mesh counts to `mesh_retention_days`.
