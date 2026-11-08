"""A speed test fills the line on purpose: its delay and loss must not open incidents ("busy is not broken", like the busy
line). The rule (rules.apply_speedtest), the core's `testing` flag, a simulated 30 s test through the incident tracker, the
live classifier's switch, the mask the component wires, and the outage-rules replay's use of the stored tests."""

import sqlite3

import pytest

from pengumon.components import Context
from pengumon.config import AppConfig
from pengumon.engine.classifier import ClassifierCore, OutageClassifier
from pengumon.engine.classifier.metrics import round_metrics
from pengumon.engine.classifier.rules import apply_speedtest, diagnose
from pengumon.engine.classifier_settings import ClassifierConfig
from pengumon.engine.load import LoadReading
from pengumon.engine.replay import run_replay
from pengumon.incidents.taxonomy import KINDS, LOAD_EXPLAINABLE_KINDS, incident_cause
from pengumon.models.probes import ProbeSample, TargetStats
from pengumon.speedtest import component as speedtest_component
from pengumon.state import AppState

from test_classifier_characterization import T0
from test_classifier_rules import CASES, CONFIG, LONG_AGO, TS, rm
from test_replay import make_db, spec_for


def stats(*specs):
    return {tid: TargetStats(target_id=tid, name=tid, category=cat, address="192.0.2.1", enabled=True,
                             recent_latencies=list(window))
            for tid, cat, window in specs}


SLOW = stats(("lan", "local", [1.0] * 10), ("gw", "gateway", [2.0] * 10), ("pub", "public", [200.0] * 10))     # latency
LOSSY = stats(("lan", "local", [1.0] * 10), ("gw", "gateway", [2.0] * 10), ("pub", "public", [None] * 2 + [20.0] * 8))
DEAD = stats(("lan", "local", [1.0] * 10), ("gw", "gateway", [2.0] * 10), ("pub", "public", [None] * 10))


# --- the rule ----------------------------------------------------------------------------------------------------

def test_the_kind_is_registered_as_a_busy_line_kind_that_load_explains():
    assert KINDS["busy_speedtest"].cause == "load" and "busy_speedtest" in LOAD_EXPLAINABLE_KINDS
    assert incident_cause("busy_speedtest", "speed_test") == "load" and incident_cause(None, "speed_test") == "load"


@pytest.mark.parametrize("spec,kind,status", [(c[0], c[1], c[2]) for c in CASES if c[2] in ("degraded", "down")],
                         ids=[c[1] for c in CASES if c[2] in ("degraded", "down")])
def test_every_fault_but_a_hard_outage_becomes_busy_speedtest(spec, kind, status):
    base = diagnose(rm(**spec), CONFIG, TS, LONG_AGO)
    masked = apply_speedtest(base)
    if status == "down":
        assert masked is base                                            # an outage during a test is still an outage
        return
    assert (masked.kind, masked.status, masked.cause, masked.severity) == ("busy_speedtest", "busy", "speed_test", "info")
    assert masked.title == "Speed Test Running: Delay Expected" and not masked.is_fault
    assert masked.affected == base.affected
    assert base.title in masked.description and base.description in masked.description
    assert "speed test" in masked.description


async def test_it_runs_before_the_busy_line_rule_and_wins_over_it():
    m = round_metrics(SLOW, [])
    heavy = LoadReading(pct=90.0, rx_mbps=900, tx_mbps=1, down_mbps=1000, up_mbps=100, down_source="configured",
                        up_source="configured")
    result = await ClassifierCore(ClassifierConfig()).step(m, 100.0, heavy, testing=True)
    assert result.verdict.kind == "busy_speedtest"
    result = await ClassifierCore(ClassifierConfig()).step(m, 100.0, heavy)
    assert result.verdict.kind == "busy_latency"                          # not testing: the busy line rule, as before


async def test_a_healthy_round_is_not_touched_by_the_flag():
    core = ClassifierCore(ClassifierConfig())
    ok = stats(("lan", "local", [1.0] * 10), ("gw", "gateway", [2.0] * 10), ("pub", "public", [20.0] * 10))
    assert (await core.step(round_metrics(ok, []), 1.0, None, testing=True)).verdict.kind == "nominal"


# --- through the incident tracker ---------------------------------------------------------------------------------

async def run_rounds(core, metrics, rounds, testing, start=1000.0):
    kinds = []
    for n in range(rounds):
        result = await core.step(metrics, start + n, None, testing=testing)
        kinds.append(result.verdict.kind)
    return kinds


async def test_a_30_second_test_opens_no_incident_but_the_same_delay_without_one_does():
    config = ClassifierConfig()                          # 10 slow rounds make a latency incident
    masked = ClassifierCore(config)
    kinds = await run_rounds(masked, round_metrics(SLOW, []), 30, testing=True)
    assert set(kinds) == {"busy_speedtest"} and masked.tracker.active_incident is None
    unmasked = ClassifierCore(config)
    kinds = await run_rounds(unmasked, round_metrics(SLOW, []), 30, testing=False)
    assert unmasked.tracker.active_incident is not None and kinds[-1] == "upstream_latency"


async def test_loss_during_a_test_is_masked_too_and_the_line_recovers_cleanly_after():
    core = ClassifierCore(ClassifierConfig())
    assert set(await run_rounds(core, round_metrics(LOSSY, []), 30, testing=True)) == {"busy_speedtest"}
    assert core.tracker.active_incident is None
    healthy = stats(("lan", "local", [1.0] * 10), ("gw", "gateway", [2.0] * 10), ("pub", "public", [20.0] * 10))
    assert set(await run_rounds(core, round_metrics(healthy, []), 5, testing=False, start=2000.0)) == {"nominal"}
    assert core.tracker.active_incident is None


async def test_a_hard_outage_during_a_test_still_opens_an_incident():
    core = ClassifierCore(ClassifierConfig())
    kinds = await run_rounds(core, round_metrics(DEAD, []), 5, testing=True)
    assert set(kinds) == {"isp_outage"} and core.tracker.active_incident is not None
    assert core.tracker.active_incident.severity == "critical"


async def test_a_fault_that_already_has_an_incident_is_not_ended_early_by_the_mask_only_not_started():
    core = ClassifierCore(ClassifierConfig(consecutive_successes_for_recovery=2))
    await run_rounds(core, round_metrics(DEAD, []), 5, testing=False)
    assert core.tracker.active_incident is not None
    # the test starts while the outage is on: still down, still the same incident
    await run_rounds(core, round_metrics(DEAD, []), 3, testing=True, start=1100.0)
    assert core.tracker.active_incident is not None and core.tracker.active_incident.kind == "isp_outage"


async def test_an_incident_open_when_a_test_starts_is_neither_ended_nor_split_by_it():
    """The test's rounds say nothing about the line, so the tracker holds still: before, they counted as healthy and
    resolved a latency incident that was open when the test began, which then opened again as a second one after it."""
    core = ClassifierCore(ClassifierConfig(consecutive_successes_for_recovery=2))
    await run_rounds(core, round_metrics(SLOW, []), 12, testing=False)
    incident = core.tracker.active_incident
    assert incident is not None and incident.kind == "upstream_latency"
    kinds = await run_rounds(core, round_metrics(SLOW, []), 30, testing=True, start=1100.0)
    assert set(kinds) == {"busy_speedtest"}
    assert core.tracker.active_incident is incident and not incident.resolved      # not ended by the test
    await run_rounds(core, round_metrics(SLOW, []), 3, testing=False, start=1200.0)
    assert core.tracker.active_incident is incident                                # the same one goes on, no second


async def test_a_fault_building_up_when_a_test_starts_is_held_not_reset_by_it():
    core = ClassifierCore(ClassifierConfig())                  # 10 slow rounds make a latency incident
    await run_rounds(core, round_metrics(SLOW, []), 6, testing=False)
    await run_rounds(core, round_metrics(SLOW, []), 20, testing=True, start=1100.0)
    assert core.tracker.active_incident is None
    await run_rounds(core, round_metrics(SLOW, []), 4, testing=False, start=1200.0)
    assert core.tracker.active_incident is not None            # 6 before the test and 4 after it


# --- the live classifier -------------------------------------------------------------------------------------------

def samples_of(metrics_stats):
    return [ProbeSample(target_id=tid, timestamp=1.0, success=lat is not None, latency_ms=lat)
            for tid, st in metrics_stats.items() for lat in st.recent_latencies[-1:]]


async def test_the_live_classifier_asks_its_switch_each_round():
    classifier = OutageClassifier(ClassifierConfig())
    state = {"on": True}
    classifier.speedtest_active = lambda: state["on"]
    assert (await classifier.evaluate(samples_of(SLOW), SLOW)).title == "Speed Test Running: Delay Expected"
    assert classifier.last_verdict.kind == "busy_speedtest"
    state["on"] = False
    assert (await classifier.evaluate(samples_of(SLOW), SLOW)).title == "ISP Latency Spikes / Jitter"
    classifier.speedtest_active = None                                    # no speed test component: as it always was
    assert (await classifier.evaluate(samples_of(SLOW), SLOW)).title == "ISP Latency Spikes / Jitter"


async def test_the_component_wires_the_runners_mask_into_the_classifier(repo):
    config = AppConfig()
    classifier = OutageClassifier(config.classifier)
    state = AppState(config=config, repository=repo, classifier=classifier)
    ctx = Context(app=None, config=config, state=state)
    component = await speedtest_component.register(ctx)
    assert component.name == "speedtest" and state.speedtest is not None
    assert classifier.speedtest_active == state.speedtest.is_masking
    assert speedtest_component.OPTIONAL is True
    await component.start()
    assert len(ctx.tasks) == 1
    for task in ctx.tasks:
        task.cancel()


# --- the outage-rules replay --------------------------------------------------------------------------------------

TARGETS = [("local", "local", "Local", "192.0.2.1", True), ("gw", "gateway", "Gateway", "192.0.2.2", True),
           ("isp", "isp", "Hub", "198.51.100.1", True), ("pub", "public", "Public", "203.0.113.1", True)]


def slow_rows(first=20, last=70, total=80):
    return [(T0 + k, t, 300.0 if t == "pub" and first <= k < last else 1.0) for k in range(total)
            for t in ("local", "gw", "isp", "pub", "dns")]


def add_test(db, ts, duration, status="ok"):
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO speed_tests (ts, duration_s, trigger, provider, status) VALUES (?, ?, 'scheduled', "
                 "'cloudflare', ?)", (ts, duration, status))
    conn.commit()
    conn.close()


async def test_the_replay_masks_the_stored_speed_tests_as_the_live_classifier_does(tmp_path):
    db = tmp_path / "r.db"
    await make_db(db, slow_rows())                  # slow from T0+20 to T0+70
    spec = spec_for(TARGETS, T0, T0 + 80)
    without = await run_replay(spec, sqlite3.connect(db))
    assert without["current"]["incidents"] == 1 and without["draft"]["incidents"] == 1
    add_test(db, T0 + 15, 30.0)                      # T0+15 to T0+45, and its 15 s tail: the mask ends at T0+60
    partly = await run_replay(spec, sqlite3.connect(db))
    assert partly["current"]["incidents"] == 1       # 10 slow rounds are left after it: an incident, from T0+60 on
    conn = sqlite3.connect(db)
    conn.execute("UPDATE speed_tests SET duration_s = 55.0")      # T0+15 to T0+70, tail to T0+85: all of it
    conn.commit()
    conn.close()
    masked = await run_replay(spec, sqlite3.connect(db))
    assert masked["current"]["incidents"] == 0 and masked["draft"]["incidents"] == 0
    assert masked["current"]["busy_hidden"] == 1 and masked["changes"] == []


async def test_the_replay_ignores_skipped_tests_and_tests_outside_the_window(tmp_path):
    db = tmp_path / "s.db"
    await make_db(db, slow_rows())
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO speed_tests (ts, duration_s, trigger, provider, status, reason) VALUES (?, NULL, 'scheduled', "
                 "'cloudflare', 'skipped', 'line busy')", (T0 + 15,))        # never ran: no duration
    conn.commit()
    conn.close()
    add_test(db, T0 - 5000, 30.0)                                           # long before the window
    result = await run_replay(spec_for(TARGETS, T0, T0 + 80), sqlite3.connect(db))
    assert result["current"]["incidents"] == 1


async def test_the_replay_tail_follows_each_rule_sets_own_window(tmp_path):
    """A draft that judges over 20 probes keeps the mask 10 rounds longer than the current rules (10 probes)."""
    db = tmp_path / "t.db"
    await make_db(db, slow_rows(first=30, last=75, total=75))
    add_test(db, T0 + 5, 40.0)                       # ends at T0+45; tails: current to T0+60, draft (20 probes) to T0+70
    spec = spec_for(TARGETS, T0, T0 + 75, draft={"window_probes": 20})
    result = await run_replay(spec, sqlite3.connect(db))
    assert result["current"]["incidents"] == 1 and result["draft"]["incidents"] == 0
