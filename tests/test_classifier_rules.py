"""The diagnosis rules (pure functions of the round metrics), branch by branch."""

import pytest

from pengumon.engine.classifier_settings import ClassifierConfig
from pengumon.engine.classifier.metrics import CATEGORIES, CategoryMetrics, RoundMetrics
from pengumon.lines import wording
from pengumon.engine.classifier.rules import apply_hop_deprioritisation, apply_load, diagnose
from pengumon.engine.classifier.verdict import NOMINAL
from pengumon.engine.load import LoadReading
from pengumon.incidents.taxonomy import ISP_LINE_KINDS
from pengumon.models.probes import TargetStats

CONFIG = ClassifierConfig()  # latency 25/15 local, 35/20 gateway, 80/45 public, dns 300 ms
NAMES = {"local": "Router", "gateway": "Modem", "isp": "Hub", "public": "Pub", "dns": "Resolver"}
LONG_AGO = 1_000_000.0  # outside the DNS cooldown
TS = 2_000_000.0


def target(category, window=()):
    return TargetStats(target_id=f"{category}-1", name=NAMES[category], category=category,
                       address=f"{category}.addr", enabled=True, recent_latencies=list(window))


def rm(failed=("x",), windows=None, absent=(), **spec):
    """RoundMetrics from {category: loss | (loss, latency, jitter)}; every category has one named target, except
    those in `absent`, which have none (a segment that is not monitored)."""
    windows = windows or {}
    cats = {}
    for category in CATEGORIES:
        value = spec.get(category, 0.0)
        loss, lat, jitter = value if isinstance(value, tuple) else (value, None, None)
        targets = () if category in absent else (target(category, windows.get(category, ())),)
        cats[category] = CategoryMetrics(category, targets, loss, lat, jitter)
    return RoundMetrics(**cats, failed_targets=tuple(failed))


def verdict_for(ts=TS, last_upstream=LONG_AGO, line=None, absent=(), **spec):
    return diagnose(rm(absent=absent, **spec), CONFIG, ts, last_upstream, *([line] if line else []))


def reading(pct):
    return LoadReading(pct=pct, rx_mbps=pct * 10, tx_mbps=1.0, down_mbps=1000.0, up_mbps=100.0,
                       down_source="configured", up_source="configured")


# (spec, kind, status, severity, cause, title)
CASES = [
    ({"local": 40.0}, "local_down", "down", "critical", "local_wifi_mesh_drop", "Router Disconnect"),
    ({"local": 15.0}, "local_loss", "degraded", "warning", "local_wifi_mesh_drop", "Router Packet Loss"),
    ({"local": (0.0, 26.0, None)}, "local_latency", "degraded", "warning", "local_wifi_mesh_drop",
     "Router Latency Spike"),
    ({"gateway": 40.0}, "gateway_down", "down", "critical", "double_nat_congestion",
     "Router to Modem Link Failure"),
    ({"gateway": 15.0}, "gateway_loss", "degraded", "warning", "double_nat_congestion", "Modem Packet Loss"),
    ({"gateway": (0.0, None, 21.0)}, "gateway_latency", "degraded", "warning", "double_nat_congestion",
     "Modem Latency Delay"),
    ({"isp": 40.0}, "isp_outage", "down", "critical", "access_line_drop", "ISP Line / Hub Outage"),
    ({"public": 40.0}, "isp_outage", "down", "critical", "access_line_drop", "ISP Line / Hub Outage"),
    ({"isp": 15.0}, "isp_loss", "degraded", "warning", "access_line_drop",
     "ISP Line / Hub Packet Loss"),
    ({"public": 15.0}, "backbone_loss", "degraded", "warning", "access_line_drop",
     "ISP Upstream Backbone Packet Loss"),
    ({"public": (0.0, 81.0, None)}, "upstream_latency", "degraded", "warning", "access_line_drop",
     "ISP Latency Spikes / Jitter"),
    ({"dns": 40.0}, "dns_outage", "down", "critical", "dns_resolver_failure", "DNS Resolution Outage"),
    ({"dns": 25.0}, "dns_loss", "degraded", "warning", "dns_resolver_failure", "DNS Query Packet Drops"),
    ({"dns": (0.0, 301.0, None)}, "dns_slow", "degraded", "warning", "dns_resolver_failure", "Slow DNS Queries"),
    ({}, "nominal", "healthy", "info", "nominal", "All Network Hops Operational"),
]


@pytest.mark.parametrize("spec,kind,status,severity,cause,title", CASES, ids=[c[1] + str(i) for i, c in enumerate(CASES)])
def test_diagnose_kinds(spec, kind, status, severity, cause, title):
    v = verdict_for(**spec)
    assert (v.kind, v.status, v.severity, v.cause, v.title) == (kind, status, severity, cause, title)
    assert v.is_fault == (status in ("degraded", "down"))
    if kind == "nominal":
        assert v is NOMINAL
    else:
        assert v.affected == ("x",)


def test_busy_and_deprioritised_kinds_come_from_the_adjustments():
    base = verdict_for(public=(0.0, 90.0, None))
    busy = apply_load(base, rm(public=(0.0, 90.0, None)), reading(70.0), 60.0, False)
    assert (busy.kind, busy.status, busy.severity, busy.cause) == ("busy_latency", "busy", "info", "load_induced_latency")
    assert busy.title == "Line Busy: Delay From Heavy Load"
    assert not busy.is_fault

    m = rm(isp=20.0)
    isp = diagnose(m, CONFIG, TS, LONG_AGO)
    assert isp.kind == "isp_loss"
    busy_isp = apply_load(isp, m, reading(70.0), 60.0, m.is_loss_or_outage(isp.status))
    assert (busy_isp.kind, busy_isp.status, busy_isp.title) == ("busy_isp_hops", "busy", "Line Busy: ISP Hops Deprioritise Ping")

    hop = apply_hop_deprioritisation(isp, m)
    assert (hop.kind, hop.status, hop.severity, hop.cause, hop.title) == (
        "hop_deprioritised", "healthy", "info", "nominal", "ISP Hop Deprioritises Ping")


def test_the_seventeen_kinds_are_all_covered():
    kinds = {case[1] for case in CASES} | {"busy_latency", "busy_isp_hops", "hop_deprioritised"}
    assert len(kinds) == 17


@pytest.mark.parametrize("category,loss,kind", [
    ("local", 14.9, "nominal"), ("local", 15.0, "local_loss"), ("local", 39.9, "local_loss"), ("local", 40.0, "local_down"),
    ("gateway", 14.9, "nominal"), ("gateway", 15.0, "gateway_loss"), ("gateway", 39.9, "gateway_loss"),
    ("gateway", 40.0, "gateway_down"),
    ("isp", 14.9, "nominal"), ("isp", 15.0, "isp_loss"), ("isp", 39.9, "isp_loss"), ("isp", 40.0, "isp_outage"),
    ("public", 14.9, "nominal"), ("public", 15.0, "backbone_loss"), ("public", 39.9, "backbone_loss"),
    ("public", 40.0, "isp_outage"),
    ("dns", 24.9, "nominal"), ("dns", 25.0, "dns_loss"), ("dns", 39.9, "dns_loss"), ("dns", 40.0, "dns_outage"),
])
def test_loss_boundaries_per_layer(category, loss, kind):
    assert verdict_for(**{category: loss}).kind == kind


def test_gateway_down_needs_the_local_router_to_answer():
    assert verdict_for(gateway=40.0, local=14.9).kind == "gateway_down"
    assert verdict_for(gateway=40.0, local=15.0).kind == "local_loss"


def test_isp_outage_detail_switches_at_thirty_percent():
    docsis = wording("docsis")
    assert "coaxial line sync drop" in verdict_for(isp=40.0, line=docsis).description
    assert "coaxial line sync drop" in verdict_for(isp=30.0, public=40.0, line=docsis).description  # via public loss
    assert "peering or backbone link failure" in verdict_for(isp=29.9, public=40.0).description
    assert "peering or backbone link failure" in verdict_for(public=40.0).description


def test_line_faults_are_worded_for_the_line_technology():
    """Cable wording only for a DOCSIS line; without a known technology the description stays generic."""
    assert verdict_for(isp=40.0).description.endswith(
        "Possible causes: the access line to the ISP going down or an outage at the ISP's first hop.")
    assert verdict_for(isp=40.0, line=wording("docsis")).description.endswith(
        "Possible causes: a coaxial line sync drop or a CMTS neighborhood outage.")
    assert verdict_for(isp=15.0, line=wording("docsis")).description.endswith(
        "Possible causes: coaxial RF plant impairment, CMTS node congestion, or the hop deprioritising ping replies.")
    assert verdict_for(isp=15.0, line=wording("fiber-not-known")).description.endswith(
        "Possible causes: access-line impairment, congestion at the ISP's first hop, or the hop deprioritising "
        "ping replies.")


# --- segments that are not monitored ---------------------------------------------------------------------

def test_an_unmonitored_segment_is_not_measured():
    assert not CategoryMetrics("gateway", (), 0.0, None, None).measured
    assert CategoryMetrics("gateway", (), 0.0, None, None, samples=3).measured  # this round's samples count
    assert CategoryMetrics("gateway", (target("gateway"),), 0.0, None, None).measured


@pytest.mark.parametrize("public, kind, status, title", [
    (100.0, "internet_outage", "down", "Internet Unreachable"),
    (20.0, "internet_loss", "degraded", "Internet Packet Loss"),
])
def test_without_lan_targets_internet_loss_is_not_blamed_on_the_line(public, kind, status, title):
    v = verdict_for(public=public, absent=("local", "gateway"))
    assert (v.kind, v.status, v.cause, v.title) == (kind, status, "unknown_outage", title)
    assert "cannot be told apart" in v.description


def test_without_lan_targets_latency_is_still_reported_as_upstream_latency():
    """A known limit: latency is not re-attributed when the LAN is not monitored (loss and outages are)."""
    assert verdict_for(public=(0.0, 200.0, None), absent=("local", "gateway")).kind == "upstream_latency"


def test_without_a_gateway_target_the_line_diagnosis_names_only_the_router():
    v = verdict_for(isp=40.0, absent=("gateway",))
    assert v.kind == "isp_outage" and "while the local router answers normally" in v.description
    v = verdict_for(isp=10.0, public=40.0, absent=("gateway",))
    assert v.description.startswith("The local router and Hub answer")
    assert "while the local router and modem answer normally" in verdict_for(isp=40.0).description


def test_a_lone_router_that_stops_answering_may_be_the_gateway_itself():
    single_box = verdict_for(local=40.0, absent=("gateway", "isp"))
    assert single_box.kind == "local_down" and single_box.description.endswith("the router itself is a possible cause.")
    assert "possible cause." not in verdict_for(local=40.0).description.split("interface.")[-1]


def test_isp_loss_wins_over_backbone_loss_at_fifteen_percent():
    assert verdict_for(isp=15.0, public=20.0).kind == "isp_loss"
    assert verdict_for(isp=14.9, public=20.0).kind == "backbone_loss"


@pytest.mark.parametrize("category", ["local", "gateway", "isp", "public"])
def test_dns_is_only_judged_while_every_other_layer_is_below_ten_percent(category):
    assert verdict_for(dns=40.0, **{category: 9.9}).kind == "dns_outage"
    assert verdict_for(dns=40.0, **{category: 10.0}).kind == "nominal"


def test_dns_cooldown_after_an_upstream_issue_is_strictly_over_ninety_seconds():
    assert verdict_for(dns=40.0, ts=190.0, last_upstream=100.0).kind == "nominal"
    assert verdict_for(dns=40.0, ts=190.5, last_upstream=100.0).kind == "dns_outage"


@pytest.mark.parametrize("category,spec_ok,spec_over,kind", [
    ("local", (0.0, 25.0, None), (0.0, 25.01, None), "local_latency"),
    ("local", (0.0, None, 15.0), (0.0, None, 15.01), "local_latency"),
    ("gateway", (0.0, 35.0, None), (0.0, 35.01, None), "gateway_latency"),
    ("gateway", (0.0, None, 20.0), (0.0, None, 20.01), "gateway_latency"),
    ("public", (0.0, 80.0, None), (0.0, 80.01, None), "upstream_latency"),
    ("public", (0.0, None, 45.0), (0.0, None, 45.01), "upstream_latency"),
    ("isp", (0.0, 80.0, None), (0.0, 80.01, None), "upstream_latency"),
    ("dns", (0.0, 300.0, None), (0.0, 300.01, None), "dns_slow"),
])
def test_latency_and_jitter_thresholds_are_strict(category, spec_ok, spec_over, kind):
    assert verdict_for(**{category: spec_ok}).kind == "nominal"
    assert verdict_for(**{category: spec_over}).kind == kind


def test_a_missing_or_zero_latency_never_triggers():
    assert verdict_for(local=(0.0, None, None), public=(0.0, 0.0, 0.0)).kind == "nominal"


def test_upstream_latency_description_uses_the_larger_of_isp_and_public():
    v = verdict_for(isp=(0.0, 90.0, 5.0), public=(0.0, 85.0, 7.0))
    assert "is 90.0ms (jitter 7.0ms)" in v.description


# --- apply_load ------------------------------------------------------------------------------------------

def test_apply_load_threshold():
    m = rm(public=(0.0, 90.0, None))
    base = diagnose(m, CONFIG, TS, LONG_AGO)
    assert apply_load(base, m, reading(59.9), 60.0, False) is base
    assert apply_load(base, m, reading(60.0), 60.0, False).kind == "busy_latency"


def test_busy_texts_embed_the_base_title_and_keep_affected():
    m = rm(public=(0.0, 90.0, None))
    base = diagnose(m, CONFIG, TS, LONG_AGO)
    busy = apply_load(base, m, reading(80.0), 60.0, False)
    assert busy.description.startswith(f"{base.title} while the line is busy (")
    assert busy.affected == base.affected


def test_loss_only_at_the_isp_hops_while_busy_is_busy_isp_hops():
    m = rm(isp=20.0)
    base = diagnose(m, CONFIG, TS, LONG_AGO)
    busy = apply_load(base, m, reading(70.0), 60.0, m.is_loss_or_outage(base.status))
    assert busy.kind == "busy_isp_hops" and busy.affected == base.affected


def test_other_loss_while_busy_stays_a_fault_with_a_suffix():
    m = rm(local=20.0)
    base = diagnose(m, CONFIG, TS, LONG_AGO)
    loaded = apply_load(base, m, reading(70.0), 60.0, m.is_loss_or_outage(base.status))
    assert loaded.kind == "local_loss" and loaded.status == "degraded" and loaded.title == base.title
    assert loaded.description.startswith(base.description + " During heavy load (70%): download ")
    assert loaded.description.endswith(".")

    m = rm(isp=20.0, public=20.0)  # loss beyond the ISP hops: not isp_loss_only
    base = diagnose(m, CONFIG, TS, LONG_AGO)
    assert apply_load(base, m, reading(70.0), 60.0, True).kind == "isp_loss"


# --- apply_hop_deprioritisation -----------------------------------------------------------------------------

def isp_verdict(m):
    v = diagnose(m, CONFIG, TS, LONG_AGO)
    assert v.kind in ISP_LINE_KINDS
    return v


def test_hop_rule_boundaries():
    m = rm(isp=40.0, public=15.0)
    assert apply_hop_deprioritisation(isp_verdict(m), m).kind == "isp_outage"      # public loss 15.0: kept
    m = rm(isp=20.0, public=10.0)
    assert apply_hop_deprioritisation(isp_verdict(m), m).kind == "isp_loss"        # exactly isp / 2: kept
    m = rm(isp=20.0, public=9.9)
    assert apply_hop_deprioritisation(isp_verdict(m), m).kind == "hop_deprioritised"
    m = rm(isp=20.0, dns=25.0)
    assert apply_hop_deprioritisation(isp_verdict(m), m).kind == "isp_loss"        # dns loss 25.0: kept
    m = rm(isp=20.0, dns=24.9)
    assert apply_hop_deprioritisation(isp_verdict(m), m).kind == "hop_deprioritised"


def test_hop_rule_result_is_healthy_with_no_affected_targets():
    m = rm(isp=20.0)
    out = apply_hop_deprioritisation(isp_verdict(m), m)
    assert (out.status, out.cause, out.severity, out.affected) == ("healthy", "nominal", "info", ())
    assert "20.0% of recent probes to the ISP hops were lost, against 0.0% to the public targets" in out.description


def _two_isp_hops(first_window, second_window):
    first = target("isp", first_window)
    second = TargetStats(target_id="isp-2", name="Hub2", category="isp", address="hub2.addr", enabled=True,
                         recent_latencies=list(second_window))
    base = rm(isp=20.0)
    isp = CategoryMetrics("isp", (first, second), 20.0, None, None)
    return RoundMetrics(base.local, base.gateway, isp, base.public, base.dns, base.failed_targets)


def test_hop_rule_names_the_hop_that_lost_the_most():
    m = _two_isp_hops([1.0, None, 1.0], [None, None, 1.0])
    out = apply_hop_deprioritisation(isp_verdict(m), m)
    assert "Hub2 drops some ping replies" in out.description


def test_hop_rule_ties_go_to_the_first_hop_in_stats_order():
    m = _two_isp_hops([None, 1.0], [None, 1.0])
    assert "Hub drops some ping replies" in apply_hop_deprioritisation(isp_verdict(m), m).description


def test_hop_rule_only_counts_the_last_ten_probes_when_naming_the_hop():
    m = _two_isp_hops([None] * 5 + [1.0] * 10, [None, 1.0, 1.0] + [1.0] * 7)
    assert "Hub2 drops some ping replies" in apply_hop_deprioritisation(isp_verdict(m), m).description


def test_hop_rule_falls_back_to_a_generic_name_when_no_hop_lost():
    m = _two_isp_hops([1.0, 1.0], [1.0])
    assert "An ISP hop drops some ping replies" in apply_hop_deprioritisation(isp_verdict(m), m).description
