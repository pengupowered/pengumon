"""The outage-rules replay (engine/replay.py): the stored samples run again through ClassifierCore.

The fidelity test is what makes "N incidents instead of M" trustworthy: the characterization scenario runs through the live
OutageClassifier, its samples (and the scripted load) are written to a database, and the replay of that database with the
same rules must give the same incidents (start, end, kind, severity).
"""

import json
import sqlite3
from pathlib import Path
import subprocess
import sys

import pytest

from pengumon.db.migrations import migrate
from pengumon.db.migrations.v014_path_versions import BASELINE_SUMMARY
from pengumon.engine import load as load_module
from pengumon.engine.classifier_settings import ClassifierConfig
from pengumon.engine.load import LineLoad
from pengumon.engine.load_settings import LineConfig
from pengumon.engine.replay import StoredLoad, replay, run_replay
from pengumon.notify.settings import AlertsConfig
from pengumon.settings.preview import PreviewJobs

from test_classifier_characterization import CONFIGS, T0, Texts, run_scenario

LINE = {"down_mbps": 1000.0, "up_mbps": 50.0}
DEVICE = "router"


async def make_db(path, rows, rates=()):
    """rows: (ts, target key, latency or None); rates: (ts, rx_mbps, tx_mbps)."""
    await migrate(str(path))
    conn = sqlite3.connect(path)
    keys = sorted({r[1] for r in rows})
    conn.executemany("INSERT INTO targets (key) VALUES (?)", [(k,) for k in keys])
    ids = {k: i for i, k in conn.execute("SELECT id, key FROM targets")}
    conn.executemany("INSERT OR IGNORE INTO samples (ts, target, ok, latency_ms) VALUES (?, ?, ?, ?)",
                     [(ts, ids[k], 0 if lat is None else 1, lat) for ts, k, lat in rows])
    conn.executemany("INSERT INTO device_samples (device_id, ts, status, wan_rx_mbps, wan_tx_mbps) "
                     "VALUES (?, ?, 'ok', ?, ?)", [(DEVICE, ts, rx, tx) for ts, rx, tx in rates])
    conn.commit()
    conn.close()


def spec_for(targets, start, end, draft=None, dns=("dns",), **extra):
    """The replay's input; targets are (id, category, name, address, enabled)."""
    classifier = ClassifierConfig().model_dump(mode="json")
    rules = {"classifier": classifier, "line": LINE}
    draft_rules = {"classifier": {**classifier, **(draft or {})}, "line": LINE}
    return {
        "db_path": "", "start": start, "end": end, "round_seconds": 1.0,
        "targets": [{"id": k, "name": n, "category": c, "address": a, "enabled": e} for k, c, n, a, e in targets],
        "dns_probes": [{"id": d, "name": "DNS Lookup", "domain": "example.com"} for d in dns],
        "rules": {"current": rules, "draft": draft_rules}, "wan_device_id": DEVICE, "timezone": "UTC",
        "line_technology": "docsis", **extra,
    }


def triples(rule_set):
    return [(round(i.start_time, 2), None if i.end_time is None else round(i.end_time, 2), i.kind, i.severity)
            for i in rule_set.incidents]


@pytest.mark.parametrize("name, config, targets, order, base, phases", CONFIGS, ids=[c[0] for c in CONFIGS])
async def test_replay_with_the_current_rules_gives_exactly_the_incidents_the_live_classifier_gave(
        monkeypatch, tmp_path, name, config, targets, order, base, phases):
    # The scripted load is one value per round; with these windows the stored rate of the round is the whole reading,
    # which is what the scripted live load gave.
    monkeypatch.setattr(load_module, "LIVE_WINDOW_SECONDS", 0.5)
    monkeypatch.setattr(load_module, "STALE_AFTER_SECONDS", 0.5)
    live, rows, rates = [], [], []

    def tap(classifier):
        started, resolved, evaluate = classifier.on_incident_started, classifier.on_incident_resolved, classifier.evaluate
        classifier.on_incident_started = lambda inc: (live.append(inc), started(inc))
        classifier.on_incident_resolved = resolved

        async def spy(samples, stats):
            pct = classifier.load.pct
            if pct is not None:
                rates.append((samples[0].timestamp, pct * 10.0, 3.0))
            rows.extend((s.timestamp, s.target_id, s.latency_ms) for s in samples)
            return await evaluate(samples, stats)

        classifier.evaluate = spy

    await run_scenario(monkeypatch, config, targets, order, base, phases, Texts(), tap=tap)
    assert live and rows
    times = [r[0] for r in rows]
    if any(b < a for a, b in zip(times, times[1:])):
        # custom_levels sets the clock back (upstream_age: "the last upstream issue was 200 s ago"), which a running
        # app never does; samples stored by time can't give such a run back in its order
        pytest.skip(f"the {name} scenario sets its clock back")
    assert name == "fallback" or len(live) >= 10
    expected = [(round(i.start_time, 2), None if i.end_time is None else round(i.end_time, 2), i.kind, i.severity)
                for i in live]

    db = tmp_path / "scenario.db"
    await make_db(db, rows, rates)
    spec = spec_for(targets, T0, rows[-1][0], dns=("dns",))
    spec["rules"]["current"]["classifier"] = spec["rules"]["draft"]["classifier"] = config.model_dump(mode="json")
    result, (current, draft) = await replay(spec, sqlite3.connect(db))
    assert result["window"]["gaps"] == [] and result["window"]["rounds"] == len({r[0] for r in rows})
    assert triples(current) == expected
    assert triples(draft) == expected
    assert result["changes"] == [] and result["current"] == result["draft"]
    assert result["current"]["incidents"] == len(live)


async def test_other_rules_give_other_incidents_and_the_changes_say_where(tmp_path):
    rows = [(T0 + k, t, None if t == "local" and 20 <= k < 40 else 1.0) for k in range(80)
            for t in ("local", "gw", "isp", "pub", "dns")]
    db = tmp_path / "a.db"
    await make_db(db, rows)
    targets = [("local", "local", "Local", "192.0.2.1", True), ("gw", "gateway", "Gateway", "192.0.2.2", True),
               ("isp", "isp", "Hub", "198.51.100.1", True), ("pub", "public", "Public", "203.0.113.1", True)]
    base = spec_for(targets, T0, T0 + 80, draft={"consecutive_failures_for_incident": 30})
    result, (current, draft) = await replay(base, sqlite3.connect(db))
    assert result["current"]["incidents"] == 1 and result["draft"]["incidents"] == 0
    assert result["current"]["total_seconds"] > 0 and result["draft"]["total_seconds"] == 0
    assert [c["in"] for c in result["changes"]] == ["current"] and result["changes"][0]["kind"].startswith("local")
    assert result["window"]["rounds"] == 80


async def test_samples_staggered_within_a_round_are_one_round(tmp_path):
    rows = [(T0 + k + 0.025 * i, t, 1.0) for k in range(60) for i, t in enumerate(("local", "gw", "isp", "pub", "dns"))]
    db = tmp_path / "s.db"
    await make_db(db, rows)
    targets = [("local", "local", "Local", "192.0.2.1", True), ("gw", "gateway", "Gateway", "192.0.2.2", True),
               ("isp", "isp", "Hub", "198.51.100.1", True), ("pub", "public", "Public", "203.0.113.1", True)]
    result = await run_replay(spec_for(targets, T0, T0 + 61), sqlite3.connect(db))
    assert result["window"]["rounds"] == 60 and result["window"]["gaps"] == []


async def test_a_round_also_ends_when_a_target_id_repeats(tmp_path):
    # 0.3 s apart: less than half a round would not split them, the repeated id does
    rows = [(T0 + 0.3 * k, "local", 1.0) for k in range(10)]
    db = tmp_path / "r.db"
    await make_db(db, rows)
    targets = [("local", "local", "Local", "192.0.2.1", True)]
    result = await run_replay(spec_for(targets, T0, T0 + 5, dns=()), sqlite3.connect(db))
    assert result["window"]["rounds"] == 10


async def test_a_gap_resolves_the_open_incident_at_its_last_bad_round_and_starts_with_empty_windows(tmp_path):
    lost = lambda k: 20 <= k < 40  # noqa: E731
    rows = [(T0 + k, t, None if t == "local" and lost(k) else 1.0) for k in list(range(40)) + list(range(140, 160))
            for t in ("local", "gw", "isp", "pub", "dns")]
    db = tmp_path / "g.db"
    await make_db(db, rows)
    targets = [("local", "local", "Local", "192.0.2.1", True), ("gw", "gateway", "Gateway", "192.0.2.2", True),
               ("isp", "isp", "Hub", "198.51.100.1", True), ("pub", "public", "Public", "203.0.113.1", True)]
    result, (current, draft) = await replay(spec_for(targets, T0, T0 + 160), sqlite3.connect(db))
    assert result["window"]["gaps"] == [{"start": T0 + 39, "end": T0 + 140}]
    (start, end, kind, _), = triples(current)
    assert end == T0 + 39  # not stretched by the old window's losses after the restart
    assert result["current"]["incidents"] == 1 and current.core.tracker.active_incident is None


async def test_an_incident_open_at_the_window_end_has_no_end(tmp_path):
    rows = [(T0 + k, t, None if t == "local" and k >= 20 else 1.0) for k in range(40)
            for t in ("local", "gw", "isp", "pub", "dns")]
    db = tmp_path / "o.db"
    await make_db(db, rows)
    targets = [("local", "local", "Local", "192.0.2.1", True), ("gw", "gateway", "Gateway", "192.0.2.2", True)]
    result, (current, _) = await replay(spec_for(targets, T0, T0 + 40), sqlite3.connect(db))
    assert triples(current)[0][1] is None and result["current"]["total_seconds"] > 10


def test_the_same_outage_found_differently_is_one_changed_entry_not_gone_plus_new():
    from types import SimpleNamespace as NS

    from pengumon.engine.replay import _changes

    def rules(*incidents):
        return NS(incidents=[NS(start_time=s, end_time=e, kind=k, severity=v) for s, e, k, v in incidents])

    current = rules((100.0, 160.0, "local_wifi", "warning"), (500.0, 520.0, "isp_outage", "critical"))
    draft = rules((130.0, 160.0, "local_wifi", "critical"), (900.0, 910.0, "dns", "warning"))
    changes = _changes(current, draft)
    assert [(c["in"], c["kind"]) for c in changes] == [("changed", "local_wifi"), ("current", "isp_outage"), ("draft", "dns")]
    assert (changes[0]["start"], changes[0]["was_start"], changes[0]["was_severity"]) == (130.0, 100.0, "warning")


async def test_load_makes_the_replay_call_a_slow_line_busy_and_counts_it(tmp_path):
    rows = [(T0 + k, t, 300.0 if t == "pub" and k >= 20 else 1.0) for k in range(60)
            for t in ("local", "gw", "isp", "pub", "dns")]
    rates = [(T0 + k, 950.0, 3.0) for k in range(0, 60, 10)]
    db = tmp_path / "l.db"
    await make_db(db, rows, rates)
    targets = [("local", "local", "Local", "192.0.2.1", True), ("gw", "gateway", "Gateway", "192.0.2.2", True),
               ("isp", "isp", "Hub", "198.51.100.1", True), ("pub", "public", "Public", "203.0.113.1", True)]
    spec = spec_for(targets, T0, T0 + 60)
    spec["rules"]["draft"]["classifier"]["busy_load_pct"] = 99.0  # the draft does not call 95 % busy
    result = await run_replay(spec, sqlite3.connect(db))
    assert result["current"]["busy_hidden"] == 1 and result["current"]["incidents"] == 0
    assert result["draft"]["busy_hidden"] == 0 and result["draft"]["incidents"] == 1
    assert [c["in"] for c in result["changes"]] == ["draft"]


async def test_a_draft_window_is_judged_over_its_own_probes(tmp_path):
    # 8 slow rounds: more than half of the last 10 probes for 7 rounds (an incident after 3), never of the last 20
    rows = [(T0 + k, t, 300.0 if t == "pub" and 20 <= k < 28 else 1.0) for k in range(60)
            for t in ("local", "gw", "isp", "pub", "dns")]
    db = tmp_path / "w.db"
    await make_db(db, rows)
    targets = [("local", "local", "Local", "192.0.2.1", True), ("gw", "gateway", "Gateway", "192.0.2.2", True),
               ("isp", "isp", "Hub", "198.51.100.1", True), ("pub", "public", "Public", "203.0.113.1", True)]
    spec = spec_for(targets, T0, T0 + 60, draft={"consecutive_failures_for_latency_incident": 3, "window_probes": 20})
    spec["rules"]["current"]["classifier"]["consecutive_failures_for_latency_incident"] = 3
    result = await run_replay(spec, sqlite3.connect(db))
    assert result["current"]["incidents"] == 1 and result["draft"]["incidents"] == 0


@pytest.mark.parametrize("seed", [1, 2, 3])
async def test_a_draft_window_judges_hop_status_as_if_it_were_live(tmp_path, seed):
    """Which hops ignore ping depends on their status over the window: a draft with another window_probes gives the
    incidents those rules give when they are the current ones."""
    db = tmp_path / "s.db"
    await make_db(db, _random_rows(seed))
    draft = {"window_probes": 30, "consecutive_failures_for_incident": 2}
    as_draft = await replay(spec_for(TARGETS5, T0, T0 + 3000, draft=draft), sqlite3.connect(db))
    live = spec_for(TARGETS5, T0, T0 + 3000, draft=draft)
    live["rules"]["current"] = live["rules"]["draft"]
    as_current = await replay(live, sqlite3.connect(db))
    assert as_draft[0]["draft"] == as_current[0]["current"]
    assert _document(*as_draft)[1][1] == _document(*as_current)[1][0] and _document(*as_current)[1][0]


async def test_notes_name_a_path_change_and_missing_load(tmp_path):
    rows = [(T0 + k, "local", 1.0) for k in range(20)]
    db = tmp_path / "n.db"
    await make_db(db, rows)
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO path_versions (revision, applied_at, hop_count, summary, hops) VALUES "
                 "(2, ?, 6, '3 hops added', '[]')", (T0 + 5,))
    conn.execute("INSERT INTO path_versions (revision, applied_at, hop_count, summary, hops) VALUES "
                 "(1, ?, 6, ?, '[]')", (T0 + 4, BASELINE_SUMMARY))
    conn.commit()
    spec = spec_for([("local", "local", "Local", "192.0.2.1", True)], T0, T0 + 20, dns=())
    notes = (await run_replay(spec, conn))["notes"]
    assert any("path was changed" in n and "3 hops added" in n and "today's path" in n for n in notes)
    assert any("load reading" in n for n in notes)
    assert len([n for n in notes if "path was changed" in n]) == 1  # the first version is not a change
    assert all(n.endswith(".") for n in notes)


async def test_recorded_counts_the_stored_network_incidents_in_the_window(tmp_path):
    db = tmp_path / "c.db"
    await make_db(db, [(T0, "local", 1.0)])
    conn = sqlite3.connect(db)
    cols = ("start_time, end_time, duration_seconds, cause_code, title, description, severity, resolved, kind, expected, "
            "affected_targets")
    for start, kind, expected in ((T0 + 1, "local_down", 0), (T0 + 2, "service_down", 0), (T0 + 3, "local_loss", 1),
                                  (T0 - 500, "local_down", 0)):
        conn.execute(f"INSERT INTO incidents ({cols}) VALUES (?, ?, 1, 'x', 't', 'd', 'critical', 1, ?, ?, '[]')",
                     (start, start + 1, kind, expected))
    conn.commit()
    result = await run_replay(spec_for([("local", "local", "L", "192.0.2.1", True)], T0, T0 + 10, dns=()), conn)
    assert result["recorded"] == 1


def test_stored_load_answers_what_the_live_line_load_answers():
    rates = [(100.0 + 10 * i, 100.0 * ((i * 7) % 11), 5.0 * ((i * 3) % 7)) for i in range(40)]
    line = LineLoad(LineConfig(down_mbps=1000.0, up_mbps=50.0))
    stored = StoredLoad(rates, line)
    for tenths in range(900, 5200, 7):
        ts = tenths / 10.0
        live = LineLoad(LineConfig(down_mbps=1000.0, up_mbps=50.0), rates_source=lambda: rates, clock=lambda: ts)
        assert stored.current(ts) == live.current(ts), ts


def test_stored_load_is_unknown_without_a_capacity_and_when_the_last_rate_is_stale():
    rates = [(100.0, 500.0, 5.0)]
    assert StoredLoad(rates, LineLoad(LineConfig())).current(101.0) is None
    line = LineLoad(LineConfig(down_mbps=1000.0, up_mbps=50.0))
    assert StoredLoad(rates, line).current(101.0).pct == 50.0
    assert StoredLoad(rates, line).current(100.0 + load_module.STALE_AFTER_SECONDS + 1) is None
    learned = LineLoad(LineConfig())
    learned.learned_down_mbps, learned.learned_up_mbps = 1000.0, 50.0
    assert StoredLoad(rates, learned).current(101.0).down_source == "learned"


def _importable(monkeypatch):
    """The suite imports pengumon from its source tree; the process it starts has to find it there too."""
    import pengumon
    monkeypatch.setenv("PYTHONPATH", str(Path(pengumon.__file__).parent.parent))


# --- the process -------------------------------------------------------------------------------------------------

async def test_the_process_reads_the_database_read_only_and_answers_in_json_lines(tmp_path, monkeypatch):
    _importable(monkeypatch)
    rows = [(T0 + k, t, None if t == "local" and 10 <= k < 30 else 1.0) for k in range(50)
            for t in ("local", "gw", "isp", "pub", "dns")]
    db = tmp_path / "p.db"
    await make_db(db, rows)
    before = db.read_bytes()
    targets = [("local", "local", "Local", "192.0.2.1", True), ("gw", "gateway", "Gateway", "192.0.2.2", True)]
    spec = spec_for(targets, T0, T0 + 50, draft={"consecutive_failures_for_incident": 30})
    spec["db_path"] = str(db)
    done = subprocess.run([sys.executable, "-m", "pengumon.engine.replay"], input=json.dumps(spec), text=True,
                          capture_output=True, timeout=60)
    assert done.returncode == 0, done.stderr
    lines = [json.loads(line) for line in done.stdout.splitlines()]
    assert "progress" in lines[0] and set(lines[-1]) == {"result"}
    result = lines[-1]["result"]
    assert result["current"]["incidents"] == 1 and result["draft"]["incidents"] == 0
    assert db.read_bytes() == before  # nothing written, not even a journal change in the main file


async def test_a_job_runs_the_real_process_end_to_end(tmp_path, monkeypatch):
    _importable(monkeypatch)
    rows = [(T0 + k, t, None if t == "local" and 10 <= k < 30 else 1.0) for k in range(50)
            for t in ("local", "gw", "isp", "pub", "dns")]
    db = tmp_path / "j.db"
    await make_db(db, rows)
    spec = spec_for([("local", "local", "Local", "192.0.2.1", True)], T0, T0 + 50)
    spec["db_path"] = str(db)
    store = PreviewJobs(tick=0.05)
    job = store.start("k", spec)
    await job.task
    body = store.describe(job)
    assert body.state == "done", body.error
    assert body.current.incidents == 1 and body.window.rounds == 50


# --- the alert counts: the notifier's AlertPolicy over each rule set's incidents ---------------------------------------

LOSS_TARGETS = [("local", "local", "Local", "192.0.2.1", True), ("gw", "gateway", "Gateway", "192.0.2.2", True),
                ("isp", "isp", "Hub", "198.51.100.1", True), ("pub", "public", "Public", "203.0.113.1", True)]


async def _loss_db(tmp_path, bursts, length=200):
    """Local loss in each (from, to) second range of a `length`-second window, everything else answering."""
    rows = [(T0 + k, t, None if t == "local" and any(a <= k < b for a, b in bursts) else 1.0) for k in range(length)
            for t in ("local", "gw", "isp", "pub", "dns")]
    db = tmp_path / "alerts.db"
    await make_db(db, rows)
    return db


def _alert_spec(length, current=None, draft=None):
    spec = spec_for(LOSS_TARGETS, T0, T0 + length)
    spec["rules"]["current"]["alerts"] = {**AlertsConfig().model_dump(mode="json"), **(current or {})}
    spec["rules"]["draft"]["alerts"] = {**AlertsConfig().model_dump(mode="json"), **(draft or {})}
    return spec


async def test_every_incident_sends_one_alert_at_the_default_rules_and_a_longer_delay_drops_a_short_one(tmp_path):
    db = await _loss_db(tmp_path, [(20, 40)])  # one 20-second loss
    result, (current, _) = await replay(_alert_spec(200, draft={"delay_seconds": 60}), sqlite3.connect(db))
    assert result["current"]["incidents"] == result["draft"]["incidents"] == 1
    assert result["current"]["alerts"] == 1
    assert result["draft"]["alerts"] == 0  # it was over before the minute was up: nothing is sent
    result = await run_replay(_alert_spec(200, draft={"delay_seconds": 5}), sqlite3.connect(db))
    assert result["draft"]["alerts"] == 1  # it lasted longer than five seconds


async def test_the_repeat_window_counts_one_alert_per_cause_and_zero_counts_every_flap(tmp_path):
    db = await _loss_db(tmp_path, [(20, 30), (60, 70), (100, 110)])  # the same cause three times in two minutes
    result, (current, _) = await replay(_alert_spec(200, draft={"repeat_window_seconds": 0}), sqlite3.connect(db))
    assert result["current"]["incidents"] == 3 and len({i.cause_code for i in current.incidents}) == 1
    assert result["current"]["alerts"] == 1
    assert result["draft"]["alerts"] == 3


async def test_critical_only_and_quiet_hours_hold_or_drop_alerts(tmp_path):
    db = await _loss_db(tmp_path, [(20, 40)])
    _, (current, _) = await replay(_alert_spec(200), sqlite3.connect(db))
    (incident,) = current.incidents
    severity = incident.severity
    # quiet hours around T0 (13:46 UTC) that end long after the incident: it is over by then, so nothing is sent
    quiet = {"quiet_hours": {"enabled": True, "start": "13:00", "end": "15:00", "critical_still": False}}
    result = await run_replay(_alert_spec(200, draft=quiet), sqlite3.connect(db))
    assert result["current"]["alerts"] == 1 and result["draft"]["alerts"] == 0
    exempt = {"quiet_hours": {**quiet["quiet_hours"], "critical_still": True}}
    result = await run_replay(_alert_spec(200, draft=exempt), sqlite3.connect(db))
    assert result["draft"]["alerts"] == (1 if severity == "critical" else 0)
    result = await run_replay(_alert_spec(200, draft={"min_severity": "critical"}), sqlite3.connect(db))
    assert result["draft"]["alerts"] == (1 if severity == "critical" else 0)


def test_a_warning_that_rises_to_critical_is_counted_under_critical_only_from_its_escalation():
    from pengumon.engine.replay import _RuleSet, _alerts
    from pengumon.incidents.model import Incident
    rule_set = _RuleSet("draft", ClassifierConfig(), None, None, AlertsConfig(min_severity="critical", delay_seconds=60))
    from pengumon.incidents.taxonomy import CAUSE_CODES
    incident = Incident(start_time=T0, end_time=T0 + 100, cause_code=sorted(CAUSE_CODES)[0], title="t", description="d",
                        severity="critical")
    rule_set.incidents.append(incident)
    rule_set.first_severity[id(incident)] = "warning"
    rule_set.escalated_at[id(incident)] = T0 + 50
    assert _alerts(rule_set, "UTC", T0 + 200) == 0  # considered at +50, due at +110: over at +100
    rule_set.escalated_at[id(incident)] = T0 + 30
    assert _alerts(rule_set, "UTC", T0 + 200) == 1  # due at +90, still going
    rule_set.alerts = AlertsConfig(min_severity="warning", delay_seconds=60)
    rule_set.escalated_at[id(incident)] = T0 + 50
    assert _alerts(rule_set, "UTC", T0 + 200) == 1  # a warning passes at its start: due at +60


# The replay keeps less than the prober does (engine.replay._Target) and skips the rounds in which nothing can happen.
# Both must change nothing in the answer.

def _random_rows(seed, rounds=3000):
    """One probe per target per second: calm, with bursts of loss and of slow answers at random places."""
    import random
    rng = random.Random(seed)
    names = ["local", "gw", "isp1", "isp2", "pub1", "pub2", "dns"]
    rows, burst = [], {n: (0, None) for n in names}
    for r in range(rounds):
        for name in names:
            left, kind = burst[name]
            if left == 0 and rng.random() < 0.004:
                left, kind = rng.randint(1, 25), rng.choice(["lost", "slow", "lost", "edge"])
            base = {"local": 2.0, "gw": 8.0, "isp1": 14.0, "isp2": 18.0, "pub1": 22.0, "pub2": 30.0, "dns": 40.0}[name]
            lat = base + rng.random() * 3
            if left:
                lat = None if kind == "lost" else (base * 6 if kind == "slow" else base * 3)
                left -= 1
            burst[name] = (left, kind)
            rows.append((T0 + r + rng.random() * 0.1, name, lat))
    return rows


TARGETS5 = [("local", "local", "Local", "192.0.2.1", True), ("gw", "gateway", "Gateway", "192.0.2.2", True),
            ("isp1", "isp", "Hop one", "192.0.2.3", True), ("isp2", "isp", "Hop two", "192.0.2.4", True),
            ("pub1", "public", "Public one", "192.0.2.5", True), ("pub2", "public", "Public two", "192.0.2.6", True)]
RANDOM_DRAFTS = [{}, {"consecutive_failures_for_incident": 2, "degraded_loss_pct": 8},
                 {"public_latency_spike_threshold_ms": 25, "consecutive_failures_for_latency_incident": 3},
                 {"local_jitter_spike_threshold_ms": 1.5, "gateway_latency_spike_threshold_ms": 9,
                  "isp_latency_spike_threshold_ms": 20},
                 {"window_probes": 30, "consecutive_failures_for_incident": 2}]  # a status per window


def _document(result, rule_sets):
    return result, [[(i.start_time, i.end_time, i.kind, i.severity, i.title, i.description, i.affected_targets,
                      i.load_pct) for i in rs.incidents] for rs in rule_sets]


@pytest.mark.parametrize("seed, draft", [(s, d) for s in (1, 2) for d in RANDOM_DRAFTS])
async def test_skipping_calm_rounds_changes_nothing_in_the_answer(tmp_path, monkeypatch, seed, draft):
    from pengumon.engine import replay as replay_module
    db = tmp_path / "r.db"
    rows = _random_rows(seed)
    rates = [(T0 + k * 10, 100.0 + (k % 7) * 150.0, 5.0) for k in range(300)]  # some of it busy (60 % of 1000)
    await make_db(db, rows, rates)
    spec = spec_for(TARGETS5, T0, T0 + 3000, draft=draft)
    skipped = _document(*await replay(spec, sqlite3.connect(db)))
    monkeypatch.setattr(replay_module, "SKIP_CALM_ROUNDS", False)
    full = _document(*await replay(spec, sqlite3.connect(db)))
    assert skipped == full
    assert full[1][0] or full[1][1]  # the scenario does open incidents, or the comparison proves little


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_the_replays_target_keeps_what_the_prober_keeps_that_a_verdict_reads(seed):
    import random
    from pengumon.engine.prober import NetworkProber
    from pengumon.engine.replay import _Target
    from pengumon.models.probes import ProbeSample, TargetConfig
    rng = random.Random(seed)
    prober = NetworkProber()
    prober.register_target(TargetConfig(id="t", name="T", category="public", address="192.0.2.9"))
    target = _Target(prober.stats["t"], 50.0)
    for k in range(2000):
        lat = None if rng.random() < (0.5 if (k // 100) % 3 == 0 else 0.03) else rng.random() * 120
        prober._update_target_stats(ProbeSample(target_id="t", timestamp=float(k), success=lat is not None,
                                                latency_ms=lat, packet_loss_pct=0.0 if lat is not None else 100.0))
        target.add(lat)
        target.judge(10, prober.status_thresholds.down_loss_fraction)
        assert target.recent_latencies == prober.stats["t"].recent_latencies[-10:]
        assert (target.status == "down") == (prober.stats["t"].status == "down")
        window = target.recent_latencies
        assert bool(target.hot) == (None in window or max((v for v in window if v is not None), default=0) >= 50.0)
