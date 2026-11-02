"""What an incident can be: one row per kind and one per cause code.

A new diagnosis is one row in KINDS. IncidentKind, LOAD_EXPLAINABLE_KINDS, ISP_LINE_KINDS and incident_cause are derived
from the tables, so a kind cannot be left out of the cause mapping (tests/test_incident_cause.py checks every row).
Control flow (load backfills, the intermediate-hop rule) keys off the kind, never off the display title. This module
imports nothing from pengumon, so engine.load can use it without importing the classifier (which imports engine.load).

The network classifier's kinds are the rows below. A feature that raises incidents of its own registers its kind with
register_kind (the services feature does, for service_down); such a kind has scope "service" and stays out of the
network incident list and counts.
"""

from dataclasses import dataclass
from typing import Dict, FrozenSet, Literal, Optional, Tuple

# The small, stable set of causes the timeline lanes group incidents by, in the words of the dashboard's path health:
#   router (the local router or mesh: the "local" probe category), line (the gateway / modem: the "gateway" category),
#   isp (the provider's hops and beyond: the "isp" category and the backbone), internet (public targets only), dns,
#   load (explained by a busy line), service (an app on this host) or unknown.
CAUSE_CATEGORIES = ("router", "line", "isp", "internet", "dns", "load", "service", "unknown")

Scope = Literal["network", "service"]


@dataclass(frozen=True)
class Kind:
    name: str
    cause: Optional[str]  # a CAUSE_CATEGORIES value; None: the incident's cause code decides (rows from before kinds)
    load_explainable: bool = False  # heavy line load explains it: latency or jitter only, or loss seen only at ISP hops
    isp_line: bool = False  # loss diagnosed at the ISP hops (the intermediate-hop rule applies to these)
    scope: Scope = "network"


_ROWS = (
    Kind("nominal", "unknown"),
    Kind("local_down", "router"),
    Kind("local_loss", "router"),
    Kind("local_latency", "router", load_explainable=True),
    Kind("gateway_down", "line"),
    Kind("gateway_loss", "line"),
    Kind("gateway_latency", "line", load_explainable=True),
    Kind("isp_outage", "isp", isp_line=True),
    Kind("isp_loss", "isp", load_explainable=True, isp_line=True),
    Kind("backbone_loss", "isp"),
    Kind("upstream_latency", "isp", load_explainable=True),
    Kind("dns_outage", "dns"),
    Kind("dns_loss", "dns"),
    Kind("dns_slow", "dns"),
    Kind("busy_latency", "load", load_explainable=True),
    Kind("busy_isp_hops", "load"),
    Kind("hop_deprioritised", "isp"),
    Kind("legacy_latency", None, load_explainable=True),  # only from the schema v6 backfill of older incidents
    Kind("legacy_other", None),
    Kind("internet_outage", "internet"),  # public targets fail while no LAN target is configured: not attributable
    Kind("internet_loss", "internet"),
)

KINDS: Dict[str, Kind] = {k.name: k for k in _ROWS}

# Cause code -> cause category. The codes an incident or a diagnosis can carry.
CAUSE_CODES: Dict[str, str] = {
    "nominal": "unknown",
    "local_wifi_mesh_drop": "router",
    "local_switch_cable_fault": "router",
    "double_nat_congestion": "line",  # the rules raise it for the gateway kinds (modem / gateway link)
    "access_line_drop": "line",  # the line to the ISP (cable, fiber, DSL, ...); the isp_* kinds that carry it are "isp"
    "xfinity_last_mile_coax_drop": "line",  # legacy name of access_line_drop; read as access_line_drop (LEGACY_CAUSES)
    "dns_resolver_failure": "dns",
    "internet_backbone_transit_loss": "isp",
    "target_specific_outage": "internet",
    "unknown_outage": "unknown",
    "load_induced_latency": "load",
    "service_unavailable": "service",  # an app on this host stopped answering its health check (services/checker.py)
}
# Cause codes renamed since; stored and incoming values are read as the new name (schema v8 renames stored rows).
LEGACY_CAUSES = {"xfinity_last_mile_coax_drop": "access_line_drop"}

# What the classifier can diagnose. Fixed when this module loads: kinds that features register are not in it.
IncidentKind = Literal[tuple(KINDS)]
CauseCode = Literal[tuple(CAUSE_CODES)]

LOAD_EXPLAINABLE_KINDS: FrozenSet[str] = frozenset(k.name for k in _ROWS if k.load_explainable)
ISP_LINE_KINDS: FrozenSet[str] = frozenset(k.name for k in _ROWS if k.isp_line)


def register_kind(kind: Kind) -> None:
    """Adds a kind a feature raises incidents of. Registering the same row again is harmless."""
    existing = KINDS.get(kind.name)
    if existing is not None and existing != kind:
        raise ValueError(f"incident kind {kind.name!r} is already registered differently")
    KINDS[kind.name] = kind


def kinds_in_scope(scope: Scope) -> Tuple[str, ...]:
    """The registered kinds of one scope (the repository filters its network and service lists by these)."""
    return tuple(k.name for k in KINDS.values() if k.scope == scope)


def is_service_kind(kind: Optional[str]) -> bool:
    k = KINDS.get(kind or "")
    return k is not None and k.scope == "service"


def current_cause(code: str) -> str:
    return LEGACY_CAUSES.get(code, code)


def incident_cause(kind: Optional[str], cause_code: str) -> str:
    """The cause category of an incident, from its kind where that says enough, from its cause code otherwise."""
    k = KINDS.get(kind or "")
    return (k.cause if k else None) or CAUSE_CODES.get(current_cause(cause_code), "unknown")
