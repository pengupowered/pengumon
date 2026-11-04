"""Telemetry of the database: the sample buffer, flush failures, file sizes, retention and the incremental vacuum."""

import asyncio
import time
from types import SimpleNamespace

import pytest

import obs_helpers
from pengumon.db import probes
from pengumon.db.repository import DatabaseRepository
from pengumon.models.probes import ProbeSample
from pengumon.observability import export
from pengumon.observability.gauges import GAUGES

obs_helpers.install()
DAY = 86400


def _failing_write():
    raise OSError("database or disk is full")


def _samples(n, t0, target="local"):
    return [ProbeSample(target_id=target, timestamp=t0 + i, success=True, latency_ms=1.0) for i in range(n)]


def _read(gauge_name, state):
    gauge = next(g for g in GAUGES if g.otel == gauge_name)
    export.observe_state(state)
    try:
        return [(o.attributes, o.value) for o in export._guard(gauge)(None)]
    finally:
        export.observe_state(None)


async def test_failed_flushes_are_counted_by_error_kind_and_dropped_samples_are_counted(tmp_path, monkeypatch):
    monkeypatch.setattr(probes, "SAMPLE_BUFFER_MAX", 100)
    repo = DatabaseRepository(db_path=str(tmp_path / "p.db"), sample_flush_seconds=0)
    await repo.initialize()
    monkeypatch.setattr(repo._db, "write", _failing_write)
    failures = obs_helpers.total("pengumon.db.flush.failures", error="OSError")
    dropped = obs_helpers.total("pengumon.db.samples.dropped")
    t0 = time.time()
    for r in range(20):
        await repo.insert_samples(_samples(12, t0 + r * 12))
    assert repo.sample_buffer_size == 100
    assert obs_helpers.total("pengumon.db.flush.failures", error="OSError") - failures == 20
    assert obs_helpers.total("pengumon.db.samples.dropped") - dropped == 20 * 12 - 100


async def test_a_healthy_buffer_drops_nothing_and_fails_nothing(tmp_path):
    repo = DatabaseRepository(db_path=str(tmp_path / "p.db"), sample_flush_seconds=3600)
    await repo.initialize()
    dropped = obs_helpers.total("pengumon.db.samples.dropped")
    failures = obs_helpers.total("pengumon.db.flush.failures")
    await repo.insert_samples(_samples(50, time.time() - 600))
    assert repo.sample_buffer_size == 50
    await repo.flush_samples()
    assert repo.sample_buffer_size == 0
    assert obs_helpers.total("pengumon.db.samples.dropped") == dropped
    assert obs_helpers.total("pengumon.db.flush.failures") == failures


async def test_the_buffer_and_file_size_gauges_read_the_repository(tmp_path):
    repo = DatabaseRepository(db_path=str(tmp_path / "p.db"), sample_flush_seconds=3600)
    await repo.initialize()
    await repo.insert_samples(_samples(7, time.time()))
    state = SimpleNamespace(repository=repo)
    assert _read("pengumon.db.sample_buffer", state) == [({}, 7)]
    sizes = dict((a["file"], v) for a, v in _read("pengumon.db.file.size", state))
    assert set(sizes) == {"main", "wal"} and sizes["main"] > 0 and sizes["wal"] >= 0
    await repo.flush_samples()
    assert _read("pengumon.db.sample_buffer", state) == [({}, 0)]


def test_the_storage_gauges_stay_out_without_a_repository():
    assert _read("pengumon.db.sample_buffer", SimpleNamespace(repository=None)) == []
    assert _read("pengumon.db.file.size", SimpleNamespace(repository=None)) == []


async def test_retention_counts_the_rows_it_removes_by_table_and_the_pages_it_frees(tmp_path):
    repo = DatabaseRepository(db_path=str(tmp_path / "p.db"), sample_flush_seconds=0)
    await repo.initialize()
    old = time.time() - 30 * DAY
    for batch in range(10):
        await repo.insert_samples(_samples(2000, old + batch * 2000))
    await repo.insert_samples(_samples(5, time.time() - 5))
    await repo.flush_samples()
    rows = obs_helpers.total("pengumon.db.retention.rows", table="samples")
    pages = obs_helpers.total("pengumon.db.vacuum.pages")
    runs = obs_helpers.total("pengumon.db.retention.duration")

    removed = await repo.cleanup_old_samples(retention_days=7)

    assert removed["samples"] == 20000
    assert obs_helpers.total("pengumon.db.retention.rows", table="samples") - rows == 20000
    assert obs_helpers.total("pengumon.db.vacuum.pages") - pages > 10  # 20 000 rows are well over ten pages
    assert obs_helpers.total("pengumon.db.retention.duration") - runs == 1


async def test_a_retention_run_with_nothing_to_remove_adds_no_rows_but_is_timed(tmp_path):
    repo = DatabaseRepository(db_path=str(tmp_path / "p.db"), sample_flush_seconds=0)
    await repo.initialize()
    rows = obs_helpers.total("pengumon.db.retention.rows")
    runs = obs_helpers.total("pengumon.db.retention.duration")
    await repo.cleanup_old_samples()
    assert obs_helpers.total("pengumon.db.retention.rows") == rows
    assert obs_helpers.total("pengumon.db.retention.duration") - runs == 1
