"""The probe interval (0.28): a setting with the choices 1, 2, 5, 10, 30 and 60 s, applied without a restart.

What is checked, at every interval (generated samples, no real waiting): a loss rule counted in probes opens an incident
after the same number of lost probes; a minute of lost probes is dead; the uptime strips and the inspector's hop table see
the drop; a DNS lookup that is not due in a round is not probed (never lost) and the DNS rules count lookups. And the
one-time import of config.yaml's interval, the DNS interval's validation, the live apply, the wording of the rules."""

import asyncio
import logging
from types import SimpleNamespace

import pytest

from pengumon.analysis.incident import hop_impact, inspection_window
from pengumon.config import AppConfig
from pengumon.db.repository import DatabaseRepository
from pengumon.engine import loop as loop_module
from pengumon.engine.classifier import ClassifierCore
from pengumon.engine.classifier.metrics import round_metrics
from pengumon.engine.classifier_settings import ClassifierConfig
from pengumon.engine.loop import dns_due, probe_background_worker
from pengumon.engine.prober import NetworkProber
from pengumon.incidents.model import Incident
from pengumon.engine.prober_settings import DnsProbeConfig
from pengumon.incidents.model import SystemDiagnosis
from pengumon.models.probes import ProbeSample, TargetConfig
from pengumon.probe_interval import (
    INTERVAL_CHOICES, dead_minute_probes, dns_every, dns_interval_problem, daily_storage_bytes, monthly_traffic_bytes,
    probing_cost, uptime_min_probes,
)
from pengumon.reports.uptime import block_status, build_uptime
from pengumon.settings.document import SettingsDocument
from settings_helpers import make_manager

NOW = 1_793_448_000.0 + 7 * 60  # 12:07 UTC; its 15-minute block is 12:00-12:15
BLOCK = int(NOW // 900)
INTERVALS = list(INTERVAL_CHOICES)


# --- the numbers that depend on the interval ---------------------------------------------------------------------------

@pytest.mark.parametrize("interval, dead, minimum", [(1, 10, 10), (2, 10, 10), (5, 6, 10), (10, 3, 10), (30, 1, 10),
                                                      (60, 1, 7.5)])
def test_a_dead_minute_and_an_uptime_block_need_the_probes_the_interval_gives(interval, dead, minimum):
    assert dead_minute_probes(interval) == dead
    assert uptime_min_probes(900, interval) == minimum
    assert uptime_min_probes(86400, interval) == 10  # a day cell: always the full ten


def test_the_uptime_minimum_is_at_least_one_probe():
    assert uptime_min_probes(60, 60) == 1 and uptime_min_probes(1, 60) == 1


def test_the_dns_interval_must_be_a_multiple_of_the_round():
    assert dns_every(5, None) == 1 and dns_every(2, 10) == 5 and dns_every(10, 10) == 1
    assert dns_interval_problem(5, None) is None and dns_interval_problem(5, 30) is None
    assert "shorter" in dns_interval_problem(10, 5) and "multiple" in dns_interval_problem(2, 5)


def test_the_cost_of_an_interval_is_about_the_packet_sizes():
    # 12 ICMP hops past the modem: (84 + 38) * 2 = 244 B a round each; a month of 1 s rounds is about 7.6 GB
    month = monthly_traffic_bytes(1, 12 * 244, 0)
    assert 7.5e9 < month < 7.7e9
    assert monthly_traffic_bytes(10, 12 * 244, 0) == pytest.approx(month / 10)
    assert monthly_traffic_bytes(5, 0, 2, dns_interval=10) == pytest.approx(2 * 150 / 10 * 86400 * 30)
    assert daily_storage_bytes(1, 14, 0) == pytest.approx(14 * 86400 * 40)  # about 48 MB a day
    assert daily_storage_bytes(2, 14, 0) == pytest.approx(daily_storage_bytes(1, 14, 0) / 2)


def test_the_traffic_counts_the_hops_past_the_gateway_by_method_and_family():
    def hop(category, address="203.0.113.5", **kw):
        return TargetConfig(id=kw.pop("id", category), name="x", category=category, address=address, **kw)

    cost = probing_cost([hop("local", "192.0.2.1"), hop("gateway", "192.0.2.2"), hop("isp", id="i"),
                         hop("public", id="p6", family=6), hop("public", id="t", probe="tcp", port=443),
                         hop("public", id="off", enabled=False)])
    assert cost["hops"] == 5  # the disabled one is not probed
    assert cost["hop_bytes_per_round"] == 244 + 284 + 700  # IPv4 echo, IPv6 echo, a TCP check


# --- the classifier counts probes, at every interval -------------------------------------------------------------------

def _path_prober(with_dns=False):
    prober = NetworkProber()
    for tid, category in (("lan", "local"), ("gw", "gateway"), ("pub", "public")):
        prober.register_target(TargetConfig(id=tid, name=tid, category=category, address="192.0.2.9"))
    if with_dns:
        prober.register_dns_target(DnsProbeConfig(id="dns", name="dns", domain="example.com"))
    return prober


def _round(prober, ts, lost=(), skip=()):
    """One generated round at `ts`: every target answers except `lost`; the ones in `skip` are not probed at all."""
    samples = [ProbeSample(target_id=tid, timestamp=ts, success=tid not in lost,
                           latency_ms=None if tid in lost else 5.0, packet_loss_pct=100.0 if tid in lost else 0.0)
               for tid in prober.stats if tid not in skip]
    for sample in samples:
        prober._update_target_stats(sample)
    return samples


@pytest.mark.parametrize("interval", INTERVALS)
async def test_a_3_probe_outage_rule_opens_after_3_lost_probes_at_every_interval(interval):
    config = ClassifierConfig(consecutive_failures_for_incident=3, window_probes=3, consecutive_successes_for_recovery=3)
    core, prober = ClassifierCore(config, None), _path_prober()
    t = 10_000.0
    for _ in range(5):
        samples = _round(prober, t)
        result = await core.step(round_metrics(prober.stats, samples, 3), t, None)
        assert result.verdict.kind == "nominal" and result.outcome.started is None
        t += interval
    first_lost, opened = t, []
    for k in range(3):
        samples = _round(prober, t, lost={"pub"})
        result = await core.step(round_metrics(prober.stats, samples, 3), t, None)
        opened.append(result.outcome.started)
        t += interval
    assert opened[0] is None and opened[1] is None and opened[2] is not None  # the third lost probe, not before
    assert opened[2].start_time == first_lost  # back-dated to the first lost probe, whatever the spacing
    assert opened[2].kind == "isp_outage" and core.tracker.loss_failure_counter == 3
    for k in range(6):  # healthy again: the window (3 probes) clears, then 3 healthy probes in a row end it
        samples = _round(prober, t)
        result = await core.step(round_metrics(prober.stats, samples, 3), t, None)
        if result.outcome.resolved:
            break
        t += interval
    assert result.outcome.resolved is not None
    assert result.outcome.resolved.duration_seconds == pytest.approx(4 * interval)  # first lost probe to last fault round


# --- DNS lookups at their own interval -------------------------------------------------------------------------------

async def _dns_rounds(every, lost_lookups, rounds=40, hold=True):
    """Runs `rounds` rounds of a path whose DNS lookups are due every `every` rounds, the first `lost_lookups` due
    lookups lost; returns the incidents started and the DNS stats."""
    config = ClassifierConfig(consecutive_failures_for_incident=3, window_probes=3, dns_cooldown_seconds=0)
    core, prober = ClassifierCore(config, None), _path_prober(with_dns=True)
    started, due_seen = [], 0
    for index in range(rounds):
        ts = 20_000.0 + index
        due = index % every == 0
        lost = {"dns"} if due and due_seen < lost_lookups else set()
        samples = _round(prober, ts, lost=lost, skip=() if due else ("dns",))
        due_seen += due
        m = round_metrics(prober.stats, samples, 3)
        assert m.dns_probed is due
        result = await core.step(m, ts, None)
        if result.outcome.started:
            started.append((index, result.outcome.started.kind))
    return started, prober.stats["dns"]


@pytest.mark.parametrize("every", [1, 2, 5, 10])
async def test_a_dns_rule_counts_lookups_not_rounds(every):
    """The incident opens at the same lookup whatever the DNS interval: the third one, which is round 2 * every."""
    started, dns = await _dns_rounds(every, lost_lookups=4)
    assert dns.sent == -(-40 // every)  # one sample per due round and no more: a lookup not due is not probed
    assert [kind for _, kind in started] == ["dns_outage"]  # one incident, however many rounds the stale window spans
    assert started[0][0] == 2 * every


async def test_the_prober_leaves_the_dns_lookups_out_of_a_round_that_has_none_due(monkeypatch):
    prober = NetworkProber()
    prober.register_target(TargetConfig(id="gw", name="gw", category="gateway", address="192.0.2.1"))
    asked = []

    async def fake_icmp(self, target):
        return ProbeSample(target_id=target.id, timestamp=1.0, success=True, latency_ms=1.0)

    async def fake_dns(self, cfg):
        asked.append(cfg.id)
        return ProbeSample(target_id=cfg.id, timestamp=1.0, success=True, latency_ms=1.0)

    monkeypatch.setattr(NetworkProber, "probe_icmp", fake_icmp)
    monkeypatch.setattr(NetworkProber, "probe_dns", fake_dns)
    monkeypatch.setattr(NetworkProber, "backend", lambda self: object())
    gw = [TargetConfig(id="gw", name="gw", category="gateway", address="192.0.2.1")]
    dns = [DnsProbeConfig(id="d", name="d", domain="example.com")]
    assert [s.target_id for s in await prober.execute_probe_round(gw, dns)] == ["gw", "d"]
    assert [s.target_id for s in await prober.execute_probe_round(gw, [])] == ["gw"]
    assert asked == ["d"]


def test_the_lookups_are_due_every_dns_interval_over_the_round_from_the_first_round():
    config = SimpleNamespace(interval_seconds=2, dns_interval_seconds=10)
    assert [i for i in range(12) if dns_due(i, config)] == [0, 5, 10]
    assert all(dns_due(i, SimpleNamespace(interval_seconds=5, dns_interval_seconds=None)) for i in range(5))


# --- the loop: the interval is read every round -------------------------------------------------------------------------

class _RecordingProber:
    stats = {}

    def __init__(self):
        self.rounds = []

    def probing_problem(self):
        return None

    async def execute_probe_round(self, targets, dns_probes):
        self.rounds.append((asyncio.get_running_loop().time(), list(dns_probes)))
        return []


class _Classifier:
    async def evaluate(self, samples, stats):
        return SystemDiagnosis(load_pct=1.0)


async def _run_loop(config, seconds, repo, prober=None, between=None):
    prober = prober or _RecordingProber()
    task = asyncio.create_task(probe_background_worker(config, prober, _Classifier(), repo))
    try:
        if between:
            await between(prober)
        await asyncio.sleep(seconds)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    return prober


async def test_the_loop_runs_the_dns_lookups_every_dns_interval_and_the_path_every_round(tmp_path):
    repo = DatabaseRepository(db_path=str(tmp_path / "l.db"))
    await repo.initialize()
    config = AppConfig(dns_probes=[DnsProbeConfig(id="d", name="d", domain="example.com")])
    config.prober.interval_seconds = 0.1  # (tests assign it: not validated, never offered to users)
    config.prober.dns_interval_seconds = 0.3
    prober = await _run_loop(config, 1.05, repo)
    dns_rounds = [bool(dns) for _, dns in prober.rounds]
    assert len(dns_rounds) >= 8
    assert dns_rounds[:7] == [True, False, False, True, False, False, True]  # due on rounds 0, 3, 6; every round probed
    await repo.close()


async def test_a_new_interval_takes_effect_at_the_next_round_without_a_restart(tmp_path, monkeypatch):
    late = _count_late_rounds(monkeypatch)
    repo = DatabaseRepository(db_path=str(tmp_path / "l.db"))
    await repo.initialize()
    config = AppConfig()
    config.prober.interval_seconds = 60  # a long wait: a loop that read it once would not run again

    async def change(prober):
        await asyncio.sleep(0.2)
        assert len(prober.rounds) == 1
        config.prober.interval_seconds = 0.1  # what the settings manager does in place before its appliers run

    prober = await _run_loop(config, 1.5, repo, between=change)
    assert len(prober.rounds) >= 4
    gaps = [b[0] - a[0] for a, b in zip(prober.rounds, prober.rounds[1:])]
    assert max(gaps[1:]) < 0.45 and gaps[0] < 0.8  # the first wait ends within one slice of the change
    assert late == []  # a shorter interval is not made late by the wait that was still running
    await repo.close()


def _count_late_rounds(monkeypatch):
    late = []
    monkeypatch.setattr(loop_module, "probe_rounds_late", SimpleNamespace(add=lambda *a, **k: late.append(a)))
    return late


async def test_a_longer_interval_does_not_count_as_a_late_round(tmp_path, monkeypatch):
    late = _count_late_rounds(monkeypatch)
    repo = DatabaseRepository(db_path=str(tmp_path / "l.db"))
    await repo.initialize()
    config = AppConfig()
    config.prober.interval_seconds = 0.1

    async def change(prober):
        await asyncio.sleep(0.35)
        config.prober.interval_seconds = 0.6  # four times longer: each gap is on time for the interval now in force

    await _run_loop(config, 1.6, repo, between=change)
    assert late == []
    await repo.close()


# --- rollups, uptime strips and the inspector, at every interval -----------------------------------------------------------

def _samples(interval, start, minutes, lost_minutes, target="a"):
    """A probe every `interval` seconds for `minutes` minutes from `start`; every probe of the minutes in
    `lost_minutes` (offsets from the start) is lost."""
    out, t = [], start
    while t < start + minutes * 60:
        lost = int((t - start) // 60) in lost_minutes
        out.append(ProbeSample(target_id=target, timestamp=t, success=not lost, latency_ms=None if lost else 5.0,
                               packet_loss_pct=100.0 if lost else 0.0, jitter_ms=None, error="x" if lost else None))
        t += interval
    return out


@pytest.mark.parametrize("interval", INTERVALS)
async def test_every_probe_lost_minute_is_dead_and_the_strip_shows_the_outage_at_every_interval(tmp_path, interval):
    repo = DatabaseRepository(db_path=str(tmp_path / "u.db"), sample_flush_seconds=0)
    await repo.initialize()
    repo.probe_interval_seconds = interval  # what the probing component keeps current
    start = BLOCK * 900.0 - 900  # the previous block: 15 minutes, minutes 3 and 4 entirely lost
    await repo.insert_samples(_samples(interval, start, 15, {3, 4}))
    await repo.flush_samples()
    async with repo.read() as db:
        async with db.execute("SELECT dead_minutes, n FROM rollup_15m") as cur:
            (dead, sent), = await cur.fetchall()
    assert dead == 2 and sent == 15 * 60 // interval  # the quarter row counted both dead minutes
    out = await build_uptime(repo, AppConfig(prober={"interval_seconds": interval}), "24h", now=NOW)
    (row,) = out["targets"]
    assert row["cells"][-2] == "outage"  # two dead minutes make an outage block
    assert row["cells"][-1] == "no_data"  # the current block has no probe yet
    await repo.close()


@pytest.mark.parametrize("interval", INTERVALS)
async def test_a_dead_minute_is_read_the_same_from_the_minutes_at_the_edge_of_a_window(tmp_path, interval):
    from pengumon.reports.queries import block_stats
    repo = DatabaseRepository(db_path=str(tmp_path / "e.db"), sample_flush_seconds=0)
    await repo.initialize()
    repo.probe_interval_seconds = interval
    start = BLOCK * 900.0 - 900
    await repo.insert_samples(_samples(interval, start, 15, {3, 4}))
    await repo.flush_samples()
    whole = await block_stats(repo, start, start + 900, 900)  # from the quarter row
    edge = await block_stats(repo, start + 120, start + 420, 900)  # minutes 2-6: read from the per-minute rows
    assert whole[0]["dead_minutes"] == 2 and edge[0]["dead_minutes"] == 2
    await repo.close()


def test_a_block_of_too_few_probes_is_no_data_and_what_is_enough_depends_on_the_interval():
    assert block_status(3, 3, 0, min_probes=uptime_min_probes(900, 60)) == "no_data"  # 3 of 7.5 needed
    assert block_status(8, 8, 0, min_probes=uptime_min_probes(900, 60)) == "ok"
    assert block_status(8, 8, 0) == "no_data"  # the 1 s rule: ten probes


@pytest.mark.parametrize("interval", INTERVALS)
async def test_the_inspector_sees_the_lost_probes_of_an_incident_at_every_interval(interval):
    t0 = 50_000.0
    samples = [{"target_id": "pub", "timestamp": t0 + k * interval, "success": 0 if 6 <= k < 9 else 1,
                "latency_ms": None if 6 <= k < 9 else 5.0, "packet_loss_pct": 0.0, "jitter_ms": None, "error": None}
               for k in range(0, 20)]
    start = t0 + 6 * interval  # the first lost probe: where the tracker back-dates an incident
    end = t0 + 8 * interval
    incident = Incident(start_time=start, end_time=end, duration_seconds=end - start, resolved=True,
                        cause_code="unknown_outage", title="t", description="d", severity="critical")
    win = inspection_window(incident.start_time, incident.end_time, incident.duration_seconds, True)
    (hop,) = hop_impact(samples, win.calc_start, win.calc_end, [])
    assert hop.sent - hop.received == 3  # all three lost probes are in the table, whatever their spacing
    assert hop.loss_pct == pytest.approx(100.0 * 3 / hop.sent, abs=0.06) and hop.sent >= 3


# --- the one-time import of config.yaml's interval ----------------------------------------------------------------------

async def _upgrade(tmp_path, text, db="pengumon.db"):
    """A manager on a store initialized before 0.28 (it has no interval), started with this config.yaml."""
    manager, _ = await make_manager(tmp_path, text, load=False, db=db)
    if not await manager.repository.settings_initialized():
        await manager.repository.initialize_settings(SettingsDocument(), "Started with the defaults", [])
    await manager.load()
    return manager


@pytest.mark.parametrize("file, stored", [(1.0, 1), (5, 5), (5.0, 5), (60, 60), (3, 2), (4, 5), (7, 5), (0.5, 1), (0.05, 1),
                                          (20, 10), (25, 30), (45, 30), (46, 60)])
async def test_config_yamls_interval_is_stored_once_snapped_to_a_choice(tmp_path, file, stored):
    manager = await _upgrade(tmp_path, f"prober:\n  interval_seconds: {file}\n")
    assert manager.document().values["prober.interval_seconds"] == stored
    assert manager.config.prober.interval_seconds == stored  # live at once
    (top, *_), _ = (await manager.versions(), None)
    assert (top["revision"], top["source"]) == (2, "import") and f"Probe every {stored} s" in top["summary"]


async def test_a_value_that_moved_says_so_in_the_log(tmp_path, caplog):
    with caplog.at_level(logging.INFO, logger="pengumon.settings"):
        await _upgrade(tmp_path, "prober:\n  interval_seconds: 3\n")
    assert "prober.interval_seconds from config.yaml: 2 s (config.yaml sets 3 s; the choices are 1, 2, 5, 10, 30, 60 s, so it moved to 2 s)" in caplog.text
    caplog.clear()
    (tmp_path / "ok").mkdir()
    with caplog.at_level(logging.INFO, logger="pengumon.settings"):
        await _upgrade(tmp_path / "ok", "prober:\n  interval_seconds: 10\n")
    assert "from config.yaml: 10 s;" in caplog.text and "so it moved to" not in caplog.text


async def test_the_file_is_ignored_after_the_import_and_the_log_says_so(tmp_path, caplog):
    first = await _upgrade(tmp_path, "prober:\n  interval_seconds: 10\n")
    assert first.config.prober.interval_seconds == 10
    # the same database again, as at the next start, with another value in the file:
    with caplog.at_level(logging.INFO, logger="pengumon.settings"):
        second, _ = await make_manager(tmp_path, "prober:\n  interval_seconds: 2\n")
    assert second.config.prober.interval_seconds == 10 and second.revision == first.revision
    assert "prober.interval_seconds: ignored, the probe interval is a setting now" in caplog.text


async def test_a_setting_changed_in_settings_is_not_taken_back_by_the_file_even_after_a_reset(tmp_path):
    manager = await _upgrade(tmp_path, "prober:\n  interval_seconds: 10\n")
    await manager.apply(manager.revision, {"prober.interval_seconds": None}, {})  # reset to the default
    again, _ = await make_manager(tmp_path, "prober:\n  interval_seconds: 10\n")
    assert again.config.prober.interval_seconds == 1 and "prober.interval_seconds" not in again.document().values


async def test_a_file_without_an_interval_imports_nothing_and_a_later_edit_of_it_is_ignored(tmp_path):
    manager = await _upgrade(tmp_path, "classifier:\n  busy_load_pct: 70\n")
    assert "prober.interval_seconds" not in manager.document().values and manager.revision == 1
    later, _ = await make_manager(tmp_path, "prober:\n  interval_seconds: 30\n")
    assert later.config.prober.interval_seconds == 1 and later.revision == 1


async def test_a_fresh_store_takes_a_valid_value_from_the_first_import_and_snaps_an_invalid_one(tmp_path):
    fresh, _ = await make_manager(tmp_path, "prober:\n  interval_seconds: 30\n")
    assert fresh.document().values["prober.interval_seconds"] == 30 and fresh.revision == 1
    (tmp_path / "x").mkdir()
    other, _ = await make_manager(tmp_path / "x", "prober:\n  interval_seconds: 0.2\n")
    assert other.document().values["prober.interval_seconds"] == 1 and other.revision == 2


async def test_importing_a_yaml_in_settings_treats_the_interval_as_a_normal_setting(tmp_path):
    manager, _ = await make_manager(tmp_path, "")
    draft = await manager.import_yaml("prober:\n  interval_seconds: 5\n  dns_interval_seconds: 10\n")
    assert draft["values"]["prober.interval_seconds"] == 5 and draft["values"]["prober.dns_interval_seconds"] == 10


# --- saving the interval --------------------------------------------------------------------------------------------------

async def test_a_save_applies_the_interval_and_the_dns_interval_to_the_live_config(tmp_path):
    manager, _ = await make_manager(tmp_path, "")
    applied = await manager.apply(manager.revision, {"prober.interval_seconds": 10, "prober.dns_interval_seconds": 30}, {})
    assert manager.config.prober.interval_seconds == 10 and manager.config.prober.dns_interval_seconds == 30
    assert "Probe every 1 s → 10 s" in applied.summary and "DNS lookups run with every probe round → 30 s" in applied.summary


async def test_the_save_refuses_a_value_that_is_not_a_choice(tmp_path):
    from pengumon.settings.manager import SettingsInvalid
    manager, _ = await make_manager(tmp_path, "")
    for bad in (3, 0.05, 120):
        with pytest.raises(SettingsInvalid) as e:
            await manager.apply(manager.revision, {"prober.interval_seconds": bad}, {})
        assert e.value.errors[0]["where"] == "values.prober.interval_seconds"


async def test_a_dns_interval_the_new_probe_interval_does_not_fit_stops_the_save_until_fixed(tmp_path):
    from pengumon.settings.manager import SettingsInvalid
    manager, _ = await make_manager(tmp_path, "")
    await manager.apply(manager.revision, {"prober.interval_seconds": 2, "prober.dns_interval_seconds": 10}, {})
    with pytest.raises(SettingsInvalid) as e:
        await manager.apply(manager.revision, {"prober.interval_seconds": 30}, {})  # 10 s lookups on a 30 s round
    assert e.value.errors == [{"where": "values.prober.interval_seconds",
                               "message": "“DNS lookups run” must not be shorter than the probe interval (30 s)"}]
    assert manager.config.prober.interval_seconds == 2  # nothing was applied
    await manager.apply(manager.revision, {"prober.interval_seconds": 30, "prober.dns_interval_seconds": 60}, {})
    assert manager.config.prober.interval_seconds == 30


async def test_the_dns_interval_can_go_back_to_every_round(tmp_path):
    manager, _ = await make_manager(tmp_path, "")
    await manager.apply(manager.revision, {"prober.dns_interval_seconds": 10}, {})
    await manager.apply(manager.revision, {"prober.dns_interval_seconds": None}, {})
    assert manager.config.prober.dns_interval_seconds is None


async def test_the_rules_are_summarized_in_probes(tmp_path):
    manager, _ = await make_manager(tmp_path, "")
    applied = await manager.apply(manager.revision, {"classifier.consecutive_failures_for_incident": 5,
                                                     "classifier.window_probes": 20}, {})
    assert "Loss outage starts after 3 lost probes in a row → 5 lost probes in a row" in applied.summary
    assert "Judge each hop by its last 10 probes → 20 probes" in applied.summary


async def test_the_interval_reaches_the_repository_and_the_dead_minute_rule_through_the_hub(tmp_path):
    from pengumon.engine import probing
    from pengumon.settings.hub import SettingsHub

    hub = SettingsHub()
    manager, _ = await make_manager(tmp_path, "", hub=hub)
    probing.subscribe(hub, manager.config, _path_prober(), manager.repository)
    await manager.apply(manager.revision, {"prober.interval_seconds": 10}, {})
    assert manager.repository.probe_interval_seconds == 10
