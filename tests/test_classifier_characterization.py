"""Characterization of the outage classifier: a long scripted scenario recorded round by round.

The scenario drives a real NetworkProber and the classifier with a fake clock (one second per round), a fake
repository and a scripted line load, and records for every round the diagnosis, the active incident, the failure
counters and the per-target status, plus every repository call, callback and path-trace start. The result is
compared with tests/golden/classifier_characterization/<scenario>.json: one file per config (`default`,
`two_failures`, `fallback`, each with its own "texts" list) and one for the status matrix.

Regenerate only on purpose: PENGUMON_REGEN_GOLDEN=1 python -m pytest tests/test_classifier_characterization.py
"""

import asyncio
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Optional

from pengumon.engine.classifier_settings import ClassifierConfig
from pengumon.engine.prober_settings import DnsProbeConfig
from pengumon.engine import classifier as classifier_module
from pengumon.engine.classifier import OutageClassifier
from pengumon.engine.load import LoadReading
from pengumon.engine.prober import NetworkProber
from pengumon.incidents.taxonomy import ISP_LINE_KINDS, LOAD_EXPLAINABLE_KINDS
from pengumon.models.probes import ProbeSample, TargetConfig

from golden_files import check_golden

T0 = 1_000_000.0

# key (also the target id), category, name, address, enabled
TARGETS = [
    ("local", "local", "Local Router", "192.168.1.1", True),
    ("gw", "gateway", "Modem Gateway", "192.168.100.1", True),
    ("isp_a", "isp", "ISP Hub A", "198.51.100.1", True),
    ("isp_b", "isp", "ISP Hub B", "198.51.100.3", True),
    ("pub_a", "public", "Cloudflare", "1.1.1.1", True),
    ("pub_b", "public", "Google", "8.8.8.8", True),
    ("spare", "custom", "Spare Probe", "9.9.9.9", False),  # disabled: never probed, never in a category
]
# "ghost" answers but is not registered with the prober (its samples reach the classifier without stats)
ORDER = ["local", "gw", "isp_a", "ghost", "isp_b", "pub_a", "pub_b", "dns"]
BASE = {"local": 1.0, "gw": 2.0, "isp_a": 9.0, "isp_b": 11.0, "pub_a": 14.0, "pub_b": 16.0, "dns": 20.0, "ghost": 5.0}

# Every public target disabled (but still answering): the classifier falls back to the current round's samples.
FALLBACK_TARGETS = [
    ("local", "local", "Local Router", "192.168.1.1", True),
    ("gw", "gateway", "Modem Gateway", "192.168.100.1", True),
    ("isp_a", "isp", "ISP Hub A", "198.51.100.1", True),
    ("px1", "public", "Public One", "1.1.1.1", False),
    ("px2", "public", "Public Two", "8.8.8.8", False),
    ("px3", "public", "Public Three", "9.9.9.9", False),
]
FALLBACK_ORDER = ["local", "gw", "isp_a", "ghost", "px1", "px2", "px3", "dns"]
FALLBACK_BASE = {**BASE, "px1": 14.0, "px2": 15.0, "px3": 16.0}

STATUS_LETTER = {"up": "u", "degraded": "g", "down": "x"}


# --- scripting ---------------------------------------------------------------------------------------

def C(value):
    """Constant latency (None: always lost)."""
    return lambda k, base: value


def L(period, lost=1):
    """Lost in the first `lost` rounds of every `period` rounds, otherwise the target's normal latency."""
    return lambda k, base: None if k % period < lost else base


def A(a, b):
    """Alternating latency, for jitter."""
    return lambda k, base: a if k % 2 == 0 else b


@dataclass
class Phase:
    name: str
    rounds: int
    overrides: dict = field(default_factory=dict)
    load: object = None  # None, a percentage, or a callable of the round number in the phase
    skip: float = 0.0  # seconds the clock jumps before the phase
    upstream_age: Optional[float] = None  # clock set so that the first round is this old after the last upstream issue


def P(name, rounds, load=None, skip=0.0, upstream_age=None, **overrides):
    expanded = {}
    for key, spec in overrides.items():
        if key == "pub":
            expanded["pub_a"] = expanded["pub_b"] = spec
        elif key == "isp":
            expanded["isp_a"] = expanded["isp_b"] = spec
        else:
            expanded[key] = spec
    return Phase(name, rounds, expanded, load, skip, upstream_age)


def dns_in_blocks(k, base):
    """Loss in rounds 0-7, 16-23, 32-39 (every other round), healthy in between."""
    return None if (k // 8) % 2 == 0 and k % 2 == 0 else base


def load_rising(k):
    return 70.0 if k < 8 else 95.0


def load_appearing(k):
    return None if k < 8 else 40.0


PHASES = [
    P("nominal", 12),
    # --- local router
    P("local latency", 14, local=C(300.0)),
    P("recover", 14),
    P("local jitter", 6, local=A(1.0, 40.0), ghost=C(200.0)),
    P("recover", 14),
    P("local loss 20%", 15, local=L(5), ghost=L(4, 2)),
    P("local loss 50%", 15, local=L(2)),
    P("recover", 16),
    P("local loss 10%", 12, local=L(10)),
    P("recover", 12),
    # --- gateway
    P("gateway latency", 14, gw=C(200.0)),
    P("recover", 14),
    P("gateway jitter", 8, gw=A(1.0, 60.0)),
    P("recover", 14),
    P("gateway loss 25%", 14, gw=L(4)),
    P("gateway loss 40%", 14, gw=L(5, 2)),
    P("gateway loss 50%", 14, gw=L(2), ghost=C(None)),
    P("recover", 16),
    # --- ISP hops: loss that stops at the hop is not a fault
    P("isp hub A loss", 14, isp_a=L(2)),
    P("isp hub B loss", 14, isp_b=L(2)),
    P("isp both hubs 50%", 14, isp=L(2)),
    P("recover", 14),
    # --- ISP loss that reaches the public targets
    P("isp loss 17% with public loss", 12, isp_a=L(3), pub=L(3), ghost=C(None)),
    P("isp and public loss 50%", 14, isp=L(2), pub=L(2)),
    P("recover", 16),
    P("isp loss 20% public 10%", 14, isp_a=L(5, 2), pub_a=L(5)),
    P("recover", 14),
    P("isp loss 20% with dns loss", 12, isp_a=L(5, 2), dns=L(10, 3)),
    P("recover", 14),
    # --- public targets
    P("public loss 33%", 14, pub=L(3), ghost=C(200.0)),
    P("public loss 50%", 14, pub=L(2)),
    P("recover", 16),
    P("public latency", 14, pub=C(150.0)),
    P("recover", 14),
    P("isp latency", 14, isp=C(120.0)),
    P("recover", 14),
    P("public jitter", 8, pub=A(5.0, 110.0)),
    P("recover", 14),
    # --- DNS (only judged when the layers below are healthy and the last upstream issue is over 90 s old)
    P("dns loss within the cooldown", 12, dns=L(2)),
    P("dns outage at the cooldown boundary", 14, upstream_age=88.0, dns=L(2)),
    P("dns drops", 14, dns=L(3)),
    P("recover", 14),
    P("dns slow", 14, dns=C(500.0)),
    P("recover", 14),
    # --- load
    P("busy latency", 14, load=95.0, pub=C(250.0)),
    P("busy recover", 14, load=95.0),
    P("busy isp-only loss", 14, load=95.0, isp=L(2)),
    P("busy recover", 14, load=95.0),
    P("load at the busy threshold", 6, load=60.0, pub=C(250.0)),
    P("load just below the busy threshold", 6, load=59.9, pub=C(250.0)),
    P("recover", 14),
    P("real loss, load rising", 20, load=load_rising, pub=L(3)),
    P("recover", 16, load=40.0),
    P("real loss, load below busy", 16, load=30.0, pub=L(3)),
    P("recover", 16, load=30.0),
    P("real loss, load appearing", 16, load=load_appearing, pub=L(3)),
    P("recover", 16),
    P("latency incident", 16, pub=C(150.0)),
    P("busy hides it", 2, load=90.0, pub=C(150.0)),
    P("load gone", 1, pub=C(150.0)),
    P("busy again", 6, load=90.0, pub=C(150.0)),
    P("recover", 14, load=90.0),
    # --- latency and loss rounds interleaved
    P("latency with dns loss in blocks", 40, pub=C(150.0), dns=dns_in_blocks),
    P("recover", 16),
]

FALLBACK_PHASES = [
    P("nominal", 6),
    P("one public lost", 6, px1=C(None)),
    P("public slow", 6, px1=C(100.0), px2=C(100.0), px3=C(100.0)),
    P("slow and one lost", 4, px1=C(None), px2=C(100.0), px3=C(100.0)),
    P("nominal", 6),
]

CONFIGS = [
    ("default", ClassifierConfig(), TARGETS, ORDER, BASE, PHASES),
    ("two_failures", ClassifierConfig(consecutive_failures_for_incident=2), TARGETS, ORDER, BASE, PHASES),
    ("fallback", ClassifierConfig(), FALLBACK_TARGETS, FALLBACK_ORDER, FALLBACK_BASE, FALLBACK_PHASES),
]

CONFIG_NAMES = [config[0] for config in CONFIGS]

EXPECTED_TITLES = {
    "Local Router Disconnect",
    "Local Router Packet Loss",
    "Local Router Latency Spike",
    "Local Router to Modem Gateway Link Failure",
    "Modem Gateway Packet Loss",
    "Modem Gateway Latency Delay",
    "ISP Line / ISP Hub A Outage",
    "ISP Line / ISP Hub A Packet Loss",
    "ISP Upstream Backbone Packet Loss",
    "ISP Latency Spikes / Jitter",
    "DNS Resolution Outage",
    "DNS Query Packet Drops",
    "Slow DNS Queries",
    "Line Busy: Delay From Heavy Load",
    "Line Busy: ISP Hops Deprioritise Ping",
    "ISP Hop Deprioritises Ping",
    "All Network Hops Operational",
}


# --- harness -----------------------------------------------------------------------------------------

class Clock:
    def __init__(self, now):
        self.now = now

    def time(self):
        return self.now


class Texts:
    """Long strings are stored once per scenario, in its "texts" list, and referenced as #n."""

    def __init__(self):
        self.items = []

    def ref(self, text):
        if text not in self.items:
            self.items.append(text)
        return f"#{self.items.index(text)}"


class ScriptedLoad:
    def __init__(self):
        self.pct = None
        self.calls = 0

    def current(self, ts=None):
        self.calls += 1
        if self.pct is None:
            return None
        return LoadReading(pct=self.pct, rx_mbps=self.pct * 10.0, tx_mbps=3.0, down_mbps=1000.0, up_mbps=50.0,
                           down_source="configured", up_source="configured")


class FakeRepository:
    def __init__(self, log, texts):
        self.log = log
        self.texts = texts
        self.next_id = 1

    async def create_incident(self, incident):
        incident_id = self.next_id
        self.next_id += 1
        self.log("repo.create", full_snapshot(incident, self.texts))
        return incident_id

    async def resolve_incident(self, incident_id, end_time):
        self.log("repo.resolve", f"id={incident_id} end={end_time - T0!r}")

    async def set_incident_load(self, incident_id, load_pct, expected=False):
        self.log("repo.load", f"id={incident_id} load={load_pct!r} expected={int(expected)}")

    async def update_incident_classification(self, *args):
        pass  # deliberately unrecorded: the golden predates it, and the incident's own state is snapshotted anyway


def _offset(value):
    return None if value is None else value - T0


def full_snapshot(incident, texts):
    return (
        f"id={incident.id} start={_offset(incident.start_time)!r} end={_offset(incident.end_time)!r} "
        f"dur={incident.duration_seconds!r} sev={incident.severity} cause={incident.cause_code} "
        f"title={texts.ref(incident.title)} desc={texts.ref(incident.description)} "
        f"aff={','.join(incident.affected_targets)} resolved={int(incident.resolved)} load={incident.load_pct!r} "
        f"expected={int(incident.expected)} trace={incident.path_trace!r}"
    )


def short_snapshot(incident, texts):
    if incident is None:
        return "-"
    return (
        f"id={incident.id} start={_offset(incident.start_time)!r} sev={incident.severity} "
        f"cause={incident.cause_code} title={texts.ref(incident.title)} desc={texts.ref(incident.description)} "
        f"load={incident.load_pct!r} aff={','.join(incident.affected_targets)}"
    )


async def run_scenario(monkeypatch, config, targets, order, base, phases, texts, tap=None):
    clock = Clock(T0)
    monkeypatch.setattr(classifier_module, "time", SimpleNamespace(time=clock.time))
    events, rounds = [], []
    state = {"round": 0}

    def log(kind, text=""):
        events.append(f"r{state['round']:03d} {kind} {text}".rstrip())

    async def trace_stub(self, incident_id):
        log("trace", str(incident_id))

    monkeypatch.setattr(OutageClassifier, "_trigger_path_trace", trace_stub)

    prober = NetworkProber()
    for key, category, name, address, enabled in targets:
        prober.register_target(TargetConfig(id=key, name=name, category=category, address=address, enabled=enabled))
    prober.register_dns_target(DnsProbeConfig(id="dns", name="DNS Lookup", domain="example.com"))

    load = ScriptedLoad()
    classifier = OutageClassifier(
        config=config, repository=FakeRepository(log, texts), load=load,
        line_technology="docsis",  # the recorded network is a cable (DOCSIS) line
        on_incident_started=lambda inc: log("cb.started", full_snapshot(inc, texts)),
        on_incident_resolved=lambda inc: log("cb.resolved", full_snapshot(inc, texts)),
    )
    if tap is not None:
        tap(classifier)

    titles, descriptions = set(), set()
    for phase in phases:
        if phase.upstream_age is not None:
            clock.now = classifier.last_upstream_issue_ts + phase.upstream_age - 1.0
        clock.now += phase.skip
        for k in range(phase.rounds):
            state["round"] += 1
            clock.now += 1.0
            load.pct = phase.load(k) if callable(phase.load) else phase.load
            samples = []
            for key in order:
                spec = phase.overrides.get(key)
                latency = spec(k, base[key]) if spec else base[key]
                samples.append(ProbeSample(
                    target_id=key, timestamp=clock.now, success=latency is not None, latency_ms=latency,
                    packet_loss_pct=0.0 if latency is not None else 100.0,
                ))
            for sample in samples:
                prober._update_target_stats(sample)
            diagnosis = await classifier.evaluate(samples, prober.stats)
            await asyncio.sleep(0)  # lets the (stubbed) path-trace task run and be discarded
            await asyncio.sleep(0)
            assert classifier.last_diagnosis is diagnosis
            titles.add(diagnosis.title)
            descriptions.add(diagnosis.description)
            last_failure = classifier.last_failure_ts
            rounds.append(
                f"{state['round']:03d} {phase.name!r} t={int(clock.now - T0)} {diagnosis.status} "
                f"{diagnosis.cause_code} title={texts.ref(diagnosis.title)} desc={texts.ref(diagnosis.description)} "
                f"load={diagnosis.load_pct!r} same={int(diagnosis.active_incident is classifier.active_incident)} "
                f"| inc={short_snapshot(classifier.active_incident, texts)} "
                f"| cnt={classifier.failure_counter},{classifier.loss_failure_counter},"
                f"{classifier.latency_failure_counter},{classifier.recovery_counter} "
                f"lastfail={_offset(last_failure) if last_failure else 0.0!r} "
                f"| st={''.join(STATUS_LETTER[prober.stats[key].status] for key in prober.stats)}"
            )
    summary = (
        f"rounds={state['round']} load_calls={load.calls} background={len(classifier._background)} "
        f"upstream_issue={_offset(classifier.last_upstream_issue_ts)!r} active={classifier.active_incident is not None}"
    )
    return {"rounds": rounds, "events": events, "summary": summary}, titles, descriptions


MATRIX_PATTERNS = (
    [(f"const{v}", [v] * 12) for v in (1.0, 10.0, 20.0, 20.1, 30.0, 30.1, 60.0, 60.1, 100.0, 100.1, 500.0)]
    + [(f"jitter{j}", [0.5 if i % 2 == 0 else 0.5 + j for i in range(12)])
       for j in (10.0, 10.1, 15.0, 15.1, 25.0, 25.1, 40.0, 40.1)]
    + [(f"lost{n}of10", [None if 2 <= i < 2 + n else 1.0 for i in range(12)]) for n in (1, 2, 4, 5)]
    + [("short5_1lost", [1.0, 1.0, 1.0, 1.0, None]), ("short3_1lost", [1.0, 1.0, None]),
       ("short2_1lost", [1.0, None]), ("short1_lost", [None])]
)


def status_matrix():
    """The per-target colour (up / degraded / down) for each category, at and around the boundaries."""
    rows = []
    for category in ("local", "gateway", "isp", "public", "dns", "custom"):
        for name, values in MATRIX_PATTERNS:
            prober = NetworkProber()
            if category == "dns":
                prober.register_dns_target(DnsProbeConfig(id="t", name="T", domain="example.com"))
            else:
                prober.register_target(TargetConfig(id="t", name="T", category=category, address="192.0.2.1"))
            for value in values:
                prober._update_target_stats(ProbeSample(
                    target_id="t", timestamp=0.0, success=value is not None, latency_ms=value,
                ))
            stat = prober.stats["t"]
            rows.append(f"{category}|{name}|{stat.status}|avg={stat.avg_ms}|jitter={stat.jitter_ms}|"
                        f"loss={stat.loss_recent_pct}")
    return rows


async def build_golden(monkeypatch, tap=None):
    golden, titles, descriptions = {}, set(), set()
    for name, config, targets, order, base, phases in CONFIGS:
        texts = Texts()
        run, run_titles, run_descriptions = await run_scenario(
            monkeypatch, config, targets, order, base, phases, texts, tap=tap)
        golden[name] = {**run, "texts": texts.items}
        if name == "default":
            titles, descriptions = run_titles, run_descriptions
    golden["status_matrix"] = status_matrix()
    return golden, titles, descriptions


# --- tests -------------------------------------------------------------------------------------------

async def test_scenario_reaches_every_verdict_and_state_path(monkeypatch):
    golden, titles, descriptions = await build_golden(monkeypatch)
    assert EXPECTED_TITLES <= titles, sorted(EXPECTED_TITLES - titles)
    assert any("coaxial line sync drop" in d for d in descriptions)
    assert any("peering or backbone link failure" in d for d in descriptions)
    assert any("33.33333333333333%" in d for run in CONFIG_NAMES for d in golden[run]["texts"])  # unrounded fallback loss
    for name in ("default", "two_failures"):
        events = "\n".join(golden[name]["events"])
        for kind in ("repo.create", "repo.resolve", "repo.load", "cb.started", "cb.resolved", "trace"):
            assert f" {kind}" in events, (name, kind)
    assert golden["default"]["summary"].split()[2] == "background=0"


async def test_classifier_matches_the_recorded_golden(monkeypatch):
    golden, _, _ = await build_golden(monkeypatch)
    check_golden("classifier_characterization", golden)


def _old_load_explainable(title):
    """Frozen copy of the title rule that engine.load used before incidents carried a kind."""
    return any(word in title for word in ("Latency", "Jitter", "Delay")) or (
        title.startswith("ISP Line /") and "Packet Loss" in title)


def _old_isp_line(title):
    """Frozen copy of the title rule the classifier used for the intermediate-hop rule."""
    return title.startswith("ISP Line /")


NON_LEGACY_KINDS = {
    "nominal", "local_down", "local_loss", "local_latency", "gateway_down", "gateway_loss", "gateway_latency",
    "isp_outage", "isp_loss", "backbone_loss", "upstream_latency", "dns_outage", "dns_loss", "dns_slow",
    "busy_latency", "busy_isp_hops", "hop_deprioritised",
}


async def test_verdict_kinds_reproduce_the_title_rules(monkeypatch):
    verdicts = []

    def tap(classifier):
        original = classifier.evaluate

        async def evaluate(samples, stats):
            diagnosis = await original(samples, stats)
            verdicts.append(classifier.last_verdict)
            return diagnosis

        classifier.evaluate = evaluate

    await build_golden(monkeypatch, tap=tap)
    assert verdicts
    titles_by_kind = {}
    for verdict in verdicts:
        assert (verdict.kind in ISP_LINE_KINDS) == _old_isp_line(verdict.title), verdict
        assert (verdict.kind in LOAD_EXPLAINABLE_KINDS) == _old_load_explainable(verdict.title), verdict
        titles_by_kind.setdefault(verdict.kind, set()).add(verdict.title)
    # A kind names a title template, not a particular rendering: the names inside the title vary by target,
    # but this scenario uses one set of names per kind.
    assert all(len(titles) == 1 for titles in titles_by_kind.values()), titles_by_kind
    assert set(titles_by_kind) == NON_LEGACY_KINDS, sorted(NON_LEGACY_KINDS ^ set(titles_by_kind))
