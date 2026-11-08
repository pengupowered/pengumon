"""Characterization of the outage classifier on network shapes other than the owner's.

The owner's network has five segments (local router, modem gateway, ISP hops, public targets, DNS). Other homes
lack some of them: a router straight on a fiber ONT has no modem target, a combined router/modem has no separate
gateway or ISP hops, a monitor outside the LAN has no local targets. This module drives the same scripted phases
(one fault per segment, each followed by a recovery, then everything down at once) through six shapes and records
every round: diagnosis, verdict kind, active incident, per-segment metrics and the repository calls. The result is
compared with tests/golden/topology_variants/<shape>.json, one file per shape.

It records current behavior. A segment with no targets reports 0 % loss and no latency, but is not *measured*
(CategoryMetrics.measured): with no LAN target at all, failing internet targets are "Internet Unreachable" / "Internet
Packet Loss" (cause unknown_outage), not a fault on the ISP line.

Regenerate only on purpose: PENGUMON_REGEN_GOLDEN=1 python -m pytest tests/test_topology_variants.py
"""

import asyncio
from types import SimpleNamespace

from pengumon.engine.classifier_settings import ClassifierConfig
from pengumon.engine.prober_settings import DnsProbeConfig
from pengumon.engine import classifier as classifier_module
from pengumon.engine.classifier import OutageClassifier
from pengumon.engine.classifier.metrics import CATEGORIES, category_metrics, round_metrics
from pengumon.engine.prober import NetworkProber
from pengumon.models.probes import ProbeSample, TargetConfig

from golden_files import check_golden

T0 = 1_000_000.0

# key (also the target id), category, name, address
_LOCAL = ("local", "local", "Local Router", "192.168.1.1")
_GW = ("gw", "gateway", "Modem Gateway", "192.168.100.1")
_ISP_A = ("isp_a", "isp", "ISP Hub A", "198.51.100.1")
_ISP_B = ("isp_b", "isp", "ISP Hub B", "198.51.100.3")
_PUB_A = ("pub_a", "public", "Cloudflare", "1.1.1.1")
_PUB_B = ("pub_b", "public", "Google", "8.8.8.8")

SHAPES = {
    "full": [_LOCAL, _GW, _ISP_A, _ISP_B, _PUB_A, _PUB_B],
    "no_gateway": [_LOCAL, _ISP_A, _PUB_A],
    "single_box": [_LOCAL, _PUB_A],
    "no_isp": [_LOCAL, _GW, _PUB_A],
    "wan_only": [_PUB_A, _PUB_B],
    "no_dns": [_LOCAL, _GW, _ISP_A, _PUB_A],
}
HAS_DNS = {name: name != "no_dns" for name in SHAPES}
BASE = {"local": 1.0, "gw": 2.0, "isp_a": 9.0, "isp_b": 11.0, "pub_a": 14.0, "pub_b": 16.0, "dns": 20.0}

# segment name -> ids of the targets in it
GROUPS = {
    "local": ("local",), "gateway": ("gw",), "isp": ("isp_a", "isp_b"), "public": ("pub_a", "pub_b"),
    "dns": ("dns",),
}
LOST = None
SPIKE = 200.0  # well above public_latency_spike_threshold_ms (80)


def every_other(k, base):
    return LOST if k % 2 == 0 else base


# name, rounds, {segment: latency | LOST | callable(k, base)}; a segment the shape lacks is simply not faulted
PHASES = [
    ("warm-up", 12, {}),
    ("local loss", 12, {"local": LOST}),
    ("recover local", 14, {}),
    ("gateway loss", 12, {"gateway": LOST}),
    ("recover gateway", 14, {}),
    ("isp loss 1 in 2", 14, {"isp": every_other}),
    ("recover isp", 14, {}),
    ("public loss", 12, {"public": LOST}),
    ("recover public", 14, {}),
    ("quiet before dns", 90, {}),  # DNS is only judged 90 s after the last upstream issue
    ("dns timeout", 12, {"dns": LOST}),
    ("recover dns", 14, {}),
    ("public latency spike", 14, {"public": SPIKE}),
    ("recover latency", 14, {}),
    ("everything down", 12, {"local": LOST, "gateway": LOST, "isp": LOST, "public": LOST, "dns": LOST}),
    ("recover all", 14, {}),
]


class Clock:
    def __init__(self, now):
        self.now = now

    def time(self):
        return self.now


class FakeRepository:
    def __init__(self, log):
        self.log = log
        self.next_id = 1

    async def create_incident(self, incident):
        incident_id = self.next_id
        self.next_id += 1
        self.log("repo.create", f"id={incident_id} kind={incident.kind} cause={incident.cause_code} "
                                f"title={incident.title!r}")
        return incident_id

    async def resolve_incident(self, incident_id, end_time):
        self.log("repo.resolve", f"id={incident_id} end={end_time - T0!r}")

    async def set_incident_load(self, incident_id, load_pct, expected=False):
        pass

    async def update_incident_classification(self, *args):
        pass


def metrics_text(metrics):
    """category:loss_pct/latency_ms/number of targets"""
    avg = "-" if metrics.latency_ms is None else f"{metrics.latency_ms}"
    return f"{metrics.category}:{metrics.loss_pct}/{avg}/{len(metrics.targets)}"


async def run_shape(monkeypatch, shape):
    """Runs PHASES on one shape. Returns the recorded rounds and events (plain lists of strings)."""
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
    order = []
    for key, category, name, address in SHAPES[shape]:
        prober.register_target(TargetConfig(id=key, name=name, category=category, address=address))
        order.append(key)
    if HAS_DNS[shape]:
        prober.register_dns_target(DnsProbeConfig(id="dns", name="DNS Lookup", domain="example.com"))
        order.append("dns")

    classifier = OutageClassifier(config=ClassifierConfig(), repository=FakeRepository(log))
    for name, count, faults in PHASES:
        by_target = {}
        for group, spec in faults.items():
            for key in GROUPS[group]:
                by_target[key] = spec
        for k in range(count):
            state["round"] += 1
            clock.now += 1.0
            samples = []
            for key in order:
                spec = by_target.get(key, BASE[key])
                latency = spec(k, BASE[key]) if callable(spec) else spec
                samples.append(ProbeSample(
                    target_id=key, timestamp=clock.now, success=latency is not None, latency_ms=latency,
                    packet_loss_pct=0.0 if latency is not None else 100.0,
                ))
            for sample in samples:
                prober._update_target_stats(sample)
            diagnosis = await classifier.evaluate(samples, prober.stats)
            await asyncio.sleep(0)  # lets the (stubbed) path-trace task run
            await asyncio.sleep(0)
            metrics = round_metrics(prober.stats, samples)
            incident = classifier.active_incident
            active = "-" if incident is None else f"{incident.id}/{incident.kind}/{incident.cause_code}"
            rounds.append(
                f"{state['round']:03d} {name!r} {diagnosis.status} {diagnosis.cause_code} "
                f"kind={classifier.last_verdict.kind} title={diagnosis.title!r} inc={active} | "
                + " ".join(metrics_text(getattr(metrics, c)) for c in CATEGORIES)
            )
    return {"rounds": rounds, "events": events}


async def build_golden(monkeypatch):
    return {shape: await run_shape(monkeypatch, shape) for shape in SHAPES}


# --- tests -------------------------------------------------------------------------------------------

async def test_topology_variants_match_the_recorded_golden(monkeypatch):
    check_golden("topology_variants", await build_golden(monkeypatch))


def test_empty_category_is_not_measured():
    """A category with no targets and no samples has 0 % loss and no latency, and says it is not measured, so the
    rules do not read it as a healthy segment."""
    metrics = category_metrics("gateway", (), [])
    assert metrics.loss_pct == 0.0
    assert metrics.latency_ms is None
    assert metrics.targets == () and not metrics.measured


async def test_wan_only_public_and_dns_failure_is_not_blamed_on_the_line(monkeypatch):
    """With no LAN targets, losing every public target and DNS at once ("everything down") is "Internet
    Unreachable" (cause unknown_outage): nothing shows the local network working, so the ISP line is not blamed."""
    run = await run_shape(monkeypatch, "wan_only")
    creates = [e for e in run["events"] if "repo.create" in e]
    assert creates[-1].endswith("id=4 kind=internet_outage cause=unknown_outage title='Internet Unreachable'")
    last_down = [r for r in run["rounds"] if "'everything down'" in r][-1]
    assert " down unknown_outage kind=internet_outage " in last_down
    assert "local:0.0/-/0 gateway:0.0/-/0 isp:0.0/-/0 public:100.0/-/2 dns:100.0/-/1" in last_down


async def test_single_box_local_router_loss_is_a_local_disconnect(monkeypatch):
    """With only local + public + DNS targets, a router that stops answering is a local_down incident with the
    local_wifi_mesh_drop cause (cause codes are stable); its description names the router itself as a possible
    cause, since it may also be the internet gateway (tests/test_classifier_rules.py checks the text)."""
    run = await run_shape(monkeypatch, "single_box")
    assert run["events"][0].startswith("r016 repo.create id=1 kind=local_down cause=local_wifi_mesh_drop ")
    local_down = [r for r in run["rounds"] if "'local loss'" in r][-1]
    assert " down local_wifi_mesh_drop kind=local_down " in local_down
    assert "local:100.0/-/1 gateway:0.0/-/0 isp:0.0/-/0" in local_down
