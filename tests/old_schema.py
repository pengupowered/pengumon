"""Databases as older builds left them, for the migration tests that roll a database built by this build back to an
older schema version and migrate it again."""

from pengumon.db import legacy_samples

# The raw probe table back to its shape before schema v25 (latency and jitter in milliseconds as REAL), rows kept. Run
# the statements in order, in one transaction, on a database of this build.
SAMPLES_BEFORE_V25 = (
    "ALTER TABLE samples RENAME TO samples_now",
    legacy_samples.CREATE_SAMPLES_TABLE,
    "INSERT INTO samples (ts, target, ok, latency_ms, jitter_ms, error) "
    "SELECT ts, target, ok, latency_cms / 100.0, jitter_cms / 100.0, error FROM samples_now",
    "DROP TABLE samples_now",
)


def samples_before_v25(con) -> None:
    """SAMPLES_BEFORE_V25 on a sqlite3 connection."""
    for statement in SAMPLES_BEFORE_V25:
        con.execute(statement)
