"""Telemetry budget: retention covers every high-volume table, the live message carries the incidents version rather
than the incident history, and the database keeps a connection open so the WAL isn't rebuilt on every write."""

import json
import os
import time

import aiosqlite
import pytest

from pengumon.live.ws import ConnectionManager
from pengumon.db.repository import DatabaseRepository
from pengumon.devices.samples import device_sample
from pengumon.devices.snapshots import RouterMetricsSnapshot
from pengumon.incidents.model import Incident, SystemDiagnosis
from pengumon.live.message import LiveStreamMessage
from pengumon.models.probes import ProbeSample

DAY = 86400


# --- retention -----------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cleanup_prunes_device_samples_and_reports_counts(repo):
    now = time.time()
    for age_days in (40, 10, 8, 1, 0):
        await repo.insert_device_sample(device_sample("router", RouterMetricsSnapshot(timestamp=now - age_days * DAY,
                                                                                      status="healthy")))
        await repo.insert_samples([ProbeSample(target_id="t", timestamp=now - age_days * DAY, success=True, latency_ms=1.0)])

    removed = await repo.cleanup_old_samples(retention_days=7)

    assert removed["device_samples"] == 3 and removed["samples"] == 3
    async with aiosqlite.connect(repo.db_path) as db:
        n = (await (await db.execute("select count(*) from device_samples")).fetchone())[0]
    assert n == 2


@pytest.mark.asyncio
async def test_cleanup_keeps_incidents(repo):
    old = time.time() - 60 * DAY
    await repo.create_incident(Incident(start_time=old, cause_code="unknown_outage", title="Old outage", description="d",
                                        severity="warning", affected_targets=[]))
    await repo.cleanup_old_samples(retention_days=7)
    assert len(await repo.get_recent_incidents(limit=10)) == 1


# --- incident list cache -----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_incident_list_is_cached_until_an_incident_changes(repo):
    now = time.time()
    first = await repo.get_recent_incidents(limit=500)
    assert first == [] and await repo.get_recent_incidents(limit=500) is first  # same object: no query

    v0 = repo.incidents_version
    inc_id = await repo.create_incident(Incident(start_time=now, cause_code="unknown_outage", title="T", description="d",
                                                 severity="warning", affected_targets=["a"]))
    assert repo.incidents_version > v0
    after_create = await repo.get_recent_incidents(limit=500)
    assert [i.id for i in after_create] == [inc_id] and not after_create[0].resolved

    v1 = repo.incidents_version
    await repo.resolve_incident(inc_id, now + 30)
    assert repo.incidents_version > v1
    assert (await repo.get_recent_incidents(limit=500))[0].resolved is True


# --- websocket: incident history only when it changed ---------------------------------------------

class FakeSocket:
    def __init__(self):
        self.sent = []

    async def accept(self):
        pass

    async def send_text(self, text):
        self.sent.append(json.loads(text))


def message(version=1):
    return LiveStreamMessage(
        timestamp=time.time(),
        diagnosis=SystemDiagnosis(status="healthy", cause_code="nominal", title="ok", description="d",
                                  timestamp=time.time()),
        targets=[], incidents_version=version,
    )


@pytest.mark.asyncio
async def test_the_live_message_carries_the_incidents_version_not_the_history():
    mgr = ConnectionManager()
    a = FakeSocket()
    await mgr.connect(a)

    await mgr.broadcast(message(1))
    await mgr.broadcast(message(2))  # an incident changed: the dashboard fetches the list itself
    assert [m["incidents_version"] for m in a.sent] == [1, 2]
    assert all("recent_incidents" not in m for m in a.sent)
    assert a.sent[1]["diagnosis"]["status"] == "healthy"


# --- database connection behavior ----------------------------------------------------------------

@pytest.mark.asyncio
async def test_wal_survives_between_writes(repo):
    """The connections stay open, so a write does not checkpoint and delete the WAL."""
    await repo.insert_samples([ProbeSample(target_id="t", timestamp=time.time(), success=True, latency_ms=1.0)])
    await repo.flush_samples()
    assert os.path.exists(repo.db_path + "-wal")
    await repo.set_meta("k", "v")
    assert os.path.exists(repo.db_path + "-wal")


@pytest.mark.asyncio
async def test_every_connection_uses_synchronous_normal(repo):
    async with repo._db.write() as db:
        assert (await (await db.execute("PRAGMA synchronous")).fetchone())[0] == 1  # NORMAL
    async with repo._db.read() as db:
        assert (await (await db.execute("PRAGMA synchronous")).fetchone())[0] == 1


# --- batched sample commits ----------------------------------------------------------------------

def _sample(i=0):
    return ProbeSample(target_id="t", timestamp=time.time() + i, success=True, latency_ms=1.0)


async def _stored(repo):
    async with aiosqlite.connect(repo.db_path) as db:
        return (await (await db.execute("select count(*) from samples")).fetchone())[0]


@pytest.mark.asyncio
async def test_samples_are_committed_in_batches(tmp_path):
    repo = DatabaseRepository(db_path=str(tmp_path / "batch.db"), sample_flush_seconds=3600)
    await repo.initialize()
    await repo.insert_samples([_sample(0), _sample(1)])
    await repo.insert_samples([_sample(2)])
    assert await _stored(repo) == 0            # still in memory

    await repo.flush_samples()
    assert await _stored(repo) == 3
    await repo.close()


@pytest.mark.asyncio
async def test_reads_see_pending_samples(tmp_path):
    repo = DatabaseRepository(db_path=str(tmp_path / "batch2.db"), sample_flush_seconds=3600)
    await repo.initialize()
    now = time.time()
    await repo.insert_samples([ProbeSample(target_id="t", timestamp=now, success=True, latency_ms=2.0)])
    history = await repo.get_history(start_time=now - 5, end_time=now + 5)
    assert len(history) == 1                   # flushed by the read itself
    await repo.close()


@pytest.mark.asyncio
async def test_interval_triggers_a_flush_and_close_writes_the_rest(tmp_path):
    repo = DatabaseRepository(db_path=str(tmp_path / "batch3.db"), sample_flush_seconds=0)
    await repo.initialize()
    await repo.insert_samples([_sample()])
    assert await _stored(repo) == 1            # interval 0 = every call

    slow = DatabaseRepository(db_path=str(tmp_path / "batch4.db"), sample_flush_seconds=3600)
    await slow.initialize()
    await slow.insert_samples([_sample(), _sample(1)])
    await slow.close()                         # shutdown path
    assert await _stored(slow) == 2
    await repo.close()


@pytest.mark.asyncio
async def test_failed_flush_keeps_the_samples(tmp_path, monkeypatch):
    repo = DatabaseRepository(db_path=str(tmp_path / "batch5.db"), sample_flush_seconds=3600)
    await repo.initialize()
    await repo.insert_samples([_sample()])

    def broken():
        raise RuntimeError("disk full")

    monkeypatch.setattr(repo._db, "write", broken)
    with pytest.raises(RuntimeError):
        await repo.flush_samples()
    assert len(repo._sample_buffer) == 1       # nothing lost
    monkeypatch.undo()
    await repo.flush_samples()
    assert await _stored(repo) == 1
    await repo.close()
