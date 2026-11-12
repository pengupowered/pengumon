"""Unit tests for SQLite database repository."""

import os
import time
import pytest
from pengumon.incidents.model import Incident
from pengumon.models.probes import ProbeSample
from pengumon.db.repository import DatabaseRepository


@pytest.mark.asyncio
async def test_db_repository(tmp_path):
    db_file = str(tmp_path / "test_pengumon.db")
    repo = DatabaseRepository(db_path=db_file)
    await repo.initialize()

    # Test sample insertion
    now = time.time()
    samples = [
        ProbeSample(target_id="local", timestamp=now, success=True, latency_ms=1.5),
        ProbeSample(target_id="public", timestamp=now, success=True, latency_ms=12.4),
    ]
    await repo.insert_samples(samples)

    history = await repo.get_history(start_time=now - 10, end_time=now + 10)
    assert len(history) == 2

    # Test Incident lifecycle
    inc = Incident(
        start_time=now,
        cause_code="xfinity_last_mile_coax_drop",
        title="Test ISP Outage",
        description="Testing incident storage",
        severity="critical",
        affected_targets=["public"],
    )
    inc_id = await repo.create_incident(inc)
    assert inc_id is not None

    incidents = await repo.get_recent_incidents(limit=10)
    assert len(incidents) == 1
    assert incidents[0].id == inc_id
    assert incidents[0].resolved is False

    # Resolve incident
    await repo.resolve_incident(inc_id, end_time=now + 45.0)
    resolved_incidents = await repo.get_recent_incidents(limit=10)
    assert resolved_incidents[0].resolved is True
    assert resolved_incidents[0].duration_seconds == 45.0

    # Test DOCSIS error snapshot persistence (closing outage blind spot)
    from pengumon.lines.docsis import DocsisHistory, line_sample
    from pengumon.lines.docsis_model import DocsisSignalSnapshot
    err_snap = DocsisSignalSnapshot(
        timestamp=now + 50.0,
        status="error",
        error_message="Gateway unreachable",
        channels=[]
    )
    await repo.insert_line_sample(line_sample("modem", err_snap))
    latest_snap = await DocsisHistory(repo, "modem").get_latest_docsis_snapshot()
    assert latest_snap is not None
    assert latest_snap.status == "error"
    assert len(latest_snap.channels) == 0


@pytest.mark.asyncio
async def test_one_unreadable_incident_row_does_not_take_the_list_down(tmp_path, caplog):
    import logging
    import aiosqlite
    from pengumon.incidents.model import Incident
    path = str(tmp_path / "rows.db")
    repo = DatabaseRepository(db_path=path)
    await repo.initialize()
    ids = [await repo.create_incident(Incident(start_time=1000.0 + k, cause_code="unknown_outage", title=f"t{k}",
                                               description="d", severity="warning", affected_targets=["gw"]))
           for k in range(4)]
    async with aiosqlite.connect(path) as db:
        await db.execute("UPDATE incidents SET cause_code = 'a_cause_from_a_newer_build' WHERE id = ?", (ids[0],))
        await db.execute("UPDATE incidents SET severity = 'catastrophic' WHERE id = ?", (ids[1],))
        await db.execute("UPDATE incidents SET affected_targets = 'not json' WHERE id = ?", (ids[2],))
        await db.commit()
    repo._incidents_version += 1
    with caplog.at_level(logging.WARNING, logger="pengumon.db"):
        got = {i.id: i for i in await repo.get_recent_incidents(10)}
        repo._incidents_version += 1
        await repo.get_recent_incidents(10)
    assert set(got) == set(ids)
    assert (got[ids[0]].cause_code, got[ids[1]].severity, got[ids[2]].affected_targets) == ("unknown_outage", "info", [])
    assert got[ids[3]].affected_targets == ["gw"]
    assert len([r for r in caplog.records if "cannot be read" in r.getMessage()]) == 3  # once per incident
    assert (await repo.get_incident_by_id(ids[0])).title == "t0"
    await repo.close()


@pytest.mark.asyncio
async def test_incidents_since_can_include_those_that_started_before_and_overlap(tmp_path):
    from pengumon.incidents.model import Incident
    repo = DatabaseRepository(db_path=str(tmp_path / "since.db"))
    await repo.initialize()

    async def add(start, end):
        i = await repo.create_incident(Incident(start_time=start, cause_code="unknown_outage", title="t", description="d",
                                                severity="warning"))
        if end is not None:
            await repo._resolve_row(i, end)
        return i
    before = await add(100.0, 150.0)
    overlaps = await add(180.0, 250.0)
    still_open = await add(190.0, None)
    inside = await add(300.0, 310.0)
    assert [r["id"] for r in await repo.get_incidents_since(200.0)] == [inside]
    assert [r["id"] for r in await repo.get_incidents_since(200.0, overlapping=True)] == [inside, still_open, overlaps]
    assert before not in [r["id"] for r in await repo.get_incidents_since(200.0, overlapping=True)]
    await repo.close()
