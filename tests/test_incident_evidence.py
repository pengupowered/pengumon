"""Each closed incident keeps its own evidence (db/evidence.py): written once after its tail, read first by the
inspector, equal to what the shared tables give while they exist, and still there after they are pruned."""

import sqlite3
import time
from types import SimpleNamespace

import pytest

from pengumon.analysis.incident import INSPECT_RAW_MAX_SECONDS, inspect_incident
from pengumon.db import evidence as evidence_module
from pengumon.db.evidence import EVIDENCE_RAW_EDGE_SECONDS, settle_seconds
from pengumon.db.migrations import v022_incident_evidence, v025_slim_samples
from pengumon.db.repository import DatabaseRepository
from pengumon.devices.samples import RouterHistory, device_sample
from pengumon.devices.snapshots import RouterMetricsSnapshot
from pengumon.incidents.evidence import write_pending
from pengumon.incidents.model import Incident
from pengumon.lines.docsis import DocsisHistory, line_sample
from pengumon.lines.docsis_model import DocsisChannel, DocsisSignalSnapshot
from pengumon.models.probes import ProbeSample
from old_schema import SAMPLES_BEFORE_V25

TARGET_IDS = ["router", "gateway", "isp"]
TARGETS = [SimpleNamespace(id=t, name=t.title(), category=c) for t, c in zip(TARGET_IDS, ("local", "gateway", "isp"))]


def probes(target, start, end, lost=lambda t: False):
    out = []
    t = start
    while t <= end:
        n = int(t - start)
        if lost(t):
            out.append(ProbeSample(target_id=target, timestamp=t, success=False, packet_loss_pct=100.0, error="timeout"))
        else:
            out.append(ProbeSample(target_id=target, timestamp=t, success=True, latency_ms=10.0 + (n % 5) * 0.5,
                                   jitter_ms=0.5 + (n % 3) * 0.25))
        t += 1.0
    return out


def modem_snap(ts, recent=0):
    channels = [DocsisChannel(channel_id=1, direction="downstream", frequency_mhz=600.0, power_dbmv=1.0, snr_db=36.5,
                              modulation="QAM256", correcteds=5, uncorrectables=recent, uncorrectables_recent=recent)]
    return DocsisSignalSnapshot(timestamp=ts, downstream_power_avg=1.5, downstream_snr_avg=37.5, upstream_power_avg=42.0,
                                uncorrectables_total=recent, uncorrectables_recent=recent, channels=channels,
                                status="healthy")


def router_snap(ts):
    return RouterMetricsSnapshot(timestamp=ts, wan_rx_bytes=int(ts) * 1000, wan_tx_bytes=int(ts) * 100,
                                 wan_rx_rate_mbps=20.0, wan_tx_rate_mbps=2.0, cpu_usage_pct=30.0, mem_usage_pct=40.0,
                                 client_count=12, status="healthy", model_name="Example_Router")


async def seeded(tmp_path, duration, *, ago=600.0, readings=True, name="ev.db", **repo_options):
    """A repository holding the probes of an incident of `duration` seconds that ended `ago` seconds ago, router polls
    every 10 s and modem readings every 5 minutes around it, and the incident (resolved)."""
    repo = DatabaseRepository(db_path=str(tmp_path / name), **repo_options)
    await repo.initialize()
    start = float(int(time.time() - ago - duration))
    for tid in TARGET_IDS:
        lost = (lambda t: start + 20 <= t <= start + 60 and int(t) % 2 == 0) if tid == "isp" else (lambda t: False)
        await repo.insert_samples(probes(tid, start - 60.0, start + duration + 60.0, lost))
    if readings:
        t = start - 300.0
        while t <= start + duration + 300.0:
            await repo.insert_device_sample(device_sample("router", router_snap(t)))
            t += 10.0
        t = start - 600.0
        while t <= start + duration + 600.0:
            await repo.insert_line_sample(line_sample("modem", modem_snap(t, recent=int(t) % 3)))
            t += 300.0
    incident = Incident(start_time=start, end_time=start + duration, duration_seconds=duration,
                        cause_code="unknown_outage", title="t", description="d", severity="warning", resolved=True)
    incident.id = await repo.create_incident(incident)
    return repo, incident


async def inspect(repo, incident):
    return (await inspect_incident(repo, TARGETS, incident, line=DocsisHistory(repo, "modem"),
                                   router=RouterHistory(repo, "router"))).to_response()


def count(repo, table, where="1", *args):
    con = sqlite3.connect(repo.db_path)
    try:
        return con.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", args).fetchone()[0]
    finally:
        con.close()


async def forget_shared_data(repo):
    """What the retention does to everything the inspector reads, long after the incident."""
    await repo.flush_samples()
    async with repo._db.write() as db:
        for table in ("samples", "rollup_10s", "rollup_1m", "rollup_15m", "device_samples", "line_samples"):
            await db.execute(f"DELETE FROM {table}")


# --- when it is written ------------------------------------------------------------------------------------------------

async def test_evidence_is_written_once_after_the_tail_has_passed(tmp_path):
    repo, incident = await seeded(tmp_path, 120.0)
    end = incident.end_time
    assert await write_pending(repo, now=end + 10.0) == 0  # the tail has not passed
    assert await write_pending(repo, now=end + settle_seconds() - 1.0) == 0
    assert await repo.get_incident_evidence(incident.id) is None
    assert await write_pending(repo, now=end + settle_seconds() + 1.0) == 1
    first = await repo.get_incident_evidence(incident.id)
    assert first.resolution == "raw"
    assert await write_pending(repo, now=end + settle_seconds() + 90.0) == 0  # once
    again = await repo.get_incident_evidence(incident.id)
    assert again.written_at == first.written_at
    assert await repo.write_incident_evidence(incident.id) is None  # nor by a direct call
    rows = count(repo, "incident_evidence_probes", "incident_id = ?", incident.id)
    assert rows == 3 * (120 + 90 + 1)  # every probe of the window, three targets


async def test_an_open_incident_gets_no_evidence_until_it_closes(tmp_path):
    repo, incident = await seeded(tmp_path, 120.0)
    open_one = Incident(start_time=incident.start_time, title="o", description="d", severity="warning",
                        cause_code="unknown_outage", resolved=False)
    open_id = await repo.create_incident(open_one)
    now = incident.end_time + settle_seconds() + 5.0
    assert await write_pending(repo, now=now) == 1  # the closed one only
    assert await repo.get_incident_evidence(open_id) is None
    assert await repo.write_incident_evidence(open_id) is None
    await repo.resolve_incident(open_id, incident.end_time)
    assert await write_pending(repo, now=now) == 1
    assert (await repo.get_incident_evidence(open_id)).resolution == "raw"


async def test_the_writer_does_a_few_incidents_per_pass(tmp_path):
    repo, incident = await seeded(tmp_path, 60.0)
    for _ in range(4):
        extra = Incident(start_time=incident.start_time, end_time=incident.end_time, duration_seconds=60.0,
                         cause_code="unknown_outage", title="t", description="d", severity="warning", resolved=True)
        await repo.create_incident(extra)
    now = incident.end_time + settle_seconds() + 5.0
    assert await write_pending(repo, now=now, limit=2) == 2
    assert await write_pending(repo, now=now, limit=2) == 2
    assert await write_pending(repo, now=now, limit=2) == 1


async def test_an_incident_with_nothing_stored_gets_a_row_that_says_so(tmp_path):
    repo, incident = await seeded(tmp_path, 60.0)
    gone = Incident(start_time=time.time() - 40 * 86400.0, end_time=time.time() - 40 * 86400.0 + 30,
                    duration_seconds=30.0, cause_code="unknown_outage", title="t", description="d",
                    severity="warning", resolved=True)
    gone.id = await repo.create_incident(gone)
    await write_pending(repo, now=time.time())
    assert (await repo.get_incident_evidence(gone.id)).resolution == "none"
    assert count(repo, "incident_evidence_readings", "incident_id = ?", gone.id) == 0
    assert await repo.write_incident_evidence(gone.id) is None  # "none" is done too
    assert (await inspect_incident(repo, TARGETS, gone)).samples == []  # the shared tables, as before


# --- what is kept ------------------------------------------------------------------------------------------------------

async def test_a_long_window_keeps_raw_ends_and_ten_second_buckets_between(tmp_path):
    duration = 3600.0
    repo, incident = await seeded(tmp_path, duration, readings=False)
    assert duration + 90 > INSPECT_RAW_MAX_SECONDS
    await repo.write_incident_evidence(incident.id)
    evidence = await repo.get_incident_evidence(incident.id)
    assert evidence.resolution == "mixed"
    ws, we = evidence.window_start, evidence.window_end
    raw = count(repo, "incident_evidence_probes", "incident_id = ? AND width = 0", incident.id)
    buckets = count(repo, "incident_evidence_probes", "incident_id = ? AND width = 10", incident.id)
    assert raw == pytest.approx(3 * 2 * EVIDENCE_RAW_EDGE_SECONDS, abs=3 * 22)
    assert buckets == pytest.approx(3 * (we - ws - 2 * EVIDENCE_RAW_EDGE_SECONDS) / 10, abs=3 * 3)
    # a bucket holds the probes of its slot
    con = sqlite3.connect(repo.db_path)
    try:
        n, ok = con.execute("SELECT n, ok FROM incident_evidence_probes WHERE incident_id = ? AND width = 10 "
                            "ORDER BY ts LIMIT 1", (incident.id,)).fetchone()
    finally:
        con.close()
    assert n == 10 and ok == 10
    # the writer's total stays far under the raw size
    assert raw + buckets < 0.6 * 3 * (duration + 90)


async def test_the_readings_kept_are_those_the_inspector_would_read(tmp_path):
    repo, incident = await seeded(tmp_path, 120.0)
    await repo.write_incident_evidence(incident.id)
    ws, we = incident.start_time - 45.0, incident.end_time + 45.0
    routers = await repo.evidence_device_samples(incident.id, "router")
    assert routers == await repo.get_device_samples("router", ws, we, 120.0)  # the same rows, padding included
    lines = await repo.evidence_line_samples(incident.id, "modem")
    assert lines and lines == await repo.get_line_samples("modem", ws, we, 120.0)


# --- what the inspector shows --------------------------------------------------------------------------------------------

async def test_a_short_incident_inspects_the_same_with_and_without_its_evidence(tmp_path):
    repo, incident = await seeded(tmp_path, 120.0)
    before = await inspect(repo, incident)
    assert before["samples"] and before["router_snapshots"] and before["docsis_correlation"]["nearest_snapshot"]
    await repo.write_incident_evidence(incident.id)
    assert await inspect(repo, incident) == before


async def test_a_long_incident_inspects_the_same_with_and_without_its_evidence(tmp_path):
    # 17000 + 90 s: about 10 s per chart bucket, a width the shared tables and the evidence both give
    repo, incident = await seeded(tmp_path, 17000.0, ago=3600.0)
    before = await inspect(repo, incident)
    assert before["sample_bucket_seconds"] == 10 and before["hop_impact"]
    await repo.write_incident_evidence(incident.id)
    assert (await repo.get_incident_evidence(incident.id)).resolution == "mixed"
    after = await inspect(repo, incident)
    assert after == before


@pytest.mark.parametrize("duration", [120.0, 3600.0])
async def test_the_inspector_still_shows_it_all_after_the_shared_data_is_gone(tmp_path, duration):
    repo, incident = await seeded(tmp_path, duration)
    await repo.write_incident_evidence(incident.id)
    with_shared = await inspect(repo, incident)
    await forget_shared_data(repo)
    assert await inspect(repo, incident) == with_shared
    assert with_shared["samples"] and with_shared["hop_impact"] and with_shared["router_snapshots"]
    assert with_shared["docsis_snapshots"]


async def test_without_evidence_the_pruned_incident_shows_nothing_so_the_test_above_reads_the_evidence(tmp_path):
    repo, incident = await seeded(tmp_path, 120.0)
    await forget_shared_data(repo)
    gone = await inspect(repo, incident)
    assert gone["samples"] == [] and gone["hop_impact"] == [] and gone["router_snapshots"] == []


async def test_the_modem_reading_nearest_within_reach_is_kept_and_shown(tmp_path):
    repo = DatabaseRepository(db_path=str(tmp_path / "reach.db"))
    await repo.initialize()
    start = float(int(time.time() - 7200))
    await repo.insert_samples(probes("router", start - 60, start + 120))
    # no reading within 120 s of the window; one 400 s before it and one 2000 s after
    for ts in (start - 45.0 - 400.0, start + 60.0 + 45.0 + 2000.0):
        await repo.insert_line_sample(line_sample("modem", modem_snap(ts)))
    incident = Incident(start_time=start, end_time=start + 60.0, duration_seconds=60.0, cause_code="unknown_outage",
                        title="t", description="d", severity="warning", resolved=True)
    incident.id = await repo.create_incident(incident)
    before = await inspect_incident(repo, TARGETS, incident, snapshot_reach_seconds=900.0,
                                    line=DocsisHistory(repo, "modem"))
    assert before.docsis_correlation.nearest_snapshot is not None and before.docsis_snapshots == []
    await repo.write_incident_evidence(incident.id, reach=900.0)
    await forget_shared_data(repo)
    after = await inspect_incident(repo, TARGETS, incident, snapshot_reach_seconds=900.0,
                                   line=DocsisHistory(repo, "modem"))
    assert after.to_response() == before.to_response()


# --- upgrade and minute resolution -------------------------------------------------------------------------------------------

async def test_the_migration_writes_the_evidence_of_every_resolved_incident(tmp_path):
    repo, incident = await seeded(tmp_path, 120.0)
    before = await inspect(repo, incident)
    open_one = Incident(start_time=incident.start_time, title="o", description="d", severity="warning",
                        cause_code="unknown_outage", resolved=False)
    open_id = await repo.create_incident(open_one)
    await repo.flush_samples()
    async with repo._db.write() as db:  # as a database from before v22 is (and its samples, from before v25)
        for table in ("incident_evidence", "incident_evidence_probes", "incident_evidence_readings"):
            await db.execute(f"DROP TABLE {table}")
        for statement in SAMPLES_BEFORE_V25:
            await db.execute(statement)
    import aiosqlite
    async with aiosqlite.connect(repo.db_path, isolation_level=None) as db:
        for step in (v022_incident_evidence.migrate, v025_slim_samples.migrate):  # the evidence, then the slimmer rows
            await db.execute("BEGIN IMMEDIATE")
            await step(db)
            await db.execute("COMMIT")
    assert (await repo.get_incident_evidence(incident.id)).resolution == "raw"
    assert await repo.get_incident_evidence(open_id) is None
    await forget_shared_data(repo)
    assert await inspect(repo, incident) == before


async def test_without_raw_probes_the_evidence_holds_the_minutes(tmp_path):
    repo, incident = await seeded(tmp_path, 120.0)
    await repo.flush_samples()
    async with repo._db.write() as db:
        await db.execute("DELETE FROM samples")  # past the raw retention; the per-minute rollups remain
    before = await inspect(repo, incident)
    assert before["hop_resolution"] == "minute" and before["sample_bucket_seconds"] == 60 and before["samples"]
    assert await repo.write_incident_evidence(incident.id) == "minute"
    assert count(repo, "incident_evidence_probes", "incident_id = ? AND width = 60", incident.id) > 0
    assert count(repo, "incident_evidence_probes", "incident_id = ? AND width = 0", incident.id) == 0
    assert await inspect(repo, incident) == before
    async with repo._db.write() as db:
        await db.execute("DELETE FROM rollup_1m")
        await db.execute("DELETE FROM rollup_15m")
    after = await inspect(repo, incident)
    assert after["hop_resolution"] == "minute" and after["samples"] == before["samples"]
    assert after["hop_impact"] == before["hop_impact"]


# --- prune -------------------------------------------------------------------------------------------------------------------

async def test_prune_removes_the_evidence_with_its_incident_and_keeps_the_rest(tmp_path):
    repo, old = await seeded(tmp_path, 120.0, incident_retention_days=30)
    recent = Incident(start_time=old.start_time + 1000.0, end_time=old.start_time + 1060.0, duration_seconds=60.0,
                      cause_code="unknown_outage", title="t", description="d", severity="warning", resolved=True)
    recent.id = await repo.create_incident(recent)
    await repo.write_incident_evidence(old.id)
    await repo.write_incident_evidence(recent.id)
    assert await repo.cleanup_old_samples() and count(repo, "incident_evidence") == 2  # nothing is old yet
    kept = count(repo, "incident_evidence_probes", "incident_id = ?", recent.id)
    kept_readings = count(repo, "incident_evidence_readings", "incident_id = ?", recent.id)
    assert count(repo, "incident_evidence_readings", "incident_id = ?", old.id) > 0
    # the first incident is now 40 days old, the evidence is exactly as it was
    async with repo._db.write() as db:
        await db.execute("UPDATE incidents SET start_time = start_time - ?, end_time = end_time - ? WHERE id = ?",
                         (40 * 86400.0, 40 * 86400.0, old.id))
    removed = await repo.cleanup_old_samples()
    assert removed["incidents"] == 1 and removed["incident_evidence"] > 0
    for table in ("incident_evidence", "incident_evidence_probes", "incident_evidence_readings"):
        assert count(repo, table, "incident_id = ?", old.id) == 0
    assert count(repo, "incident_evidence", "incident_id = ?", recent.id) == 1
    assert count(repo, "incident_evidence_probes", "incident_id = ?", recent.id) == kept
    assert count(repo, "incident_evidence_readings", "incident_id = ?", recent.id) == kept_readings


async def test_the_other_retentions_never_touch_the_evidence(tmp_path):
    repo, incident = await seeded(tmp_path, 120.0, incident_retention_days=0)
    await repo.write_incident_evidence(incident.id)
    before = [count(repo, t) for t in ("incident_evidence", "incident_evidence_probes", "incident_evidence_readings")]
    await repo.cleanup_old_samples(retention_days=1, rollup_retention_days=1, latency_retention_days=1,
                                   router_retention_days=1, mesh_retention_days=1, line_retention_days=1,
                                   traffic_retention_days=1, speedtest_retention_days=1)
    async with repo._db.write() as db:
        await db.execute("UPDATE incidents SET end_time = end_time - 900 * 86400, start_time = start_time - 900 * 86400")
    await repo.cleanup_old_samples()
    assert [count(repo, t) for t in ("incident_evidence", "incident_evidence_probes",
                                     "incident_evidence_readings")] == before
    assert evidence_module  # the module the rows come from
