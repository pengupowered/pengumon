"""Round metrics: the per-category rolling loss, latency and jitter of the classifier."""

import random
from typing import Dict, List, Optional

from pengumon.engine.classifier.metrics import CATEGORIES, category_metrics, round_metrics
from pengumon.engine.classifier_settings import ClassifierConfig
from pengumon.models.probes import ProbeSample, TargetStats



def stat(target_id, category, window, enabled=True, name=None):
    return TargetStats(target_id=target_id, name=name or target_id, category=category,
                       address=f"{target_id}.addr", enabled=enabled, recent_latencies=list(window))


def sample(target_id, success=True, latency=10.0):
    return ProbeSample(target_id=target_id, success=success, latency_ms=latency if success else None)


# --- verbatim copies of the pre-refactor OutageClassifier._calc_rolling_* methods (self removed) ---------

def _old_jitter(stats: Dict[str, TargetStats], category: str) -> Optional[float]:
    cat_stats = [st for st in stats.values() if st.category == category and st.enabled]
    if not cat_stats:
        return None

    jitters = []
    for st in cat_stats:
        recent = st.recent_latencies[-10:] if len(st.recent_latencies) >= 10 else st.recent_latencies
        valid_lats = [x for x in recent if x is not None]
        if len(valid_lats) > 1:
            diffs = [abs(valid_lats[i] - valid_lats[i-1]) for i in range(1, len(valid_lats))]
            if diffs:
                jitters.append(sum(diffs) / len(diffs))
    return round(sum(jitters) / len(jitters), 2) if jitters else None


def _old_loss(stats: Dict[str, TargetStats], category: str, samples: List[ProbeSample]) -> float:
    cat_stats = [st for st in stats.values() if st.category == category and st.enabled]
    if not cat_stats:
        # Fallback to current round samples
        cat_samples = [s for s in samples if stats.get(s.target_id) and stats[s.target_id].category == category]
        if not cat_samples:
            return 0.0
        return (sum(1 for s in cat_samples if not s.success) / len(cat_samples)) * 100.0

    total_recent = []
    for st in cat_stats:
        recent = st.recent_latencies[-10:] if len(st.recent_latencies) >= 10 else st.recent_latencies
        total_recent.extend(recent)

    if not total_recent:
        cat_samples = [s for s in samples if stats.get(s.target_id) and stats[s.target_id].category == category]
        if not cat_samples:
            return 0.0
        return (sum(1 for s in cat_samples if not s.success) / len(cat_samples)) * 100.0

    lost = total_recent.count(None)
    return round((lost / len(total_recent)) * 100.0, 1)

def _old_lat(stats: Dict[str, TargetStats], category: str, samples: List[ProbeSample]) -> Optional[float]:
    cat_stats = [st for st in stats.values() if st.category == category and st.enabled]
    if not cat_stats:
        valid = [s.latency_ms for s in samples if s.success and s.latency_ms is not None and stats.get(s.target_id) and stats[s.target_id].category == category]
        return round(sum(valid) / len(valid), 2) if valid else None

    valid_lats = []
    for st in cat_stats:
        recent = st.recent_latencies[-10:] if len(st.recent_latencies) >= 10 else st.recent_latencies
        valid_lats.extend([x for x in recent if x is not None])

    if not valid_lats:
        valid = [s.latency_ms for s in samples if s.success and s.latency_ms is not None and stats.get(s.target_id) and stats[s.target_id].category == category]
        return round(sum(valid) / len(valid), 2) if valid else None

    return round(sum(valid_lats) / len(valid_lats), 2)


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
        assert (category.loss_pct, category.avg_latency_ms, category.jitter_ms) == (0.0, None, None)
        assert category.primary is None
        assert category.name("dflt") == "dflt" and category.address("addr") == "addr"
    assert m.failed_targets == ()


def test_target_without_history_falls_back_to_the_round_samples():
    stats = {"gw": stat("gw", "gateway", [])}
    m = round_metrics(stats, [sample("gw", latency=4.0), sample("gw", success=False)])
    assert m.gateway.loss_pct == 50.0
    assert m.gateway.avg_latency_ms == 4.0
    assert m.gateway.jitter_ms is None  # jitter has no sample fallback
    assert m.gateway.primary.target_id == "gw"


def test_only_the_last_ten_probes_count():
    window = [None] * 5 + [10.0] * 10
    m = category_metrics("local", [stat("l", "local", window)], [])
    assert m.loss_pct == 0.0
    assert m.avg_latency_ms == 10.0
    assert m.jitter_ms == 0.0


def test_short_windows():
    for window, loss, lat in (
        ([5.0], 0.0, 5.0),
        ([5.0, None], 50.0, 5.0),
        ([None, None, 6.0], 66.7, 6.0),
        ([1.0, None, 3.0, None, 5.0], 40.0, 3.0),
    ):
        m = category_metrics("local", [stat("l", "local", window)], [])
        assert (m.loss_pct, m.avg_latency_ms) == (loss, lat), window


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


def _random_round(rng):
    categories = list(CATEGORIES) + ["custom"]
    stats: Dict[str, TargetStats] = {}
    for i in range(rng.randint(0, 9)):
        window = [None if rng.random() < 0.3 else round(rng.uniform(0, 120), rng.choice([0, 1, 3]))
                  for _ in range(rng.randint(0, 15))]
        stats[f"t{i}"] = stat(f"t{i}", rng.choice(categories), window, enabled=rng.random() < 0.8)
    samples: List[ProbeSample] = []
    for _ in range(rng.randint(0, 12)):
        target = rng.choice(list(stats) + ["ghost"]) if stats else "ghost"
        ok = rng.random() < 0.7
        latency = None if rng.random() < 0.15 else round(rng.uniform(0, 200), rng.choice([0, 2, 5]))
        samples.append(ProbeSample(target_id=target, success=ok, latency_ms=latency))
    return stats, samples


def test_randomised_equivalence_with_the_old_calculations():
    rng = random.Random(20261016)
    for case in range(2000):
        stats, samples = _random_round(rng)
        m = round_metrics(stats, samples)
        for category in CATEGORIES:
            got = getattr(m, category)
            want = (_old_loss(stats, category, samples), _old_lat(stats, category, samples),
                    _old_jitter(stats, category))
            have = (got.loss_pct, got.avg_latency_ms, got.jitter_ms)
            assert have == want and repr(have) == repr(want), (case, category, have, want)
