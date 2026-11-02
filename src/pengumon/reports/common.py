"""What the reports share: the configured targets in their order, hop labels, the failing-hop rule and the
split of incident rows into real and expected ones."""

from typing import Any, Dict, Iterable, List, Optional

from pengumon.incidents.taxonomy import is_service_kind


# A hop counts as lossy, and as failing, from this many percentage points above the hops before it.
LOSS_MATERIAL_PCT = 0.5


def _targets(config) -> list:
    if not config:
        return []
    return list(config.targets) + list(config.dns_probes)


def _canonical_order(cfg_targets: list) -> List[str]:
    return [t.id for t in cfg_targets]


def find_primary_failing_hop(hop_stats: List[Dict[str, Any]], ignoring: Iterable[str] = ()) -> Optional[Dict[str, Any]]:
    """First hop (list order) whose loss is at least LOSS_MATERIAL_PCT above every hop before it.

    hop_stats must be in canonical order. Hops without probes are skipped, and so are the hops that ignore ping
    (`ignoring`, engine.path_rules): their unanswered pings are no loss on the path.
    """
    skip = set(ignoring)
    worst_before = 0.0
    for h in hop_stats:
        if not h["sent"] or h["target_id"] in skip:
            continue
        if h["loss_pct"] >= worst_before + LOSS_MATERIAL_PCT:
            return h
        worst_before = max(worst_before, h["loss_pct"])
    return None


def _hop_label(target_id: str, names: Dict[str, str], addresses: Dict[str, str]) -> str:
    name = names.get(target_id, target_id)
    addr = addresses.get(target_id, "")
    return f"{name} ({addr})" if addr and addr not in name else name


def _split_incidents(rows: List[Dict[str, Any]]):
    """(real network incidents, count of expected ones). App health incidents (service_down) are not about the
    line, so the network report leaves them out entirely."""
    rows = [r for r in rows if not is_service_kind(r.get("kind"))]
    real = [r for r in rows if not r.get("expected")]
    return real, len(rows) - len(real)
