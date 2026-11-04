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
from pengumon.observability.runtime import loop_monitor, process_stats

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


def _process(key: str) -> Callable[[Any], List[Sample]]:
    def collect(state) -> List[Sample]:
        return [({}, process_stats()[key])]
    return collect


def _process_cpu(state) -> List[Sample]:
    stats = process_stats()
    return [({"mode": "user"}, stats["user"]), ({"mode": "system"}, stats["system"])]


def _asyncio_tasks(state) -> List[Sample]:
    return [({}, loop_monitor.tasks)]  # counted on the loop at its last sample, not here on the export thread


def _dns_stuck(state) -> List[Sample]:
    return [({}, state.prober.lookups_stuck)] if state.prober is not None and hasattr(state.prober, "lookups_stuck") \
        else []


def _sample_buffer(state) -> List[Sample]:
    size = getattr(state.repository, "sample_buffer_size", None)
    return [({}, size)] if size is not None else []


def _db_size(state) -> List[Sample]:
    sizes = state.repository.file_sizes() if hasattr(state.repository, "file_sizes") else {}
    return [({"file": name}, size) for name, size in sizes.items()]


def _ws_clients(state) -> List[Sample]:
    from pengumon.api.ws import ws_manager  # (not at import: the api package imports this one)
    return [({}, len(ws_manager.active_connections))]


def _per_device(getter) -> Callable[[Any], List[Sample]]:
    def collect(state) -> List[Sample]:
        return [({"device": d.id}, getter(d)) for d in (state.devices.all() if state.devices is not None else ())
                if d.enabled]
    return collect


def _alerts_held(state) -> List[Sample]:
    notifier = state.notifier
    return [({}, notifier.pending_count())] if notifier is not None and hasattr(notifier, "pending_count") else []


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
    Gauge("pengumon_line_load_percent", "pengumon.line.load", "%", "Current WAN load, percent of line capacity.", _line_load),
    Gauge("pengumon_line_capacity_mbps", "pengumon.line.capacity", "Mbit/s", "Line capacity in use for load calculations.",
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
    Gauge("pengumon_process_memory_rss_bytes", "pengumon.process.memory.rss", "By",
          "Resident memory of the pengumon process (Linux).", _process("rss")),
    Gauge("pengumon_process_cpu_time_seconds", "pengumon.process.cpu.time", "s",
          "CPU time the pengumon process has used since it started, by mode (user, system); graph its rate.",
          _process_cpu),
    Gauge("pengumon_process_threads", "pengumon.process.threads", "{thread}", "Threads of the pengumon process.",
          _process("threads")),
    Gauge("pengumon_process_open_fds", "pengumon.process.open_fds", "{fd}",
          "Open file descriptors of the pengumon process (Linux and macOS).", _process("fds")),
    Gauge("pengumon_asyncio_tasks", "pengumon.asyncio.tasks", "{task}",
          "Asyncio tasks alive at the event-loop monitor's last sample.", _asyncio_tasks),
    Gauge("pengumon_dns_lookups_stuck", "pengumon.dns.lookups.stuck", "{lookup}",
          "System-resolver DNS lookups still running past their timeout (each holds a thread until the resolver gives up).",
          _dns_stuck),
    Gauge("pengumon_db_sample_buffer", "pengumon.db.sample_buffer", "{row}",
          "Probe samples buffered in memory, waiting for the next commit (the buffer is capped while writes fail).",
          _sample_buffer),
    Gauge("pengumon_db_file_size_bytes", "pengumon.db.file.size", "By",
          "Size of the SQLite database file and of its write-ahead log, by file (main, wal).", _db_size),
    Gauge("pengumon_ws_clients", "pengumon.ws.clients", "{client}", "Dashboards connected to the live stream.",
          _ws_clients),
    Gauge("pengumon_device_failures", "pengumon.device.failures", "{poll}",
          "Consecutive failed polls of a device (0 once a poll works).", _per_device(lambda d: d.consecutive_failures)),
    Gauge("pengumon_device_backoff_seconds", "pengumon.device.backoff", "s",
          "How much longer than its interval a device is waited for after failures (devices with a minimum interval).",
          _per_device(lambda d: d.backoff_seconds)),
    Gauge("pengumon_alerts_held", "pengumon.alerts.held", "{alert}",
          "Alerts waiting for their time (the delay setting or the end of quiet hours).", _alerts_held),
    Gauge("pengumon_build_info", None, "1", "Version of this build.", _build_info),
]


# The gauges of the device modules (cable line, line load, router, Wi-Fi and mesh) live in their own table.
from pengumon.observability.module_gauges import MODULE_GAUGES  # noqa: E402  (it needs Gauge, defined above)

GAUGES += MODULE_GAUGES
