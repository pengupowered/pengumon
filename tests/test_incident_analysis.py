"""pengumon.analysis.incident: the pure decisions of the incident inspector."""

from types import SimpleNamespace

import pytest

from pengumon.analysis.incident import (
    HopImpact,
    IncidentInspection,
    docsis_correlation,
    hop_impact,
)
from pengumon.incidents.model import Incident
from pengumon.lines.docsis_model import DocsisChannel, DocsisSignalSnapshot


def sample(target, ts, ok=True, latency=10.0, jitter=1.0):
    return {"target_id": target, "timestamp": ts, "success": 1 if ok else 0,
            "latency_ms": latency if ok else None, "packet_loss_pct": 0.0 if ok else 100.0,
            "jitter_ms": jitter if ok else None, "error": None if ok else "timeout"}


def target(tid, name=None, category=None):
    t = SimpleNamespace(id=tid, name=name or tid.title())
    if category:
        t.category = category
    return t


def hop(tid="gateway", category="gateway", loss=0.0):
    return HopImpact(target_id=tid, name=tid, category=category, sent=100, received=int(100 - loss), loss_pct=loss,
                     avg_ms=1.0, min_ms=1.0, max_ms=1.0, jitter_ms=None)


def chan(i, direction="downstream", snr=38.0, recent=0, total=0):
    return DocsisChannel(channel_id=i, direction=direction, frequency_mhz=600.0 + i, power_dbmv=1.0, snr_db=snr,
                         uncorrectables=total, uncorrectables_recent=recent)


def snap(ts=1000.0, status="healthy", channels=(), recent=0):
    return DocsisSignalSnapshot(timestamp=ts, status=status, channels=list(channels), uncorrectables_recent=recent)


def incident(duration=30.0, start=1000.0):
    return Incident(start_time=start, end_time=start + (duration or 0), duration_seconds=duration,
                    cause_code="unknown_outage", title="t", description="d", severity="warning")


# --- hop_impact ----------------------------------------------------------------------------------------------

def test_hop_impact_counts_only_the_calculation_window_and_keeps_config_order():
    samples = [sample("isp", 5.0, ok=False), sample("router", 9.0), sample("router", 10.0), sample("isp", 11.0, ok=False),
               sample("isp", 12.0, latency=40.0), sample("router", 20.0), sample("router", 21.0)]
    hops = hop_impact(samples, 10.0, 20.0, [target("router", "Router", "local"), target("isp", "ISP", "isp")])
    assert [h.target_id for h in hops] == ["router", "isp"]
    router, isp = hops
    assert (router.sent, router.received, router.loss_pct) == (2, 2, 0.0)  # both bounds are inclusive
    assert (isp.sent, isp.received, isp.loss_pct) == (2, 1, 50.0)
    assert (isp.avg_ms, isp.min_ms, isp.max_ms, isp.jitter_ms) == (40.0, 40.0, 40.0, 1.0)
    assert (router.name, router.category) == ("Router", "local")


def test_hop_impact_falls_back_to_every_sample_when_the_calculation_window_is_empty():
    samples = [sample("a", 1.0), sample("a", 2.0, ok=False)]
    (a,) = hop_impact(samples, 100.0, 200.0, [])
    assert (a.sent, a.received, a.loss_pct) == (2, 1, 50.0)


def test_hop_impact_without_samples_is_empty():
    assert hop_impact([], 0.0, 10.0, [target("a")]) == []


def test_hop_impact_lost_probes_have_no_latency_and_jitter_averages_whatever_reported_it():
    samples = [sample("a", 1.0, ok=False), sample("a", 2.0, latency=4.0, jitter=2.0), sample("a", 3.0, latency=6.0, jitter=None)]
    (a,) = hop_impact(samples, 0.0, 10.0, [])
    assert (a.avg_ms, a.min_ms, a.max_ms, a.jitter_ms) == (5.0, 4.0, 6.0, 2.0)
    (b,) = hop_impact([sample("b", 1.0, ok=False)], 0.0, 10.0, [])
    assert (b.avg_ms, b.min_ms, b.max_ms, b.jitter_ms) == (None, None, None, None)
    assert b.loss_pct == 100.0


@pytest.mark.parametrize("tid, category", [("dns_extra", "dns"), ("mystery", "custom")])
def test_hop_impact_guesses_the_category_of_targets_missing_from_the_config(tid, category):
    (h,) = hop_impact([sample(tid, 1.0)], 0.0, 10.0, [target("router", category="local")])
    assert (h.category, h.name) == (category, tid)


def test_hop_impact_probes_without_a_category_attribute_count_as_dns():
    (h,) = hop_impact([sample("dns_google", 1.0)], 0.0, 10.0, [target("dns_google", "Google DNS")])
    assert h.category == "dns"


def test_hop_impact_unknown_ids_sort_after_configured_ones():
    samples = [sample("zzz", 1.0), sample("b", 1.0), sample("a", 1.0)]
    assert [h.target_id for h in hop_impact(samples, 0.0, 10.0, [target("a"), target("b")])] == ["a", "b", "zzz"]


# --- docsis_correlation --------------------------------------------------------------------------------------

def correlate(hops=(), snapshots=(), nearest="first", duration=30.0):
    snapshots = list(snapshots)
    if nearest == "first":
        nearest = snapshots[0] if snapshots else None
    return docsis_correlation(incident(duration), list(hops), snapshots, nearest)


@pytest.mark.parametrize("gateway_loss, router_loss, offline", [
    (50.0, 0.0, True),        # the limit itself counts
    (49.9, 0.0, False),
    (80.0, 14.9, True),
    (80.0, 15.0, False),      # a router at the limit is not "clean"
    (80.0, 40.0, False),
])
def test_gateway_loss_makes_the_gateway_offline_only_when_the_router_is_clean(gateway_loss, router_loss, offline):
    hops = [hop("router", "local", router_loss), hop("gateway", "gateway", gateway_loss)]
    c = correlate(hops, snapshots=[snap()])
    assert c.gateway_offline is offline
    assert c.status == ("error" if offline else "healthy")


def test_gateway_loss_without_a_router_hop_is_offline():
    assert correlate([hop("gateway", "gateway", 60.0)], snapshots=[snap()]).gateway_offline is True


def test_gateway_is_found_by_category_only():
    """The segment comes from the target's category; a target id ("isp_gateway") means nothing."""
    assert correlate([hop("isp_gateway", "custom", 70.0)], snapshots=[snap()]).gateway_offline is False
    assert correlate([hop("gw", "gateway", 70.0)], snapshots=[snap()]).gateway_offline is True
    assert correlate([hop("gw", "isp", 70.0)], snapshots=[snap()]).gateway_offline is False


@pytest.mark.parametrize("duration", [30.0, 90.1, 3600.0, None])
def test_no_snapshot_and_no_hop_evidence_is_unknown_not_offline(duration):
    c = correlate(snapshots=[], nearest=None, duration=duration)
    assert c.gateway_offline is False and c.status == "unknown"


def test_no_snapshot_but_healthy_gateway_hops_is_unknown_not_offline():
    c = correlate([hop("router", "local", 0.0), hop("gateway", "gateway", 10.0)], snapshots=[], nearest=None,
                  duration=600.0)
    assert c.gateway_offline is False and c.status == "unknown"


def test_gateway_probe_loss_alone_is_evidence_without_any_snapshot():
    c = correlate([hop("router", "local", 0.0), hop("gateway", "gateway", 80.0)], snapshots=[], nearest=None)
    assert c.gateway_offline is True and c.status == "error"


@pytest.mark.parametrize("kind, offline", [("gateway_down", True), ("gateway_loss", False), ("isp_outage", False),
                                           ("local_down", False), (None, False)])
def test_minute_resolution_hops_leave_the_gateway_to_the_classification(kind, offline):
    """Minute rollups dilute a short drop, so even 100% gateway loss in the table is not compared with the threshold
    there and a clean-looking table does not outvote the classifier."""
    inc = incident(30.0)
    inc.kind = kind
    for gateway_loss in (0.0, 100.0):
        c = docsis_correlation(inc, [hop("router", "local", 0.0), hop("gateway", "gateway", gateway_loss)], [], None,
                               minute_hops=True)
        assert c.gateway_offline is offline
        assert c.status == ("error" if offline else "unknown")


def test_a_long_incident_with_snapshots_is_not_assumed_offline():
    assert correlate(snapshots=[snap()], duration=600.0).gateway_offline is False


def test_an_error_snapshot_anywhere_in_the_window_means_offline():
    snapshots = [snap(1001.0), snap(1040.0, status="error")]
    c = correlate(snapshots=snapshots, nearest=snapshots[0])
    assert c.gateway_offline is True and c.status == "error"


def test_an_error_snapshot_outside_the_window_is_shown_but_is_not_evidence():
    c = correlate([hop("router", "local", 0.0), hop("gateway", "gateway", 0.0)], snapshots=[],
                  nearest=snap(status="error"))
    assert c.gateway_offline is False and c.status == "unknown"
    assert c.nearest_snapshot.status == "error"  # kept for display


def test_status_comes_from_the_nearest_snapshot_or_is_unknown():
    assert correlate(snapshots=[snap(status="warning")]).status == "warning"
    assert correlate(snapshots=[], nearest=None).status == "unknown"


def test_the_worst_channel_has_recent_errors_then_total_errors_then_the_lowest_snr():
    channels = [chan(1, snr=40.0, recent=3, total=10), chan(2, snr=30.0, recent=3, total=10),
                chan(3, snr=35.0, recent=3, total=11), chan(4, snr=20.0, recent=0, total=999)]
    assert correlate(snapshots=[snap(channels=channels)]).worst_channel.channel_id == 3
    assert correlate(snapshots=[snap(channels=channels[:2])]).worst_channel.channel_id == 2  # lower SNR first
    assert correlate(snapshots=[snap(channels=[chan(1, recent=0, total=5), chan(2, recent=1, total=0)])]
                     ).worst_channel.channel_id == 2


def test_upstream_channels_are_never_the_worst_channel():
    c = correlate(snapshots=[snap(channels=[chan(1, direction="upstream", recent=9), chan(2, recent=0)])])
    assert c.worst_channel.channel_id == 2


def test_lowest_snr_is_the_worst_channels_and_none_without_one():
    assert correlate(snapshots=[snap(channels=[chan(1, snr=33.5, recent=1)])]).lowest_snr == 33.5
    assert correlate(snapshots=[snap(channels=[chan(1, snr=None)])]).lowest_snr is None  # 99.0 sentinel hidden
    assert correlate(snapshots=[snap(channels=[chan(1, direction="upstream")])]).lowest_snr is None
    assert correlate(snapshots=[snap()]).lowest_snr is None
    assert correlate(snapshots=[snap(channels=[chan(1, snr=90.0)])]).lowest_snr is None  # not below 90


def test_rf_correlation_from_window_errors_or_the_worst_channel():
    assert correlate(snapshots=[snap(recent=2)]).rf_correlated is True
    assert correlate(snapshots=[snap(recent=0, channels=[chan(1, recent=1)])]).rf_correlated is True
    quiet = correlate(snapshots=[snap(recent=0, channels=[chan(1, recent=0, total=50)])])
    assert quiet.rf_correlated is False and quiet.active_errors_in_window == 0
    assert correlate(snapshots=[snap(recent=2), snap(recent=3)]).active_errors_in_window == 5


# --- to_response ---------------------------------------------------------------------------------------------

def test_to_response_keeps_models_and_the_key_order():
    nearest = snap(channels=[chan(1, recent=1)])
    inspection = IncidentInspection(
        incident=incident(), window_start=1.0, window_end=2.0, event_start=3.0, event_end=4.0,
        hop_impact=[hop()], samples=[{"target_id": "a"}], docsis_snapshots=[nearest],
        docsis_correlation=correlate(snapshots=[nearest]), router_snapshots=[])
    out = inspection.to_response()
    assert list(out) == ["incident", "window_start", "window_end", "event_start", "event_end", "hop_impact", "samples",
                         "docsis_snapshots", "docsis_correlation", "router_snapshots"]
    assert isinstance(out["incident"], Incident) and isinstance(out["docsis_snapshots"][0], DocsisSignalSnapshot)
    corr = out["docsis_correlation"]
    assert isinstance(corr, dict) and list(corr) == [
        "rf_correlated", "active_errors_in_window", "nearest_snapshot", "worst_channel", "lowest_snr", "status",
        "gateway_offline"]
    assert corr["nearest_snapshot"] is nearest and isinstance(corr["worst_channel"], DocsisChannel)
    assert out["hop_impact"][0] == {
        "target_id": "gateway", "name": "gateway", "category": "gateway", "sent": 100, "received": 100, "loss_pct": 0.0,
        "avg_ms": 1.0, "min_ms": 1.0, "max_ms": 1.0, "jitter_ms": None}


# --- error kinds: only "could not reach the gateway" is evidence of an outage -------------------------------------

@pytest.mark.parametrize("kind, offline", [("timeout", True), ("unreachable", True), (None, True),
                                           ("protocol", False), ("auth", False)])
def test_an_error_snapshot_counts_as_offline_only_when_the_modem_could_not_be_reached(kind, offline):
    errored = DocsisSignalSnapshot(timestamp=1040.0, status="error", error_kind=kind)  # None: a pre-v7 row
    snapshots = [snap(1001.0), errored]
    c = correlate(snapshots=snapshots, nearest=snapshots[0])
    assert c.gateway_offline is offline
    assert c.status == ("error" if offline else "healthy")
