"""Load-aware classification: line capacity, busy status, incident load annotation and the backfill."""

import sqlite3
import time

import pytest
from httpx import AsyncClient, ASGITransport

from pengumon.engine.classifier_settings import ClassifierConfig
from pengumon.engine.load_settings import LineConfig
from pengumon.db.migrations import legacy_incident_kind
from pengumon.db.repository import DatabaseRepository
from pengumon.engine.classifier import OutageClassifier
from pengumon.engine.load import (
    BACKFILL_META_KEY,
    CAPACITY_MIN_SAMPLES,
    LineLoad,
    backfill_incident_load,
    is_load_explainable,
)
from pengumon.main import app
from pengumon.incidents.model import Incident
from pengumon.models.probes import ProbeSample, TargetStats

LINE = LineConfig(down_mbps=1000, up_mbps=50)


class Rates:
    """Stand-in for RouterMonitor.recent_rates: samples relative to the current time."""

    def __init__(self, *samples):
        self.samples = samples  # (age_seconds, rx_mbps, tx_mbps)

    def __call__(self):
        now = time.time()
        return [(now - age, rx, tx) for age, rx, tx in sorted(self.samples, reverse=True)]


def make_classifier(rates=None, repository=None, line=LINE, **cfg):
    config = ClassifierConfig(consecutive_failures_for_incident=3, **cfg)
    load = LineLoad(line, rates_source=rates) if rates is not None else None
    return OutageClassifier(config=config, repository=repository, load=load)


def stats_with(local=None, gateway=None, isp=None, public=None):
    def st(tid, cat, addr, lats):
        return TargetStats(target_id=tid, name=tid.title(), category=cat, address=addr, enabled=True,
                           recent_latencies=lats)
    return {
        "local": st("local", "local", "192.168.1.1", local or [1.0] * 10),
        "gateway": st("gateway", "gateway", "192.168.100.1", gateway or [2.0] * 10),
        "isp": st("isp", "isp", "198.51.100.1", isp or [9.0] * 10),
        "public": st("public", "public", "1.1.1.1", public or [14.0] * 10),
    }


def round_samples(stats):
    return [
        ProbeSample(target_id=t, success=s.recent_latencies[-1] is not None, latency_ms=s.recent_latencies[-1])
        for t, s in stats.items()
    ]


async def run_rounds(classifier, stats, n):
    diagnosis = None
    for _ in range(n):
        diagnosis = await classifier.evaluate(round_samples(stats), stats)
    return diagnosis


BLOATED = dict(public=[250.0] * 10)  # latency only
ISP_HOP_LOSS = dict(isp=[None] * 3 + [12.0] * 7, public=[90.0] * 10)  # 30% at the ISP hops, public answers
PUBLIC_LOSS = dict(public=[None] * 3 + [15.0] * 7)


# --- classification --------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_latency_under_heavy_load_is_busy_and_never_an_incident():
    classifier = make_classifier(Rates((5, 954.0, 3.0), (15, 900.0, 2.0)))
    diagnosis = await run_rounds(classifier, stats_with(**BLOATED), 15)

    assert diagnosis.status == "busy"
    assert diagnosis.cause_code == "load_induced_latency"
    assert diagnosis.load_pct == 95.4
    assert "download 954 Mbps, 95% of the configured 1000 Mbps capacity" in diagnosis.description
    assert "ISP Latency Spikes" in diagnosis.description  # names what was seen
    assert diagnosis.active_incident is None
    assert classifier.latency_failure_counter == 0


@pytest.mark.asyncio
async def test_upload_load_counts_too():
    classifier = make_classifier(Rates((5, 20.0, 45.0)))
    diagnosis = await run_rounds(classifier, stats_with(**BLOATED), 12)
    assert diagnosis.status == "busy"
    assert "upload 45 Mbps, 90% of the configured 50 Mbps capacity" in diagnosis.description


@pytest.mark.asyncio
@pytest.mark.parametrize("isp", [[None] * 3 + [12.0] * 7, [None] * 6 + [12.0] * 4])  # degraded and "outage"
async def test_loss_only_at_isp_hops_under_heavy_load_is_busy(isp):
    classifier = make_classifier(Rates((3, 980.0, 4.0)))
    diagnosis = await run_rounds(classifier, stats_with(isp=isp, public=[90.0] * 10), 10)

    assert diagnosis.status == "busy"
    assert diagnosis.cause_code == "load_induced_latency"
    assert "intermediate ISP routers" in diagnosis.description
    assert classifier.active_incident is None
    assert classifier.loss_failure_counter == 0


@pytest.mark.asyncio
async def test_real_loss_under_heavy_load_is_an_incident_with_its_load(tmp_path, monkeypatch):
    async def no_trace(self, incident_id):
        pass
    monkeypatch.setattr(OutageClassifier, "_trigger_path_trace", no_trace)
    repo = DatabaseRepository(db_path=str(tmp_path / "p.db"), sample_flush_seconds=0)
    await repo.initialize()
    classifier = make_classifier(Rates((4, 954.0, 3.0)), repository=repo)
    diagnosis = await run_rounds(classifier, stats_with(**PUBLIC_LOSS), 3)

    assert diagnosis.status == "degraded"
    assert diagnosis.cause_code == "access_line_drop"
    assert diagnosis.load_pct == 95.4
    assert diagnosis.description.endswith(
        "During heavy load (95%): download 954 Mbps, 95% of the configured 1000 Mbps capacity."
    )
    incident = diagnosis.active_incident
    assert incident is not None and incident.load_pct == 95.4 and not incident.expected
    stored = await repo.get_incident_by_id(incident.id)
    assert stored.load_pct == 95.4 and stored.expected is False
    assert "During heavy load (95%)" in stored.description


@pytest.mark.asyncio
async def test_isp_hop_loss_with_public_loss_stays_a_fault_under_load():
    classifier = make_classifier(Rates((4, 954.0, 3.0)))
    diagnosis = await run_rounds(
        classifier, stats_with(isp=[None] * 3 + [12.0] * 7, public=[None] * 3 + [15.0] * 7), 3
    )
    assert diagnosis.status == "degraded"
    assert classifier.active_incident is not None


@pytest.mark.asyncio
async def test_moderate_load_is_reported_but_changes_nothing():
    classifier = make_classifier(Rates((4, 300.0, 3.0)))
    diagnosis = await run_rounds(classifier, stats_with(**BLOATED), 10)
    assert diagnosis.status == "degraded"
    assert diagnosis.load_pct == 30.0
    assert "heavy load" not in diagnosis.description
    assert diagnosis.active_incident is not None
    assert diagnosis.active_incident.load_pct == 30.0


@pytest.mark.asyncio
@pytest.mark.parametrize("rates", [
    None,  # no load estimator at all
    Rates(),  # router collector has no samples (unconfigured / failing)
    Rates((61, 990.0, 3.0), (75, 990.0, 3.0)),  # newest sample older than 60 s
], ids=["no-estimator", "no-router-data", "stale"])
async def test_without_fresh_router_data_classification_is_unchanged(rates):
    classifier = make_classifier(rates)
    diagnosis = await run_rounds(classifier, stats_with(**BLOATED), 10)
    assert diagnosis.status == "degraded"
    assert diagnosis.load_pct is None
    assert diagnosis.active_incident is not None
    assert "Latency" in diagnosis.active_incident.title
    assert diagnosis.active_incident.load_pct is None


@pytest.mark.asyncio
async def test_a_missed_router_poll_keeps_the_last_sample_up_to_60_s():
    load = LineLoad(LINE, rates_source=Rates((45, 954.0, 3.0), (300, 10.0, 1.0)))
    assert load.load_at() == 95.4  # nothing in the 20 s window: the newest sample still counts
    load = LineLoad(LINE, rates_source=Rates((15, 100.0, 1.0), (25, 954.0, 3.0)))
    assert load.load_at() == 10.0  # samples in the window win over older ones


@pytest.mark.asyncio
async def test_active_incident_recovers_normally_when_only_busy_latency_remains():
    rates = Rates()
    classifier = make_classifier(rates)
    await run_rounds(classifier, stats_with(**PUBLIC_LOSS), 3)
    assert classifier.active_incident is not None

    rates.samples = ((3, 960.0, 2.0),)
    bloated = stats_with(**BLOATED)
    diagnosis = await run_rounds(classifier, bloated, 1)
    assert diagnosis.status == "busy"
    assert diagnosis.active_incident is not None  # not resolved on the first busy round
    assert classifier.active_incident.load_pct == 96.0

    await run_rounds(classifier, bloated, 1)
    assert classifier.active_incident is not None
    diagnosis = await run_rounds(classifier, bloated, 1)
    assert classifier.active_incident is None  # consecutive_failures_for_incident = 3 recovery rounds
    assert diagnosis.status == "busy"


# --- capacity --------------------------------------------------------------------------------------

def _insert_rates(path, rows, device_id="router"):
    con = sqlite3.connect(path)
    con.executemany(
        "INSERT INTO device_samples (device_id, ts, wan_rx_mbps, wan_tx_mbps, status) VALUES (?, ?, ?, ?, 'healthy')",
        [(device_id, *row) for row in rows],
    )
    con.commit()
    con.close()


@pytest.mark.asyncio
async def test_capacity_is_learned_from_router_history_with_floor_and_overrides(tmp_path):
    path = str(tmp_path / "p.db")
    repo = DatabaseRepository(db_path=path)
    await repo.initialize()
    now = time.time()
    n = CAPACITY_MIN_SAMPLES + 340  # 2500
    _insert_rates(path, [(now - 10 * i, i * 0.4, 2.0) for i in range(n)])
    _insert_rates(path, [(now - 8 * 86400, 5000.0, 5000.0)])  # older than 7 days: ignored

    clock = [now]
    learned = LineLoad(LineConfig(), repository=repo, clock=lambda: clock[0], device_id="router")
    await learned.refresh_capacity()
    assert learned.down_mbps == 998.8  # 99.9th percentile (nearest rank 2498 of 2500)
    assert learned.up_mbps == 10.0  # floor

    _insert_rates(path, [(now + 1 + i, 2000.0, 2000.0) for i in range(100)])  # (one poll per device and time)
    clock[0] = now + 60
    await learned.refresh_capacity()
    assert learned.down_mbps == 998.8  # refreshed at most hourly
    clock[0] = now + 3700
    await learned.refresh_capacity()
    assert learned.down_mbps == 2000.0

    override = LineLoad(LineConfig(down_mbps=500), repository=repo, clock=lambda: now, device_id="router")
    await override.refresh_capacity()
    assert override.down_mbps == 500 and override.up_mbps == 2000.0  # up still learned
    assert override.reading_from([(now, 400.0, 1.0)]).down_source == "configured"


@pytest.mark.asyncio
async def test_capacity_is_not_learned_from_too_little_history(tmp_path):
    path = str(tmp_path / "p.db")
    repo = DatabaseRepository(db_path=path)
    await repo.initialize()
    _insert_rates(path, [(time.time() - 10 * i, 3.0, 1.0) for i in range(100)])
    load = LineLoad(LineConfig(), rates_source=Rates((2, 900.0, 1.0)), repository=repo, device_id="router")
    await load.refresh_capacity()
    assert load.down_mbps is None
    assert load.load_at() is None  # unknown capacity: classify as before


# --- backfill --------------------------------------------------------------------------------------

def test_load_explainable_kinds():
    for kind in ("upstream_latency", "gateway_latency", "local_latency", "isp_loss", "busy_latency",
                 "legacy_latency"):
        assert is_load_explainable(kind), kind
    for kind in ("isp_outage", "backbone_loss", "gateway_loss", "gateway_down", "local_loss", "local_down",
                 "dns_outage", "dns_loss", "dns_slow", "busy_isp_hops", "hop_deprioritised", "nominal",
                 "legacy_other", None):
        assert not is_load_explainable(kind), kind


@pytest.mark.asyncio
async def test_backfill_annotates_old_incidents_once(tmp_path):
    path = str(tmp_path / "p.db")
    repo = DatabaseRepository(db_path=path)
    await repo.initialize()
    base = time.time() - 86400

    async def incident(offset, title, cause="xfinity_last_mile_coax_drop", duration=30.0):
        return await repo.create_incident(Incident(
            start_time=base + offset, end_time=base + offset + duration, duration_seconds=duration,
            cause_code=cause, title=title, description="d", severity="warning", resolved=True,
            kind=legacy_incident_kind(title, cause),  # what schema v6 labels an old incident
        ))

    latency_busy = await incident(0, "ISP Latency Spikes / Jitter")
    isp_busy = await incident(1000, "ISP Line / Hop 3: ISP Regional Hub Packet Loss")
    gateway_busy = await incident(2000, "Hop 2: ISP Modem / Gateway Packet Loss", "double_nat_congestion")
    latency_quiet = await incident(3000, "ISP Latency Spikes / Jitter")
    latency_no_data = await incident(4000, "ISP Latency Spikes / Jitter")
    _insert_rates(path, [
        (base - 15, 950.0, 3.0),  # just before the start: inside [start - 20 s, end + 5 s]
        (base + 1010, 30.0, 47.0),  # upload-bound
        (base + 2035, 990.0, 1.0),  # end + 5 s
        (base + 3010, 120.0, 1.0),
        (base + 3060, 990.0, 1.0),  # after end + 5 s: not counted
        (base + 3975, 990.0, 1.0),  # before start - 20 s: not counted
    ])

    load = LineLoad(LINE, device_id="router")
    assert await backfill_incident_load(repo, load, 60.0) == 4

    got = {i.id: i for i in await repo.get_recent_incidents(limit=10)}
    assert (got[latency_busy].load_pct, got[latency_busy].expected) == (95.0, True)
    assert (got[isp_busy].load_pct, got[isp_busy].expected) == (94.0, True)
    assert (got[gateway_busy].load_pct, got[gateway_busy].expected) == (99.0, False)  # real loss stays
    assert (got[latency_quiet].load_pct, got[latency_quiet].expected) == (12.0, False)
    assert (got[latency_no_data].load_pct, got[latency_no_data].expected) == (None, False)
    assert await repo.get_meta(BACKFILL_META_KEY)

    # Idempotent: a second start does nothing, even with new router data around an incident.
    _insert_rates(path, [(base + 4010, 990.0, 1.0)])
    assert await backfill_incident_load(repo, load, 60.0) is None
    assert (await repo.get_incident_by_id(latency_no_data)).load_pct is None

    visible = await repo.get_recent_incidents(limit=10, include_expected=False)
    assert {i.id for i in visible} == {gateway_busy, latency_quiet, latency_no_data}


@pytest.mark.asyncio
async def test_capacity_and_backfill_read_only_their_devices_rates(tmp_path, caplog):
    path = str(tmp_path / "p.db")
    repo = DatabaseRepository(db_path=path)
    await repo.initialize()
    now = time.time()
    _insert_rates(path, [(now - 10 * i, 900.0, 40.0) for i in range(CAPACITY_MIN_SAMPLES)], device_id="other")
    _insert_rates(path, [(now - 10 * i, 300.0, 20.0) for i in range(CAPACITY_MIN_SAMPLES)])
    load = LineLoad(LineConfig(), repository=repo, clock=lambda: now, device_id="router")
    await load.refresh_capacity()
    assert (load.down_mbps, load.up_mbps) == (300.0, 20.0)

    without = LineLoad(LineConfig(), repository=repo, clock=lambda: now)  # no device reports WAN counters
    with caplog.at_level("INFO", logger="pengumon.load"):
        await without.refresh_capacity(force=True)
        await without.refresh_capacity(force=True)
    assert without.down_mbps is None
    assert [r.getMessage() for r in caplog.records].count(
        "Line capacity: no device reports WAN counters, so it cannot be learned; load-aware classification needs "
        "the line speeds (Settings, Your line)") == 1  # said once, not every hour
    assert await backfill_incident_load(repo, LineLoad(LINE), 60.0) is None  # postponed, not recorded as done
    assert await repo.get_meta(BACKFILL_META_KEY) is None


@pytest.mark.asyncio
async def test_backfill_waits_for_a_known_capacity(tmp_path):
    repo = DatabaseRepository(db_path=str(tmp_path / "p.db"))
    await repo.initialize()
    assert await backfill_incident_load(repo, LineLoad(LineConfig()), 60.0) is None
    assert await repo.get_meta(BACKFILL_META_KEY) is None  # retried on the next start


@pytest.mark.asyncio
async def test_backfill_runs_once_even_when_the_capacity_or_the_busy_level_changes(tmp_path):
    """Outage rules apply from the moment they are set: a later capacity or busy level never re-annotates the
    incidents recorded under the old one."""
    import json
    from pengumon.engine.load import PING_BACKFILL_META_KEY

    path = str(tmp_path / "p.db")
    repo = DatabaseRepository(db_path=path)
    await repo.initialize()
    base = time.time() - 86400

    async def incident(offset, title):
        return await repo.create_incident(Incident(
            start_time=base + offset, end_time=base + offset + 30.0, duration_seconds=30.0,
            cause_code="xfinity_last_mile_coax_drop", title=title, description="d", severity="warning",
            resolved=True, kind=legacy_incident_kind(title, "xfinity_last_mile_coax_drop"),
        ))

    latency = await incident(0, "ISP Latency Spikes / Jitter")
    ping_marked = await incident(1000, "ISP Line / Hop 3: ISP Regional Hub Packet Loss")
    _insert_rates(path, [(base + 10, 650.0, 1.0), (base + 1010, 10.0, 1.0)])
    await repo.set_meta(PING_BACKFILL_META_KEY, json.dumps({"incident_ids": [ping_marked]}))
    await repo.set_incident_expected(ping_marked)

    assert await backfill_incident_load(repo, LineLoad(LINE, device_id="router"), 60.0) == 2  # 650 of 1000 Mbps = 65%
    assert (await repo.get_incident_by_id(latency)).expected is True
    assert await backfill_incident_load(repo, LineLoad(LINE, device_id="router"), 60.0) is None  # done

    bigger = LineConfig(down_mbps=1100, up_mbps=40)
    assert await backfill_incident_load(repo, LineLoad(bigger, device_id="router"), 60.0) is None  # 59% now: kept
    assert await backfill_incident_load(repo, LineLoad(LINE, device_id="router"), 70.0) is None  # busy 70: kept
    got = await repo.get_incident_by_id(latency)
    assert (round(got.load_pct), got.expected) == (65, True)
    assert (await repo.get_incident_by_id(ping_marked)).expected is True
    assert json.loads(await repo.get_meta(BACKFILL_META_KEY))["busy_load_pct"] == 60.0  # what the one run used


@pytest.mark.asyncio
async def test_backfill_decides_by_kind_not_by_title(tmp_path):
    path = str(tmp_path / "p.db")
    repo = DatabaseRepository(db_path=path)
    await repo.initialize()
    base = time.time() - 86400

    async def incident(offset, title, kind):
        return await repo.create_incident(Incident(
            start_time=base + offset, end_time=base + offset + 30.0, duration_seconds=30.0,
            cause_code="local_wifi_mesh_drop", title=title, description="d", severity="warning",
            resolved=True, kind=kind,
        ))

    # The title says latency, the stored kind says loss: loss is not something heavy load explains.
    loss = await incident(0, "Hop 1: Local Router (LAN) Latency Spike", "local_loss")
    latency = await incident(1000, "Hop 1: Local Router (LAN) Latency Spike", "local_latency")
    unlabelled = await incident(2000, "Hop 1: Local Router (LAN) Latency Spike", None)
    _insert_rates(path, [(base + 10, 950.0, 1.0), (base + 1010, 950.0, 1.0), (base + 2010, 950.0, 1.0)])

    assert await backfill_incident_load(repo, LineLoad(LINE, device_id="router"), 60.0) == 3
    got = {i.id: i for i in await repo.get_recent_incidents(limit=10)}
    assert (got[loss].load_pct, got[loss].expected) == (95.0, False)
    assert (got[latency].load_pct, got[latency].expected) == (95.0, True)
    assert got[unlabelled].expected is False


# --- API -------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_api_exposes_load_and_can_hide_expected_incidents(tmp_path, app_state):
    repo = DatabaseRepository(db_path=str(tmp_path / "p.db"))
    await repo.initialize()
    real = await repo.create_incident(Incident(
        start_time=100.0, cause_code="double_nat_congestion", title="Gateway Packet Loss", description="d",
        severity="warning", load_pct=72.5,
    ))
    bloat = await repo.create_incident(Incident(
        start_time=200.0, cause_code="xfinity_last_mile_coax_drop", title="ISP Latency Spikes / Jitter",
        description="d", severity="warning",
    ))
    await repo.set_incident_load(bloat, 95.0, expected=True)
    classifier = make_classifier(Rates((4, 954.0, 3.0)))
    await run_rounds(classifier, stats_with(**BLOATED), 1)
    app_state.repository = repo
    app_state.classifier = classifier

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        everything = (await ac.get("/api/incidents")).json()
        assert [(i["id"], i["load_pct"], i["expected"]) for i in everything] == [
            (bloat, 95.0, True), (real, 72.5, False)
        ]
        hidden = (await ac.get("/api/incidents", params={"include_expected": "false"})).json()
        assert [i["id"] for i in hidden] == [real]

        status = (await ac.get("/api/status")).json()
        assert status["status"] == "busy"
        assert status["load_pct"] == 95.4


# --- impossible rates (a stored incident once showed 1337 %: 535 Mbps of upload on a 40 Mbps line) ------------------------

def test_an_impossible_rate_is_not_load():
    load = LineLoad(LineConfig(down_mbps=1100, up_mbps=40))
    now = time.time()
    glitch = (now, 10.0, 535.0)
    assert load.reading_from([glitch]) is None  # nothing but a bad sample: the load is unknown
    reading = load.reading_from([(now - 10, 220.0, 8.0), glitch])
    assert reading.pct == 20.0 and reading.tx_mbps == 8.0  # the good sample alone decides
    assert load.reading_from([(now, 10.0, 59.9)]).pct == 149.8  # a real burst over the capacity stays
    assert load.reading_from([(now, 10.0, 60.1)]) is None  # just beyond the limit


async def test_the_live_load_and_the_classifier_ignore_a_counter_glitch():
    classifier = make_classifier(Rates((1, 5.0, 535.0)), line=LineConfig(down_mbps=1100, up_mbps=40))
    assert classifier.load.current() is None and classifier.load.load_at() is None


async def test_an_old_incident_with_an_impossible_load_is_flagged_not_shown_as_a_figure(tmp_path):
    repo = DatabaseRepository(db_path=str(tmp_path / "s.db"))
    await repo.initialize()
    ids = []
    for load in (149.0, 1336.6, None):
        i = await repo.create_incident(Incident(start_time=1.0, cause_code="access_line_drop", title="t", description="d",
                                                severity="warning", affected_targets=[], kind="isp_outage"))
        await repo.set_incident_load(i, load)
        ids.append(i)
    got = {i.id: i for i in await repo.get_recent_incidents(limit=10)}
    assert [got[i].load_suspect for i in ids] == [False, True, False]
    assert got[ids[1]].model_dump()["load_suspect"] is True
    await repo.close()
