"""What the reports share: the configured targets in their order, hop labels, the failing-hop rule and the
split of incident rows into real and expected ones; and the lock that builds one AI pack at a time."""

import asyncio
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional

from pengumon.incidents.taxonomy import incident_cause, is_service_kind
from pengumon.reports.settings import ReportsConfig

_DEFAULTS = ReportsConfig()

# One AI pack at a time, whoever asks (GET /api/ai-pack, the MCP `ai_pack` tool): a build reads the whole window and
# every module's history. A caller that finds it locked answers "busy" rather than queue.
pack_building = asyncio.Lock()


def material_loss_points(config) -> float:
    """How many percentage points above the hops before it make a hop lossy, and failing (reports.material_loss_points
    of the live config; its default without one)."""
    return getattr(getattr(config, "reports", None), "material_loss_points", _DEFAULTS.material_loss_points)


def _targets(config) -> list:
    if not config:
        return []
    return list(config.targets) + list(config.dns_probes)


def _canonical_order(cfg_targets: list) -> List[str]:
    return [t.id for t in cfg_targets]


def find_primary_failing_hop(hop_stats: List[Dict[str, Any]], ignoring: Iterable[str] = (),
                            material: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """First hop (list order) whose loss is at least `material` percentage points above every hop before it (None:
    the default of reports.material_loss_points).

    hop_stats must be in canonical order. Hops without probes are skipped, and so are the hops that ignore ping
    (`ignoring`, engine.path_rules): their unanswered pings are no loss on the path.
    """
    if material is None:
        material = _DEFAULTS.material_loss_points
    skip = set(ignoring)
    worst_before = 0.0
    for h in hop_stats:
        if not h["sent"] or h["target_id"] in skip:
            continue
        if h["loss_pct"] >= worst_before + material:
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


def lasted(r: Dict[str, Any], now: float) -> float:
    """How long an incident row lasted, in seconds; one still open has no duration yet: it has lasted until now."""
    return r["duration_seconds"] if r["duration_seconds"] is not None else max(0.0, now - r.get("start_time", now))


def cause_totals(incidents: Iterable[Dict[str, Any]], now: float) -> Dict[str, List[float]]:
    """{cause category: [incident count, minutes]} (incidents.taxonomy: the incident lanes' words), from the kind where
    it says enough: a cause code alone can stand for more than one place (older rows, and access_line_drop for upstream
    latency). The reports order them as they need."""
    causes: Dict[str, List[float]] = defaultdict(lambda: [0, 0.0])
    for r in incidents:
        cause = incident_cause(r.get("kind"), r["cause_code"], r.get("description"))
        causes[cause][0] += 1
        causes[cause][1] += lasted(r, now) / 60.0
    return causes
