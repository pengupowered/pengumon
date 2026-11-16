"""Brief latency spikes (0.29, incidents/spikes.py): a latency-only episode waits classifier.latency_incident_after_seconds
before it becomes an incident (then from its start); one that ends sooner is stored as a latency spike, counted per day
and by stage, never announced. Same boundaries as an incident: the start rule (10 slow rounds) and the end rule
(3 healthy rounds)."""

import time
from datetime import timedelta, timezone
from types import SimpleNamespace

import pytest

from pengumon.engine.classifier import component as classifier_component
from pengumon.engine.classifier.metrics import round_metrics
from pengumon.engine.classifier.tracker import IncidentTracker
from pengumon.engine.classifier.verdict import NOMINAL, Verdict
from pengumon.engine.classifier_settings import ClassifierConfig
from pengumon.incidents.spikes import LatencySpike
from pengumon.models.probes import TargetStats
from pengumon.reports.spikes import summarize_spikes

SLOW = Verdict("upstream_latency", "degraded", "access_line_drop", "ISP Latency Spikes / Jitter", "slow", "warning")
LOCAL_SLOW = Verdict("local_latency", "degraded", "local_wifi_mesh_drop", "Router Latency Spike", "slow", "warning")
LOSS = Verdict("isp_loss", "degraded", "access_line_drop", "Line or Hub Packet Loss", "loss", "warning", ("isp",))
DNS_SLOW = Verdict("dns_slow", "degraded", "dns_resolver_failure", "Slow DNS Queries", "slow dns", "warning")
T0 = 1000.0


class Repo:
    """The repository calls the tracker makes, recorded."""

    def __init__(self, fail_spikes: int = 0):
        self.incidents, self.resolved, self.spikes = [], [], []
        self.fail_spikes = fail_spikes

    async def create_incident(self, incident):
        self.incidents.append(incident.model_copy())
        return len(self.incidents)

    async def resolve_incident(self, incident_id, end_time):
        self.resolved.append((incident_id, end_time))

    async def set_incident_load(self, incident_id, load_pct, expected=False):
        pass

    async def update_incident_classification(self, *args):
        pass

    async def create_latency_spike(self, spike):
        if self.fail_spikes:
            self.fail_spikes -= 1
            raise RuntimeError("database is locked")
        self.spikes.append(spike)
        return len(self.spikes)


def make(repo=None, **config):
    return IncidentTracker(ClassifierConfig(**config), repo)


async def feed(tracker, script, start=T0):
    """script: (verdict, rounds, with loss, peak ms) steps, one round a second from start + 1. Returns the outcomes."""
    outcomes, ts = [], start
    for verdict, rounds, loss, peak in script:
        for _ in range(rounds):
            ts += 1.0
            outcomes.append(await tracker.observe(verdict, is_loss_or_outage=loss, reading=None, ts=ts,
                                                  peak_ms=peak if verdict.is_fault else None))
    return outcomes


def started_at(outcomes):
    return [k for k, o in enumerate(outcomes) if o.started is not None]


# --- the wait ------------------------------------------------------------------------------------------------------

async def test_a_90_second_latency_episode_is_one_spike_and_no_incident():
    repo = Repo()
    tracker = make(repo)
    out = await feed(tracker, [(SLOW, 30, False, 120.0), (SLOW, 60, False, 180.0), (NOMINAL, 3, False, None)])
    assert repo.incidents == [] and started_at(out) == []
    assert [o.spike for o in out if o.spike] == repo.spikes
    (spike,) = repo.spikes
    assert (spike.start_time, spike.end_time) == (T0 + 1, T0 + 90)  # the first and last slow round
    assert (spike.kind, spike.stage, spike.expected, spike.peak_ms) == ("upstream_latency", "isp", False, 180.0)
    assert out[-1].spike is spike and tracker.pending is None and tracker.idle


async def test_a_150_second_episode_is_one_incident_from_the_episode_start():
    repo = Repo()
    tracker = make(repo)
    out = await feed(tracker, [(SLOW, 150, False, 150.0), (NOMINAL, 3, False, None)])
    assert started_at(out) == [120]  # the round 120 s after the first slow one, not the 10th
    (incident,) = repo.incidents
    assert incident.start_time == T0 + 1 and incident.kind == "upstream_latency"
    assert repo.resolved == [(1, T0 + 150)] and repo.spikes == []


async def test_one_healthy_round_inside_does_not_split_it_and_the_end_rule_ends_it():
    repo = Repo()
    out = await feed(make(repo), [(SLOW, 40, False, 100.0), (NOMINAL, 1, False, None), (SLOW, 20, False, 90.0),
                                  (NOMINAL, 2, False, None), (SLOW, 20, False, 95.0), (NOMINAL, 3, False, None)])
    assert repo.incidents == [] and started_at(out) == []
    assert [(s.start_time, s.end_time, s.peak_ms) for s in repo.spikes] == [(T0 + 1, T0 + 83, 100.0)]


async def test_a_run_shorter_than_the_start_rule_is_neither_spike_nor_incident():
    repo = Repo()
    await feed(make(repo), [(SLOW, 9, False, 300.0), (NOMINAL, 1, False, None), (SLOW, 9, False, 300.0),
                            (NOMINAL, 5, False, None)])
    assert repo.incidents == [] and repo.spikes == []


async def test_latency_then_loss_is_one_loss_incident_from_the_episode_start_and_no_spike():
    repo = Repo()
    tracker = make(repo)
    out = await feed(tracker, [(SLOW, 30, False, 150.0), (LOSS, 3, True, None), (NOMINAL, 3, False, None)])
    assert started_at(out) == [30]  # at the first round with loss
    (incident,) = repo.incidents
    assert (incident.start_time, incident.kind, incident.cause_code, incident.affected_targets) == (
        T0 + 1, "isp_loss", "access_line_drop", ["isp"])
    assert repo.resolved == [(1, T0 + 33)] and repo.spikes == []


async def test_loss_alone_still_opens_at_once():
    repo = Repo()
    out = await feed(make(repo), [(LOSS, 5, True, None)])
    assert started_at(out) == [2] and repo.incidents[0].start_time == T0 + 1


async def test_slow_dns_is_not_a_latency_spike_kind_and_opens_as_before():
    repo = Repo()
    out = await feed(make(repo), [(DNS_SLOW, 12, False, None)])
    assert started_at(out) == [9] and repo.spikes == []


async def test_setting_0_is_the_behavior_before_the_wait():
    repo = Repo()
    out = await feed(make(repo, latency_incident_after_seconds=0), [(SLOW, 90, False, 150.0), (NOMINAL, 3, False, None)])
    assert started_at(out) == [9] and repo.incidents[0].start_time == T0 + 1
    assert repo.resolved == [(1, T0 + 90)] and repo.spikes == []


async def test_a_changed_setting_applies_to_a_pending_episode_from_its_next_slow_round():
    repo = Repo()
    tracker = make(repo)
    await feed(tracker, [(SLOW, 30, False, 150.0)])
    assert tracker.pending is not None and repo.incidents == []
    tracker.config = ClassifierConfig(latency_incident_after_seconds=20)
    out = await feed(tracker, [(SLOW, 1, False, 150.0)], start=T0 + 30)
    assert started_at(out) == [0] and repo.incidents[0].start_time == T0 + 1


async def test_a_spike_that_cannot_be_stored_stays_pending_and_is_stored_at_the_next_healthy_round():
    repo = Repo(fail_spikes=1)
    tracker = make(repo)
    await feed(tracker, [(SLOW, 30, False, 150.0), (NOMINAL, 2, False, None)])
    with pytest.raises(RuntimeError):
        await feed(tracker, [(NOMINAL, 1, False, None)], start=T0 + 32)
    assert tracker.pending is not None
    out = await feed(tracker, [(NOMINAL, 1, False, None)], start=T0 + 33)
    assert out[0].spike is not None and [(s.start_time, s.end_time) for s in repo.spikes] == [(T0 + 1, T0 + 30)]


# --- when measuring stops, at shutdown, after a restart -----------------------------------------------------------

async def test_a_pending_episode_is_a_spike_when_measuring_stops():
    repo = Repo()
    tracker = make(repo)
    await feed(tracker, [(LOCAL_SLOW, 30, False, 40.0)])
    assert await tracker.stop_measuring(T0 + 31) is None  # no incident was open
    assert [(s.start_time, s.end_time, s.stage) for s in repo.spikes] == [(T0 + 1, T0 + 30, "router")]
    assert tracker.pending is None and tracker.idle


async def test_at_shutdown_the_pending_episode_is_stored_as_a_spike_and_a_restart_starts_afresh(tmp_path):
    from pengumon.db.repository import DatabaseRepository
    repo = DatabaseRepository(db_path=str(tmp_path / "s.db"))
    await repo.initialize()
    ctx = SimpleNamespace(notifier=None, line_load=None, settings=None,
                          state=SimpleNamespace(repository=repo, devices=SimpleNamespace(line_technology=lambda: None),
                                                classifier=None),
                          config=SimpleNamespace(classifier=ClassifierConfig(), targets=[]))
    component = await classifier_component.register(ctx)
    await feed(ctx.state.classifier.tracker, [(SLOW, 30, False, 150.0)])
    await component.stop()
    rows = await repo.latency_spikes_between(0, T0 + 100)
    assert [(r["start_time"], r["end_time"], r["kind"], r["stage"], r["peak_ms"]) for r in rows] == [
        (T0 + 1, T0 + 30, "upstream_latency", "isp", 150.0)]
    assert await repo.count_incidents() == 0
    # the next start's tracker holds nothing: a slow spell after it is a new episode
    again = await classifier_component.register(ctx)
    assert ctx.state.classifier.tracker.idle
    await again.stop()
    await repo.close()


# --- never announced -------------------------------------------------------------------------------------------------

async def test_the_notifier_hears_nothing_of_a_spike(monkeypatch):
    from test_classifier_characterization import BASE, C, ORDER, P, TARGETS, Texts, run_scenario
    spikes = []

    def tap(classifier):
        record = classifier.repository.create_latency_spike

        async def spy(spike):
            spikes.append(spike)
            return await record(spike)

        classifier.repository.create_latency_spike = spy

    run, _, _ = await run_scenario(monkeypatch, ClassifierConfig(), TARGETS, ORDER, BASE,
                                   [P("nominal", 12), P("public latency", 60, pub=C(150.0)), P("recover", 14)],
                                   Texts(), tap=tap)
    assert len(spikes) == 1 and spikes[0].kind == "upstream_latency"
    assert not [e for e in run["events"] if " cb." in e or " repo.create " in e or " trace" in e]


# --- the peak --------------------------------------------------------------------------------------------------------

def test_the_peak_is_the_slowest_latest_answered_probe_of_the_verdicts_stage():
    stats = {tid: TargetStats(target_id=tid, name=tid, category=cat, address="192.0.2.1", enabled=True,
                              recent_latencies=window)
             for tid, cat, window in (("lan", "local", [1.0, 30.0]), ("gw", "gateway", [2.0, 3.0]),
                                      ("hop", "isp", [9.0, 95.0]), ("pub", "public", [500.0, 88.0]),
                                      ("pub2", "public", [14.0, None]))}
    m = round_metrics(stats, [])
    assert m.peak_latency("upstream_latency") == 95.0  # the ISP hops and the public targets, this round's probe
    assert m.peak_latency("local_latency") == 30.0 and m.peak_latency("gateway_latency") == 3.0
    assert m.peak_latency("dns_slow") is None and m.peak_latency("legacy_latency") is None


# --- the counts --------------------------------------------------------------------------------------------------------

def _row(ts, stage="isp", expected=False):
    return {"id": 1, "start_time": ts, "end_time": ts + 30, "kind": "upstream_latency", "stage": stage,
            "expected": expected, "peak_ms": 120.0}


def test_the_summary_counts_by_local_day_with_partial_first_and_last_days_and_by_stage():
    tz = timezone(timedelta(hours=-5))
    end = 1_793_000_000.0  # 2026-10-26 02:33 at UTC-5
    start = end - 2 * 86400
    rows = [_row(start + 60), _row(start + 600, "router"), _row(end - 86400), _row(end - 60, "router"),
            _row(end - 30), _row(end - 20, "load", expected=True)]
    out = summarize_spikes(rows, start, end, tz, 120.0)
    assert (out["count"], out["expected"], out["incident_after_seconds"]) == (5, 1, 120.0)
    assert out["by_stage"] == [{"stage": "isp", "count": 3}, {"stage": "router", "count": 2}]
    assert [d["day"] for d in out["days"]] == ["2026-10-24", "2026-10-25", "2026-10-26"]
    assert [d["partial"] for d in out["days"]] == [True, False, True]
    assert [d["count"] for d in out["days"]] == [2, 1, 2] and sum(d["hours"] for d in out["days"]) == 48.0
    assert out["days"][0]["start"] == start and out["days"][-1]["end"] == end
