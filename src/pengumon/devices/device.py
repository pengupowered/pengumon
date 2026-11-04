"""A configured device: its driver, the schedule it is polled on, and the monitor that keeps its snapshot.

Everything a driver does not do happens here, the same way for every driver: the schedule and on-demand polls,
one span per poll (with the driver's HTTP exchanges as children), duration and outcome metrics, classifying
transport exceptions into error kinds, and logging failures by state (a device that is down for an hour logs a
handful of lines, not one per poll). A capability that stops producing readings is logged by state as well.

Devices that are easily upset by being read (a gateway's web UI) declare a minimum interval between attempts. It is
enforced here, inside the poll, for every caller: the schedule, the API and a forced poll alike. A caller inside the
window gets the latest snapshot, so a burst of callers costs one attempt. Such a device also backs off after
failures, and stops polling after a refused login until its credentials change.
"""

import asyncio
import time
from typing import Callable, Dict, Optional, Protocol

import httpx
from opentelemetry.trace import Status, StatusCode

from pengumon.capabilities import Capability, Driver, DriverError, PollResult
from pengumon.observability import StateLog, get_logger
from pengumon.observability.telemetry import (
    device_gate_handovers, device_poll_duration, device_polls, device_polls_skipped, tracer,
)


# The scheduled loop doubles its wait after each failure up to this, for devices with a minimum interval.
MAX_BACKOFF_SECONDS = 3600.0
MAX_BACKOFF_DOUBLINGS = 16  # 2 ** 1024 does not fit a float: past this the cap applies anyway
# The credentials test may skip the minimum interval, but not be repeated within this many seconds.
CREDENTIALS_TEST_FLOOR_SECONDS = 60.0


class Monitor(Protocol):
    latest_snapshot: object

    def on_unconfigured(self, disabled: bool) -> object: ...

    async def on_error(self, ts: float, error: DriverError) -> object: ...

    async def on_result(self, ts: float, result: PollResult) -> object: ...


def classify(exc: BaseException) -> DriverError:
    """The DriverError for anything a driver's poll raised."""
    if isinstance(exc, DriverError):
        return exc
    detail = str(exc) or type(exc).__name__
    if isinstance(exc, httpx.TimeoutException):
        return DriverError("timeout", detail, transport=True)
    if isinstance(exc, httpx.TransportError):  # connection refused, DNS, reset, ...
        return DriverError("unreachable", detail, transport=True)
    if isinstance(exc, httpx.HTTPError):
        return DriverError("protocol", detail, transport=True)
    return DriverError("internal", f"{type(exc).__name__}: {detail}")


class Device:
    def __init__(self, device_id: str, driver: Driver, monitor: Monitor, *, interval_seconds: float,
                 enabled: bool = True, clock: Callable[[], float] = time.time,
                 monotonic: Callable[[], float] = time.monotonic, config=None,
                 credentials_source: Optional[str] = None, min_interval_seconds: float = 0.0):
        """`config` is the DeviceConfig it was built from (None for devices built directly, in tests);
        `credentials_source` names where its password came from (None: it has none). Neither affects polling.
        `min_interval_seconds` is the least time between two attempts to read the device (0: no limit)."""
        self.id = device_id
        self.driver = driver
        self.monitor = monitor
        self.interval_seconds = interval_seconds
        self.min_interval_seconds = min_interval_seconds
        self.enabled = enabled
        self.config = config
        self.credentials_source = credentials_source
        self._clock = clock  # wall clock: snapshot timestamps and the "recent poll" check
        self._monotonic = monotonic  # outage durations in the logs (not thrown by clock adjustments)
        self._last_attempt: float = 0.0  # when the device was last read (or its reading was last tried)
        self._failures = 0  # consecutive failed attempts, for the backoff
        self._auth_stopped = False  # the login was refused: no polling until set_credentials
        self._credentials_changed = asyncio.Event()
        self._log = get_logger("pengumon.devices", device=device_id, driver=driver.name, host=driver.host)
        self._state = StateLog(self._log, f"Polling {device_id} ({driver.name} at {driver.host})", clock=monotonic)
        self._missing: Dict[Capability, StateLog] = {}
        self._unconfigured_logged: Optional[str] = None
        # One poll at a time per device: the API's test poll after new credentials must not interleave with the
        # scheduled one (driver session state, CPU and rate baselines); it waits and then polls itself.
        self._lock = asyncio.Lock()
        self._attributes = {"device.id": device_id, "device.driver": driver.name}

    def inherit_gate(self, previous: "Device") -> None:
        """Takes over when `previous` (the device this one replaces, same id and driver at the same host) last tried
        to read the device, and its failures in a row. A rebuild during a poll must not start a second one inside
        min_interval_seconds: the cancelled poll left no stored reading for _restore to find."""
        self._last_attempt = max(self._last_attempt, previous._last_attempt)
        self._failures = previous._failures
        device_gate_handovers.add(1, {"device.id": self.id})
        self._log.debug("took over the previous device's gate", failures=self._failures)

    @property
    def consecutive_failures(self) -> int:
        return self._failures

    @property
    def backoff_seconds(self) -> float:
        """How much longer than its interval the schedule waits after a poll now (0 unless failures are backing off a
        device with a minimum interval)."""
        return max(0.0, self._wait() - self.interval_seconds)

    def _skipped(self, reason: str) -> None:
        device_polls_skipped.add(1, {**self._attributes, "reason": reason})

    @property
    def latest_snapshot(self):
        return self.monitor.latest_snapshot

    @property
    def has_credentials(self) -> bool:
        return self.credentials_source is not None

    def set_credentials(self, username: str, password: str, *, source: Optional[str] = None) -> None:
        self.driver.set_credentials(username, password)
        self._auth_stopped = False
        self._failures = 0
        self._credentials_changed.set()  # wakes run() if it stopped on a refused login
        if source is not None:
            self.credentials_source = source

    async def aclose(self) -> None:
        await self.driver.aclose()

    async def run(self) -> None:
        """Polls every interval_seconds until cancelled (measured from the start of one poll to the next). A device
        read recently, before a restart, is not read again at once: the loop waits out the rest of its interval."""
        self._log.debug(f"Polling {self.id} every {self.interval_seconds:g} s")  # the startup line says it at INFO
        delay = await self._restore()
        while True:
            gate = self._gate_remaining()
            if gate > delay:
                self._skipped("gate_wait")
            await asyncio.sleep(max(delay, gate))
            await self._while_auth_stopped()
            started = time.monotonic()
            try:
                await self.poll(force=True)
            except asyncio.CancelledError:
                raise
            except Exception:  # a bug in a monitor; the next poll may well work
                self._log.exception("poll crashed")
            delay = max(0.1, self._wait() - (time.monotonic() - started))
            if self._auth_stopped:  # the credentials test that follows new credentials counts as a poll
                await self._while_auth_stopped()
                delay = max(0.1, self.interval_seconds - (self._clock() - self._last_attempt))

    async def _while_auth_stopped(self) -> None:
        if self._auth_stopped:
            self._skipped("auth_stopped")
        while self._auth_stopped:
            await self._credentials_changed.wait()
            self._credentials_changed.clear()

    async def _restore(self) -> float:
        """Takes the last stored reading as the latest snapshot when it is younger than an interval (a monitor that
        can: restore(oldest_ts)) and returns how long to wait before the first poll (0: poll now)."""
        restore = getattr(self.monitor, "restore", None)
        if restore is None:
            return 0.0
        now = self._clock()
        try:
            ts = await restore(now - self.interval_seconds)
        except Exception as ex:  # an unreadable database only costs the head start
            self._log.warning("could not restore the last reading", error=f"{type(ex).__name__}: {ex}")
            return 0.0
        if ts is None:
            return 0.0
        self._last_attempt = max(self._last_attempt, ts)  # (an attempt handed over by inherit_gate may be newer)
        wait = max(0.0, self.interval_seconds - (now - ts))
        self._log.info(f"{self.id} was read {now - ts:.0f} s ago; the next poll is in {wait:.0f} s",
                       age_s=round(now - ts), wait_s=round(wait))
        return wait

    def _wait(self) -> float:
        """The wait after a poll: the interval, doubled for each failure in a row (up to MAX_BACKOFF_SECONDS) on a
        device with a minimum interval. A refused login is not retried at all (see run)."""
        if not self.min_interval_seconds or not self._failures:
            return self.interval_seconds
        return max(self.interval_seconds, min(MAX_BACKOFF_SECONDS, self.interval_seconds * 2 ** min(self._failures, MAX_BACKOFF_DOUBLINGS)))

    def _gate_remaining(self) -> float:
        return max(0.0, self.min_interval_seconds - (self._clock() - self._last_attempt))

    async def poll(self, force: bool = False, *, bypass_gate: bool = False):
        """Polls now (or returns the current snapshot when the last poll is less than an interval old and
        `force` is false). Never raises for a device problem: that becomes an error snapshot. Inside the minimum
        interval the current snapshot is returned even when forced; `bypass_gate` (the credentials test) allows an
        attempt anyway, but not within CREDENTIALS_TEST_FLOOR_SECONDS of the last one."""
        async with self._lock:
            return await self._poll(force, bypass_gate)

    async def current(self):
        """The snapshot to show without polling: the latest one; else, when a poll is in flight, its result; else
        (before the first poll) a device that is switched off or has no password says so, and any other has a
        "waiting" snapshot. Used by the API, which must never cost the device a read."""
        if self.monitor.latest_snapshot:
            return self.monitor.latest_snapshot
        if self._lock.locked():
            async with self._lock:
                pass
            if self.monitor.latest_snapshot:
                return self.monitor.latest_snapshot
        if not self.enabled or self.driver.missing_configuration():
            return await self.poll()  # nothing is read from the device
        pending = getattr(self.monitor, "pending_snapshot", None)
        return pending() if pending else None

    async def _poll(self, force: bool, bypass_gate: bool = False):
        now = self._clock()
        if not force and now - self._last_attempt < self.interval_seconds and self.monitor.latest_snapshot:
            return self.monitor.latest_snapshot

        if not self.enabled:
            return self._unconfigured("disabled in configuration", disabled=True)
        missing = self.driver.missing_configuration()
        if missing:
            return self._unconfigured(missing, disabled=False)
        self._unconfigured_logged = None

        gate = CREDENTIALS_TEST_FLOOR_SECONDS if bypass_gate else self.min_interval_seconds
        if now - self._last_attempt < gate and self.monitor.latest_snapshot:
            self._skipped("credentials_floor" if bypass_gate else "min_interval")
            self._log.debug(f"{self.id} was read {now - self._last_attempt:.0f} s ago; serving that reading",
                            min_interval_s=gate)
            return self.monitor.latest_snapshot
        self._last_attempt = now

        started = time.perf_counter()
        outcome, kind = "ok", None
        with tracer.start_as_current_span("device.poll", attributes=self._attributes) as span:
            try:
                result = await self.driver.poll()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                error = classify(exc)
                outcome, kind = "error", error.kind
                self._failures += 1
                # Only a device that is easily upset (one with a minimum interval) stops: a router reports a
                # passing lockout the same way as a wrong password, and must not go quiet for good over it
                if error.kind == "auth" and self.min_interval_seconds and not self._auth_stopped:
                    self._auth_stopped = True
                    self._log.warning(f"{self.id} refused the login: not polling it again until its credentials "
                                      "are changed", error="auth")
                span.set_status(Status(StatusCode.ERROR, error.message))
                span.set_attribute("error.type", error.kind)
                logged = self._state.failure(f"{error.kind}: {error.message}", key=error.kind)
                if error.kind == "internal":
                    span.record_exception(exc)
                    # With the state log's cadence (first time, changed error, reminders), not on every poll.
                    log = self._log.exception if logged else self._log.debug
                    log("driver raised an unexpected exception", error=error.kind)
                snapshot = await self.monitor.on_error(self._clock(), error)
            else:
                self._failures = 0
                span.set_attribute("device.readings", sorted(c.value for c in result.readings))
                if result.missing:
                    outcome = "partial"
                    span.set_attribute("device.missing", sorted(c.value for c in result.missing))
                self._state.success()
                self._track_missing(result)
                snapshot = await self.monitor.on_result(self._clock(), result)
        attributes = {**self._attributes, "outcome": outcome}
        if kind:
            attributes["error.type"] = kind
        device_poll_duration.record(time.perf_counter() - started, attributes)
        device_polls.add(1, attributes)
        return snapshot

    def _unconfigured(self, reason: str, *, disabled: bool):
        if self._unconfigured_logged != reason:
            self._unconfigured_logged = reason
            self._log.info(f"{self.id} is not polled: {reason}")
        device_polls.add(1, {**self._attributes, "outcome": "unconfigured"})
        return self.monitor.on_unconfigured(disabled)

    def _track_missing(self, result: PollResult) -> None:
        for capability in self.driver.offers:
            state = self._missing.get(capability)
            reason = result.missing.get(capability)
            if reason is not None:
                if state is None:
                    state = self._missing[capability] = StateLog(
                        self._log.bind(capability=capability.value), f"{self.id} {capability.value}",
                        clock=self._monotonic)
                state.failure(reason)
            elif state is not None:
                state.success()
