"""The gauges pengumon reports from its app state, in one table that both exporters read: `metrics.render_metrics`
(Prometheus text at /metrics, names in `prom`) and the OTel observable gauges `observability.export` registers (names in
`otel`). A new metric is one `Gauge` here (or, from a feature's own module, one more entry appended to GAUGES).

`collect(state)` takes the app's api.state.AppState and returns `[(labels, value)]`; a value of None leaves that
sample out, and a gauge without samples is not declared at all. OTel callbacks run on the SDK's export thread, so a
gauge that has an `otel` name must collect synchronously; one that is Prometheus-only (`otel` None) may be async.
"""

import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple, Union

from pengumon import __version__

Sample = Tuple[Dict[str, str], Optional[float]]


@dataclass(frozen=True)
class Gauge:
    prom: str  # Prometheus metric name
    otel: Optional[str]  # OTel instrument name; None: not exported over OTLP
    unit: str  # OTel unit
    help: str
    collect: Callable[[Any], Union[List[Sample], Awaitable[List[Sample]]]]
    otel_drop: Tuple[str, ...] = ()  # labels the Prometheus text carries that the OTel attributes leave out


_STATUS_VALUE = {"up": 0, "degraded": 1, "down": 2}
_SYSTEM_STATUSES = ("healthy", "degraded", "down", "busy")
_DEVICE_STATUSES = ("healthy", "warning", "critical", "unconfigured", "error")


def _stats(state) -> list:
    return list(state.prober.stats.values()) if state.prober else []


def _per_target(getter) -> Callable[[Any], List[Sample]]:
    def collect(state) -> List[Sample]:
        return [({"target": s.target_id, "name": s.name, "category": s.category}, getter(s)) for s in _stats(state)]
    return collect


def _system_status(state) -> List[Sample]:
    diagnosis = state.classifier.last_diagnosis if state.classifier else None
    if diagnosis is None:
        return []
    return [({"status": st}, 1 if diagnosis.status == st else 0) for st in _SYSTEM_STATUSES]


def _line_load(state) -> List[Sample]:
    diagnosis = state.classifier.last_diagnosis if state.classifier else None
    return [({}, diagnosis.load_pct)] if diagnosis is not None else []


def _line_capacity(state) -> List[Sample]:
    load = getattr(state.classifier, "load", None) if state.classifier else None
    if load is None:
        return []
    return [({"direction": "down"}, load.down_mbps), ({"direction": "up"}, load.up_mbps)]


def _incident_active(state) -> List[Sample]:
    return [({}, 1 if state.classifier.active_incident else 0)] if state.classifier is not None else []


async def _incidents_total(state) -> List[Sample]:
    if state.repository is None:
        return []
    return [({}, await state.repository.count_network_incidents(include_expected=False))]


def _checked_services(state) -> list:
    checker = state.service_checker
    if checker is None:
        return []
    return [(s, checker.status[s.id]) for s in checker.services
            if checker.status.get(s.id) and checker.status[s.id].ok is not None]


def _service_up(state) -> List[Sample]:
    return [({"service": s.id}, 1 if status.ok else 0) for s, status in _checked_services(state)]


def _service_check_seconds(state) -> List[Sample]:
    return [({"service": s.id}, None if status.latency_ms is None else status.latency_ms / 1000.0)
            for s, status in _checked_services(state)]


def _device_status(state) -> List[Sample]:
    samples: List[Sample] = []
    for device in state.devices.all() if state.devices is not None else ():
        snap = device.latest_snapshot
        if device.enabled and snap is not None:
            samples += [({"device": device.id, "status": st}, 1 if snap.status == st else 0) for st in _DEVICE_STATUSES]
    return samples


def _probe_loop_age(state) -> List[Sample]:
    last = getattr(state.prober, "last_round_ts", None) if state.prober else None
    return [({}, max(0.0, time.time() - last) if last else None)]


def _build_info(state) -> List[Sample]:
    return [({"version": __version__}, 1)]


GAUGES: List[Gauge] = [
    Gauge("pengumon_target_last_latency_ms", None, "ms", "Latency of the last successful probe.",
          _per_target(lambda s: s.last_ms)),
    Gauge("pengumon_target_avg_latency_ms", None, "ms", "Average latency since start.",
          _per_target(lambda s: s.avg_ms)),
    Gauge("pengumon_target_loss_recent_percent", None, "%", "Packet loss over the recent probe window.",
          _per_target(lambda s: s.loss_recent_pct)),
    Gauge("pengumon_target_status", "pengumon.target.status", "{state}", "Target state: 0 up, 1 degraded, 2 down.",
          _per_target(lambda s: _STATUS_VALUE.get(s.status, 2)), otel_drop=("name",)),
    Gauge("pengumon_system_status", "pengumon.system.status", "{state}",
          "1 for the current overall status, 0 for the others.", _system_status),
    Gauge("pengumon_line_load_percent", None, "%", "Current WAN load, percent of line capacity.", _line_load),
    Gauge("pengumon_line_capacity_mbps", None, "Mbit/s", "Line capacity in use for load calculations.",
          _line_capacity),
    Gauge("pengumon_incident_active", "pengumon.incident.active", "{state}", "1 while an incident is open.",
          _incident_active),
    Gauge("pengumon_incidents_total", None, "{incident}",
          "Recorded network incidents, expected ones (heavy load) and host service outages not counted.",
          _incidents_total),
    Gauge("pengumon_service_up", None, "{state}",
          "1 when the host service answered its last health check, 0 when not.", _service_up),
    Gauge("pengumon_service_check_seconds", None, "s", "Time the host service took to answer its last health check.",
          _service_check_seconds),
    Gauge("pengumon_collector_status", "pengumon.device.status", "{state}",
          "1 for the collector's current status, 0 for the others.", _device_status),
    Gauge("pengumon_probe_loop_age_seconds", "pengumon.probe.loop.age", "s",
          "Seconds since the last probe round finished.", _probe_loop_age),
    Gauge("pengumon_build_info", None, "1", "Version of this build.", _build_info),
]
