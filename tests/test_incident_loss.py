"""The loss span of an incident (pengumon.incidents.loss): from the first lost probe of its affected targets to the last,
plus one probe interval, as opposed to the detection times start_time / end_time. Computed when an incident closes,
backfilled at start for older ones whose raw samples are kept, stored and listed by /api/incidents."""

import json
import time

import aiosqlite
import pytest

from pengumon.db.repository import DatabaseRepository
from pengumon.incidents.loss import LossSpan, TargetLoss, loss_span, probe_window
from pengumon.incidents.model import Incident
from pengumon.models.probes import ProbeSample


def row(lost, sent_from=980.0, sent_to=1030.0, step=1.0, tid="gw"):
    """The aggregate the repository hands over for one target probed every `step` s from sent_from to sent_to, with the
    probes at the times in `lost` unanswered."""
    n = int(round((sent_to - sent_from) / step)) + 1
    return TargetLoss(tid, n, n - len(lost), min(lost) if lost else None, max(lost) if lost else None, sent_from, sent_to)


# ---- the rule ----

def test_seven_lost_probes_are_seven_seconds_whatever_the_incident_recorded():
    # detected at 1000, closed at 1012 (12 s); the probes were lost from 997 to 1003: 7 probes, 7 s
    assert loss_span([row([997.0 + k for k in range(7)])]) == LossSpan(start=997.0, seconds=7.0)


def test_loss_right_at_the_start_of_the_window_counts_from_its_first_probe():
    assert loss_span([row([980.0, 981.0, 982.0])]) == LossSpan(start=980.0, seconds=3.0)


def test_intermittent_loss_spans_first_to_last_lost_probe():
    assert loss_span([row([1000.0, 1004.0, 1009.0])]) == LossSpan(start=1000.0, seconds=10.0)


def test_no_lost_probe_is_zero_seconds_not_unknown():
    assert loss_span([row([])]) == LossSpan(start=None, seconds=0.0)


def test_no_stored_probe_is_unknown():
    assert loss_span([]) is None


def test_a_target_that_answered_nothing_in_the_window_is_not_the_incidents_loss():
    silent = TargetLoss("quiet_hop", 51, 0, 980.0, 1030.0, 980.0, 1030.0)  # a hop that ignores ping
    assert loss_span([silent]) == LossSpan(start=None, seconds=0.0)
    assert loss_span([silent, row([1001.0, 1002.0])]) == LossSpan(start=1001.0, seconds=2.0)


def test_several_targets_span_the_earliest_to_the_latest_lost_probe():
    a = row([999.0, 1000.0], tid="a")
    b = row([1000.0, 1005.0], tid="b")
    assert loss_span([a, b]) == LossSpan(start=999.0, seconds=7.0)


def test_the_interval_is_the_targets_own_probe_spacing():
    slow = row([1000.0, 1002.6], sent_from=980.0, sent_to=1032.0, step=1.3)  # timeouts stretch the rounds
    assert loss_span([slow]) == LossSpan(start=1000.0, seconds=3.9)
    one = TargetLoss("gw", 1, 0, 1000.0, 1000.0, 1000.0, 1000.0)
    assert one.interval == 1.0


def test_the_probe_window_is_the_inspectors_episode():
    from pengumon.analysis import incident as analysis
    lo, hi = probe_window(1000.0, 1012.0)
    assert (lo, hi) == (1000.0 - analysis.CALC_LEAD_SECONDS, 1012.0 + analysis.CALC_TAIL_SECONDS)


# ---- stored with the incident ----

def probes(base, tid, lost=(), start=-20, end=30):
    """One probe a second for target tid from base+start to base+end, those at base+k for k in `lost` unanswered."""
    return [ProbeSample(target_id=tid, timestamp=base + k, success=k not in lost, latency_ms=None if k in lost else 9.0,
                        packet_loss_pct=100.0 if k in lost else 0.0) for k in range(start, end + 1)]


def incident(base, seconds, affected):
    return Incident(start_time=base, cause_code="unknown_outage", title="Gateway Packet Loss", description="d",
                    severity="warning", affected_targets=affected)


@pytest.mark.asyncio
async def test_resolving_an_incident_stores_its_loss_span_and_the_list_shows_it(tmp_path):
    repo = DatabaseRepository(db_path=str(tmp_path / "loss.db"))
    await repo.initialize()
    base = float(int(time.time())) - 600
    lost = set(range(-3, 4))  # 997..1003 in the example: 7 probes
    # another target loses probes far outside the window, and one not in affected_targets loses some inside it
    await repo.insert_samples(probes(base, "gw", lost) + probes(base, "router") + probes(base, "other", {5, 6, 7, 8, 9, 10})
                              + probes(base, "gw", set(range(-200, -150)), start=-200, end=-150))
    iid = await repo.create_incident(incident(base, 12.0, ["gw", "router"]))
    await repo.resolve_incident(iid, base + 12.0)
    (inc,) = await repo.get_recent_incidents()
    assert (inc.duration_seconds, inc.loss_start, inc.loss_seconds) == (12.0, base - 3, 7.0)
    assert (await repo.get_incident_by_id(iid)).loss_seconds == 7.0


@pytest.mark.asyncio
async def test_an_incident_without_probe_targets_or_without_samples_keeps_only_its_duration(tmp_path):
    repo = DatabaseRepository(db_path=str(tmp_path / "loss.db"))
    await repo.initialize()
    base = float(int(time.time())) - 600
    await repo.insert_samples(probes(base, "gw"))
    service = await repo.create_incident(incident(base, 30.0, ["svc_down"]))  # a host service, not a probe target
    later = await repo.create_incident(incident(base + 3600, 30.0, ["gw"]))  # no sample stored for its window
    await repo.resolve_incident(service, base + 30.0)
    await repo.resolve_incident(later, base + 3630.0)
    for iid in (service, later):
        got = await repo.get_incident_by_id(iid)
        assert (got.duration_seconds, got.loss_start, got.loss_seconds) == (30.0, None, None)


@pytest.mark.asyncio
async def test_an_incident_raised_on_latency_lost_nothing(tmp_path):
    repo = DatabaseRepository(db_path=str(tmp_path / "loss.db"))
    await repo.initialize()
    base = float(int(time.time())) - 600
    await repo.insert_samples(probes(base, "gw"))
    iid = await repo.create_incident(incident(base, 20.0, ["gw"]))
    await repo.resolve_incident(iid, base + 20.0)
    got = await repo.get_incident_by_id(iid)
    assert (got.loss_start, got.loss_seconds) == (None, 0.0)


@pytest.mark.asyncio
async def test_start_fills_the_loss_span_of_older_incidents_whose_samples_are_kept(tmp_path):
    path = str(tmp_path / "loss.db")
    repo = DatabaseRepository(db_path=path)
    await repo.initialize()
    base = float(int(time.time())) - 600
    await repo.insert_samples(probes(base, "gw", {0, 1, 2}))
    await repo.flush_samples()
    iid = await repo.create_incident(incident(base, 10.0, ["gw"]))
    gone = await repo.create_incident(incident(base - 86400 * 30, 10.0, ["gw"]))  # long before the oldest sample
    open_id = await repo.create_incident(incident(base + 100, 10.0, ["gw"]))
    await repo.close()
    # as an older build left them: resolved without a loss span (and one still open, closed at the next start)
    async with aiosqlite.connect(path) as db:
        await db.execute("UPDATE incidents SET end_time = start_time + 10, duration_seconds = 10, resolved = 1 WHERE id IN (?, ?)", (iid, gone))
        await db.commit()
    repo = DatabaseRepository(db_path=path)
    await repo.initialize()
    assert (await repo.get_incident_by_id(iid)).loss_seconds == 3.0
    assert (await repo.get_incident_by_id(gone)).loss_seconds is None
    assert (await repo.get_incident_by_id(open_id)).resolved is True  # closed at start; its window has no samples


@pytest.mark.asyncio
async def test_the_api_lists_the_loss_span(tmp_path, app_state):
    from httpx import ASGITransport, AsyncClient
    from pengumon.main import app

    repo = DatabaseRepository(db_path=str(tmp_path / "loss.db"))
    await repo.initialize()
    base = float(int(time.time())) - 600
    await repo.insert_samples(probes(base, "gw", set(range(-3, 4))))
    iid = await repo.create_incident(incident(base, 12.0, ["gw"]))
    await repo.resolve_incident(iid, base + 12.0)
    app_state.repository = repo
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        (listed,) = json.loads((await ac.get("/api/incidents")).text)
    assert (listed["duration_seconds"], listed["loss_start"], listed["loss_seconds"]) == (12.0, base - 3, 7.0)
