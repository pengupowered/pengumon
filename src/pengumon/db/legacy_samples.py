"""The raw probe table as schema v2 to v24 had it, frozen for the migrations that run before v25 rebuilds it.

Until v25, `samples` stored seconds and milliseconds as 8-byte REALs: `samples(ts, target, ok, latency_ms, jitter_ms,
error)`. The migrations of that time (v2 creates the table and builds rollup_1m from it, v11 recomputes the per-minute
percentiles, v18 builds rollup_10s, v22 copies incident evidence) still meet that table on an old database and on a
new one alike (every database goes through every migration), so they read and write it with these copies of the SQL of
the time, never with db/schema.py's current one. v25 (v025_slim_samples.py) converts it to the current shape.

Edit nothing here: the statements are what those migrations ran."""

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

# (Re)computes the rollup rows of every minute in [?, ?) (seconds, multiples of 60) from the raw samples.
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

# (Re)computes the rollup_10s rows of every slot in [?, ?) (seconds, multiples of 10) from the raw samples.
UPSERT_ROLLUP_10S_FROM_SAMPLES = """
INSERT OR REPLACE INTO rollup_10s (slot, target, n, ok, lat_sum, lat_min, lat_max, jit_sum, jit_n, lat_p10, lat_p90)
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
    WHERE ts >= ? AND ts < ?
    WINDOW w AS (PARTITION BY CAST(ts / 10 AS INTEGER), target, ok ORDER BY latency_ms)
)
GROUP BY slot, target
"""

# Incident evidence (db/evidence.py) copied from this table: the raw probes of [?2, ?3] (`{op}`: "<" or "<=") and the
# 10-second buckets of [?2, ?3), both for incident ?1. Bounds in seconds.
EVIDENCE_COPY_RAW = (
    "INSERT INTO incident_evidence_probes (incident_id, target, ts, width, ok, latency_ms, jitter_ms, error) "
    "SELECT ?, target, ts, 0, ok, latency_ms, jitter_ms, error FROM samples WHERE ts >= ? AND ts {op} ?"
)
EVIDENCE_COPY_SLOTS = """
INSERT INTO incident_evidence_probes (incident_id, target, ts, width, n, ok, lat_sum, lat_min, lat_max, jit_sum, jit_n)
SELECT ?, target, CAST(ts / 10 AS INTEGER) * 10, 10, COUNT(*), SUM(ok),
       SUM(CASE WHEN ok = 1 THEN latency_ms END), MIN(CASE WHEN ok = 1 THEN latency_ms END),
       MAX(CASE WHEN ok = 1 THEN latency_ms END), SUM(jitter_ms), COUNT(jitter_ms)
FROM samples WHERE ts >= ? AND ts < ?
GROUP BY target, CAST(ts / 10 AS INTEGER)
"""
