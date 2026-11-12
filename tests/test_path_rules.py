"""Hops that ignore ping, decided on the server (engine/path_rules.py): the rule, the prober's per-round flag, and where
it is used: the classifier's affected targets (never its verdicts), the "when it breaks" grid and the dossier."""

import asyncio
import time
from types import SimpleNamespace

import pytest

from pengumon.db.repository import DatabaseRepository
from pengumon.engine import classifier as classifier_module
from pengumon.engine.classifier import OutageClassifier
from pengumon.engine.classifier.metrics import round_metrics
from pengumon.engine.classifier_settings import ClassifierConfig
from pengumon.engine.path_rules import ignoring_ping, ignoring_ping_ids
from pengumon.engine.prober import NetworkProber
from pengumon.models.probes import ProbeSample, TargetConfig
from pengumon.reports.common import find_primary_failing_hop
from pengumon.reports.dossier import build_dossier
from pengumon.reports.loss_grid import build_loss_grid


def t(tid, category, status="up", enabled=True):
    return SimpleNamespace(target_id=tid, category=category, status=status, enabled=enabled)


# ---- the rule (the dashboard's former path.js ignoringPing, same semantics) ----

def test_a_silent_hop_with_a_later_stage_answering_ignores_ping():
    assert ignoring_ping([t("r", "local"), t("g", "gateway", "down"), t("p", "public")]) == {"g"}


def test_a_silent_hop_with_everything_after_it_silent_is_down():
    assert ignoring_ping([t("r", "local"), t("g", "gateway", "down"), t("i", "isp", "down"), t("p", "public", "down")]) == set()


def test_isp_hops_vouch_only_for_earlier_isp_hops():
    hops = [t("i1", "isp"), t("i2", "isp", "down"), t("i3", "isp")]
    assert ignoring_ping(hops) == {"i2"}
    assert ignoring_ping([t("i1", "isp"), t("i2", "isp", "down")]) == set()  # nothing after the last hop answers


def test_two_targets_of_one_local_or_gateway_category_never_vouch_for_each_other():
    assert ignoring_ping([t("r1", "local", "down"), t("r2", "local")]) == set()
    assert ignoring_ping([t("g1", "gateway", "down"), t("g2", "gateway")]) == set()


def test_public_dns_custom_and_disabled_targets_are_no_stage():
    assert ignoring_ping([t("p1", "public", "down"), t("p2", "public")]) == set()
    assert ignoring_ping([t("g", "gateway", "down"), t("d", "dns"), t("c", "custom")]) == set()
    assert ignoring_ping([t("g", "gateway", "down"), t("p", "public", enabled=False)]) == set()
    assert ignoring_ping([t("g", "gateway", "down", enabled=False), t("p", "public")]) == set()


def test_degraded_is_not_silent_and_a_degraded_later_stage_still_answers():
    assert ignoring_ping([t("g", "gateway", "degraded"), t("p", "public")]) == set()
    assert ignoring_ping([t("g", "gateway", "down"), t("p", "public", "degraded")]) == {"g"}


# ---- the prober flags it every round ----

def prober_with(*targets):
    prober = NetworkProber()
    for tid, category in targets:
        prober.register_target(TargetConfig(id=tid, name=tid, category=category, address="192.0.2.1"))
    return prober


def feed(prober, ts, lost=()):
    samples = [ProbeSample(target_id=tid, timestamp=ts, success=tid not in lost, latency_ms=None if tid in lost else 5.0,
                           packet_loss_pct=100.0 if tid in lost else 0.0) for tid in prober.stats]
    for s in samples:
        prober._update_target_stats(s)
    prober.update_path_flags()
    return samples


def test_the_prober_flags_a_hop_that_stops_answering_while_the_path_after_it_answers_and_clears_it_again():
    prober = prober_with(("r", "local"), ("i", "isp"), ("p", "public"))
    for k in range(10):
        feed(prober, 1000 + k)
    assert ignoring_ping_ids(prober) == set()
    for k in range(10):
        feed(prober, 1010 + k, lost={"i"})
    assert prober.stats["i"].status == "down" and ignoring_ping_ids(prober) == {"i"}
    for k in range(10):
        feed(prober, 1020 + k, lost={"i", "p"})  # now nothing after it answers: down, not ignoring ping
    assert ignoring_ping_ids(prober) == set()
    assert ignoring_ping_ids(None) == set()


async def test_execute_probe_round_sets_the_flag(monkeypatch):
    prober = prober_with(("g", "gateway"), ("p", "public"))
    replies = {"g": None, "p": 4.0}

    async def ping(target):
        ms = replies[target.id]
        return ProbeSample(target_id=target.id, timestamp=time.time(), success=ms is not None, latency_ms=ms)

    monkeypatch.setattr(prober, "probe_icmp", ping)
    targets = [TargetConfig(id="g", name="g", category="gateway", address="192.0.2.1"),
               TargetConfig(id="p", name="p", category="public", address="192.0.2.2")]
    for _ in range(10):
        await prober.execute_probe_round(targets=targets, dns_probes=[])
    assert prober.stats["g"].ignores_ping is True and prober.stats["p"].ignores_ping is False


# ---- the classifier: affected targets only, never its verdicts ----

def test_a_flagged_hop_is_no_affected_target_but_an_unflagged_failing_one_is():
    prober = prober_with(("r", "local"), ("i", "isp"), ("p", "public"))
    for k in range(10):
        feed(prober, 1000 + k, lost={"i"})
    samples = feed(prober, 1010, lost={"i", "r"})  # the router has only just started failing: not down, not flagged
    m = round_metrics(prober.stats, samples)
    assert prober.stats["i"].ignores_ping and not prober.stats["r"].ignores_ping
    assert m.failed_targets == ("r",)


class Repo:
    def __init__(self):
        self.created = []

    async def create_incident(self, incident):
        self.created.append(incident)
        return len(self.created)

    async def resolve_incident(self, incident_id, end_time):
        pass

    async def set_incident_load(self, *a, **k):
        pass

    async def update_incident_classification(self, *args):
        pass


async def run(monkeypatch, flags: bool):
    """Scripted rounds: warm-up, then the ISP hop stops answering (the public servers still answer), then the gateway
    and everything after it go down, then recovery. Returns every round's verdict and the incidents opened."""
    clock = SimpleNamespace(now=1_000_000.0)
    monkeypatch.setattr(classifier_module, "time", SimpleNamespace(time=lambda: clock.now))

    async def no_trace(self, incident_id):
        pass

    monkeypatch.setattr(OutageClassifier, "_trigger_path_trace", no_trace)
    prober = prober_with(("r", "local"), ("g", "gateway"), ("i", "isp"), ("p", "public"))
    repo = Repo()
    classifier = OutageClassifier(config=ClassifierConfig(), repository=repo)
    verdicts = []
    for lost, rounds in ((set(), 12), ({"i"}, 20), ({"g", "i", "p"}, 12), (set(), 14)):
        for _ in range(rounds):
            clock.now += 1.0
            samples = [ProbeSample(target_id=tid, timestamp=clock.now, success=tid not in lost,
                                   latency_ms=None if tid in lost else 5.0) for tid in prober.stats]
            for s in samples:
                prober._update_target_stats(s)
            if flags:
                prober.update_path_flags()
            d = await classifier.evaluate(samples, prober.stats)
            await asyncio.sleep(0)
            verdicts.append((d.status, d.cause_code, classifier.last_verdict.kind))
    return verdicts, repo.created


async def test_the_flag_changes_no_verdict_and_no_incident_only_who_is_affected(monkeypatch):
    plain, plain_incidents = await run(monkeypatch, flags=False)
    flagged, flagged_incidents = await run(monkeypatch, flags=True)
    assert flagged == plain
    assert [(i.start_time, i.kind) for i in flagged_incidents] == [(i.start_time, i.kind) for i in plain_incidents]
    # the gateway going down with everything after it still opens its incident, with the gateway affected
    assert any("g" in i.affected_targets for i in flagged_incidents)
    # an incident opened while the ISP hop was flagged does not name it
    assert all("i" not in i.affected_targets for i in flagged_incidents if "g" not in i.affected_targets)


# ---- the reports ----

NOON = 1_793_448_000.0


async def test_the_loss_grid_leaves_out_the_hops_that_ignore_ping(tmp_path):
    repo = DatabaseRepository(db_path=str(tmp_path / "g.db"), sample_flush_seconds=0)
    await repo.initialize()
    base = NOON - 3600 * 2
    rows = [ProbeSample(target_id="quiet", timestamp=base + i, success=False, latency_ms=None) for i in range(60)]
    rows += [ProbeSample(target_id="pub", timestamp=base + i, success=i >= 3, latency_ms=5.0 if i >= 3 else None) for i in range(60)]
    await repo.insert_samples(rows)
    every = await build_loss_grid(repo, 1, now=NOON)
    grid = await build_loss_grid(repo, 1, now=NOON, ignoring={"quiet"})
    assert (every["sent"][0][10], every["lost"][0][10]) == (120, 63)
    assert (grid["sent"][0][10], grid["lost"][0][10], grid["loss_pct"][0][10]) == (60, 3, 5.0)
    await repo.close()


def test_the_failing_hop_is_never_one_that_ignores_ping():
    hop = lambda tid, p: {"target_id": tid, "sent": 100, "loss_pct": p}
    hops = [hop("router", 0.0), hop("quiet", 100.0), hop("hub", 100.0), hop("pub", 4.0)]
    assert find_primary_failing_hop(hops)["target_id"] == "quiet"
    assert find_primary_failing_hop(hops, {"quiet"})["target_id"] == "hub"
    assert find_primary_failing_hop([hop("router", 0.0), hop("quiet", 100.0), hop("pub", 0.0)], {"quiet"}) is None


async def test_the_dossier_lists_a_hop_that_ignores_ping_but_counts_it_in_no_finding(tmp_path):
    repo = DatabaseRepository(db_path=str(tmp_path / "d.db"))
    await repo.initialize()
    now = time.time()
    samples = []
    for i in range(600):
        ts = now - 1200 + i * 2
        for tid in ("router", "modem", "hub"):
            ok = tid != "modem"  # the modem never answers ping, the hub behind it always does
            samples.append(ProbeSample(target_id=tid, timestamp=ts, success=ok, latency_ms=5.0 if ok else None))
    await repo.insert_samples(samples)
    await repo.flush_samples()  # a 24-hour read leaves the last seconds in the buffer
    cfg = SimpleNamespace(targets=[
        SimpleNamespace(id="router", name="Router", address="192.168.1.1", category="local"),
        SimpleNamespace(id="modem", name="Modem", address="192.168.100.1", category="gateway"),
        SimpleNamespace(id="hub", name="ISP Hub", address="198.51.100.5", category="isp"),
    ], dns_probes=[], classifier=SimpleNamespace(busy_load_pct=60.0))
    before = await build_dossier(repo, cfg, 24)
    after = await build_dossier(repo, cfg, 24, ignoring={"modem"})
    assert before["primary_failing_hop"] == "modem"
    assert after["primary_failing_hop"] is None
    assert "Ignores ping" in after["formatted_markdown"] and "no single failing hop was isolated" in after["formatted_markdown"].lower()
    assert "100.0% worst loss" not in after["formatted_markdown"]
    await repo.close()


# ---- hops probed by TCP or HTTP ----

def test_a_tcp_gateway_that_is_down_with_a_public_hop_answering_is_flagged_but_an_http_hop_is_no_stage():
    prober = NetworkProber()
    prober.register_target(TargetConfig(id="g", name="G", category="gateway", address="192.0.2.1", probe="tcp", port=443))
    prober.register_target(TargetConfig(id="p", name="P", category="public", address="192.0.2.2"))
    prober.register_target(TargetConfig(id="w", name="W", category="custom", address="example.com", probe="http"))
    for k in range(10):
        feed(prober, 1000 + k, lost={"g"})
    assert prober.stats["g"].ignores_ping is True  # up, "ignores its probe"; the page words it for the method
    # an HTTP hop (custom) is no stage: it neither vouches for others nor is judged
    for k in range(10):
        feed(prober, 1100 + k, lost={"w", "p"})
    assert prober.stats["w"].ignores_ping is False
