"""WAN load relative to the line capacity, so the classifier can tell queuing delay from faults.

A line running near its capacity queues packets in the modem and router (bufferbloat): ping times rise and
intermediate ISP routers drop ICMP replies first. That is normal behavior of a busy line, not a fault, so
the classifier needs to know how busy the line is at the moment it judges the probes.
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Callable, Iterable, List, Optional, Tuple

from pengumon.engine.load_settings import LineConfig
from pengumon.incidents.taxonomy import LOAD_EXPLAINABLE_KINDS
from pengumon.incidents.model import LOAD_IMPLAUSIBLE_PCT

logger = logging.getLogger("pengumon.load")

Rate = Tuple[float, float, float]  # (ts, rx_mbps, tx_mbps)

LIVE_WINDOW_SECONDS = 20.0  # the router is polled every 10 s, so this holds one or two samples
STALE_AFTER_SECONDS = 60.0  # no sample this recent: load unknown, classify without it
BACKFILL_BEFORE_SECONDS = 20.0
BACKFILL_AFTER_SECONDS = 5.0
CAPACITY_PERCENTILE = 0.999
CAPACITY_HISTORY_SECONDS = 7 * 86400
CAPACITY_FLOOR_MBPS = 10.0
# Too little history makes the percentile meaningless (a quiet afternoon would set a tiny capacity and every
# download would look like saturation), so learning waits for 6 hours of 10 s samples.
CAPACITY_MIN_SAMPLES = 2160
CAPACITY_REFRESH_SECONDS = 3600.0
BACKFILL_META_KEY = "incident_load_backfill"


@dataclass(frozen=True)
class LoadReading:
    pct: float
    rx_mbps: float
    tx_mbps: float
    down_mbps: float
    up_mbps: float
    down_source: str  # "configured" or "learned"
    up_source: str

    def describe(self) -> str:
        """The dominant direction, e.g. "download 954 Mbps, 95% of the learned 1000 Mbps capacity"."""
        rx_pct = self.rx_mbps / self.down_mbps * 100.0
        tx_pct = self.tx_mbps / self.up_mbps * 100.0
        if rx_pct >= tx_pct:
            name, rate, pct, cap, source = "download", self.rx_mbps, rx_pct, self.down_mbps, self.down_source
        else:
            name, rate, pct, cap, source = "upload", self.tx_mbps, tx_pct, self.up_mbps, self.up_source
        return f"{name} {rate:.0f} Mbps, {pct:.0f}% of the {source} {cap:.0f} Mbps capacity"


class LineLoad:
    """Line capacity (configured, or learned from the stored WAN rates) and the load derived from the live rates.

    `rates_source` returns the live (ts, rx_mbps, tx_mbps) samples of the enabled WAN device
    (DeviceRegistry.wan_rates_source), oldest first. `device_id` is the device whose stored rates the capacity and the
    incident backfill read: DeviceRegistry.primary(wan_counters), which may be switched off in config and still have
    its history (None: no device reports WAN counters, so there is no history to learn from).
    """

    def __init__(
        self,
        config: Optional[LineConfig] = None,
        rates_source: Optional[Callable[[], Iterable[Rate]]] = None,
        repository=None,
        clock: Callable[[], float] = time.time,
        device_id: Optional[str] = None,
    ):
        self.config = config or LineConfig()
        self.rates_source = rates_source
        self.repository = repository
        self.clock = clock
        self.device_id = device_id
        self.learned_down_mbps: Optional[float] = None
        self.learned_up_mbps: Optional[float] = None
        self._last_refresh: Optional[float] = None
        self._said_no_device = False

    @property
    def down_mbps(self) -> Optional[float]:
        return self.config.down_mbps or self.learned_down_mbps

    @property
    def up_mbps(self) -> Optional[float]:
        return self.config.up_mbps or self.learned_up_mbps

    async def refresh_capacity(self, force: bool = False) -> None:
        """Re-learns the capacity from the last 7 days of the device's WAN rates (at most hourly unless forced)."""
        now = self.clock()
        if not force and self._last_refresh is not None and now - self._last_refresh < CAPACITY_REFRESH_SECONDS:
            return
        self._last_refresh = now
        if self.repository is None or (self.config.down_mbps and self.config.up_mbps):
            return
        if self.device_id is None:
            if not self._said_no_device:
                self._said_no_device = True
                logger.info("Line capacity: no device reports WAN counters, so it cannot be learned; load-aware "
                            "classification needs the line speeds (Settings, Your line)")
            return
        result = await self.repository.get_wan_rate_percentile(
            self.device_id, now - CAPACITY_HISTORY_SECONDS, CAPACITY_PERCENTILE)
        if not result or result[0] < CAPACITY_MIN_SAMPLES:
            logger.info(
                f"Line capacity: {result[0] if result else 0} {self.device_id} samples in 7 days, need "
                f"{CAPACITY_MIN_SAMPLES} to learn it; load-aware classification waits (or set the line speeds in Settings, Your line)"
            )
            return
        _, rx, tx = result
        self.learned_down_mbps = round(max(CAPACITY_FLOOR_MBPS, rx), 1)
        self.learned_up_mbps = round(max(CAPACITY_FLOOR_MBPS, tx), 1)
        logger.info(
            f"Line capacity: down {self.down_mbps:g} Mbps, up {self.up_mbps:g} Mbps "
            f"(learned {self.learned_down_mbps:g}/{self.learned_up_mbps:g} from {result[0]:,} {self.device_id} samples)"
        )

    async def run(self) -> None:
        """Hourly capacity refresh, in its own task so the probe loop never waits for the query."""
        while True:
            try:
                await self.refresh_capacity()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Line capacity refresh failed: {e}")
            try:
                await asyncio.sleep(CAPACITY_REFRESH_SECONDS)
            except asyncio.CancelledError:
                break

    def reading_from(self, rates: Iterable[Rate]) -> Optional[LoadReading]:
        """Peak load over the given samples: max over samples of max(rx/down, tx/up). None without
        samples or without a known capacity. A sample above LOAD_IMPLAUSIBLE_PCT of the capacity is a bad counter
        reading, not load (one stored incident showed 1337 %: 535 Mbps of upload on a 40 Mbps line) and is left out;
        when nothing else remains the load is unknown."""
        down, up = self.down_mbps, self.up_mbps
        if not down or not up:
            return None
        best: Optional[Rate] = None
        best_pct = -1.0
        for sample in rates:
            pct = max(sample[1] / down, sample[2] / up) * 100.0
            if pct > LOAD_IMPLAUSIBLE_PCT:
                logger.warning(f"Ignoring an impossible WAN rate: {sample[1]:.0f}/{sample[2]:.0f} Mbps is {pct:.0f}% "
                               f"of the {down:g}/{up:g} Mbps capacity")
                continue
            if pct > best_pct:
                best, best_pct = sample, pct
        if best is None:
            return None
        return LoadReading(
            pct=round(best_pct, 1),
            rx_mbps=best[1],
            tx_mbps=best[2],
            down_mbps=down,
            up_mbps=up,
            down_source="configured" if self.config.down_mbps else "learned",
            up_source="configured" if self.config.up_mbps else "learned",
        )

    def current(self, now: Optional[float] = None) -> Optional[LoadReading]:
        """Load now, from router samples in [now - 20 s, now]. When a poll was missed the latest sample still
        counts while it is at most 60 s old; older than that the load is unknown (None)."""
        if self.rates_source is None:
            return None
        now = self.clock() if now is None else now
        rates: List[Rate] = [r for r in self.rates_source() if r[0] <= now]
        if not rates or now - rates[-1][0] > STALE_AFTER_SECONDS:
            return None
        window = [r for r in rates if r[0] >= now - LIVE_WINDOW_SECONDS] or rates[-1:]
        return self.reading_from(window)


def is_load_explainable(kind: Optional[str]) -> bool:
    """Whether a stored incident is of a kind heavy load explains: latency/jitter only, or loss seen only at
    the ISP hops (see LOAD_EXPLAINABLE_KINDS). An incident without a kind is not."""
    return kind in LOAD_EXPLAINABLE_KINDS


async def backfill_incident_load(repository, line_load: LineLoad, busy_load_pct: float) -> Optional[int]:
    """One-time annotation of incidents recorded before load awareness: load_pct from the stored WAN rates of the
    line load's device around each one, and expected = 1 for load-explainable ones at or above busy_load_pct.

    Runs once: recorded in the meta table (with the capacity and threshold it used, for the record), and never
    again after that, whatever the capacity or busy_load_pct are later. Outage rules apply from the moment they
    are set: a Settings change never rewrites stored incidents. Incidents without rates around them keep load_pct
    NULL and expected 0; incidents the intermediate-hop backfill marked stay expected. Returns the number of
    incidents annotated, or None when it did not run (already done, or the capacity or the device is not known
    yet, in which case it is retried on the next start).
    """
    if not line_load.down_mbps or not line_load.up_mbps:
        logger.info("Incident load backfill postponed: line capacity unknown")
        return None
    if line_load.device_id is None:
        logger.info("Incident load backfill postponed: no device reports WAN counters")
        return None
    if await repository.get_meta(BACKFILL_META_KEY):
        return None
    ping_done = await repository.get_meta(PING_BACKFILL_META_KEY)
    keep_expected = set(json.loads(ping_done).get("incident_ids", [])) if ping_done else set()
    annotated = expected = 0
    for row in await repository.get_incidents_without_load():
        start = row["start_time"]
        end = row["end_time"] if row["end_time"] is not None else start
        rates = await repository.get_wan_rates(line_load.device_id, start - BACKFILL_BEFORE_SECONDS,
                                               end + BACKFILL_AFTER_SECONDS)
        reading = line_load.reading_from(rates)
        if reading is None:
            continue
        is_expected = (reading.pct >= busy_load_pct and is_load_explainable(row["kind"])) or row["id"] in keep_expected
        await repository.set_incident_load(row["id"], reading.pct, is_expected)
        annotated += 1
        expected += is_expected
    await repository.set_meta(BACKFILL_META_KEY, json.dumps({
        "ts": time.time(), "annotated": annotated, "expected": expected,
        "down_mbps": line_load.down_mbps, "up_mbps": line_load.up_mbps, "busy_load_pct": busy_load_pct,
    }))
    logger.info(f"Incident load backfill: {annotated} incidents annotated, {expected} marked expected")
    return annotated


PING_BACKFILL_META_KEY = "incident_ping_deprioritization_backfill"
PING_BACKFILL_REASON = "intermediate-hop ping deprioritization"
PING_BACKFILL_BEFORE_SECONDS = 10.0
PING_BACKFILL_AFTER_SECONDS = 2.0


def _loss_pct(rows) -> Optional[float]:
    return (sum(1 for r in rows if not r["success"]) / len(rows) * 100.0) if rows else None


async def backfill_ping_deprioritization(repository, targets: Iterable, degraded_loss_pct: float) -> Optional[int]:
    """One-time reclassification of ISP-line incidents (kinds isp_loss and isp_outage) recorded before the
    intermediate-hop rule: where the raw samples around the incident show the public targets losing under
    degraded_loss_pct (the classifier's, 15 % by default) and under half of what the ISP-category hops lost, the
    loss never reached the end targets, so the incident is marked expected (the routers only deprioritized ping
    replies).

    `targets` are the configured targets (id, category). Incidents whose window [start - 10 s, end + 2 s]
    no longer has raw samples are left alone. Recorded in its own meta key, separate from the load backfill,
    so it runs once. Returns the number of incidents marked expected, or None when it did not run.
    """
    if await repository.get_meta(PING_BACKFILL_META_KEY):
        return None
    category = {t.id: t.category for t in targets}
    checked = 0
    marked: List[int] = []
    for row in await repository.get_isp_line_incidents():
        start = row["start_time"]
        end = row["end_time"] if row["end_time"] is not None else start
        samples = await repository.get_raw_samples(
            start - PING_BACKFILL_BEFORE_SECONDS, end + PING_BACKFILL_AFTER_SECONDS
        )
        isp = _loss_pct([s for s in samples if category.get(s["target_id"]) == "isp"])
        public = _loss_pct([s for s in samples if category.get(s["target_id"]) == "public"])
        if isp is None or public is None:
            continue
        checked += 1
        if public < degraded_loss_pct and public < isp / 2:
            await repository.set_incident_expected(row["id"])
            marked.append(row["id"])
    await repository.set_meta(PING_BACKFILL_META_KEY, json.dumps({
        "ts": time.time(), "checked": checked, "expected": len(marked), "incident_ids": marked,
        "reason": PING_BACKFILL_REASON,
    }))
    logger.info(f"Ping deprioritization backfill: {checked} ISP-line incidents had samples, {len(marked)} marked expected")
    return len(marked)
