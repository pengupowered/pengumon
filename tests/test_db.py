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
