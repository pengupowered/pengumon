"""The current snapshot of each device role, in the shapes the API, the live stream and the tables use today.

A monitor receives every poll outcome from its Device (a result, an error, or "not configured"), builds the
snapshot, keeps it as `latest_snapshot` and stores it. Storage failures are logged by state and never turn into a
device error: a full disk is not a modem fault.

Snapshots are stored per device id (schema v9): LineMonitor as a line sample (pengumon.lines.docsis.line_sample),
RouterMonitor as a device sample (pengumon.devices.samples.device_sample). The snapshot shapes themselves stay until
step 5 of docs/superpowers/plans/2026-10-31-device-capabilities.md.
"""

from dataclasses import dataclass
from typing import Awaitable, Callable, Dict, List, Optional

from pengumon.capabilities import (
    Capability, ClientsReading, DriverError, LineReading, MeshReading, PollResult, SystemReading, TrafficReading,
    WanCounters,
)
from pengumon.devices import traffic
from pengumon.devices.events import EventTracker
from pengumon.devices.levels import RouterLevelsConfig
from pengumon.engine.classifier_settings import ClassifierConfig
from pengumon.devices.samples import device_sample, mesh_counts
from pengumon.devices.wan import Rate, WanRates
from pengumon.grades import GOOD, WARN, Range, known_grades
from pengumon.lines import docsis
from pengumon.lines.docsis import (
    TECHNOLOGY, DocsisErrorTracker, grade_snapshot, line_sample, snapshot_from_sample, summarize,
)
from pengumon.lines.levels import LineSignalConfig
from pengumon.devices.snapshots import RouterMetricsSnapshot
from pengumon.lines.docsis_model import DocsisSignalSnapshot
from pengumon.observability import StateLog, get_logger
from pengumon.observability.telemetry import line_errors, line_restarts, router_restarts
from pengumon.timeofday import viewer_zone

# The router levels (the Settings page's router_levels: section, pengumon.devices.levels) a monitor grades by until
# watch_levels hands it the live config; never changed.
DEFAULT_LEVELS = RouterLevelsConfig()
DEFAULT_BUSY_LOAD_PCT = ClassifierConfig.model_fields["busy_load_pct"].default  # until watch_load says otherwise


def router_ranges(levels: RouterLevelsConfig, busy_load_pct: float) -> Dict[str, Range]:
    """The good zone of each router metric. CPU is warn above cpu_warning_pct (as the snapshot status always was;
    the dashboard used to color at 50 and 80 %), and the WAN load, in % of the line capacity, is warn from the
    classifier's busy_load_pct (the load at which delay stops counting as a fault) and critical when saturated
    (wan_critical_pct)."""
    wan = Range("%", 0.0, 100.0, good_high=busy_load_pct, critical_high=levels.wan_critical_pct)
    return {
        "cpu": Range("%", 0.0, 100.0, good_high=levels.cpu_warning_pct),
        "mem": Range("%", 0.0, 100.0, good_high=levels.mem_warning_pct, critical_high=levels.mem_critical_pct),
        "wan_rx": wan,
        "wan_tx": wan,
    }


@dataclass(frozen=True)
class UnconfiguredMessages:
    """What the UI shows for a device that is switched off in config, or has no credentials yet."""

    disabled: str
    missing_credentials: str


class _Store:
    """Stores a snapshot under its device id: `convert` gives the stored form, `insert` (a repository method) writes
    it. Failures, of either, are logged by state (one warning per outage, not one per poll)."""

    def __init__(self, what: str, insert: Optional[Callable[[object], Awaitable[None]]],
                 convert: Callable[[str, object], object], device_id: Optional[str]):
        if insert is not None and not device_id:
            raise ValueError(f"storing {what} needs the device id they belong to")
        self._insert = insert
        self._convert = convert
        self._device_id = device_id
        context = {"device": device_id} if device_id else {}
        self._state = StateLog(get_logger("pengumon.devices", **context), f"Storing {what}")

    async def __call__(self, snapshot, **extra) -> None:
        if self._insert is None:
            return
        try:
            await self._insert(self._convert(self._device_id, snapshot, **extra))
        except Exception as ex:
            self._state.failure(f"{type(ex).__name__}: {ex}")
        else:
            self._state.success()


class LineMonitor:
    """The access line (today: DOCSIS) as a DocsisSignalSnapshot. Error snapshots are stored too: an unreadable
    modem during an incident is evidence."""

    def __init__(self, repository=None, *, messages: UnconfiguredMessages, role: str = "gateway",
                 device_id: Optional[str] = None):
        """`device_id`: the device this monitor belongs to, which its snapshots are stored under (needed with a
        repository)."""
        self.latest_snapshot: Optional[DocsisSignalSnapshot] = None
        self.device_id = device_id
        self._repository = repository
        self._messages = messages
        self._role = role
        self._errors = DocsisErrorTracker()
        self._config = None  # the live config whose line_signal section it grades by (watch_levels)
        self._probe_loss = None
        self._seen: Optional[tuple] = None  # (uncorrectable, corrected) totals of the last good reading, for the counters
        self._store = _Store("modem snapshots", repository.insert_line_sample if repository is not None else None,
                             line_sample, device_id)

    def watch_levels(self, config) -> None:
        """Grades by `config.line_signal` as it is at each poll (the live config: a saved level applies at the next
        poll, with no applier)."""
        self._config = config

    @property
    def levels(self) -> LineSignalConfig:
        """The line levels in force now (the defaults until watch_levels)."""
        return getattr(self._config, "line_signal", None) or docsis.DEFAULT_LEVELS

    def watch_probes(self, probe_loss: Callable[[float, float], Awaitable[Optional[bool]]]) -> None:
        """Cross-checks new line errors against the probes: `probe_loss(start, end)` says whether the probes to the
        gateway and the ISP hops lost packets in that window (None: it cannot tell)."""
        self._probe_loss = probe_loss

    async def _cross_check(self, snapshot: DocsisSignalSnapshot) -> None:
        """Adds to the note of new errors whether the probes noticed them: no loss in the same window means the errors
        had no measurable effect. A failing check changes nothing."""
        if self._probe_loss is None or snapshot.uncorrectables_recent <= 0:
            return
        try:
            lost = await self._probe_loss(snapshot.timestamp - (snapshot.uncorrectables_interval_seconds or 0.0),
                                          snapshot.timestamp)
        except Exception as ex:
            get_logger("pengumon.devices", device=self.device_id).warning(
                f"Probe cross-check of line errors failed: {type(ex).__name__}: {ex}")
            return
        snapshot.errors_probe_loss = lost
        if lost is not None:
            tail = ("the probes to the gateway or the ISP lost packets in that window" if lost
                    else "no packet loss on the probes in that window: no measurable effect")
            snapshot.errors_note = f"{snapshot.errors_note}; {tail}" if snapshot.errors_note else tail

    def on_unconfigured(self, disabled: bool) -> DocsisSignalSnapshot:
        message = self._messages.disabled if disabled else self._messages.missing_credentials
        self.latest_snapshot = DocsisSignalSnapshot(status="unconfigured", error_message=message)
        return self.latest_snapshot

    def pending_snapshot(self) -> DocsisSignalSnapshot:
        """What to show before the first poll has finished; not kept as the latest snapshot."""
        return DocsisSignalSnapshot(status="unconfigured", error_message=f"Waiting for the first reading from {self._role}.")

    async def restore(self, oldest_ts: float) -> Optional[float]:
        """Takes the newest stored reading as the latest snapshot, when it is no older than `oldest_ts`, so a restart
        does not have to read the modem again. Returns its timestamp (None: nothing restored). A good reading also
        becomes the baseline of the error deltas."""
        if self._repository is None or not self.device_id:
            return None
        sample = await self._repository.get_latest_line_sample(self.device_id)
        if sample is None or sample.technology != TECHNOLOGY or sample.ts < oldest_ts:
            return None
        snapshot = grade_snapshot(snapshot_from_sample(sample), self.levels)
        snapshot.error_message = sample.error_message
        self.latest_snapshot = snapshot
        if snapshot.status not in ("error", "unconfigured"):
            self._errors.seed(snapshot)
            self._seen = (snapshot.uncorrectables_total, None)  # corrected totals are not stored: no baseline
        return sample.ts

    async def on_error(self, ts: float, error: DriverError) -> DocsisSignalSnapshot:
        message = f"Failed to communicate with {self._role}: {error.message}" if error.transport else error.message
        self.latest_snapshot = DocsisSignalSnapshot(timestamp=ts, status="error", error_message=message,
                                                    error_kind=error.kind)
        await self._store(self.latest_snapshot)
        return self.latest_snapshot

    def _count_errors(self, snapshot: DocsisSignalSnapshot) -> None:
        """Counts the codewords that went wrong since the previous good reading (pengumon.line.errors), and a restart
        when a total went backwards (pengumon.line.restarts). The first reading only sets the baseline."""
        downstream = [c for c in snapshot.channels if c.direction == "downstream"]
        now = (snapshot.uncorrectables_total, sum(c.correcteds or 0 for c in downstream))
        before, self._seen = self._seen, now
        if before is None or not self.device_id:
            return
        attributes = {"device": self.device_id}
        if (now[0] < before[0]) or (before[1] is not None and now[1] < before[1]):
            line_restarts.add(1, attributes)
            return
        if now[0] > before[0]:
            line_errors.add(now[0] - before[0], {**attributes, "kind": "uncorrectable"})
        if before[1] is not None and now[1] > before[1]:
            line_errors.add(now[1] - before[1], {**attributes, "kind": "corrected"})

    async def on_result(self, ts: float, result: PollResult) -> DocsisSignalSnapshot:
        reading = result.readings.get(Capability.ACCESS_LINE)
        if not isinstance(reading, LineReading):
            reason = result.missing.get(Capability.ACCESS_LINE, "the device reported no line data")
            return await self.on_error(ts, DriverError("protocol", reason))
        if reading.technology != "docsis":  # the only line technology judged so far (pengumon.lines)
            return await self.on_error(ts, DriverError("unsupported", f"no rules for {reading.technology} lines"))
        levels = self.levels
        self.latest_snapshot = self._errors.apply(summarize(reading.channels, ts, levels), levels)
        self._count_errors(self.latest_snapshot)
        await self._cross_check(self.latest_snapshot)
        await self._store(self.latest_snapshot)
        return self.latest_snapshot


class RouterMonitor:
    """WAN rates and router system data as a RouterMetricsSnapshot. A rate the router could not measure is None,
    never 0: load learning and the charts must not see a quiet line that wasn't there."""

    def __init__(self, repository=None, *, messages: UnconfiguredMessages, model: Optional[str] = None,
                 wan: Optional[WanRates] = None, role: str = "router", device_id: Optional[str] = None):
        self.latest_snapshot: Optional[RouterMetricsSnapshot] = None
        self.device_id = device_id
        self.wan = wan or WanRates()
        self._messages = messages
        self._model = model
        self._role = role
        self._store = _Store("router snapshots", repository.insert_device_sample if repository is not None else None,
                             device_sample, device_id)
        # The client list and the mesh, kept in memory only (the API serves them; the live feed never sees them). A
        # poll that did not read the client list leaves the last reading in place; `clients_at` says how old it is.
        self.latest_clients: Optional[ClientsReading] = None
        self.latest_mesh: Optional[MeshReading] = None
        self.clients_at: Optional[float] = None
        self._store_mesh = _Store("mesh client counts", getattr(repository, "insert_mesh_counts", None),
                                  lambda device_id, counts: counts, device_id)
        self._load = None  # the LineLoad whose capacity the WAN rates are graded against (see watch_load)
        self._busy: Callable[[], float] = lambda: DEFAULT_BUSY_LOAD_PCT
        self._config = None  # the live config whose router_levels section it grades by (watch_levels)
        self._repository = repository
        # uptime, CPU temperature and WAN link speed as last read: (wall time of the reading, value)
        self._extras: Dict[str, tuple] = {}
        self._events = EventTracker(device_id) if device_id else None
        self._event_log = StateLog(get_logger("pengumon.devices", **({"device": device_id} if device_id else {})),
                                   "Storing device events or traffic")
        # The traffic statistics (TRAFFIC_USAGE): why there are none, as the driver last said (None: no reason known),
        # and when some were last stored.
        self.traffic_reason: Optional[str] = None
        self.traffic_read_at: Optional[float] = None

    def watch_levels(self, config) -> None:
        """Grades by `config.router_levels` as it is at each poll (the live config: a saved level applies at the next
        poll, with no applier)."""
        self._config = config

    def watch_load(self, line_load, busy_load_pct: Callable[[], float]) -> None:
        """Grades the WAN rates as a share of the line capacity `line_load` knows (learned or configured, so it can
        change between polls) against the classifier's busy threshold, which `busy_load_pct()` gives at each poll
        (a setting that can change while the app runs)."""
        self._load = line_load
        self._busy = busy_load_pct

    @property
    def ranges(self) -> Dict[str, Range]:
        """The ranges in force now (router levels and busy threshold as they are at this moment)."""
        return router_ranges(getattr(self._config, "router_levels", None) or DEFAULT_LEVELS, self._busy())

    def recent_rates(self) -> List[Rate]:
        return self.wan.recent_rates()

    def pending_snapshot(self) -> RouterMetricsSnapshot:
        """What to show before the first poll has finished; not kept as the latest snapshot."""
        return RouterMetricsSnapshot(status="unconfigured", model_name=self._model,
                                     error_message=f"Waiting for the first reading from {self._role}.")

    def on_unconfigured(self, disabled: bool) -> RouterMetricsSnapshot:
        message = self._messages.disabled if disabled else self._messages.missing_credentials
        self.latest_snapshot = RouterMetricsSnapshot(status="unconfigured", error_message=message,
                                                     model_name=self._model)
        return self.latest_snapshot

    async def on_error(self, ts: float, error: DriverError) -> RouterMetricsSnapshot:
        message = f"Failed to communicate with {self._role}: {error.message}" if error.transport else error.message
        self.latest_snapshot = RouterMetricsSnapshot(timestamp=ts, status="error", error_message=message,
                                                     model_name=self._model, error_kind=error.kind)
        await self._store(self.latest_snapshot)
        return self.latest_snapshot

    def _load_pct(self, rx: Optional[float], tx: Optional[float]):
        """(download, upload) as % of the line capacity; None for a rate that is missing or a capacity not known yet."""
        down = getattr(self._load, "down_mbps", None)
        up = getattr(self._load, "up_mbps", None)
        return (round(rx / down * 100.0, 1) if rx is not None and down else None,
                round(tx / up * 100.0, 1) if tx is not None and up else None)

    async def on_result(self, ts: float, result: PollResult) -> RouterMetricsSnapshot:
        counters = result.readings.get(Capability.WAN_COUNTERS)
        system = result.readings.get(Capability.ROUTER_SYSTEM)
        counters = counters if isinstance(counters, WanCounters) else None
        if counters is None and not isinstance(system, SystemReading):
            reasons = "; ".join(result.missing.values()) or "the router reported nothing"
            return await self.on_error(ts, DriverError("protocol", reasons))
        fresh_system = system if isinstance(system, SystemReading) else None
        system = system if isinstance(system, SystemReading) else SystemReading()
        resets = self.wan.resets
        rx, tx = self.wan.update(ts, counters)
        if self.wan.resets > resets and self.device_id:
            router_restarts.add(self.wan.resets - resets, {"device": self.device_id})
        rx_pct, tx_pct = self._load_pct(rx, tx)
        clients = result.readings.get(Capability.CLIENT_INVENTORY)
        mesh = result.readings.get(Capability.MESH)
        clients = clients if isinstance(clients, ClientsReading) else None
        if clients is not None:
            self.latest_clients, self.clients_at = clients, ts
            self.latest_mesh = mesh if isinstance(mesh, MeshReading) else None
        graded = self.ranges
        grades = known_grades(cpu=graded["cpu"].grade(system.cpu_pct), mem=graded["mem"].grade(system.mem_pct),
                              wan_rx=graded["wan_rx"].grade(rx_pct), wan_tx=graded["wan_tx"].grade(tx_pct))
        # A mesh node the router reports offline is a fault of the network, shown as the card's warning (the last mesh
        # reading stands between client-list reads). The grade is only there for a mesh whose state is known.
        if isinstance(self.latest_mesh, MeshReading) and any(n.online is not None for n in self.latest_mesh.nodes):
            grades["mesh"] = WARN if any(n.online is False for n in self.latest_mesh.nodes) else GOOD
        warning = grades.get("cpu", GOOD) != GOOD or grades.get("mesh", GOOD) != GOOD
        self.latest_snapshot = RouterMetricsSnapshot(
            timestamp=ts,
            wan_rx_bytes=counters.rx_bytes if counters else None,
            wan_tx_bytes=counters.tx_bytes if counters else None,
            wan_rx_rate_mbps=rx,
            wan_tx_rate_mbps=tx,
            cpu_usage_pct=system.cpu_pct,
            mem_usage_pct=system.mem_pct,
            client_count=system.client_count,
            **self._latest_extras(ts, system),
            wan_rx_load_pct=rx_pct,
            wan_tx_load_pct=tx_pct,
            grades=grades,
            model_name=self._model or system.model,
            status="warning" if warning else "healthy",
        )
        if fresh_system is not None:  # (before the row is stored: the first one after a start compares with the last)
            await self._device_events(ts, fresh_system)
        await self._store(self.latest_snapshot, clients=clients, system=fresh_system)
        await self._traffic(result)
        if clients is not None and isinstance(mesh, MeshReading) and self.device_id:
            await self._store_mesh(mesh_counts(self.device_id, ts, mesh))
        return self.latest_snapshot

    def _latest_extras(self, ts: float, system: SystemReading) -> Dict[str, object]:
        """The uptime, CPU temperature and WAN link speed for the snapshot: this poll's, else the last ones read (the
        uptime counted on from its reading)."""
        for name in ("uptime_s", "cpu_temp_c", "wan_link_mbps"):
            value = getattr(system, name)
            if value is not None:
                self._extras[name] = (ts, value)
        out: Dict[str, object] = {}
        for name, (at, value) in self._extras.items():
            out[name] = int(value + max(0.0, ts - at)) if name == "uptime_s" else value
        return out

    async def _device_events(self, ts: float, system: SystemReading) -> None:
        """Restarts and WAN link changes (devices/events.py), stored as they are found. A failure is logged by state."""
        if self._events is None or (system.uptime_s is None and system.wan_link_mbps is None):
            return
        try:
            if not self._events.restored and self._repository is not None:
                self._events.restore(*await self._repository.get_latest_router_extras(self.device_id))
            for event in self._events.check(ts, system.uptime_s, system.wan_link_mbps, system.uptime_wrap_s):
                get_logger("pengumon.devices", device=self.device_id).info(
                    f"{self.device_id}: {event.kind}", **{k: v for k, v in event.detail.items()})
                if self._repository is not None:
                    await self._repository.insert_device_event(self.device_id, event.ts, event.kind, event.detail)
        except Exception as ex:
            self._event_log.failure(f"{type(ex).__name__}: {ex}")
        else:
            self._event_log.success()

    def _day_of(self, window_end: int) -> Optional[str]:
        """The local day an app window is, when it ends exactly at a local midnight (it then covers that day, midnight to
        midnight); None for any other window, which is kept only as the latest. In the zone set in Settings
        (general.timezone). devices/traffic.py adds these days up for ranges longer than a day."""
        return traffic.day_of_window(window_end, viewer_zone(self._config)[0])

    async def _traffic(self, result: PollResult) -> None:
        """Keeps why there is no traffic (said once by the driver, in `missing`) and stores what a poll read."""
        if Capability.TRAFFIC_USAGE in result.missing:
            self.traffic_reason = result.missing[Capability.TRAFFIC_USAGE]
        reading = result.readings.get(Capability.TRAFFIC_USAGE)
        if not isinstance(reading, TrafficReading):
            return
        self.traffic_reason = None
        if self._repository is None or not self.device_id:
            self.traffic_read_at = reading.read_at
            return
        try:
            await self._repository.upsert_traffic_hours(self.device_id, reading.hours, reading.read_at)
            await self._repository.store_traffic_apps(self.device_id, reading.apps, reading.read_at, self._day_of)
        except Exception as ex:
            self._event_log.failure(f"{type(ex).__name__}: {ex}")
        else:
            self.traffic_read_at = reading.read_at
            self._event_log.success()
