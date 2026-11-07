"""Per-category probe metrics for one classifier round: loss, latency and jitter over each target's window, its last
`window` probes (classifier.window_probes).

Latency is the middle value (engine/window.py `middle`, the lower median) of the category's answered probes in the
window, and jitter the mean over its targets of each one's middle probe-to-probe change (`jitter`): a level counts as
passed only when more than half of the probes (or changes) pass it, so one stalled probe is no spike. Loss stays the
share of the window's probes lost. The arithmetic (rounding, the unrounded fallback) is pinned by the
characterization golden; do not reorder operations.
"""

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from pengumon.engine.classifier_settings import ClassifierConfig
from pengumon.engine.prober_settings import DEFAULT_WINDOW_PROBES
from pengumon.engine.window import jitter as window_jitter, middle
from pengumon.incidents.model import StatusCode
from pengumon.models.probes import ProbeSample, TargetStats

AFFECTED_LATENCY_MS = 150  # internal (H9): a sample slower than this makes its target an affected one
CATEGORIES = ("local", "gateway", "isp", "public", "dns")


@dataclass(frozen=True)
class CategoryMetrics:
    """Loss, latency and jitter of one target category, over the window (the last N probes) of each target."""

    category: str
    targets: Tuple[TargetStats, ...]  # enabled targets of the category, in stats order (not ipv6_unavailable)
    loss_pct: float
    latency_ms: Optional[float]  # the middle (lower median) answered latency of the window
    jitter_ms: Optional[float]  # the mean over the targets of each one's middle probe-to-probe change
    samples: int = 0  # this round's samples of the category (the fallback while targets have no history)

    @property
    def measured(self) -> bool:
        """Whether anything in this category is probed. An unmeasured category reports 0 % loss, which must not
        be read as evidence that the segment is healthy."""
        return bool(self.targets) or self.samples > 0

    @property
    def primary(self) -> Optional[TargetStats]:
        """The target whose name and address appear in descriptions."""
        return self.targets[0] if self.targets else None

    def name(self, default: str) -> str:
        return self.primary.name if self.primary else default

    def address(self, default: str) -> str:
        return self.primary.address if self.primary else default


@dataclass(frozen=True)
class RoundMetrics:
    """The metrics of every category for one round. Holds live TargetStats: use within the round, never store."""

    local: CategoryMetrics
    gateway: CategoryMetrics
    isp: CategoryMetrics
    public: CategoryMetrics
    dns: CategoryMetrics
    failed_targets: Tuple[str, ...]

    def is_loss_or_outage(self, status: StatusCode, config: ClassifierConfig) -> bool:
        """Packet loss or a hard outage, as opposed to pure latency / jitter."""
        degraded = config.degraded_loss_pct
        return (
            status == "down"
            or self.local.loss_pct >= degraded
            or self.gateway.loss_pct >= degraded
            or self.isp.loss_pct >= degraded
            or self.public.loss_pct >= degraded
            or self.dns.loss_pct >= config.dns_degraded_loss_pct
        )

    def isp_loss_only(self, config: ClassifierConfig) -> bool:
        """Loss only at the ISP hops while the LAN, modem, public internet and DNS answer."""
        degraded = config.degraded_loss_pct
        return (
            self.isp.loss_pct >= degraded and self.local.loss_pct < degraded and self.gateway.loss_pct < degraded
            and self.public.loss_pct < degraded and self.dns.loss_pct < config.dns_degraded_loss_pct
        )

    def upstream_issue(self, config: ClassifierConfig) -> bool:
        """Trouble upstream of the home network (starts the DNS cooldown)."""
        degraded = config.degraded_loss_pct
        latency = self.public.latency_ms
        return bool(
            self.gateway.loss_pct >= degraded
            or self.isp.loss_pct >= degraded
            or self.public.loss_pct >= degraded
            or (latency and latency > config.public_latency_spike_threshold_ms)
        )


def _fallback_loss(samples: Sequence[ProbeSample]) -> float:
    if not samples:
        return 0.0
    return (sum(1 for s in samples if not s.success) / len(samples)) * 100.0


def category_metrics(
    category: str, targets: Sequence[TargetStats], fallback_samples: Sequence[ProbeSample],
    window: int = DEFAULT_WINDOW_PROBES,
) -> CategoryMetrics:
    """Metrics of one category over each target's last `window` probes. `fallback_samples` are this round's samples
    of the category, used for loss and latency while the targets have no history yet (jitter has no fallback)."""
    windows = [st.recent_latencies[-window:] for st in targets]
    total: List[Optional[float]] = [v for window in windows for v in window]

    if not total:
        loss = _fallback_loss(fallback_samples)
    else:
        loss = round((total.count(None) / len(total)) * 100.0, 1)

    valid = [v for window in windows for v in window if v is not None]
    if not valid:
        valid = [s.latency_ms for s in fallback_samples if s.success and s.latency_ms is not None]
    latency = round(middle(valid), 2) if valid else None

    jitters = [j for j in (window_jitter(w) for w in windows) if j is not None]
    jitter = round(sum(jitters) / len(jitters), 2) if jitters else None

    return CategoryMetrics(category, tuple(targets), loss, latency, jitter, len(fallback_samples))


def round_metrics(stats: Dict[str, TargetStats], samples: List[ProbeSample],
                  window: int = DEFAULT_WINDOW_PROBES) -> RoundMetrics:
    """All category metrics for one round over each target's last `window` probes (classifier.window_probes), from
    one pass over the stats and one over the samples."""
    targets: Dict[str, List[TargetStats]] = {c: [] for c in CATEGORIES}
    for st in stats.values():
        # an IPv6 target while this host has no IPv6 is not probed: its last window is no evidence either way
        if st.enabled and st.category in targets and not st.ipv6_unavailable:
            targets[st.category].append(st)

    cat_samples: Dict[str, List[ProbeSample]] = {c: [] for c in CATEGORIES}
    failed = []
    for s in samples:
        quiet = stats.get(s.target_id) is not None and stats[s.target_id].ignores_ping
        # a hop that ignores ping (engine.path_rules) is no affected target; no verdict reads this list, only `affected`
        if not quiet and (not s.success or (s.latency_ms and s.latency_ms > AFFECTED_LATENCY_MS)):
            failed.append(s.target_id)
        st = stats.get(s.target_id)
        if st is not None and st.category in cat_samples:
            cat_samples[st.category].append(s)

    per = {c: category_metrics(c, targets[c], cat_samples[c], window) for c in CATEGORIES}
    return RoundMetrics(per["local"], per["gateway"], per["isp"], per["public"], per["dns"], tuple(failed))
