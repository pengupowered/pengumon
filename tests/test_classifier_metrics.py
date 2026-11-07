"""Round metrics: the per-category rolling loss, latency and jitter of the classifier."""

from pengumon.engine.classifier.metrics import CATEGORIES, category_metrics, round_metrics
from pengumon.engine.classifier_settings import ClassifierConfig
from pengumon.models.probes import ProbeSample, TargetStats


def stat(target_id, category, window, enabled=True, name=None):
    return TargetStats(target_id=target_id, name=name or target_id, category=category,
                       address=f"{target_id}.addr", enabled=enabled, recent_latencies=list(window))


def sample(target_id, success=True, latency=10.0):
    return ProbeSample(target_id=target_id, success=success, latency_ms=latency if success else None)


# --- tests -------------------------------------------------------------------------------------------

def test_fallback_loss_is_unrounded():
    stats = {f"p{i}": stat(f"p{i}", "public", [], enabled=False) for i in range(3)}
    samples = [sample("p0"), sample("p1"), sample("p2", success=False)]
    m = round_metrics(stats, samples)
    assert m.public.loss_pct == (1 / 3) * 100.0  # the operator order is a / b * 100; 100 / 3 differs in the last bit
    assert repr(m.public.loss_pct) == "33.33333333333333"
    assert m.public.targets == ()


def test_nothing_at_all_gives_zero_and_none():
    m = round_metrics({}, [])
    for category in (m.local, m.gateway, m.isp, m.public, m.dns):
        assert (category.loss_pct, category.latency_ms, category.jitter_ms) == (0.0, None, None)
        assert category.primary is None
        assert category.name("dflt") == "dflt" and category.address("addr") == "addr"
    assert m.failed_targets == ()


def test_target_without_history_falls_back_to_the_round_samples():
    stats = {"gw": stat("gw", "gateway", [])}
    m = round_metrics(stats, [sample("gw", latency=4.0), sample("gw", success=False)])
    assert m.gateway.loss_pct == 50.0
    assert m.gateway.latency_ms == 4.0
    assert m.gateway.jitter_ms is None  # jitter has no sample fallback
    assert m.gateway.primary.target_id == "gw"


def test_only_the_last_ten_probes_count():
    window = [None] * 5 + [10.0] * 10
    m = category_metrics("local", [stat("l", "local", window)], [])
    assert m.loss_pct == 0.0
    assert m.latency_ms == 10.0
    assert m.jitter_ms == 0.0


def test_short_windows():
    for window, loss, lat in (
        ([5.0], 0.0, 5.0),
        ([5.0, None], 50.0, 5.0),
        ([None, None, 6.0], 66.7, 6.0),
        ([1.0, None, 3.0, None, 5.0], 40.0, 3.0),
    ):
        m = category_metrics("local", [stat("l", "local", window)], [])
        assert (m.loss_pct, m.latency_ms) == (loss, lat), window


def test_jitter_leaves_out_targets_with_fewer_than_two_valid_values():
    a = stat("a", "public", [10.0, 14.0, 10.0])        # mean abs diff 4.0
    b = stat("b", "public", [None, 30.0, None])        # one valid value: no jitter
    c = stat("c", "public", [20.0, 22.0])              # 2.0
    assert category_metrics("public", [a, b, c], []).jitter_ms == 3.0
    assert category_metrics("public", [b], []).jitter_ms is None
    assert category_metrics("public", [stat("d", "public", [7.0])], []).jitter_ms is None


def test_sample_without_stats_is_failed_but_in_no_category():
    stats = {"l": stat("l", "local", [1.0])}
    samples = [sample("l"), sample("ghost", success=False)]
    m = round_metrics(stats, samples)
    assert m.failed_targets == ("ghost",)
    assert m.local.loss_pct == 0.0
    assert (m.public.loss_pct, m.dns.loss_pct) == (0.0, 0.0)


def test_slow_probe_counts_as_failed_but_zero_latency_does_not():
    stats = {"l": stat("l", "local", [1.0])}
    samples = [sample("l", latency=150.0), sample("l", latency=150.5), sample("l", latency=0.0)]
    assert round_metrics(stats, samples).failed_targets == ("l",)


def test_primary_is_the_first_enabled_target_in_stats_order():
    stats = {
        "off": stat("off", "isp", [1.0], enabled=False, name="Off"),
        "one": stat("one", "isp", [1.0], name="One"),
        "two": stat("two", "isp", [1.0], name="Two"),
    }
    m = round_metrics(stats, [])
    assert [t.target_id for t in m.isp.targets] == ["one", "two"]
    assert m.isp.name("x") == "One" and m.isp.address("x") == "one.addr"


def test_round_predicates():
    def mk(**loss):
        stats = {c: stat(c, c, [None] * round(loss.get(c, 0) / 10) + [1.0] * (10 - round(loss.get(c, 0) / 10)))
                 for c in CATEGORIES}
        return round_metrics(stats, [])

    c = ClassifierConfig()
    assert not mk().is_loss_or_outage("degraded", c)
    assert mk().is_loss_or_outage("down", c)
    assert mk(local=20).is_loss_or_outage("degraded", c)
    assert not mk(dns=20).is_loss_or_outage("degraded", c)
    assert mk(dns=30).is_loss_or_outage("degraded", c)
    assert mk(isp=20).isp_loss_only(c)
    assert not mk(isp=20, public=20).isp_loss_only(c)
    assert not mk(isp=10).isp_loss_only(c)
    at_120 = ClassifierConfig(public_latency_spike_threshold_ms=120.0)
    assert mk(gateway=20).upstream_issue(at_120)
    assert not mk(local=40, dns=40).upstream_issue(at_120)
    slow = round_metrics({"p": stat("p", "public", [130.0])}, [])
    assert slow.upstream_issue(at_120) and not slow.upstream_issue(ClassifierConfig(public_latency_spike_threshold_ms=130.0))


