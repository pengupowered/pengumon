"""Storage layer: schema migrations, samples + per-minute rollups, history query rules, retention."""

import json
import os
import sqlite3
import time

import aiosqlite
import pytest

from pengumon.db import migrations
from pengumon.db.migrations import v002_samples_rollups
from pengumon.db import probes as probes_module
from pengumon.db.repository import DatabaseRepository
from pengumon.db.schema import (
    CREATE_PROBE_SAMPLES_TABLE,
    CREATE_PROBE_SAMPLES_INDEX,
    CREATE_INCIDENTS_TABLE,
    CREATE_DOCSIS_SIGNALS_TABLE,
)
from pengumon.devices.samples import RouterHistory, device_sample, router_snapshot
from pengumon.lines.docsis import DocsisHistory, line_sample
from pengumon.devices.snapshots import RouterMetricsSnapshot
from pengumon.incidents.model import Incident
from pengumon.lines.docsis_model import DocsisSignalSnapshot
from pengumon.models.probes import ProbeSample

DAY = 86400
RAW_KEYS = {"target_id", "timestamp", "success", "latency_ms", "packet_loss_pct", "jitter_ms", "error"}
BUCKET_KEYS = {"target_id", "timestamp", "latency_ms", "packet_loss_pct", "jitter_ms", "successful_count",
               "total_count", "latency_p10_ms", "latency_p90_ms"}


def _query(path, sql, params=()):
    con = sqlite3.connect(path)
    try:
        return con.execute(sql, params).fetchall()
    finally:
        con.close()


def _tables(path):
    return {r[0] for r in _query(path, "SELECT name FROM sqlite_master WHERE type = 'table'")}


def _make_v1_db(path, base_ts, minutes=10, targets=("router", "gateway", "public")):
    """A pre-versioning database as the old code created it (incidents without path_trace even)."""
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")
    for stmt in (CREATE_PROBE_SAMPLES_TABLE, CREATE_PROBE_SAMPLES_INDEX, CREATE_INCIDENTS_TABLE,
                 CREATE_DOCSIS_SIGNALS_TABLE):
        con.execute(stmt)
    rows = []
    for i in range(minutes * 60):
        for j, tid in enumerate(targets):
            ok = 0 if (i % 30 == 0 and j == 2) else 1
            rows.append((tid, base_ts + i, ok, 10.0 + j if ok else None, 0.0 if ok else 100.0, 1.5,
                         None if ok else "Request timed out"))
    rows.append(rows[5])  # an exact duplicate (same target and timestamp)
    con.executemany(
        "INSERT INTO probe_samples (target_id, timestamp, success, latency_ms, packet_loss_pct, jitter_ms, error) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
    con.execute(
        "INSERT INTO incidents (start_time, end_time, duration_seconds, cause_code, title, description, severity, "
        "affected_targets, resolved) VALUES (?, ?, 30, 'unknown_outage', 'Resolved', 'd', 'warning', '[]', 1)",
        (base_ts + 60, base_ts + 90))
    con.execute(
        "INSERT INTO incidents (start_time, cause_code, title, description, severity, affected_targets, resolved) "
        "VALUES (?, 'unknown_outage', 'Orphan', 'd', 'critical', '[\"public\"]', 0)", (base_ts + 300,))
    con.commit()
    con.close()
    return len(rows) - 1, len(targets)


# --- migrations ----------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fresh_database_gets_the_latest_schema(tmp_path):
    path = str(tmp_path / "fresh.db")
    repo = DatabaseRepository(db_path=path)
    await repo.initialize()

    assert _query(path, "PRAGMA user_version")[0][0] == migrations.LATEST_VERSION
    tables = _tables(path)
    assert {"targets", "samples", "rollup_1m", "incidents", "device_samples", "line_samples", "custom_targets",
            "meta"} <= tables
    assert not {"probe_samples", "docsis_signals", "docsis_logs", "router_metrics"} & tables
    assert _query(path, "PRAGMA auto_vacuum")[0][0] == 2  # INCREMENTAL
    cols = {r[1] for r in _query(path, "PRAGMA table_info(incidents)")}
    assert {"path_trace", "load_pct", "expected"} <= cols
    assert "kind" in cols

    await repo.close()
    again = DatabaseRepository(db_path=path)
    await again.initialize()  # idempotent
    assert _query(path, "PRAGMA user_version")[0][0] == migrations.LATEST_VERSION


@pytest.mark.asyncio
async def test_v1_database_is_migrated_with_samples_rollups_and_incidents(tmp_path):
    path = str(tmp_path / "v1.db")
    base = int((time.time() - 3600) // 60) * 60
    n_rows, n_targets = _make_v1_db(path, base)

    repo = DatabaseRepository(db_path=path)
    await repo.initialize()

    assert _query(path, "PRAGMA user_version")[0][0] == migrations.LATEST_VERSION
    assert "probe_samples" not in _tables(path)
    assert _query(path, "PRAGMA auto_vacuum")[0][0] == 2
    assert _query(path, "PRAGMA journal_mode")[0][0] == "wal"
    assert _query(path, "SELECT COUNT(*) FROM samples")[0][0] == n_rows  # duplicate dropped
    assert sorted(r[0] for r in _query(path, "SELECT key FROM targets")) == ["gateway", "public", "router"]

    # Rollups agree with the raw data, minute by minute.
    assert _query(path, "SELECT COUNT(*) FROM rollup_1m")[0][0] == 10 * n_targets
    n, ok = _query(path, "SELECT SUM(n), SUM(ok) FROM rollup_1m")[0]
    assert n == n_rows and ok == _query(path, "SELECT SUM(ok) FROM samples")[0][0]
    pub = _query(path, "SELECT r.n, r.ok, r.lat_sum, r.lat_min, r.lat_max, r.jit_sum, r.jit_n FROM rollup_1m r "
                       "JOIN targets t ON t.id = r.target WHERE t.key = 'public' AND r.minute = ?", (base // 60,))[0]
    assert pub[:2] == (60, 58) and pub[2] == pytest.approx(58 * 12.0) and pub[3:5] == (12.0, 12.0)
    assert pub[5] == pytest.approx(60 * 1.5) and pub[6] == 60

    # Incidents kept; the orphan now ends at the last sample after its start, not start + 10 s.
    incidents = {i.title: i for i in await repo.get_recent_incidents(limit=10)}
    assert set(incidents) == {"Resolved", "Orphan"}
    assert incidents["Resolved"].duration_seconds == 30
    assert incidents["Resolved"].kind == "legacy_other" and incidents["Orphan"].kind == "legacy_other"
    last_ts = base + 10 * 60 - 1
    assert incidents["Orphan"].resolved and incidents["Orphan"].end_time == last_ts
    assert incidents["Orphan"].duration_seconds == last_ts - (base + 300)

    history = await repo.get_history(base, base + 59)
    assert len(history) == 60 * n_targets
    assert set(history[0]) == RAW_KEYS and isinstance(history[0]["target_id"], str)
    failed = [h for h in history if h["success"] == 0]
    assert len(failed) == 2 and all(h["packet_loss_pct"] == 100.0 and h["latency_ms"] is None for h in failed)


@pytest.mark.asyncio
async def test_migration_interrupted_midway_is_rolled_back_and_rerun(tmp_path, monkeypatch):
    path = str(tmp_path / "crash.db")
    base = int((time.time() - 3600) // 60) * 60
    n_rows, _ = _make_v1_db(path, base)
    monkeypatch.setattr(v002_samples_rollups, "COPY_CHUNK_ROWS", 500)

    class Crash(Exception):
        pass

    real_logger = v002_samples_rollups.logger

    class CrashingLogger:
        calls = 0

        def info(self, msg, *a, **k):
            if msg.startswith("Schema v2: samples"):
                CrashingLogger.calls += 1
                if CrashingLogger.calls == 3:  # after a few chunks were copied
                    raise Crash()
            real_logger.info(msg, *a, **k)

        def __getattr__(self, name):
            return getattr(real_logger, name)

    monkeypatch.setattr(v002_samples_rollups, "logger", CrashingLogger())
    with pytest.raises(Crash):
        await migrations.migrate(path)

    # v1 committed, v2 rolled back entirely.
    assert _query(path, "PRAGMA user_version")[0][0] == 1
    tables = _tables(path)
    assert "probe_samples" in tables and "samples" not in tables
    assert _query(path, "SELECT COUNT(*) FROM probe_samples")[0][0] == n_rows + 1

    monkeypatch.setattr(v002_samples_rollups, "logger", real_logger)
    assert await migrations.migrate(path) == migrations.LATEST_VERSION
    assert _query(path, "SELECT COUNT(*) FROM samples")[0][0] == n_rows
    assert _query(path, "SELECT SUM(n) FROM rollup_1m")[0][0] == n_rows
    assert _query(path, "PRAGMA auto_vacuum")[0][0] == 2


@pytest.mark.asyncio
async def test_vacuum_step_is_repeated_if_the_previous_start_died_before_it(tmp_path):
    path = str(tmp_path / "novac.db")
    base = int((time.time() - 3600) // 60) * 60
    _make_v1_db(path, base, minutes=1)
    real = migrations.AUTO_VACUUM_INCREMENTAL
    try:
        migrations.AUTO_VACUUM_INCREMENTAL = 0  # i.e. "already done": skips the conversion
        await migrations.migrate(path)
    finally:
        migrations.AUTO_VACUUM_INCREMENTAL = real
    assert _query(path, "PRAGMA user_version")[0][0] == migrations.LATEST_VERSION
    assert _query(path, "PRAGMA auto_vacuum")[0][0] == 0
    await migrations.migrate(path)
    assert _query(path, "PRAGMA auto_vacuum")[0][0] == 2


@pytest.mark.asyncio
async def test_orphan_without_later_samples_falls_back_to_ten_seconds(tmp_path):
    path = str(tmp_path / "orphan.db")
    repo = DatabaseRepository(db_path=path)
    await repo.initialize()
    start = time.time()
    await repo.create_incident(Incident(start_time=start, cause_code="unknown_outage", title="t", description="d",
                                        severity="warning", affected_targets=[]))
    await repo.close()

    again = DatabaseRepository(db_path=path)
    await again.initialize()
    inc = (await again.get_recent_incidents(limit=1))[0]
    assert inc.resolved and inc.end_time == pytest.approx(start + 10.0) and inc.duration_seconds == pytest.approx(10.0)


# --- writes --------------------------------------------------------------------------------------

def _samples(start, seconds, targets=("a", "b"), fail_every=0):
    out = []
    for i in range(seconds):
        for tid in targets:
            ok = not (fail_every and i % fail_every == 0)
            out.append(ProbeSample(target_id=tid, timestamp=start + i, success=ok, latency_ms=5.0 if ok else None,
                                   packet_loss_pct=0.0 if ok else 100.0, jitter_ms=0.5,
                                   error=None if ok else "Request timed out"))
    return out


@pytest.fixture
async def repo(tmp_path):
    r = DatabaseRepository(db_path=str(tmp_path / "storage.db"), sample_flush_seconds=3600)
    await r.initialize()
    yield r
    await r.close()


@pytest.mark.asyncio
async def test_rollups_stay_exact_across_flushes_and_duplicates(repo):
    minute = int(time.time() // 60) * 60 - 600
    await repo.insert_samples(_samples(minute, 30, targets=("a",)))
    await repo.flush_samples()
    await repo.insert_samples(_samples(minute + 30, 30, targets=("a",)) + _samples(minute + 10, 1, targets=("a",)))
    await repo.flush_samples()
    assert _query(repo.db_path, "SELECT COUNT(*) FROM samples")[0][0] == 60
    assert _query(repo.db_path, "SELECT n, ok, lat_sum FROM rollup_1m")[0] == (60, 60, 300.0)


# --- history -------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_history_raw_shape(repo):
    start = time.time() - 120
    await repo.insert_samples(_samples(start, 60, fail_every=10))
    rows = await repo.get_history(start, start + 59)
    assert len(rows) == 120 and all(set(r) == RAW_KEYS for r in rows)
    assert [r["timestamp"] for r in rows] == sorted(r["timestamp"] for r in rows)
    assert {r["target_id"] for r in rows} == {"a", "b"}
    assert sum(1 for r in rows if r["success"] == 0) == 12
    only_a = await repo.get_history(start, start + 59, target_ids=["a"])
    assert len(only_a) == 60 and {r["target_id"] for r in only_a} == {"a"}
    assert await repo.get_history(start, start + 59, target_ids=["nope"]) == []


@pytest.mark.asyncio
async def test_history_small_buckets_come_from_raw_samples(repo):
    start = int(time.time() // 10) * 10 - 3600
    await repo.insert_samples(_samples(start, 600, fail_every=5))
    rows = await repo.get_history(start, start + 3600, bucket_seconds=10)
    assert all(set(r) == BUCKET_KEYS for r in rows)
    a = [r for r in rows if r["target_id"] == "a"]
    assert len(a) == 60 and all(r["timestamp"] % 10 == 0 for r in a)
    assert a[0]["total_count"] == 10 and a[0]["successful_count"] == 8 and a[0]["packet_loss_pct"] == 20.0
    assert a[0]["latency_ms"] == 5.0 and a[0]["jitter_ms"] == 0.5


@pytest.mark.asyncio
async def test_history_long_ranges_use_rollups(repo):
    start = int(time.time() // 3600) * 3600 - 2 * DAY
    await repo.insert_samples(_samples(start, 1800, fail_every=6))
    # 3 days with 10 s buckets requested: served from rollups in buckets of at least 60 s.
    rows = await repo.get_history(start - DAY, start + 2 * DAY, bucket_seconds=10)
    a = [r for r in rows if r["target_id"] == "a"]
    assert all(set(r) == BUCKET_KEYS for r in rows)
    assert len(a) == 30 and all(r["timestamp"] % 60 == 0 for r in a)
    assert a[0]["total_count"] == 60 and a[0]["successful_count"] == 50
    assert a[0]["packet_loss_pct"] == round(10 / 60 * 100, 1) and a[0]["latency_ms"] == 5.0

    hourly = await repo.get_history(start, start + 2 * 3600, bucket_seconds=3600)
    assert [(r["timestamp"], r["total_count"]) for r in hourly if r["target_id"] == "a"] == [(start, 1800)]

    # A raw request longer than 6 h is answered with (rollup) buckets, not millions of rows.
    long_raw = await repo.get_history(start - 3600, start + 7 * 3600)
    assert long_raw and all(set(r) == BUCKET_KEYS for r in long_raw)


def _spread_samples(start, minutes, target="a"):
    """Per minute, 100 answered probes with latencies 1..100 ms (so p10 = 10, p90 = 90) and 20 lost probes."""
    out = []
    for m in range(minutes):
        for i in range(120):
            ok = i < 100
            out.append(ProbeSample(target_id=target, timestamp=start + m * 60 + i * 0.5, success=ok,
                                   latency_ms=float(i + 1) if ok else None, packet_loss_pct=0.0 if ok else 100.0,
                                   jitter_ms=None, error=None if ok else "Request timed out"))
    return out


@pytest.mark.asyncio
async def test_latency_band_from_raw_samples_and_from_rollups_agree(repo):
    start = int(time.time() // 3600) * 3600 - 2 * 3600
    await repo.insert_samples(_spread_samples(start, 3))
    raw = await repo.get_history(start, start + 179, bucket_seconds=30)
    first = [r for r in raw if r["timestamp"] == start][0]
    assert first["successful_count"] == 60 and first["latency_p10_ms"] == 6.0 and first["latency_p90_ms"] == 54.0
    rolled = await repo.get_history(start, start + 179, bucket_seconds=180)
    assert len(rolled) == 1
    assert (rolled[0]["latency_p10_ms"], rolled[0]["latency_p90_ms"]) == (10.0, 90.0)
    assert rolled[0]["latency_ms"] == 50.5 and rolled[0]["successful_count"] == 300


@pytest.mark.asyncio
async def test_latency_band_edge_cases(repo):
    start = int(time.time() // 60) * 60 - 3600
    assert await repo.get_history(start, start + 600, bucket_seconds=60) == []  # no data at all
    # one single answered probe: both percentiles are that probe; a bucket with only lost probes has no band
    await repo.insert_samples([ProbeSample(target_id="one", timestamp=start, success=True, latency_ms=7.0,
                                           packet_loss_pct=0.0, jitter_ms=None, error=None),
                               ProbeSample(target_id="one", timestamp=start + 60, success=False, latency_ms=None,
                                           packet_loss_pct=100.0, jitter_ms=None, error="x")])
    rows = await repo.get_history(start, start + 119, target_ids=["one"], bucket_seconds=60)
    assert [(r["latency_p10_ms"], r["latency_p90_ms"]) for r in rows] == [(7.0, 7.0), (None, None)]
    raw = await repo.get_history(start, start + 119, target_ids=["one"], bucket_seconds=30)
    assert [(r["latency_p10_ms"], r["latency_p90_ms"]) for r in raw] == [(7.0, 7.0), (None, None)]


@pytest.mark.asyncio
async def test_latency_band_is_left_out_of_raw_buckets_for_long_ranges(repo):
    start = int(time.time() // 3600) * 3600 - 20 * 3600
    await repo.insert_samples(_spread_samples(start, 2))
    rows = await repo.get_history(start, start + 12 * 3600, bucket_seconds=30)  # > 6 h: sorting all probes is too slow
    assert rows and all(r["latency_p10_ms"] is None and r["latency_p90_ms"] is None for r in rows)


@pytest.mark.asyncio
async def test_v11_migration_computes_percentiles_for_stored_minutes(tmp_path):
    path = str(tmp_path / "v10.db")
    start = int(time.time() // 60) * 60 - 600
    repo = DatabaseRepository(db_path=path, sample_flush_seconds=0)
    await repo.initialize()
    await repo.insert_samples(_spread_samples(start, 2))
    await repo.close()
    con = sqlite3.connect(path)  # back to the v10 shape: no percentile columns, and an old minute with no samples
    con.execute("ALTER TABLE rollup_1m DROP COLUMN lat_p10")
    con.execute("ALTER TABLE rollup_1m DROP COLUMN lat_p90")
    con.execute("INSERT INTO rollup_1m (minute, target, n, ok, lat_sum) VALUES (1000, 1, 60, 60, 600.0)")
    con.execute("PRAGMA user_version=10")
    con.commit()
    con.close()
    assert await migrations.migrate(path) == migrations.LATEST_VERSION
    rows = _query(path, "SELECT minute, lat_p10, lat_p90 FROM rollup_1m ORDER BY minute")
    assert rows[0] == (1000, None, None)
    assert [r[1:] for r in rows[1:]] == [(10.0, 90.0), (10.0, 90.0)]


@pytest.mark.asyncio
async def test_history_is_clipped_to_retention(tmp_path):
    repo = DatabaseRepository(db_path=str(tmp_path / "clip.db"), sample_flush_seconds=3600,
                              retention_days=1, rollup_retention_days=2)
    await repo.initialize()
    now = time.time()
    await repo.insert_samples(_samples(now - 3 * DAY, 120, targets=("a",)))   # beyond both retentions
    await repo.insert_samples(_samples(now - 1.5 * DAY, 120, targets=("a",)))  # rollups only
    await repo.insert_samples(_samples(now - 600, 120, targets=("a",)))
    rows = await repo.get_history(now - 4 * DAY, now, bucket_seconds=60)
    assert rows and min(r["timestamp"] for r in rows) >= now - 2 * DAY - 60
    assert sum(r["total_count"] for r in rows) == 240
    # A short raw window that lies beyond raw retention is answered from rollups.
    old = await repo.get_history(now - 1.5 * DAY - 60, now - 1.5 * DAY + 180)
    assert old and set(old[0]) == BUCKET_KEYS
    assert await repo.get_history(now - 10 * DAY, now - 9 * DAY) == []


@pytest.mark.asyncio
async def test_history_row_cap(repo, monkeypatch):
    monkeypatch.setattr(probes_module, "HISTORY_ROW_CAP", 50)
    start = int(time.time() // 60) * 60 - 7200
    await repo.insert_samples(_samples(start, 7200, targets=("a",)))
    raw = await repo.get_history(start, start + 600)
    assert len(raw) == 50
    small = await repo.get_history(start, start + 7200, bucket_seconds=2)
    assert 0 < len(small) <= 50
    assert small[1]["timestamp"] - small[0]["timestamp"] >= 7200 / 50  # buckets widened to fit the cap
    rolled = await repo.get_history(start - 3 * DAY, start + 7200, bucket_seconds=60)
    assert 0 < len(rolled) <= 50 and sum(r["total_count"] for r in rolled) == 7200


@pytest.mark.asyncio
async def test_raw_samples_for_inspection_ignore_the_range_rule(repo):
    start = time.time() - 8 * 3600
    await repo.insert_samples(_samples(start, 10) + _samples(start + 7 * 3600, 10))
    rows = await repo.get_raw_samples(start, start + 8 * 3600)
    assert len(rows) == 40 and set(rows[0]) == RAW_KEYS


@pytest.mark.asyncio
async def test_hop_stats_come_from_rollups(repo):
    start = int(time.time() // 60) * 60 - 1200
    await repo.insert_samples(_samples(start, 600, fail_every=4))
    stats = {s["target_id"]: s for s in await repo.get_hop_stats(start)}
    assert stats["a"]["sent"] == 600 and stats["a"]["lost"] == 150 and stats["a"]["loss_pct"] == 25.0
    assert stats["a"]["avg_ms"] == 5.0 and stats["a"]["max_ms"] == 5.0


# --- retention -----------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_retention_prunes_raw_and_rollups_separately_and_frees_pages(repo):
    now = time.time()
    await repo.insert_samples(_samples(now - 10 * DAY, 3000, targets=("a", "b", "c")))
    await repo.insert_samples(_samples(now - 3600, 60))
    await repo.flush_samples()
    rollups_before = _query(repo.db_path, "SELECT COUNT(*) FROM rollup_1m")[0][0]
    recent_rollups = _query(repo.db_path, "SELECT COUNT(*) FROM rollup_1m WHERE minute >= ?",
                            (int((now - DAY) // 60),))[0][0]
    assert 0 < recent_rollups < rollups_before
    pages_before = _query(repo.db_path, "PRAGMA page_count")[0][0]

    removed = await repo.cleanup_old_samples(retention_days=7, rollup_retention_days=400)
    assert removed["samples"] == 9000 and removed["rollup_1m"] == 0
    assert _query(repo.db_path, "SELECT COUNT(*) FROM samples")[0][0] == 120
    assert _query(repo.db_path, "SELECT COUNT(*) FROM rollup_1m")[0][0] == rollups_before
    assert _query(repo.db_path, "PRAGMA freelist_count")[0][0] == 0
    assert _query(repo.db_path, "PRAGMA page_count")[0][0] < pages_before

    removed = await repo.cleanup_old_samples(retention_days=7, rollup_retention_days=5)
    assert removed["rollup_1m"] == rollups_before - recent_rollups
    assert _query(repo.db_path, "SELECT COUNT(*) FROM rollup_1m")[0][0] == recent_rollups


# --- performance sanity --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_time_range_queries_use_the_primary_key_on_a_large_table(repo):
    """500k samples: every history query is a primary-key range search, never a table scan."""
    now = int(time.time())
    con = sqlite3.connect(repo.db_path)
    con.executemany("INSERT INTO targets (id, key) VALUES (?, ?)", [(i, f"t{i}") for i in range(1, 11)])
    con.executemany(
        "INSERT INTO samples (ts, target, ok, latency_ms, jitter_ms) VALUES (?, ?, 1, 5.0, 0.5)",
        ((now - 50_000 + i // 10, i % 10 + 1) for i in range(500_000)))
    con.execute(
        "INSERT INTO rollup_1m (minute, target, n, ok, lat_sum, lat_min, lat_max, jit_sum, jit_n) SELECT CAST(ts / 60 AS INTEGER), target, COUNT(*), SUM(ok), SUM(latency_ms), "
        "MIN(latency_ms), MAX(latency_ms), SUM(jitter_ms), COUNT(jitter_ms) FROM samples GROUP BY 1, 2")
    con.commit()

    queries = {
        "samples": [repo._raw_query(now - 3600, now), repo._raw_query(now - 3600, now, [1, 2]),
                    repo._raw_bucket_query(now - 3600, now, 10)],
        "rollup_1m": [repo._rollup_query(now - DAY, now, 60), repo._rollup_query(now - DAY, now, 600, [3])],
    }
    for table, items in queries.items():
        for sql, params in items:
            plan = " | ".join(r[3] for r in con.execute("EXPLAIN QUERY PLAN " + sql, params))
            assert f"SEARCH {table} USING PRIMARY KEY" in plan, plan
            assert f"SCAN {table}" not in plan, plan

    sql, params = repo._raw_query(now - 3600, now)
    started = time.perf_counter()
    rows = con.execute(sql, params).fetchall()
    elapsed = time.perf_counter() - started
    con.close()
    assert len(rows) == 36_000
    assert elapsed < 2.0  # a scan of the whole table would not matter here; the plan check above does


# --- connections ---------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_two_long_lived_connections_serve_every_call(tmp_path, monkeypatch):
    opened = []
    real_connect = aiosqlite.connect

    def counting_connect(*args, **kwargs):
        opened.append(args[0])
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(aiosqlite, "connect", counting_connect)
    repo = DatabaseRepository(db_path=str(tmp_path / "conn.db"), sample_flush_seconds=0)
    await repo.initialize()
    migration_connections = len(opened) - 2          # migrate() has its own, before the two are opened
    assert migration_connections == 1
    now = time.time()
    for i in range(10):
        await repo.insert_samples([ProbeSample(target_id="t", timestamp=now + i, success=True, latency_ms=1.0)])
        await repo.set_meta("k", str(i))
        assert await repo.get_meta("k") == str(i)
        await repo.get_history(now - 5, now + 60)
        await repo.get_recent_incidents()
    await repo.count_custom_targets()
    assert len(opened) == migration_connections + 2
    await repo.close()


@pytest.mark.asyncio
async def test_wal_file_persists_between_writes(repo):
    for i in range(3):
        await repo.set_meta("k", str(i))
        assert os.path.exists(repo.db_path + "-wal")


@pytest.mark.asyncio
async def test_a_flush_of_many_buffered_samples_is_one_write_transaction(tmp_path, monkeypatch):
    repo = DatabaseRepository(db_path=str(tmp_path / "flush.db"), sample_flush_seconds=3600)
    await repo.initialize()
    now = time.time()
    await repo.insert_samples(
        [ProbeSample(target_id=f"t{i % 4}", timestamp=now + i, success=True, latency_ms=1.0) for i in range(200)])
    writes = []
    real_write = repo._db.write

    def spy():
        writes.append(1)
        return real_write()

    monkeypatch.setattr(repo._db, "write", spy)
    await repo.flush_samples()
    assert len(writes) == 1
    assert _query(repo.db_path, "SELECT COUNT(*) FROM samples")[0][0] == 200
    await repo.close()


@pytest.mark.asyncio
async def test_committed_writes_survive_close_and_a_closed_repository_stays_closed(tmp_path):
    path = str(tmp_path / "reopen.db")
    repo = DatabaseRepository(db_path=path)
    await repo.initialize()
    await repo.set_meta("k", "v")
    await repo.close()
    assert not os.path.exists(path + "-wal")          # the last connection closed: checkpointed and removed
    with pytest.raises(RuntimeError, match="closed"):
        await repo.get_meta("k")                      # close is final: no silent reopen after shutdown
    again = DatabaseRepository(db_path=path)
    await again.initialize()
    assert await again.get_meta("k") == "v"           # the commit made it to disk
    await again.close()


# --- schema v7: error_kind, and router rates that were not measured ----------------------------------------

def _router(ts, rx=None, tx=None, **fields):
    return RouterMetricsSnapshot(timestamp=ts, wan_rx_rate_mbps=rx, wan_tx_rate_mbps=tx, **fields)


async def _store_router(repo, snapshot, device_id="router"):
    await repo.insert_device_sample(device_sample(device_id, snapshot))


@pytest.mark.asyncio
async def test_router_snapshot_without_rates_or_model_reads_back_as_none(repo):
    await _store_router(repo, _router(1000.0, status="error", error_message="x", error_kind="timeout"))
    latest = router_snapshot(await repo.get_latest_device_sample("router"))
    (ranged,) = await RouterHistory(repo, "router").get_router_snapshots_in_range(900.0, 1100.0, padding_seconds=0)
    for snap in (latest, ranged):
        assert snap.wan_rx_rate_mbps is None and snap.wan_tx_rate_mbps is None  # not 0.0
        assert snap.model_name is None  # not a default model name
        assert snap.error_kind == "timeout" and snap.status == "error"


@pytest.mark.asyncio
async def test_docsis_error_kind_round_trips(repo):
    await repo.insert_line_sample(line_sample("modem", DocsisSignalSnapshot(timestamp=1000.0, status="error",
                                                                            error_kind="auth")))
    await repo.insert_line_sample(line_sample("modem", DocsisSignalSnapshot(timestamp=2000.0, status="healthy")))
    history = DocsisHistory(repo, "modem")
    assert (await history.get_latest_docsis_snapshot()).error_kind is None
    ranged = await history.get_docsis_snapshots_in_range(0, 3000, padding_seconds=0)
    assert [(s.timestamp, s.error_kind) for s in ranged] == [(1000.0, "auth"), (2000.0, None)]


@pytest.mark.asyncio
async def test_router_rates_and_percentile_ignore_polls_without_a_measured_rate(repo):
    await _store_router(repo, _router(1000.0, 10.0, 1.0))
    await _store_router(repo, _router(1010.0))                     # baseline poll: no rate
    await _store_router(repo, _router(1020.0, 30.0, 3.0))
    await _store_router(repo, _router(1030.0, status="error", error_kind="timeout"))
    await _store_router(repo, _router(1040.0, 20.0, 2.0))
    assert await repo.get_wan_rates("router", 0, 2000) == [(1000.0, 10.0, 1.0), (1020.0, 30.0, 3.0), (1040.0, 20.0, 2.0)]
    assert await repo.get_wan_rate_percentile("router", 0, 0.5) == (3, 20.0, 2.0)  # over the 3 measured rows only
    assert await repo.get_wan_rate_percentile("router", 0, 1.0) == (3, 30.0, 3.0)
    await _store_router(repo, _router(3000.0))
    assert await repo.get_wan_rate_percentile("router", 2500.0, 0.5) is None  # only unmeasured rows since then


@pytest.mark.asyncio
async def test_migrating_a_v6_database_adds_error_kind_to_both_tables(tmp_path, monkeypatch):
    path = str(tmp_path / "v6.db")
    monkeypatch.setattr(migrations, "MIGRATIONS", migrations.MIGRATIONS[:6])
    assert await migrations.migrate(path) == 6
    monkeypatch.undo()
    for table in ("docsis_signals", "router_metrics"):
        assert "error_kind" not in {r[1] for r in _query(path, f"PRAGMA table_info({table})")}

    monkeypatch.setattr(migrations, "MIGRATIONS", migrations.MIGRATIONS[:7])  # v9 replaces both tables
    assert await migrations.migrate(path) == 7
    for table in ("docsis_signals", "router_metrics"):
        assert "error_kind" in {r[1] for r in _query(path, f"PRAGMA table_info({table})")}
    assert await migrations.migrate(path) == 7  # a second run changes nothing


def test_error_kind_is_stored_but_not_part_of_the_models_json():
    for model in (RouterMetricsSnapshot(status="error", error_kind="timeout"),
                  DocsisSignalSnapshot(status="error", error_kind="timeout")):
        assert "error_kind" not in model.model_dump()
        assert "error_kind" not in json.loads(model.model_dump_json())
        assert model.error_kind == "timeout"
