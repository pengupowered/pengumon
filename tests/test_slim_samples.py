"""Schema v25: the raw probe rows keep latency and jitter as whole hundredths of a millisecond (samples.latency_cms,
jitter_cms), the time as it was (REAL seconds). The flush rounds once, as the migration does; the migration converts
an old table exactly; the answers are the same; a row stays small."""

import random
import sqlite3
import time

import pytest

from pengumon.db import migrations
from pengumon.db.migrations import v025_slim_samples
from pengumon.db.repository import DatabaseRepository
from pengumon.db.schema import sample_cms
from pengumon.models.probes import ProbeSample

from old_schema import samples_before_v25


def _rows(path, sql="SELECT ts, target, ok, latency_cms, jitter_cms, error FROM samples ORDER BY ts, target"):
    con = sqlite3.connect(path)
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def _probes(start, seconds, targets=("gw", "isp", "pub"), seed=7):
    """Probes as the prober makes them: a time with sub-millisecond digits, latency and jitter rounded to 0.01 ms (the
    prober's round(x, 2)), some lost, some without a jitter."""
    rng = random.Random(seed)
    out = []
    for i in range(seconds):
        for k, tid in enumerate(targets):
            ok = rng.random() > 0.05
            out.append(ProbeSample(target_id=tid, timestamp=start + i + k / 64 + rng.random() / 1000, success=ok,
                                   latency_ms=round(rng.uniform(0.2, 90), 2) if ok else None,
                                   jitter_ms=round(rng.uniform(0, 6), 2) if rng.random() < 0.8 else None,
                                   packet_loss_pct=0.0 if ok else 100.0, error=None if ok else "Request timed out"))
    return out


# --- rounding ----------------------------------------------------------------------------------------------------------

async def test_a_latency_is_stored_in_whole_hundredths_rounded_once_at_insert(repo):
    """A value of more than two decimals (the prober never sends one) is rounded once, when it is written: to the
    nearest hundredth, halves away from zero, as SQLite's ROUND(x * 100) computes it in floating point (1.005 * 100 is
    100.49999999999999 there, so 1.005 is stored as 100)."""
    t = time.time() - 60
    values = [(19.737, 1974), (69.908, 6991), (0.005, 1), (7.125, 713), (1.005, 100), (0.29, 29), (5.0, 500)]
    await repo.insert_samples([ProbeSample(target_id="a", timestamp=t + i, success=True, latency_ms=lat, jitter_ms=lat,
                                           packet_loss_pct=0.0) for i, (lat, _) in enumerate(values)])
    await repo.insert_samples([ProbeSample(target_id="a", timestamp=t + 30, success=False, latency_ms=None,
                                           packet_loss_pct=100.0, error="Request timed out")])
    await repo.flush_samples()
    stored = _rows(repo.db_path)
    assert [(r[3], r[4]) for r in stored] == [(cms, cms) for _, cms in values] + [(None, None)]
    assert [r[0] for r in stored] == [t + i for i in range(len(values))] + [t + 30]  # the time bit for bit
    history = await repo.get_history(t, t + 30)
    assert [h["latency_ms"] for h in history] == [cms / 100 for _, cms in values] + [None]
    assert history[1]["latency_ms"] == 69.91 and history[1]["jitter_ms"] == 69.91


def test_the_flush_rounds_as_the_migration_s_sql():
    """sample_cms (the flush) and CAST(ROUND(x * 100) AS INTEGER) (the v25 migration) give the same integer for any
    value, ties included, so a probe stored before the upgrade and one stored after it agree."""
    rng = random.Random(11)
    values = [0.0, 0.005, 0.015, 1.005, 2.675, 7.125, 12.345, 0.29, 999.995, 1e-9, 49.12, 987.98]
    values += [round(rng.uniform(0, 1000), 3) for _ in range(3000)] + [rng.uniform(0, 1000) for _ in range(3000)]
    con = sqlite3.connect(":memory:")
    try:
        sql = [con.execute("SELECT CAST(ROUND(? * 100) AS INTEGER)", (v,)).fetchone()[0] for v in values]
    finally:
        con.close()
    assert [sample_cms(v) for v in values] == sql
    assert sample_cms(None) is None
    # a value the prober sends (round(x, 2)) comes back as the same float
    assert all(sample_cms(round(v, 2)) / 100 == round(v, 2) for v in values)


# --- the migration -----------------------------------------------------------------------------------------------------

async def _answers(repo, t0):
    """What the readers of `samples` answer over the probes of _probes(t0, 600)."""
    return {
        "raw": await repo.get_history(t0 - 5, t0 + 605),
        "slots": await repo.get_history(t0 - 5, t0 + 605, bucket_seconds=10),
        "raw_buckets": await repo.get_history(t0 - 5, t0 + 605, bucket_seconds=7),
        "stats": await repo.get_sample_stats(t0, t0 + 600),
        "loss": await repo.probe_loss_stats(t0, t0 + 600, ["gw", "isp", "pub"]),
        "minutes": await repo.get_history(t0 - 5, t0 + 605, bucket_seconds=60),
    }


async def test_v25_converts_an_old_table_exactly_and_the_answers_stay_the_same(tmp_path):
    path = str(tmp_path / "v23.db")
    t0 = time.time() - 3600
    repo = DatabaseRepository(db_path=path, sample_flush_seconds=3600)
    await repo.initialize()
    await repo.insert_samples(_probes(t0, 600))
    await repo.flush_samples()
    before_rows = _rows(path)
    before = await _answers(repo, t0)
    await repo.close()
    rollups = _rows(path, "SELECT * FROM rollup_10s ORDER BY slot, target")

    con = sqlite3.connect(path)  # as the previous build left it: REAL latencies, schema v23
    samples_before_v25(con)
    # a value of three decimals and a tie, which only an older build could have stored
    con.execute("INSERT INTO samples (ts, target, ok, latency_ms, jitter_ms, error) VALUES (?, 1, 1, 7.125, 0.29, NULL)",
                (t0 - 1000.0001,))
    con.execute("PRAGMA user_version=23")
    con.commit()
    assert [r[3] for r in con.execute("SELECT * FROM samples WHERE ts >= ? ORDER BY ts, target", (t0,))][:3] == [
        None if r[3] is None else r[3] / 100 for r in before_rows[:3]]
    con.close()

    assert await migrations.migrate(path) == migrations.LATEST_VERSION
    after_rows = _rows(path)
    assert after_rows[0] == (t0 - 1000.0001, 1, 1, 713, 29, None)
    assert after_rows[1:] == before_rows  # times bit for bit, latencies and jitters exact, NULLs kept
    assert _rows(path, "SELECT * FROM rollup_10s ORDER BY slot, target") == rollups  # not rebuilt
    assert [r[1] for r in _rows(path, "PRAGMA table_info(samples)")] == [
        "ts", "target", "ok", "latency_cms", "jitter_cms", "error"]
    again = DatabaseRepository(db_path=path, sample_flush_seconds=3600)
    await again.initialize()
    assert await _answers(again, t0) == before
    await again.close()
    assert await migrations.migrate(path) == migrations.LATEST_VERSION  # runs once


async def test_v25_interrupted_midway_leaves_the_old_table_and_runs_again(tmp_path, monkeypatch):
    path = str(tmp_path / "crash.db")
    t0 = time.time() - 3 * 86400
    repo = DatabaseRepository(db_path=path, sample_flush_seconds=3600)
    await repo.initialize()
    await repo.insert_samples(_probes(t0, 200) + _probes(t0 + 86400, 200) + _probes(t0 + 2 * 86400, 200))
    await repo.flush_samples()
    await repo.close()
    rows = _rows(path)
    con = sqlite3.connect(path)
    samples_before_v25(con)
    con.execute("PRAGMA user_version=23")
    con.commit()
    con.close()

    class Crash(Exception):
        pass

    real = v025_slim_samples.logger

    class CrashingLogger:
        def info(self, msg, *a, **k):
            if msg.startswith("Schema v25: samples"):  # after the first day was copied
                raise Crash()
            real.info(msg, *a, **k)

        def __getattr__(self, name):
            return getattr(real, name)

    monkeypatch.setattr(v025_slim_samples, "logger", CrashingLogger())
    with pytest.raises(Crash):
        await migrations.migrate(path)
    assert _rows(path, "PRAGMA user_version") == [(24,)]  # v24 (latency spikes) went through; v25 rolled back
    assert "latency_ms" in {r[1] for r in _rows(path, "PRAGMA table_info(samples)")}
    assert not _rows(path, "SELECT name FROM sqlite_master WHERE name = 'samples_v25'")
    assert _rows(path, "SELECT COUNT(*) FROM samples") == [(len(rows),)]

    monkeypatch.setattr(v025_slim_samples, "logger", real)
    assert await migrations.migrate(path) == migrations.LATEST_VERSION
    assert _rows(path) == rows


# --- size ------------------------------------------------------------------------------------------------------------

def _has_dbstat() -> bool:
    try:
        sqlite3.connect(":memory:").execute("SELECT 1 FROM dbstat LIMIT 1")
        return True
    except sqlite3.OperationalError:
        return False


@pytest.mark.skipif(not _has_dbstat(), reason="this SQLite has no dbstat table (e.g. Python on Windows)")
async def test_a_stored_probe_row_is_small(repo):
    """Bytes of the samples table (its pages, as dbstat counts them) per row, for a few thousand typical probes: about
    25 (the live database: 213.5 MB over 8.46 M rows). With latency and jitter as 8-byte REALs it was about 40, so a
    change back to REAL columns fails here."""
    await repo.insert_samples(_probes(time.time() - 3 * 3600, 1500, targets=tuple(f"hop{i}" for i in range(6))))
    await repo.flush_samples()
    con = sqlite3.connect(repo.db_path)
    try:
        rows = con.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
        size = con.execute("SELECT SUM(pgsize) FROM dbstat WHERE name = 'samples'").fetchone()[0]
    finally:
        con.close()
    assert rows == 9000
    assert size / rows < 30, size / rows
