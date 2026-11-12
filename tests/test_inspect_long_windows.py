"""The incident inspector on long windows: buckets for the charts, hop statistics aggregated over the whole event."""

import time
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from pengumon.analysis.incident import (
    INSPECT_MAX_POINTS,
    INSPECT_RAW_MAX_SECONDS,
    bucketed_samples,
    hop_impact,
    hop_impact_from_stats,
    inspect_incident,
)
from pengumon.db import probes as probes_module
from pengumon.db.repository import DatabaseRepository
from pengumon.lines.docsis import DocsisHistory, line_sample
from pengumon.main import app
from pengumon.incidents.model import Incident
from pengumon.lines.docsis_model import DocsisSignalSnapshot
from pengumon.models.probes import ProbeSample

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


async def seeded(tmp_path, duration, targets=TARGET_IDS, lost_from=60.0, lost_to=400.0):
    """A repository holding `duration` seconds of 1 s probes ending a few minutes ago, and the incident covering them."""
    repo = DatabaseRepository(db_path=str(tmp_path / "long.db"))
    await repo.initialize()
    start = float(int(time.time())) - duration - 600.0
    for tid in targets:
        lost = (lambda t: start + lost_from <= t <= start + lost_to and int(t) % 2 == 0) if tid == "isp" else (lambda t: False)
        await repo.insert_samples(probes(tid, start - 45.0, start + duration + 45.0, lost))
    incident = Incident(start_time=start, end_time=start + duration, duration_seconds=duration, cause_code="unknown_outage",
                        title="t", description="d", severity="warning", resolved=True)
    incident.id = await repo.create_incident(incident)
    return repo, incident


async def test_a_two_hour_incident_comes_back_as_bounded_buckets(tmp_path):
    repo, incident = await seeded(tmp_path, 7200.0)
    inspection = await inspect_incident(repo, TARGETS, incident)
    n = len(TARGET_IDS)
    assert inspection.sample_bucket_seconds == 5  # ceil(7290 / 1800)
    assert 0 < len(inspection.samples) <= INSPECT_MAX_POINTS * n
    sample = inspection.samples[0]
    assert set(sample) == {"target_id", "timestamp", "success", "latency_ms", "packet_loss_pct", "jitter_ms", "error"}
    assert sample["error"] is None and sample["success"] in (0, 1)
    assert [s["timestamp"] for s in inspection.samples] == sorted(s["timestamp"] for s in inspection.samples)
    assert {s["target_id"] for s in inspection.samples} == set(TARGET_IDS)


async def test_hop_statistics_of_a_long_incident_equal_the_ones_computed_from_every_raw_sample(tmp_path):
    repo, incident = await seeded(tmp_path, 7200.0)
    inspection = await inspect_incident(repo, TARGETS, incident)
    raw = await repo.get_raw_samples(incident.start_time - 45.0, incident.end_time + 45.0)
    expected = hop_impact(raw, incident.start_time - 10.0, incident.end_time + 2.0, TARGETS)
    assert [h.target_id for h in inspection.hop_impact] == [h.target_id for h in expected] == TARGET_IDS
    for got, want in zip(inspection.hop_impact, expected):
        assert (got.name, got.category, got.sent, got.received, got.loss_pct) == \
               (want.name, want.category, want.sent, want.received, want.loss_pct)
        assert (got.min_ms, got.max_ms) == (want.min_ms, want.max_ms)
        assert got.avg_ms == pytest.approx(want.avg_ms, abs=0.011)
        assert got.jitter_ms == pytest.approx(want.jitter_ms, abs=0.011)
    isp = inspection.hop_impact[2]
    assert isp.sent == 7200 + 10 + 2 + 1 and 0 < isp.loss_pct < 5.0


async def test_the_response_of_a_two_hour_incident_stays_small(tmp_path, app_state):
    repo, incident = await seeded(tmp_path, 7200.0)
    app_state.repository = repo
    app_state.config = SimpleNamespace(targets=TARGETS, dns_probes=[],
                                       docsis=SimpleNamespace(poll_interval_seconds=900.0))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        res = await ac.get(f"/api/incidents/{incident.id}/inspect")
    assert res.status_code == 200
    assert len(res.content) < 1_000_000
    body = res.json()
    assert body["sample_bucket_seconds"] == 5
    assert list(body)[:8] == ["incident", "window_start", "window_end", "event_start", "event_end", "hop_impact",
                              "samples", "sample_bucket_seconds"]


async def test_a_short_window_keeps_the_raw_samples_and_the_old_response_shape(tmp_path, monkeypatch):
    repo, incident = await seeded(tmp_path, 120.0)
    inspection = await inspect_incident(repo, TARGETS, incident)
    assert inspection.sample_bucket_seconds is None
    assert len(inspection.samples) == len(TARGET_IDS) * 211  # 1 s probes over the 210 s window
    assert inspection.samples[0]["error"] in (None, "timeout") and "successful_count" not in inspection.samples[0]
    assert "sample_bucket_seconds" not in inspection.to_response()


@pytest.mark.parametrize("duration, bucketed", [
    (INSPECT_RAW_MAX_SECONDS - 90.0, False),       # the window is exactly 1800 s
    (INSPECT_RAW_MAX_SECONDS - 90.0 + 1.0, True),
])
async def test_the_switch_to_buckets_happens_just_above_1800_seconds(tmp_path, duration, bucketed):
    repo, incident = await seeded(tmp_path, duration, targets=["router"])
    inspection = await inspect_incident(repo, TARGETS, incident)
    assert (inspection.sample_bucket_seconds is not None) is bucketed


async def test_hop_statistics_cover_the_whole_event_where_raw_rows_are_capped(tmp_path, monkeypatch):
    repo, incident = await seeded(tmp_path, 3600.0, targets=["router"], lost_from=1e9)
    monkeypatch.setattr(probes_module, "HISTORY_ROW_CAP", 1000)
    capped = await repo.get_raw_samples(incident.start_time - 45.0, incident.end_time + 45.0)
    assert len(capped) == 1000  # what the old inspector computed its statistics from
    inspection = await inspect_incident(repo, TARGETS, incident)
    assert inspection.hop_impact[0].sent == 3600 + 10 + 2 + 1
    assert inspection.sample_bucket_seconds == 4  # the repository widened the 2 s request to stay under the cap
    assert len(inspection.samples) <= 1000


async def test_a_long_window_with_nothing_in_the_event_range_falls_back_to_the_whole_window(tmp_path):
    repo = DatabaseRepository(db_path=str(tmp_path / "gap.db"))
    await repo.initialize()
    start = float(int(time.time())) - 6000.0
    # probes only far before and after the 3000 s incident
    await repo.insert_samples(probes("router", start - 45.0, start - 20.0) + probes("router", start + 3030.0, start + 3045.0))
    incident = Incident(start_time=start, end_time=start + 3000.0, duration_seconds=3000.0, cause_code="unknown_outage",
                        title="t", description="d", severity="warning", resolved=True)
    inspection = await inspect_incident(repo, TARGETS, incident)
    assert inspection.sample_bucket_seconds is not None
    assert [(h.target_id, h.sent) for h in inspection.hop_impact] == [("router", 26 + 16)]


async def test_no_samples_in_a_long_window_gives_an_empty_table(tmp_path):
    repo = DatabaseRepository(db_path=str(tmp_path / "empty.db"))
    await repo.initialize()
    start = float(int(time.time())) - 6000.0
    incident = Incident(start_time=start, end_time=start + 3000.0, duration_seconds=3000.0, cause_code="unknown_outage",
                        title="t", description="d", severity="warning", resolved=True)
    inspection = await inspect_incident(repo, TARGETS, incident)
    assert inspection.hop_impact == [] and inspection.samples == []


# --- pure pieces and the repository --------------------------------------------------------------------------

def test_bucketed_samples_take_the_shape_the_charts_read():
    rows = [
        {"target_id": "b", "timestamp": 20, "latency_ms": None, "packet_loss_pct": 100.0, "jitter_ms": None,
         "successful_count": 0, "total_count": 5},
        {"target_id": "b", "timestamp": 10, "latency_ms": 12.5, "packet_loss_pct": 40.0, "jitter_ms": 1.0,
         "successful_count": 3, "total_count": 5},
        {"target_id": "a", "timestamp": 10, "latency_ms": 3.0, "packet_loss_pct": 0.0, "jitter_ms": 0.5,
         "successful_count": 5, "total_count": 5},
    ]
    out = bucketed_samples(rows)
    assert [(s["target_id"], s["timestamp"], s["success"]) for s in out] == [("a", 10, 1), ("b", 10, 1), ("b", 20, 0)]
    assert out[1] == {"target_id": "b", "timestamp": 10, "success": 1, "latency_ms": 12.5, "packet_loss_pct": 40.0,
                      "jitter_ms": 1.0, "error": None}
    assert out[2]["latency_ms"] is None and out[2]["packet_loss_pct"] == 100.0


def test_hop_impact_from_stats_orders_like_the_config_and_computes_the_loss():
    stats = [
        {"target_id": "zzz", "sent": 10, "received": 10, "avg_ms": 1.0, "min_ms": 1.0, "max_ms": 1.0, "jitter_ms": None},
        {"target_id": "isp", "sent": 8, "received": 6, "avg_ms": 9.0, "min_ms": 8.0, "max_ms": 12.0, "jitter_ms": 0.5},
        {"target_id": "router", "sent": 0, "received": 0, "avg_ms": None, "min_ms": None, "max_ms": None, "jitter_ms": None},
    ]
    hops = hop_impact_from_stats(stats, TARGETS)
    assert [h.target_id for h in hops] == ["router", "isp", "zzz"]
    assert hops[1].loss_pct == 25.0 and hops[0].loss_pct == 0.0 and hops[2].category == "custom"
    assert hops[1].name == "Isp" and hops[1].category == "isp"


async def test_sample_stats_aggregate_answered_probes_only_for_latency(tmp_path):
    repo = DatabaseRepository(db_path=str(tmp_path / "stats.db"))
    await repo.initialize()
    await repo.insert_samples([
        ProbeSample(target_id="a", timestamp=100.0, success=True, latency_ms=4.0, jitter_ms=1.0),
        ProbeSample(target_id="a", timestamp=101.0, success=True, latency_ms=6.0, jitter_ms=None),
        ProbeSample(target_id="a", timestamp=102.0, success=False, packet_loss_pct=100.0, latency_ms=500.0, jitter_ms=3.0),
        ProbeSample(target_id="a", timestamp=103.0, success=True, latency_ms=50.0, jitter_ms=3.0),  # outside the range
        ProbeSample(target_id="b", timestamp=100.0, success=False, packet_loss_pct=100.0),
    ])
    stats = {row["target_id"]: row for row in await repo.get_sample_stats(100.0, 102.0)}
    assert stats["a"] == {"target_id": "a", "sent": 3, "received": 2, "avg_ms": 5.0, "min_ms": 4.0, "max_ms": 6.0,
                          "jitter_ms": 2.0}  # latency: answered probes only; jitter: every probe that reported one (as hop_impact)
    assert stats["b"]["received"] == 0 and stats["b"]["avg_ms"] is None and stats["b"]["jitter_ms"] is None
    assert await repo.get_sample_stats(500.0, 600.0) == []


async def test_history_buckets_report_the_width_they_used(tmp_path):
    repo = DatabaseRepository(db_path=str(tmp_path / "w.db"))
    await repo.initialize()
    now = float(int(time.time())) - 300.0
    await repo.insert_samples(probes("a", now - 7200.0, now))
    rows, width = await repo.get_history_buckets(now - 7200.0, now, bucket_seconds=5)
    assert width == 5 and rows and rows == await repo.get_history(now - 7200.0, now, bucket_seconds=5)
    rows, width = await repo.get_history_buckets(now - 7200.0, now, bucket_seconds=90)
    assert width == 120  # rollups come in whole minutes
    rows, width = await repo.get_history_buckets(now - 600.0, now)
    assert width == 0 and rows[0]["success"] in (0, 1)


# --- incidents whose raw samples are past retention (7 d raw, 400 d rollups) ---------------------------------

async def aged(tmp_path, duration, gateway_outage=None, kind=None):
    """Probes of an incident 30 days ago, pruned to the rollups like the nightly cleanup does, and the incident.
    The incident starts on a minute boundary; the router is fine, the isp loses every other probe during it, and
    the gateway answers, or loses every probe from second gateway_outage[0] for gateway_outage[1] seconds."""
    repo = DatabaseRepository(db_path=str(tmp_path / "aged.db"))
    await repo.initialize()
    start = float(int((time.time() - 30 * 86400.0) // 60) * 60)
    inside = lambda t: start <= t <= start + duration
    batches = {}
    for tid in TARGET_IDS:
        if tid == "isp":
            lost = lambda t: inside(t) and int(t) % 2 == 0
        elif tid == "gateway" and gateway_outage:
            lost = lambda t: start + gateway_outage[0] <= t < start + gateway_outage[0] + gateway_outage[1]
        else:
            lost = lambda t: False
        batches[tid] = probes(tid, start - 45.0, start + duration + 45.0, lost)
        await repo.insert_samples(batches[tid])
    removed = await repo.cleanup_old_samples(retention_days=7, rollup_retention_days=400)
    assert removed["samples"] > 0 and await repo.get_raw_samples(start - 60.0, start + duration + 60.0) == []
    incident = Incident(start_time=start, end_time=start + duration, duration_seconds=duration, cause_code="unknown_outage",
                        title="t", description="d", severity="warning", resolved=True, kind=kind)
    incident.id = await repo.create_incident(incident)
    return repo, incident, batches


def in_minutes(samples, first, last):
    return [s for s in samples if first <= int(s.timestamp // 60) <= last]


@pytest.mark.parametrize("duration", [180.0, 3 * 3600.0])  # a short window and a long one
async def test_an_incident_past_raw_retention_gets_its_hop_table_from_the_rollups(tmp_path, duration):
    repo, incident, batches = await aged(tmp_path, duration)
    inspection = await inspect_incident(repo, TARGETS, incident)
    hops = {h.target_id: h for h in inspection.hop_impact}
    assert [h.target_id for h in inspection.hop_impact] == TARGET_IDS
    first, last = int((incident.start_time - 10.0) // 60), int((incident.end_time + 2.0) // 60)  # whole minutes
    for tid, hop in hops.items():
        want = in_minutes(batches[tid], first, last)
        assert hop.sent == len(want) and hop.received == sum(1 for s in want if s.success)
        assert hop.min_ms == 10.0 and 10.0 <= hop.avg_ms <= 12.0 and hop.max_ms == 12.0
        assert 0.5 <= hop.jitter_ms <= 1.0
    assert hops["router"].loss_pct == 0.0 and hops["gateway"].loss_pct == 0.0
    assert 30.0 < hops["isp"].loss_pct < 55.0
    # the charts come from the rollups too, at minute resolution
    assert inspection.sample_bucket_seconds >= 60 and inspection.samples
    assert {s["target_id"] for s in inspection.samples} == set(TARGET_IDS)
    # the correlation now sees the gateway and router hops
    assert hops["gateway"].sent > 0 and hops["router"].sent > 0


async def test_a_short_gateway_outage_past_raw_retention_is_found_through_the_classification(tmp_path):
    """A 20 s total loss that starts mid-minute reads about a third in the minute rollups, below the 50% threshold;
    the verdict the classifier made from the raw data (kind gateway_down) is the evidence instead."""
    repo, incident, _ = await aged(tmp_path, 20.0, gateway_outage=(17.0, 20.0), kind="gateway_down")
    inspection = await inspect_incident(repo, TARGETS, incident)
    gateway = next(h for h in inspection.hop_impact if h.target_id == "gateway")
    assert 0.0 < gateway.loss_pct < 50.0  # diluted by the rollups
    assert inspection.hop_resolution == "minute" and inspection.to_response()["hop_resolution"] == "minute"
    assert inspection.docsis_correlation.gateway_offline is True and inspection.docsis_correlation.status == "error"


async def test_a_healthy_old_incident_is_neither_offline_nor_judged(tmp_path):
    repo, incident, _ = await aged(tmp_path, 180.0, kind="isp_loss")
    inspection = await inspect_incident(repo, TARGETS, incident)
    assert inspection.hop_resolution == "minute"
    assert inspection.docsis_correlation.gateway_offline is False
    assert inspection.docsis_correlation.status == "unknown" and inspection.docsis_correlation.nearest_snapshot is None


async def test_a_gateway_outage_classified_otherwise_does_not_make_the_old_incident_offline(tmp_path):
    repo, incident, _ = await aged(tmp_path, 180.0, gateway_outage=(0.0, 180.0), kind="local_loss")
    assert (await inspect_incident(repo, TARGETS, incident)).docsis_correlation.gateway_offline is False


async def test_raw_resolution_hops_judge_the_gateway_by_their_own_loss(tmp_path):
    repo, incident = await seeded(tmp_path, 120.0, targets=["router", "gateway"])
    inspection = await inspect_incident(repo, TARGETS, incident)
    assert inspection.hop_resolution is None and "hop_resolution" not in inspection.to_response()
    assert inspection.docsis_correlation.gateway_offline is False  # hops answer, kind is irrelevant here


async def test_rollup_stats_have_the_shape_of_sample_stats_and_respect_both_bounds(tmp_path):
    repo = DatabaseRepository(db_path=str(tmp_path / "rs.db"))
    await repo.initialize()
    await repo.insert_samples(
        probes("a", 600.0, 719.0, lost=lambda t: t < 620.0)       # minutes 10 and 11
        + probes("a", 720.0, 779.0)                              # minute 12, outside the asked range
        + probes("b", 600.0, 659.0, lost=lambda t: True))        # a target that never answered
    stats = {row["target_id"]: row for row in await repo.get_rollup_stats(600.0, 719.0)}
    assert set(stats["a"]) == set((await repo.get_sample_stats(600.0, 719.0))[0])
    assert (stats["a"]["sent"], stats["a"]["received"]) == (120, 100)
    assert (stats["a"]["min_ms"], stats["a"]["max_ms"]) == (10.0, 12.0)
    assert stats["a"]["avg_ms"] == pytest.approx(11.0, abs=0.1)
    assert stats["a"]["jitter_ms"] == pytest.approx(0.75, abs=0.05)
    assert (stats["b"]["sent"], stats["b"]["received"], stats["b"]["avg_ms"], stats["b"]["min_ms"]) == (60, 0, None, None)
    # the end bound is respected, and a range with nothing in it is empty
    assert {r["target_id"]: r["sent"] for r in await repo.get_rollup_stats(600.0, 659.0)} == {"a": 60, "b": 60}
    assert await repo.get_rollup_stats(5000.0, 6000.0) == []


async def test_an_incident_with_raw_samples_does_not_touch_the_rollups(tmp_path, monkeypatch):
    repo, incident = await seeded(tmp_path, 120.0)

    async def boom(*_a, **_k):
        raise AssertionError("rollup stats read although raw samples exist")
    monkeypatch.setattr(repo, "get_rollup_stats", boom)
    inspection = await inspect_incident(repo, TARGETS, incident)
    assert inspection.sample_bucket_seconds is None and inspection.hop_impact
    repo, incident = await seeded(tmp_path / "long", 3600.0)  # a long window with raw samples: raw stats first
    monkeypatch.setattr(repo, "get_rollup_stats", boom)
    inspection = await inspect_incident(repo, TARGETS, incident)
    assert inspection.sample_bucket_seconds is not None and inspection.hop_impact[0].sent == 3600 + 10 + 2 + 1


async def test_an_incident_with_nothing_stored_at_all_keeps_the_plain_short_shape(tmp_path):
    repo = DatabaseRepository(db_path=str(tmp_path / "none.db"))
    await repo.initialize()
    start = float(int(time.time())) - 100 * 86400.0
    incident = Incident(start_time=start, end_time=start + 60.0, duration_seconds=60.0, cause_code="unknown_outage",
                        title="t", description="d", severity="warning", resolved=True)
    inspection = await inspect_incident(repo, TARGETS, incident)
    assert inspection.hop_impact == [] and inspection.samples == [] and inspection.sample_bucket_seconds is None
    assert "sample_bucket_seconds" not in inspection.to_response()


# --- which modem reading an incident shows --------------------------------------------------------------------

async def with_snapshots(tmp_path, *timestamps, status="healthy"):
    repo = DatabaseRepository(db_path=str(tmp_path / "snap.db"))
    await repo.initialize()
    for ts in timestamps:
        await repo.insert_line_sample(line_sample("modem", DocsisSignalSnapshot(timestamp=ts, downstream_snr_avg=38.0,
                                                                                status=status)))
    return repo


async def inspect_modem(repo, incident, **kw):
    """The inspection with the modem's stored readings (the device "modem")."""
    return await inspect_incident(repo, TARGETS, incident, line=DocsisHistory(repo, "modem"), **kw)


def an_incident(start, duration=60.0):
    return Incident(start_time=start, end_time=start + duration, duration_seconds=duration, cause_code="unknown_outage",
                    title="t", description="d", severity="warning", resolved=True)


async def test_an_old_incident_does_not_show_the_latest_modem_reading(tmp_path):
    repo = await with_snapshots(tmp_path, time.time() - 60.0)  # a fresh reading, 30 days after the incident
    inspection = await inspect_modem(repo, an_incident(time.time() - 30 * 86400.0))
    assert inspection.docsis_snapshots == [] and inspection.docsis_correlation.nearest_snapshot is None
    assert inspection.docsis_correlation.status == "unknown" and inspection.docsis_correlation.gateway_offline is False


async def test_a_modem_reading_within_one_poll_interval_of_the_window_is_shown(tmp_path):
    start = float(int(time.time())) - 86400.0
    window_end = start + 60.0 + 45.0
    repo = await with_snapshots(tmp_path, window_end + 600.0)  # 10 minutes after the window
    inspection = await inspect_modem(repo, an_incident(start))
    assert inspection.docsis_snapshots == []  # not "in" the window ...
    assert inspection.docsis_correlation.nearest_snapshot.timestamp == window_end + 600.0  # ... but it is the reading
    assert inspection.docsis_correlation.status == "unknown"  # outside the window: shown, not evidence
    # one second beyond the reach and it is not shown
    repo = await with_snapshots(tmp_path / "far", window_end + 901.0)
    assert (await inspect_modem(repo, an_incident(start))).docsis_correlation.nearest_snapshot is None
    # the reach is configurable (the modem poll interval)
    far = await inspect_modem(repo, an_incident(start), snapshot_reach_seconds=1800.0)
    assert far.docsis_correlation.nearest_snapshot.timestamp == window_end + 901.0


async def test_only_an_error_reading_inside_the_window_is_evidence_for_offline(tmp_path):
    start = float(int(time.time())) - 86400.0
    # 600 s after the window (clean hops: no probes at all here): shown, but not evidence
    repo = await with_snapshots(tmp_path, start + 60.0 + 45.0 + 600.0, status="error")
    corr = (await inspect_modem(repo, an_incident(start))).docsis_correlation
    assert corr.nearest_snapshot.status == "error"
    assert corr.gateway_offline is False and corr.status == "unknown"
    # inside the window it is
    repo = await with_snapshots(tmp_path / "in", start + 30.0, status="error")
    corr = (await inspect_modem(repo, an_incident(start))).docsis_correlation
    assert corr.gateway_offline is True and corr.status == "error"


async def test_an_open_incident_is_inspected_up_to_now(tmp_path):
    """An open incident has no end and no duration yet: the inspection covered its first 10 seconds, however long it had
    been going on."""
    repo = DatabaseRepository(db_path=str(tmp_path / "open.db"))
    await repo.initialize()
    now = float(int(time.time()))
    start = now - 600.0
    for tid in TARGET_IDS:
        await repo.insert_samples(probes(tid, start - 45.0, now, (lambda t: True) if tid == "isp" else (lambda t: False)))
    incident = Incident(start_time=start, cause_code="unknown_outage", title="t", description="d", severity="critical",
                        resolved=False)
    incident.id = await repo.create_incident(incident)
    inspection = await inspect_incident(repo, TARGETS, incident)
    await repo.close()
    assert inspection.event_end >= now and inspection.window_end >= now + 45.0
    isp = next(h for h in inspection.hop_impact if h.target_id == "isp")
    assert isp.sent >= 600 and isp.loss_pct == 100.0
