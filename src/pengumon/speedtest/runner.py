"""The speed test runner: decides when a scheduled test is due, runs a test (idle latency, download, upload), records it.

The scheduler (`run`) wakes every TICK_SECONDS and reads `config.speedtest` each time, so a setting applies at the next
tick. Rules (docs/speedtest.md):

* Slots every `interval_hours`, shifted by an install-specific offset (random, stored once in meta). The next scheduled
  test is the first slot after the last recorded test (a skip for the data cap counts; a skip for a busy line does not,
  but the slot is not tried again: the next one is). A restart therefore never causes an extra test: nothing is due
  until a slot passes. With no test recorded yet, the reference is when this run began.
* Only inside the window (`window_start` to `window_end` in general.timezone), when one is set.
* Before a scheduled test: the monthly data cap (a `skipped` record, once per slot), then the line (load at or above
  `busy_load_pct`: retry in 10 minutes, at most 3 times, then a `skipped` record). Unknown load does not block.
* One test at a time. A manual run ignores the window and the busy line, not the cap; it is refused while another runs.
* A test never raises into the scheduler: a provider failure is a `failed` (or `partial`) record with a short reason.
* Each direction runs for about TARGET_SECONDS of data (or its cap, TIME_LIMIT_SECONDS at most). While it runs, the
  router's WAN counters are read about once a second (speedtest/router.py, through the device registry: the polls'
  own driver and session) and the line's peak rate is recorded beside the test's own (`wan_down_mbps`,
  `wan_up_mbps`); which of the two answers is decided when read (speedtest/quality.headline).

While a test runs, and for the judged window after it, `is_masking()` is true and the classifier does not open
incidents for the delay and loss the test causes (engine.classifier.rules.apply_speedtest). The mask is bounded whatever
happens: a direction that has not ended PHASE_LIMIT_SECONDS after it began is canceled (a failed direction), and
`is_masking` gives up on a running flag older than MASK_LIMIT_SECONDS, so no fault can be hidden for longer than the
test's own limits and the tail, even by a bug that leaves the flag set.

Turning the test off while one runs stops it within STOP_CHECK_SECONDS (recorded as failed, with the bytes it moved).
"""

import asyncio
import logging
import secrets
import statistics
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from pengumon.observability.telemetry import speedtest_bytes, speedtest_runs
from pengumon.speedtest.grade import bufferbloat_grade
from pengumon.speedtest.mask import mask_tail_seconds
from pengumon.speedtest.providers.base import ProviderError
from pengumon.speedtest.providers.registry import make_provider
from pengumon.speedtest.router import SAMPLE_S, Reader, RouterSampler
from pengumon.speedtest.schedule import due_slot, month_bounds, open_at, slot_after, spread_offset
from pengumon.timeofday import zone
from pengumon.speedtest.quality import headline, trusted

_log = logging.getLogger("pengumon.speedtest")

OFFSET_KEY = "speedtest_offset_s"
SLOW_KEY = "speedtest_slow_alerted"   # meta: "1" while the last evaluated test was slow and the alert was sent
TICK_SECONDS = 15.0
STARTUP_DELAY_SECONDS = 60.0  # after the start: the probes have a minute of history to take the idle latency from
TIME_LIMIT_SECONDS = 15.0  # per direction
TARGET_SECONDS = 8.0  # a direction ends once data has flowed this long (or at its data cap, or the time limit)
# A direction still running this long after it began is canceled: the providers end by themselves well before (15 s
# and a few seconds of grace; M-Lab's server lookup up to 18 s more), so this only catches what they missed.
PHASE_LIMIT_SECONDS = TIME_LIMIT_SECONDS + 45.0
# The longest a test can run (idle latency, two directions, the record): past it, a running flag no longer masks.
MASK_LIMIT_SECONDS = 2 * PHASE_LIMIT_SECONDS + 30.0
STOP_CHECK_SECONDS = 1.0  # how often a running test looks whether the speed test was turned off
STOPPED_REASON = "stopped: the speed test was turned off"
IDLE_WINDOW_SECONDS = 30.0
BUSY_RETRY_SECONDS = 600.0
BUSY_RETRIES = 3
BUSY_REASON = "line busy"
MB = 1_000_000
REASON_MAX = 300  # characters of a test's reason that are kept


def cut_reason(text: str, limit: int = REASON_MAX) -> str:
    """At most `limit` characters, never ending in part of a word: a LibreSpeed server's host cut in two could not be
    found, and left out, where the reason is shown without it (the MCP tools, the AI pack)."""
    if len(text) <= limit:
        return text
    head = text[:limit]
    if not (text[limit].isspace() or head[-1].isspace()):  # the cut falls inside a word: the word goes
        parts = head.rsplit(None, 1)
        head = parts[0] if len(parts) == 2 else ""
    return head.rstrip()


def grade_of(row: Dict[str, Any]) -> Optional[str]:
    return bufferbloat_grade(row.get("idle_ms"), row.get("loaded_down_ms"), row.get("loaded_up_ms"))


class SpeedTestRunner:
    def __init__(self, config, repository, line_load=None, provider_factory: Callable = make_provider,
                 clock: Callable[[], float] = time.time, spawn: Optional[Callable] = None,
                 offset_source: Callable[[], int] = lambda: secrets.randbelow(86400),
                 notify_slow: Optional[Callable[..., None]] = None, wan_reader: Optional[Reader] = None):
        self.config = config  # the AppConfig: speedtest, general.timezone, targets, classifier, prober are read live
        self.repository = repository
        self.line_load = line_load
        # a light read of the router's WAN counters (DeviceRegistry.read_wan_counters), sampled during each direction
        self._wan_reader = wan_reader
        self.wan_sample_s = SAMPLE_S  # how often the counters are read during a direction
        self._factory = provider_factory
        self.clock = clock
        self._spawn = spawn or (lambda coro: asyncio.ensure_future(coro))
        self._offset_source = offset_source
        self._drawn: Optional[str] = None  # the offset drawn while none was stored (kept in memory until stored)
        self._notify_slow = notify_slow   # (down_mbps, up_mbps, plan_down, plan_up, alert_pct) -> None
        self.running = False
        self.last_ok: Optional[Dict[str, Any]] = None  # the last test that worked: the gauges (speedtest/gauges.py)
        self._running_since = 0.0
        self._mask_until = 0.0
        self._started_at = clock()
        self._enabled_since: Optional[float] = None
        self._was_off = False
        self._busy_slot: Optional[float] = None
        self._busy_retries = 0
        self._retry_at = 0.0
        self._skipped_slot: Optional[float] = None
        self._tasks: Set[asyncio.Task] = set()

    @property
    def cfg(self):
        return self.config.speedtest

    # --- the mask -------------------------------------------------------------------------------------------------

    def _start(self) -> None:
        """Takes the one-test-at-a-time flag (the callers check it first, with no await in between)."""
        self.running, self._running_since = True, self.clock()

    def is_masking(self) -> bool:
        """Whether the classifier should treat a fault as the test's doing: a test is running (for at most
        MASK_LIMIT_SECONDS), or ended less than the judged window ago."""
        now = self.clock()
        return (self.running and now - self._running_since < MASK_LIMIT_SECONDS) or now < self._mask_until

    # --- the schedule ---------------------------------------------------------------------------------------------

    async def _offset(self, interval_s: float, store: bool = False) -> float:
        """The install's offset in seconds, created once (random within a day) and kept in meta. For an interval of
        two days or more it also picks the install's day within the interval, from the same number, so weekly tests are
        not all on the same weekday (the slots count from the epoch, a Thursday), while the time of day stays.
        Only `store` writes it (the scheduler: `run` at its start, `tick`); a read (`next_run`, so GET /api/speedtests)
        writes nothing: with none stored yet it answers with the number drawn here, which is the one the scheduler
        then stores."""
        raw = await self.repository.get_meta(OFFSET_KEY)
        if raw is None or not raw.lstrip("-").isdigit():
            if self._drawn is None:
                self._drawn = str(int(self._offset_source()))
            raw = self._drawn
            if store:
                await self.repository.set_meta(OFFSET_KEY, raw)
        return spread_offset(int(raw), interval_s)

    async def ensure_offset(self) -> None:
        """Stores the install's offset when there is none yet (the scheduler's start)."""
        await self._offset(86400.0, store=True)

    async def _reference(self) -> float:
        """The last recorded test that moves the schedule: any but a skip for a busy line. With none, when this run
        began (or, after the feature was turned on while the app ran, when that happened)."""
        row = await self.repository.latest_speed_test_moving_schedule(BUSY_REASON)
        reference = row["ts"] if row is not None else self._started_at
        return max(reference, self._enabled_since) if self._enabled_since is not None else reference

    async def _due(self, now: float, store: bool = False) -> Tuple[float, float]:
        """(the slot the next scheduled test belongs to, the moment it may start: the slot, a busy retry, and the window)."""
        interval = self.cfg.interval_hours * 3600.0
        offset = await self._offset(interval, store)
        slot = due_slot(await self._reference(), now, interval, offset)
        if slot == self._skipped_slot:
            slot = slot_after(slot, interval, offset)
        at = slot
        if self._busy_slot == slot:
            at = max(at, self._retry_at)
        cfg = self.cfg
        if cfg.window_start and cfg.window_end:
            at = open_at(max(at, now), cfg.window_start, cfg.window_end, zone(self.config.general.timezone))
        return slot, at

    async def next_run(self) -> Optional[float]:
        """When the next scheduled test may start, or None when the feature is off."""
        if not self.cfg.enabled:
            return None
        return (await self._due(self.clock()))[1]

    async def run(self) -> None:
        """The scheduler: a task that lives until shutdown."""
        try:
            await self.ensure_offset()
        except Exception as e:
            _log.error(f"Speed test scheduler: could not store the offset: {e}", exc_info=True)
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                _log.error(f"Speed test scheduler: {e}", exc_info=True)
            await asyncio.sleep(TICK_SECONDS)

    async def tick(self) -> None:
        """One look at the schedule: starts a scheduled test when one is due."""
        now = self.clock()
        if not self.cfg.enabled:
            self._was_off, self._enabled_since = True, None
            self._busy_slot, self._busy_retries, self._skipped_slot = None, 0, None
            return
        if self._was_off:  # turned on while the app ran: the schedule starts now, nothing is caught up on
            self._was_off, self._enabled_since = False, now
        if self.running:
            return
        slot, at = await self._due(now, store=True)
        if now < at or now - self._started_at < STARTUP_DELAY_SECONDS:
            return
        used, cap = await self.month_used_bytes(now), self.cfg.monthly_cap_bytes
        if self.running:  # a manual test was started while the data use was read
            return
        if cap and used >= cap:
            await self._skip(now, f"monthly data cap reached ({used / MB:.0f} of {cap / MB:.0f} MB)")
            return
        reading = self.line_load.current(now) if self.line_load is not None else None
        if reading is not None and reading.pct >= self.cfg.busy_load_pct:
            if self._busy_slot != slot:
                self._busy_slot, self._busy_retries = slot, 0
            if self._busy_retries >= BUSY_RETRIES:
                self._skipped_slot, self._busy_slot = slot, None
                await self._skip(now, f"{BUSY_REASON} ({reading.pct:.0f}% load, over {self.cfg.busy_load_pct}% "
                                      f"after {BUSY_RETRIES} retries)")
                return
            self._busy_retries += 1
            self._retry_at = now + BUSY_RETRY_SECONDS
            return
        self._busy_slot, self._busy_retries = None, 0
        self._start()  # taken here, not in _run: nothing can start a second test in between
        await self._run("scheduled")

    async def _skip(self, now: float, reason: str) -> None:
        _log.info(f"Speed test skipped: {reason}")
        await self._record({"ts": now, "trigger": "scheduled", "provider": self.cfg.provider, "status": "skipped",
                            "reason": reason})

    # --- data use -------------------------------------------------------------------------------------------------

    async def month_used_bytes(self, now: Optional[float] = None) -> int:
        """Bytes the speed tests moved this calendar month (general.timezone)."""
        start, end = month_bounds(self.clock() if now is None else now, zone(self.config.general.timezone))
        return await self.repository.speed_test_bytes(start, end)

    # --- the manual run -------------------------------------------------------------------------------------------

    async def run_now(self) -> Optional[str]:
        """Starts a manual test in the background. None when it started, else why it did not."""
        cfg = self.cfg
        if not cfg.enabled:
            return "the speed test is off"
        if self.running:
            return "a speed test is already running"
        used, cap = await self.month_used_bytes(), cfg.monthly_cap_bytes
        if cap and used >= cap:
            return f"the monthly data cap is reached ({used / MB:.0f} of {cap / MB:.0f} MB)"
        if self.running:  # (taken while the cap was read)
            return "a speed test is already running"
        self._start()
        try:
            task = self._spawn(self._run("manual"))
        except BaseException:
            self.running = False
            raise
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return None

    async def aclose(self) -> None:
        """Ends a manual test still running (shutdown); it records itself as interrupted."""
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    # --- one test -------------------------------------------------------------------------------------------------

    def _public_ids(self) -> List[str]:
        return [t.id for t in self.config.targets if t.category == "public"]

    async def _median_latency(self, start: float, end: float) -> Optional[float]:
        ids = self._public_ids()
        if not ids:
            return None
        try:
            rows = await self.repository.get_raw_samples(start, end, ids)
        except Exception as e:
            _log.warning(f"Speed test: could not read the probe latencies: {e}")
            return None
        values = [r["latency_ms"] for r in rows if r["success"] and r["latency_ms"] is not None]
        return round(statistics.median(values), 1) if values else None

    async def _phase(self, call) -> Tuple[Optional[Any], Optional[ProviderError]]:
        """One direction, watched: canceled when the speed test is turned off (_Stopped) or when it runs past
        PHASE_LIMIT_SECONDS (a failed direction)."""
        task = asyncio.ensure_future(call())
        began = time.monotonic()
        try:
            while True:
                done, _ = await asyncio.wait({task}, timeout=STOP_CHECK_SECONDS)
                if done:
                    return task.result(), None
                if not self.cfg.enabled:
                    raise _Stopped()
                if time.monotonic() - began > PHASE_LIMIT_SECONDS:
                    return None, ProviderError("did not finish in time")
        except ProviderError as e:
            return None, e
        except (asyncio.CancelledError, _Stopped):
            raise
        except Exception as e:  # a provider bug is a failed test, never an exception out of the scheduler
            _log.error(f"Speed test provider error: {e.__class__.__name__}", exc_info=True)
            return None, ProviderError(f"unexpected error: {e.__class__.__name__}")
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def _run(self, trigger: str) -> None:
        """Runs one test and records it. `self.running` was set by the caller."""
        cfg = self.cfg.model_copy()  # what this test runs on, whatever is changed meanwhile
        started = self.clock()
        row: Dict[str, Any] = {"ts": started, "trigger": trigger, "provider": cfg.provider, "status": "failed"}
        provider = None
        phase = None  # the column of the bytes of the direction running now (an interrupted one records what it moved)
        try:
            provider = self._factory(cfg)
            row["idle_ms"] = await self._median_latency(started - IDLE_WINDOW_SECONDS, started)

            t0 = self.clock()
            phase = "bytes_down"
            # the line's own rate, from the router's counters
            async with RouterSampler(self._wan_reader, self.wan_sample_s) as router:
                down, down_err = await self._phase(lambda: provider.download(cfg.download_bytes, TIME_LIMIT_SECONDS,
                                                                             target_s=TARGET_SECONDS))
            row["bytes_down"] = down.bytes if down else (down_err.moved if down_err else 0)
            t1 = self.clock()
            row["loaded_down_ms"] = await self._median_latency(t0, t1)
            row["wan_down_mbps"] = router.peak(rx=True) if down else None

            phase = "bytes_up"
            async with RouterSampler(self._wan_reader, self.wan_sample_s) as router:
                up, up_err = await self._phase(lambda: provider.upload(cfg.upload_bytes, TIME_LIMIT_SECONDS,
                                                                       target_s=TARGET_SECONDS))
            row["bytes_up"] = up.bytes if up else (up_err.moved if up_err else 0)
            phase = None
            t2 = self.clock()
            row["loaded_up_ms"] = await self._median_latency(t1, t2)
            row["wan_up_mbps"] = router.peak(rx=False) if up else None

            row["server"] = provider.server
            row["down_mbps"] = round(down.mbps, 2) if down and down.mbps is not None else None
            row["up_mbps"] = round(up.mbps, 2) if up and up.mbps is not None else None
            if down_err and up_err and down_err.reason == up_err.reason:  # (a refusal, say: once is enough)
                errors = [f"download and upload: {down_err.reason}"]
            else:
                errors = [f"{name}: {err.reason}" for name, err in (("download", down_err), ("upload", up_err)) if err]
            worked = [x for x in (down, up) if x is not None]
            row["status"] = "ok" if len(worked) == 2 else "partial" if worked else "failed"
            row["reason"] = cut_reason("; ".join(errors)) or None
            if errors:
                _log.warning(f"Speed test ({trigger}): {row['reason']}")
        except (asyncio.CancelledError, _Stopped) as e:
            stopped = isinstance(e, _Stopped)
            _log.info(f"Speed test ({trigger}) {STOPPED_REASON if stopped else 'interrupted: the app stopped'}")
            row.update(status="failed", reason=STOPPED_REASON if stopped else "interrupted: the app stopped")
            if phase is not None:  # what the interrupted direction moved still counts toward the monthly data cap
                moved = getattr(provider, "moved", None)
                row[phase] = moved() if moved is not None else 0
            if not stopped:
                raise
        except Exception as e:
            _log.error(f"Speed test failed: {e}", exc_info=True)
            row.update(status="failed", reason=f"unexpected error: {e.__class__.__name__}")
        finally:
            ended = self.clock()
            row["duration_s"] = round(ended - started, 1)
            self._mask_until = ended + mask_tail_seconds(self.config.classifier.window_probes,
                                                         self.config.prober.interval_seconds)
            self.running = False
            if provider is not None:
                try:
                    await provider.aclose()
                except Exception:
                    pass
            await self._record(row)
            if row["status"] == "ok":
                await self._check_slow(row)

    async def _check_slow(self, row: Dict[str, Any]) -> None:
        """The slow-line alert (speedtest.slow_alert_pct): after a test that measured both directions, when the plan
        speed is set and a direction's headline speed (the line's rate the router measured, else the test's own:
        quality.headline) is under that share of it. Sent once; again only after a test above the line."""
        try:
            pct, line = self.cfg.slow_alert_pct, self.config.line
            plan_down, plan_up = line.down_mbps, line.up_mbps
            if not pct or not (plan_down or plan_up) or self._notify_slow is None:
                return
            down, up = trusted(row)  # a direction too short to measure the line judges nothing either way
            judged = [(value, plan) for value, plan in ((down, plan_down), (up, plan_up)) if value is not None and plan]
            if not judged:
                return
            slow = any(value < plan * pct / 100.0 for value, plan in judged)
            alerted = (await self.repository.get_meta(SLOW_KEY)) == "1"
            if slow and not alerted:
                await self.repository.set_meta(SLOW_KEY, "1")
                best = headline(row)
                self._notify_slow(best["down_mbps"], best["up_mbps"], plan_down, plan_up, pct,
                                  measured_by=best["measured_by"], note=best["note"])
            elif not slow and alerted:
                await self.repository.set_meta(SLOW_KEY, "0")
        except Exception as e:  # an alert never costs the test
            _log.error(f"Speed test: slow-line alert failed: {e}", exc_info=True)

    async def load_last(self) -> None:
        """At the start: the last test that worked, from the database, for the gauges."""
        try:
            self.last_ok = await self.repository.latest_speed_test(["ok"])
        except Exception as e:
            _log.warning(f"Speed test: could not read the last result: {e}")

    async def _record(self, row: Dict[str, Any]) -> None:
        speedtest_runs.add(1, {"status": row["status"], "trigger": row["trigger"], "provider": row["provider"]})
        for direction, column in (("download", "bytes_down"), ("upload", "bytes_up")):
            if row.get(column):
                speedtest_bytes.add(row[column], {"direction": direction})
        if row["status"] == "ok":
            self.last_ok = dict(row)
        try:
            await self.repository.insert_speed_test(row)
        except Exception as e:
            _log.error(f"Speed test: could not store the record: {e}", exc_info=True)


class _Stopped(Exception):
    """The speed test was turned off while a test ran."""
