"""Retention (db/core.py `_prune`) deletes in bounded batches, each its own write transaction, so a long backlog never
holds the write lock for the whole delete (the probe loop's sample flush waits for one batch)."""

import asyncio
import sqlite3
import time

from pengumon.db.repository import DatabaseRepository
from pengumon.models.probes import ProbeSample

DAY = 86400


def _samples(n, t0, target="local"):
    return [ProbeSample(target_id=target, timestamp=t0 + i, success=True, latency_ms=1.0) for i in range(n)]


def _count(path, sql, *args):
    con = sqlite3.connect(path)
    try:
        return con.execute(sql, args).fetchone()[0]
    finally:
        con.close()


async def _repo(tmp_path, monkeypatch, batch):
    monkeypatch.setattr("pengumon.db.core.PRUNE_BATCH_ROWS", batch, raising=False)
    monkeypatch.setattr("pengumon.db.core.PRUNE_VACUUM_PAGES", 1, raising=False)
    repo = DatabaseRepository(db_path=str(tmp_path / "b.db"), sample_flush_seconds=0)
    await repo.initialize()
    return repo


def _seed_old_device_rows(path, old):
    con = sqlite3.connect(path)
    con.execute("INSERT INTO targets (id, key) VALUES (99, 'x') ON CONFLICT DO NOTHING")
    con.executemany("INSERT INTO device_samples (device_id, ts, status) VALUES ('router', ?, 'ok')",
                    [(old + i,) for i in range(23)])
    con.executemany("INSERT INTO mesh_samples (device_id, ts, node_id, clients) VALUES ('router', ?, 'n1', 1)",
                    [(old + i,) for i in range(11)])
    con.commit()
    con.close()


async def test_a_backlog_is_deleted_completely_in_several_transactions(tmp_path, monkeypatch):
    repo = await _repo(tmp_path, monkeypatch, 7)
    old = time.time() - 30 * DAY
    await repo.insert_samples(_samples(100, old) + _samples(100, old, "other") + _samples(5, time.time() - 5))
    await repo.flush_samples()
    _seed_old_device_rows(repo.db_path, old)
    writes = []
    real = repo._db.write

    def counting():
        writes.append(1)
        return real()
    monkeypatch.setattr(repo._db, "write", counting)

    removed = await repo.cleanup_old_samples(retention_days=7)

    assert removed["samples"] == 200 and removed["device_samples"] == 23 and removed["mesh_samples"] == 11
    assert _count(repo.db_path, "SELECT COUNT(*) FROM samples") == 5
    assert _count(repo.db_path, "SELECT COUNT(*) FROM device_samples") == 0
    assert _count(repo.db_path, "SELECT COUNT(*) FROM mesh_samples") == 0
    assert _count(repo.db_path, "PRAGMA freelist_count") == 0  # the vacuum was stepped to the end
    assert len(writes) >= 200 // 7 + 23 // 7 + 11 // 7  # a transaction per batch, not one for everything
    await repo.close()


async def test_a_writer_waiting_during_retention_gets_in_before_it_ends(tmp_path, monkeypatch):
    repo = await _repo(tmp_path, monkeypatch, 5)
    old = time.time() - 30 * DAY
    await repo.insert_samples(_samples(300, old))
    await repo.flush_samples()
    started = asyncio.Event()
    real = repo._db.write

    def signalling():
        started.set()
        return real()
    monkeypatch.setattr(repo._db, "write", signalling)
    seen = {}

    async def other_writer():
        await started.wait()
        async with real() as db:  # a sample flush, say
            seen["retention_done"] = prune.done()
            await db.execute("INSERT INTO meta (key, value) VALUES ('probe', '1') ON CONFLICT DO NOTHING")

    prune = asyncio.ensure_future(repo.cleanup_old_samples(retention_days=7))
    await asyncio.gather(prune, other_writer())
    assert seen["retention_done"] is False
    assert prune.result()["samples"] == 300
    await repo.close()
