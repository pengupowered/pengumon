"""SQLite database schema definitions for Pengumon."""

CREATE_PROBE_SAMPLES_TABLE = """
CREATE TABLE IF NOT EXISTS probe_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_id TEXT NOT NULL,
    timestamp REAL NOT NULL,
    success INTEGER NOT NULL,
    latency_ms REAL,
    packet_loss_pct REAL DEFAULT 0.0,
    jitter_ms REAL,
    error TEXT
);
"""

CREATE_PROBE_SAMPLES_INDEX = """
CREATE INDEX IF NOT EXISTS idx_probe_samples_target_time 
ON probe_samples(target_id, timestamp);
"""

CREATE_INCIDENTS_TABLE = """
CREATE TABLE IF NOT EXISTS incidents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    start_time REAL NOT NULL,
    end_time REAL,
    duration_seconds REAL,
    cause_code TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    severity TEXT NOT NULL,
    affected_targets TEXT NOT NULL,
    resolved INTEGER NOT NULL DEFAULT 0
);
"""

CREATE_INCIDENTS_INDEX = """
CREATE INDEX IF NOT EXISTS idx_incidents_start_time 
ON incidents(start_time DESC);
"""

CREATE_DOCSIS_SIGNALS_TABLE = """
CREATE TABLE IF NOT EXISTS docsis_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    downstream_power_avg REAL,
    downstream_snr_avg REAL,
    upstream_power_avg REAL,
    uncorrectables_total INTEGER DEFAULT 0,
    uncorrectables_recent INTEGER DEFAULT 0,
    t3_count_today INTEGER DEFAULT 0,
    t4_count_today INTEGER DEFAULT 0,
    status TEXT NOT NULL,
    channels_json TEXT
);
"""

CREATE_DOCSIS_SIGNALS_INDEX = """
CREATE INDEX IF NOT EXISTS idx_docsis_signals_time 
ON docsis_signals(timestamp DESC);
"""

CREATE_DOCSIS_LOGS_TABLE = """
CREATE TABLE IF NOT EXISTS docsis_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    level TEXT NOT NULL,
    event_code TEXT,
    event_text TEXT NOT NULL
);
"""

CREATE_DOCSIS_LOGS_INDEX = """
CREATE INDEX IF NOT EXISTS idx_docsis_logs_time 
ON docsis_logs(timestamp DESC);
"""

# One row per distinct event: the modem returns the whole day's log on every poll.
CREATE_DOCSIS_LOGS_UNIQUE_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_docsis_logs_unique
ON docsis_logs(timestamp, event_text);
"""

CREATE_ROUTER_METRICS_TABLE = """
CREATE TABLE IF NOT EXISTS router_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    wan_rx_bytes INTEGER,
    wan_tx_bytes INTEGER,
    wan_rx_rate_mbps REAL DEFAULT 0.0,
    wan_tx_rate_mbps REAL DEFAULT 0.0,
    cpu_usage_pct REAL,
    mem_usage_pct REAL,
    client_count INTEGER,
    model_name TEXT,
    status TEXT NOT NULL
);
"""

CREATE_ROUTER_METRICS_INDEX = """
CREATE INDEX IF NOT EXISTS idx_router_metrics_time 
ON router_metrics(timestamp DESC);
"""

# --- schema v2: compact samples clustered by time, per-minute rollups -------------------------------

CREATE_TARGETS_TABLE = """
CREATE TABLE IF NOT EXISTS targets (
    id INTEGER PRIMARY KEY,
    key TEXT NOT NULL UNIQUE
);
"""

# Clustered by time (no rowid, no secondary index): a time-range query is a single primary-key range
# scan. packet_loss_pct is not stored; for a single probe it is (1 - ok) * 100.
CREATE_SAMPLES_TABLE = """
CREATE TABLE IF NOT EXISTS samples (
    ts REAL NOT NULL,
    target INTEGER NOT NULL,
    ok INTEGER NOT NULL,
    latency_ms REAL,
    jitter_ms REAL,
    error TEXT,
    PRIMARY KEY (ts, target)
) WITHOUT ROWID;
"""

# lat_* aggregate successful probes only (failed probes carry no latency); n - ok = lost probes. lat_p10 and lat_p90
# are the 10th and 90th percentile (nearest rank) of that one minute's answered probes: a long chart's latency band
# is their mean over the bucket's minutes, which no other column can give (percentiles cannot be merged).
CREATE_ROLLUP_1M_TABLE = """
CREATE TABLE IF NOT EXISTS rollup_1m (
    minute INTEGER NOT NULL,
    target INTEGER NOT NULL,
    n INTEGER NOT NULL,
    ok INTEGER NOT NULL,
    lat_sum REAL,
    lat_min REAL,
    lat_max REAL,
    jit_sum REAL,
    jit_n INTEGER NOT NULL DEFAULT 0,
    lat_p10 REAL,
    lat_p90 REAL,
    PRIMARY KEY (minute, target)
) WITHOUT ROWID;
"""

# (Re)computes the rollup rows of every minute in [?, ?) from the raw samples. Recomputing instead of
# adding the batch keeps rollups exact when INSERT OR IGNORE drops a duplicate sample. Both bounds must
# be multiples of 60 so no minute is split.
UPSERT_ROLLUP_1M_FROM_SAMPLES = """
INSERT OR REPLACE INTO rollup_1m (minute, target, n, ok, lat_sum, lat_min, lat_max, jit_sum, jit_n, lat_p10, lat_p90)
SELECT minute, target, COUNT(*), SUM(ok),
       SUM(CASE WHEN ok = 1 THEN latency_ms END),
       MIN(CASE WHEN ok = 1 THEN latency_ms END),
       MAX(CASE WHEN ok = 1 THEN latency_ms END),
       SUM(jitter_ms), COUNT(jitter_ms),
       MIN(CASE WHEN ok = 1 AND rank * 10 >= answered THEN latency_ms END),
       MIN(CASE WHEN ok = 1 AND rank * 10 >= answered * 9 THEN latency_ms END)
FROM (
    SELECT CAST(ts / 60 AS INTEGER) AS minute, target, ok, latency_ms, jitter_ms,
           ROW_NUMBER() OVER w AS rank, COUNT(*) OVER (PARTITION BY CAST(ts / 60 AS INTEGER), target, ok) AS answered
    FROM samples
    WHERE ts >= ? AND ts < ?
    WINDOW w AS (PARTITION BY CAST(ts / 60 AS INTEGER), target, ok ORDER BY latency_ms)
)
GROUP BY minute, target
"""

# --- schema v3: targets added from the dashboard ----------------------------------------------------

# One row per UI-added target; `config` is the TargetConfig as JSON so new target fields need no migration.
CREATE_CUSTOM_TARGETS_TABLE = """
CREATE TABLE IF NOT EXISTS custom_targets (
    id TEXT PRIMARY KEY,
    config TEXT NOT NULL,
    created REAL NOT NULL
);
"""

# --- schema v13: the probe path (targets), owned by the dashboard -------------------------------------------------
#
# One row per hop that is or was on the path. `config` is the TargetConfig as JSON (every field, so a new target field
# needs no migration); `position` orders the path (0 = nearest). A hop removed in the path editor keeps its row with
# `deleted_at` set: it is no longer probed and its history is kept but hidden, until DELETE /api/path/history/{id}.
# The path's revision and whether it was ever imported or applied are in `meta` (db/path.py).
CREATE_PATH_TARGETS_TABLE = """
CREATE TABLE IF NOT EXISTS path_targets (
    id TEXT PRIMARY KEY,
    position INTEGER NOT NULL,
    config TEXT NOT NULL,
    deleted_at REAL,
    updated REAL NOT NULL
);
"""

# --- schema v4: one-time jobs and other small key/value state --------------------------------------

CREATE_META_TABLE = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# --- schema v9: device readings keyed by device id ------------------------------------------------------------------
#
# The rule: a capability's scalar readings get nullable columns in device_samples (one row per poll of a device); a
# structured reading (a list of channels, of clients, ...) gets a table of its own, with the structure as compact JSON
# that the code of that capability or technology encodes. Nothing here names a vendor.

# Clustered by device and time (no rowid, no secondary index): a device's range is one primary-key range scan. Rows
# are about 100 bytes, well under the 1/20 of a page that WITHOUT ROWID tables want.
CREATE_DEVICE_SAMPLES_TABLE = """
CREATE TABLE IF NOT EXISTS device_samples (
    device_id TEXT NOT NULL,
    ts REAL NOT NULL,
    status TEXT NOT NULL,
    error_kind TEXT,
    error_message TEXT,
    wan_rx_bytes INTEGER,
    wan_tx_bytes INTEGER,
    wan_rx_mbps REAL,
    wan_tx_mbps REAL,
    cpu_pct REAL,
    mem_pct REAL,
    client_count INTEGER,
    model TEXT,
    PRIMARY KEY (device_id, ts)
) WITHOUT ROWID;
"""

# A rowid table with an index, not WITHOUT ROWID: a DOCSIS row is about 1.8 KB, and a WITHOUT ROWID (index b-tree)
# row over about 1 KB spills into an overflow page of its own, which would double the size of the table.
CREATE_LINE_SAMPLES_TABLE = """
CREATE TABLE IF NOT EXISTS line_samples (
    device_id TEXT NOT NULL,
    ts REAL NOT NULL,
    technology TEXT NOT NULL,
    status TEXT NOT NULL,
    error_kind TEXT,
    error_message TEXT,
    errors_total INTEGER,
    errors_recent INTEGER,
    summary TEXT,
    detail TEXT
);
"""

CREATE_LINE_SAMPLES_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_line_samples_device_ts
ON line_samples(device_id, ts);
"""

# --- schema v10: client counts -------------------------------------------------------------------------------------
#
# Aggregate counts only, never a client. The scalar counts (online, wired, per band) are columns of device_samples,
# added by ALTER TABLE in the migration (CREATE_DEVICE_SAMPLES_TABLE above stays the v9 shape, so a new database gets
# them from the same migration as an old one). The count per mesh node is structured (how many nodes there are varies
# and changes), so by the rule above it is a table of its own, one row per node and reading, not JSON in a column:
# bucketing a long range is then a plain GROUP BY in SQL, and a node's history is a primary-key range scan.
DEVICE_SAMPLES_V10_COLUMNS = ("clients_online", "clients_wired", "clients_wifi_2g", "clients_wifi_5g",
                              "clients_wifi_other")

CREATE_MESH_SAMPLES_TABLE = """
CREATE TABLE IF NOT EXISTS mesh_samples (
    device_id TEXT NOT NULL,
    ts REAL NOT NULL,
    node_id TEXT NOT NULL,
    clients INTEGER NOT NULL,
    PRIMARY KEY (device_id, ts, node_id)
) WITHOUT ROWID;
"""

# --- schema v14: the applied versions of the probe path, and the path's events -----------------------------------------
#
# One row per applied revision (the import is revision 1): the hops as applied (a JSON list of hop objects with their
# positions) and a one-line summary of what changed from the revision before. db/path.py keeps the last 50.
CREATE_PATH_VERSIONS_TABLE = """
CREATE TABLE IF NOT EXISTS path_versions (
    revision INTEGER PRIMARY KEY,
    applied_at REAL NOT NULL,
    hop_count INTEGER NOT NULL,
    summary TEXT NOT NULL,
    hops TEXT NOT NULL
);
"""

# Things that happened to a hop that its history should show: "address_changed" (the owner replaced a hop's
# address, keeping its id and so its history), with the old and the new address, and "probe_changed" (the hop is probed
# another way now), with the old and the new method as the path editor words them ("ping", "TCP 443").
CREATE_PATH_EVENTS_TABLE = """
CREATE TABLE IF NOT EXISTS path_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at REAL NOT NULL,
    kind TEXT NOT NULL,
    target_id TEXT NOT NULL,
    old TEXT,
    new TEXT,
    revision INTEGER NOT NULL
);
"""

CREATE_PATH_EVENTS_INDEX = """
CREATE INDEX IF NOT EXISTS idx_path_events_target ON path_events(target_id, at);
"""

# --- schema v15: the settings store (pengumon.settings, db/settings.py) ------------------------------------------------
#
# The single values the owner set (registry key -> JSON value; a missing key means the default), the items of the four
# lists (one row per item, its whole model as JSON, so a new field needs no migration) and the saved versions (the
# whole document after each save; db/settings.py keeps the last 50). None of them ever holds a secret.
CREATE_SETTINGS_VALUES_TABLE = """
CREATE TABLE IF NOT EXISTS settings_values (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated REAL NOT NULL
);
"""

CREATE_SETTINGS_ITEMS_TABLE = """
CREATE TABLE IF NOT EXISTS settings_items (
    list TEXT NOT NULL,
    id TEXT NOT NULL,
    position INTEGER NOT NULL,
    config TEXT NOT NULL,
    updated REAL NOT NULL,
    PRIMARY KEY (list, id)
);
"""

CREATE_SETTINGS_VERSIONS_TABLE = """
CREATE TABLE IF NOT EXISTS settings_versions (
    revision INTEGER PRIMARY KEY,
    applied_at REAL NOT NULL,
    source TEXT NOT NULL,
    summary TEXT NOT NULL,
    document TEXT NOT NULL
);
"""

# --- schema v17: the speed tests (pengumon.speedtest, db/speedtests.py) ------------------------------------------------
#
# One row per test, scheduled or manual, whatever came of it. `ts` is when it started; `status` ok | partial (one direction
# worked) | skipped (not run: `reason` says why) | failed. The speeds are application-level goodput in Mbps; the
# latencies are the medians of the public probe targets before the test and during each direction; the bytes are what
# was moved (the monthly data cap counts them); wan_*_mbps are the peak router WAN rates seen in each phase (a
# cross-check of the speed). Every measured column is nullable: a skipped test has none.
CREATE_SPEED_TESTS_TABLE = """
CREATE TABLE IF NOT EXISTS speed_tests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    duration_s REAL,
    trigger TEXT NOT NULL,
    provider TEXT NOT NULL,
    server TEXT,
    status TEXT NOT NULL,
    reason TEXT,
    down_mbps REAL,
    up_mbps REAL,
    idle_ms REAL,
    loaded_down_ms REAL,
    loaded_up_ms REAL,
    bytes_down INTEGER,
    bytes_up INTEGER,
    wan_down_mbps REAL,
    wan_up_mbps REAL
);
"""

CREATE_SPEED_TESTS_INDEX = """
CREATE INDEX IF NOT EXISTS idx_speed_tests_ts ON speed_tests(ts);
"""

# --- schema v18: 10-second and quarter-hour rollups ----------------------------------------------------------------------
#
# rollup_10s: the columns of rollup_1m per target and 10-second slot (`slot` = ts // 10, counted in its own unit like
# rollup_1m's `minute`, so a bucket of b seconds is (slot * 10 / b) * b). The sub-minute history buckets read it instead
# of sorting raw samples; lat_p10 and lat_p90 are that slot's exact percentiles. Kept as long as the raw samples.
CREATE_ROLLUP_10S_TABLE = """
CREATE TABLE IF NOT EXISTS rollup_10s (
    slot INTEGER NOT NULL,
    target INTEGER NOT NULL,
    n INTEGER NOT NULL,
    ok INTEGER NOT NULL,
    lat_sum REAL,
    lat_min REAL,
    lat_max REAL,
    jit_sum REAL,
    jit_n INTEGER NOT NULL DEFAULT 0,
    lat_p10 REAL,
    lat_p90 REAL,
    PRIMARY KEY (slot, target)
) WITHOUT ROWID;
"""

# rollup_15m: rollup_1m summed per target and quarter hour (`quarter` = ts // 900 = minute / 15). Every time zone's
# offset is a multiple of 15 minutes, so a local hour or day is whole quarters. The band is kept as the weighted sums
# _rollup_query divides (p10_sum = SUM(lat_p10 * ok), p10_ok = the ok of the minutes that have a lat_p10; p90 alike),
# so re-aggregating quarters gives the per-minute band; dead_minutes counts the minutes with every probe (of at least
# 10) lost, as the uptime strips do. Kept for long_rollup_retention_days, past the per-minute rollups.
CREATE_ROLLUP_15M_TABLE = """
CREATE TABLE IF NOT EXISTS rollup_15m (
    quarter INTEGER NOT NULL,
    target INTEGER NOT NULL,
    n INTEGER NOT NULL,
    ok INTEGER NOT NULL,
    lat_sum REAL,
    lat_min REAL,
    lat_max REAL,
    jit_sum REAL,
    jit_n INTEGER NOT NULL DEFAULT 0,
    p10_sum REAL,
    p10_ok INTEGER,
    p90_sum REAL,
    p90_ok INTEGER,
    dead_minutes INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (quarter, target)
) WITHOUT ROWID;
"""


def slot_rows_from_samples(where: str) -> str:
    """A SELECT of rollup_10s rows (slot, target, n, ok, lat_sum, lat_min, lat_max, jit_sum, jit_n, lat_p10, lat_p90)
    computed from the raw samples that match `where` (a condition on ts), by the rules of UPSERT_ROLLUP_1M_FROM_SAMPLES:
    the percentiles are nearest rank over the slot's answered probes."""
    return f"""
SELECT slot, target, COUNT(*), SUM(ok),
       SUM(CASE WHEN ok = 1 THEN latency_ms END),
       MIN(CASE WHEN ok = 1 THEN latency_ms END),
       MAX(CASE WHEN ok = 1 THEN latency_ms END),
       SUM(jitter_ms), COUNT(jitter_ms),
       MIN(CASE WHEN ok = 1 AND rank * 10 >= answered THEN latency_ms END),
       MIN(CASE WHEN ok = 1 AND rank * 10 >= answered * 9 THEN latency_ms END)
FROM (
    SELECT CAST(ts / 10 AS INTEGER) AS slot, target, ok, latency_ms, jitter_ms,
           ROW_NUMBER() OVER w AS rank, COUNT(*) OVER (PARTITION BY CAST(ts / 10 AS INTEGER), target, ok) AS answered
    FROM samples
    WHERE {where}
    WINDOW w AS (PARTITION BY CAST(ts / 10 AS INTEGER), target, ok ORDER BY latency_ms)
)
GROUP BY slot, target
"""


# (Re)computes the rollup_10s rows of every slot in [?, ?) (seconds, both multiples of 10) from the raw samples.
UPSERT_ROLLUP_10S_FROM_SAMPLES = (
    "INSERT OR REPLACE INTO rollup_10s (slot, target, n, ok, lat_sum, lat_min, lat_max, jit_sum, jit_n, lat_p10, lat_p90)"
    + slot_rows_from_samples("ts >= ? AND ts < ?")
)

# (Re)computes the rollup_15m rows of every quarter in the minutes [?, ?) (both multiples of 15) from rollup_1m.
UPSERT_ROLLUP_15M_FROM_1M = """
INSERT OR REPLACE INTO rollup_15m (quarter, target, n, ok, lat_sum, lat_min, lat_max, jit_sum, jit_n,
                                   p10_sum, p10_ok, p90_sum, p90_ok, dead_minutes)
SELECT minute / 15 AS quarter, target, SUM(n), SUM(ok), SUM(lat_sum), MIN(lat_min), MAX(lat_max), SUM(jit_sum), SUM(jit_n),
       SUM(lat_p10 * ok), SUM(CASE WHEN lat_p10 IS NOT NULL THEN ok END),
       SUM(lat_p90 * ok), SUM(CASE WHEN lat_p90 IS NOT NULL THEN ok END),
       SUM(CASE WHEN ok = 0 AND n >= 10 THEN 1 ELSE 0 END)
FROM rollup_1m
WHERE minute >= ? AND minute < ?
GROUP BY quarter, target
"""

# --- schema v19: router uptime, temperature and WAN link; device events; traffic per client and hour ----------------
#
# Three scalar readings of router_system become nullable columns of device_samples (added by ALTER TABLE in the
# migration, as in v10): set on the polls that read them, NULL on the others.
DEVICE_SAMPLES_V19_COLUMNS = (("uptime_s", "INTEGER"), ("cpu_temp_c", "REAL"), ("wan_link_mbps", "INTEGER"))

# Things that happened to a device, decided from its readings: "restarted" (its uptime went back) and "wan_link" (its
# WAN port's link speed changed, or the link went down or up). `detail` is compact JSON (devices/events.py writes it).
CREATE_DEVICE_EVENTS_TABLE = """
CREATE TABLE IF NOT EXISTS device_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT NOT NULL,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL
);
"""

CREATE_DEVICE_EVENTS_INDEX = """
CREATE INDEX IF NOT EXISTS idx_device_events_device_ts ON device_events(device_id, ts);
"""

# Bytes per client and clock hour as the router counted them (its traffic statistics, read once an hour). `client` is
# a MAC in normal form, '' for the whole WAN; `hour` the hour's start (epoch seconds). A later read of the same hour
# replaces the row (the router may publish an hour late or correct it). Clustered by device and hour: a range of hours
# for every client is one primary-key range scan.
CREATE_TRAFFIC_HOURLY_TABLE = """
CREATE TABLE IF NOT EXISTS traffic_hourly (
    device_id TEXT NOT NULL,
    hour INTEGER NOT NULL,
    client TEXT NOT NULL,
    up INTEGER NOT NULL,
    down INTEGER NOT NULL,
    read_at REAL NOT NULL,
    PRIMARY KEY (device_id, hour, client)
) WITHOUT ROWID;
"""

# Bytes per application over a 24-hour window, per client ('' = every client): the router keeps app figures only as
# windows of 24 hours. `day` '' is the latest window read (replaced at every read); a date (YYYY-MM-DD, local) is the
# window that ends at that day's midnight (the whole day), kept so app history exists by day. `apps` is compact JSON
# [[name, up, down], ...]. A rowid table: a long app list would spill out of a WITHOUT ROWID row.
CREATE_TRAFFIC_APPS_TABLE = """
CREATE TABLE IF NOT EXISTS traffic_apps (
    device_id TEXT NOT NULL,
    client TEXT NOT NULL,
    day TEXT NOT NULL,
    window_end INTEGER NOT NULL,
    read_at REAL NOT NULL,
    apps TEXT NOT NULL
);
"""

CREATE_TRAFFIC_APPS_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_traffic_apps_key ON traffic_apps(device_id, client, day);
"""

# The names the owner gave connected devices in Pengumon (clients/names.py), by MAC in normal form: shown instead of the
# router's name wherever a client's name is shown in full. A handful of rows; never pruned.
CREATE_CLIENT_NAMES_TABLE = """
CREATE TABLE IF NOT EXISTS client_names (
    mac TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    updated_at REAL NOT NULL
) WITHOUT ROWID;
"""
