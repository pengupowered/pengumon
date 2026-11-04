"""The sample buffer while every flush fails: bounded, logged once, and the probe round carries on."""

import logging
import time

import pytest

from pengumon.db import probes
from pengumon.db.repository import DatabaseRepository
from pengumon.models.probes import ProbeSample


def _failing_write():
    raise OSError("database or disk is full")


def _samples(n, t0):
    return [ProbeSample(target_id="local", timestamp=t0 + i, success=True, latency_ms=1.0) for i in range(n)]


@pytest.mark.asyncio
async def test_buffer_is_capped_while_flushes_fail(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(probes, "SAMPLE_BUFFER_MAX", 100)
    repo = DatabaseRepository(db_path=str(tmp_path / "p.db"), sample_flush_seconds=0)
    await repo.initialize()
    monkeypatch.setattr(repo._db, "write", _failing_write)
    caplog.set_level(logging.DEBUG, logger="pengumon.db")
    t0 = time.time()
    for r in range(50):  # does not raise: the round must go on
        await repo.insert_samples(_samples(12, t0 + r * 12))
    assert len(repo._sample_buffer) == 100
    assert repo._sample_buffer[-1].timestamp == t0 + 49 * 12 + 11  # the newest are kept
    assert repo._sample_buffer[0].timestamp == t0 + 50 * 12 - 100
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len([r for r in warnings if "dropping the oldest" in r.getMessage()]) == 1
    assert len(warnings) <= 3  # the first failure, the drop notice, no line per round


@pytest.mark.asyncio
async def test_buffer_recovers_when_writes_work_again(tmp_path, monkeypatch):
    monkeypatch.setattr(probes, "SAMPLE_BUFFER_MAX", 100)
    repo = DatabaseRepository(db_path=str(tmp_path / "p.db"), sample_flush_seconds=0)
    await repo.initialize()
    real = repo._db.write
    monkeypatch.setattr(repo._db, "write", _failing_write)
    t0 = time.time()
    for r in range(20):
        await repo.insert_samples(_samples(12, t0 + r * 12))
    monkeypatch.setattr(repo._db, "write", real)
    await repo.insert_samples(_samples(1, t0 + 1000))
    assert repo._sample_buffer == []
    assert len(await repo.get_history(start_time=t0 - 1, end_time=t0 + 2000)) == 100  # the cap, newest kept


@pytest.mark.asyncio
async def test_a_healthy_buffer_is_never_cut(tmp_path, monkeypatch):
    monkeypatch.setattr(probes, "SAMPLE_BUFFER_MAX", 100)
    repo = DatabaseRepository(db_path=str(tmp_path / "p.db"), sample_flush_seconds=3600)
    await repo.initialize()
    await repo.insert_samples(_samples(500, time.time() - 600))  # (a back-fill: written at the next flush)
    assert len(repo._sample_buffer) == 500
