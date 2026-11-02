"""Features and the capabilities they need, and whether this installation has them.

A feature (a dashboard panel, a part of the diagnosis) declares the capabilities it reads. It is available when every
one of them is offered by an enabled device that is configured (has what its driver needs, such as a password).
Otherwise the reason says exactly what is missing, so the UI can say "needs a router that reports WAN counters"
instead of showing an empty chart.

Availability is decided from configuration. Whether the device answers right now is its status (/api/devices).
"""

from dataclasses import dataclass
from types import MappingProxyType
from typing import FrozenSet, List, Literal, Mapping, Optional, Tuple

from pengumon.capabilities import Capability

FEATURES: Mapping[str, FrozenSet[Capability]] = MappingProxyType({
    "line_load": frozenset({Capability.WAN_COUNTERS}),       # WAN load %, line capacity, load-aware diagnosis
    "line_health": frozenset({Capability.ACCESS_LINE}),      # access-line levels and errors (DOCSIS RF, ...)
    "router_system": frozenset({Capability.ROUTER_SYSTEM}),  # router CPU, memory, connected clients
    "clients": frozenset({Capability.CLIENT_INVENTORY}),     # the list of connected devices and their history
    "mesh": frozenset({Capability.MESH}),                    # mesh nodes and their client counts
})


# Why a feature is (not) available, for code (the UI picks its hint by it): `none` no device offers a capability it
# needs, `disabled` only disabled devices do, `not_configured` an enabled one does but lacks e.g. its password.
ReasonKind = Literal["available", "none", "disabled", "not_configured"]


@dataclass(frozen=True)
class FeatureStatus:
    feature: str
    available: bool
    reason_kind: ReasonKind  # for a feature needing several capabilities: that of the first one missing
    reason: Optional[str]  # for people; None when available
    provided_by: Tuple[str, ...]  # ids of the configured devices that provide what it needs, config order


def _describe(device) -> str:
    return f"{device.id} ({device.driver.name})"


def _unavailable_reason(registry, capability: Capability) -> Tuple[ReasonKind, str]:
    """Why no configured, enabled device provides the capability."""
    enabled = registry.providing(capability)
    if enabled:  # offered, but none of them can poll
        return "not_configured", "; ".join(f"{_describe(d)} provides {capability.value} but is not configured: "
                                           f"{d.driver.missing_configuration()}" for d in enabled)
    disabled = [d for d in registry.offering(capability) if not d.enabled]
    if disabled:
        return "disabled", (f"no enabled device provides {capability.value} "
                            f"({', '.join(_describe(d) for d in disabled)} disabled in configuration)")
    return "none", f"no enabled device provides {capability.value}"


def feature_status(registry, features: Mapping[str, FrozenSet[Capability]] = FEATURES) -> List[FeatureStatus]:
    """One FeatureStatus per feature, in the order of `features`. `registry` is a devices.DeviceRegistry."""
    result = []
    for feature, needs in features.items():
        provided_by: List[str] = []
        reasons: List[Tuple[ReasonKind, str]] = []
        for capability in sorted(needs, key=lambda c: c.value):
            ready = [d.id for d in registry.providing(capability) if d.driver.missing_configuration() is None]
            if ready:
                provided_by += [i for i in ready if i not in provided_by]
            else:
                reasons.append(_unavailable_reason(registry, capability))
        order = [d.id for d in registry.all()]
        result.append(FeatureStatus(
            feature=feature,
            available=not reasons,
            reason_kind=reasons[0][0] if reasons else "available",
            reason="; ".join(text for _, text in reasons) or None,
            provided_by=tuple(sorted(provided_by, key=order.index)),
        ))
    return result


def host_services_status(config) -> FeatureStatus:
    """The `host_services` feature: the dashboard's card of the apps on this host (services/checker.py).

    Kept out of FEATURES because those map a feature to the device capabilities it reads, and this one needs no
    device: it is available exactly when `service_checks.services` lists an enabled service. `config` is the
    AppConfig (None before the app has one)."""
    services = [s for s in config.service_checks.services if s.enabled] if config is not None else []
    return FeatureStatus(
        feature="host_services",
        available=bool(services),
        reason_kind="available" if services else "none",
        reason=None if services else "no service_checks.services are configured",
        provided_by=tuple(s.id for s in services),
    )
