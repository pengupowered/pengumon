"""The capability contributions to the AI troubleshooting pack: line load (WAN counters), router (system readings) and
Wi-Fi and mesh (client inventory, mesh). Each explains its readings to a model, summarises the stored history of the
window in a few lines, emits coincidence events and declares its redaction hints. They read the stored history only
(`device_samples`, `mesh_samples`), plus the router's monitor for what is not stored (mesh labels and backhaul), so
they work offline. See pengumon.capabilities.contrib for the interface."""

import math
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from typing import Any, ClassVar, Dict, List, Optional, Tuple

from pengumon.capabilities import BAND_2G, BAND_5G, Capability
from pengumon.capabilities.contrib import (
    NEVER, ROLE_ROUTER, ContribContext, EventKind, ModuleEvent, ModuleSection, RedactionHints, day_key,
    effective_poll_seconds, field_key, host_of, mesh_role, median, plural,
)
from pengumon.db.devices import PackSamples
from pengumon.drivers import DRIVERS, devices_with
from pengumon.engine.load import CAPACITY_FLOOR_MBPS, CAPACITY_HISTORY_SECONDS, CAPACITY_MIN_SAMPLES, CAPACITY_PERCENTILE
from pengumon.grades import SIGNAL_LEVELS, SIGNAL_WEAK, signal_grade
from pengumon.incidents.model import LOAD_IMPLAUSIBLE_PCT

# Error kinds that mean the device could not be reached at all (analysis/incident.py UNREACHABLE_KINDS).
_UNREACHABLE_KINDS = frozenset({"timeout", "unreachable"})
_UNANSWERED = frozenset({"error", "unconfigured"})   # a poll with these statuses carries no readings
STALL_SECONDS = 60.0         # counters that stood still this long while the router answered: no traffic at all
BUSY_RUN_SECONDS = 30.0      # a run of busy readings counts as an event from this long
MAX_GAP_SECONDS = 120.0      # readings further apart than this are not one run
HISTORY_PAD_SECONDS = 600.0  # readings before the window that the client-drop rule compares with
CLIENT_DROP_RATIO = 0.7      # online clients below this share of the median of the 10 minutes before: a drop
COUNTER_WRAP_ZONE = 1 << 31  # a counter that went backwards from within 2 GiB of 2**32 wrapped (32-bit counters)
NODE_ROWS = 8                # mesh nodes listed in the table
MESH_BUCKET_SECONDS = 60
EVENT_TIMES = 6              # a node's falls to 0 clients, and the sharp drops, listed with their time: the longest
EVENT_TIMES_SHORTEST = 2     # over its budget, the section shortens such a list to this before it leaves anything out
DROP_AT_FALL_SECONDS = 120.0  # a sharp drop this close to a node's fall to 0 clients is that fall (its events' reach)
NEAR_WEAK_DB = 2             # a Wi-Fi backhaul this close above weak says so: a node on it often drops out


def _first_device(ctx: ContribContext, capability) -> Optional[Any]:
    found = devices_with(ctx.devices, capability)
    return found[0] if found else None


async def _samples(ctx: ContribContext, cfg) -> PackSamples:
    """The device's polls of the window, plus 10 minutes before it, as columns; one query for all three
    contributions."""
    key = ("device_samples", cfg.id)
    if key not in ctx.cache:
        ctx.cache[key] = await ctx.repository.get_device_pack_samples(cfg.id, ctx.start, ctx.end, HISTORY_PAD_SECONDS)
    return ctx.cache[key]


def _runs(times: List[float], member: List[bool]) -> List[Tuple[int, int]]:
    """Maximal runs of consecutive members (member[i] true) as (first, last) indexes into `times`, split where two
    members are further apart than MAX_GAP_SECONDS."""
    runs: List[Tuple[int, int]] = []
    first = last = -1
    for i, (t, is_member) in enumerate(zip(times, member)):
        if is_member:
            if first >= 0 and t - times[last] > MAX_GAP_SECONDS:
                runs.append((first, last))
                first = i
            elif first < 0:
                first = i
            last = i
        elif first >= 0:
            runs.append((first, last))
            first = -1
    if first >= 0:
        runs.append((first, last))
    return runs


def _readings(times, values) -> Tuple[List[float], List[float]]:
    """The (time, value) pairs of a column that have a value, as two lists."""
    pairs = [(t, v) for t, v in zip(times, values) if v is not None]
    return [t for t, _ in pairs], [v for _, v in pairs]


def _router_ids(cfg) -> Dict[str, str]:
    ids = {v: ROLE_ROUTER for v in (host_of(cfg), cfg.id) if v}
    if cfg.model:
        ids.setdefault(cfg.model, field_key("model"))   # kept or dropped by the hints' fields["model"]
    return ids


def _minutes(seconds: float) -> str:
    """"under a minute", "1 minute", "14 minutes"."""
    return "under a minute" if seconds < 60 else plural(round(seconds / 60), "minute")


def _percentile(values: List[float], fraction: float) -> float:
    """Nearest rank."""
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, math.ceil(fraction * len(ordered)) - 1))]


# =============================================================================================================
# Line load
# =============================================================================================================

@dataclass(frozen=True)
class LoadFacts:
    identifiers: Dict[str, str]
    readings: int = 0                  # polls in the window with a measured rate
    capacity: Optional[Tuple[float, float]] = None   # down, up Mbps
    capacity_source: str = ""          # "set in Settings", "learned from 7 days of traffic" or a mix
    busy_pct: float = 60.0
    peak_pct: Optional[float] = None
    median_pct: Optional[float] = None
    busy_seconds: float = 0.0
    stalls: int = 0
    restarts: int = 0
    events: Tuple[ModuleEvent, ...] = ()
    peak_by_day: Dict[str, float] = field(default_factory=dict)


async def _capacity(ctx: ContribContext, cfg) -> Tuple[Optional[Tuple[float, float]], str]:
    """The line capacity as the pack's load is judged against: the configured speeds, else the ones learned from the
    stored rates the way engine/load.py learns them (99.9th percentile of 7 days; waits for enough samples)."""
    line = ctx.config.line
    down, up = line.down_mbps, line.up_mbps
    learned: Optional[Tuple[float, float]] = None
    if not (down and up):
        result = await ctx.repository.get_wan_rate_percentile(
            cfg.id, ctx.end - CAPACITY_HISTORY_SECONDS, CAPACITY_PERCENTILE)
        if result and result[0] >= CAPACITY_MIN_SAMPLES:
            learned = (round(max(CAPACITY_FLOOR_MBPS, result[1]), 1), round(max(CAPACITY_FLOOR_MBPS, result[2]), 1))
    down = down or (learned[0] if learned else None)
    up = up or (learned[1] if learned else None)
    if not (down and up):
        return None, ""
    if line.down_mbps and line.up_mbps:
        source = "set in Settings"
    elif not (line.down_mbps or line.up_mbps):
        source = "learned from 7 days of traffic"
    else:
        source = "partly set in Settings, partly learned"
    return (down, up), source


def _wrapped(previous: int, current: int) -> bool:
    return current < previous and previous < (1 << 32) and previous >= (1 << 32) - COUNTER_WRAP_ZONE


class LineLoadContribution:
    id: ClassVar[str] = "wan_load"
    title: ClassVar[str] = "Line load"
    layer: ClassVar[str] = "capability"
    budget_chars: ClassVar[int] = 1200
    events: ClassVar[Tuple[EventKind, ...]] = (
        EventKind("wan.busy", "line busy, up to {value:.0f} % of capacity", frozenset({"load"}), 30.0,
                  "busy-line stretches"),
        # nothing passed the router: the router, the gateway behind it or the line can each stop it
        EventKind("wan.stall", "no traffic through the router for {value:.0f} s",
                  frozenset({"router", "gateway", "line"}), 30.0, "stretches with no traffic through the router"),
        EventKind("router.restart", "{subject} restarted", frozenset({"router"}), 30.0, "router restarts"),
    )
    redaction: ClassVar[RedactionHints] = RedactionHints(
        fields={"rates": ("keep", "keep", "keep"), "capacity": ("keep", "keep", "keep"),
                "host": ("keep", "role", "role"), "device_name": ("keep", "role", "role")},
        identify=lambda facts: dict(facts.identifiers),
    )
    instruments: ClassVar[Tuple[str, ...]] = (
        "pengumon.line.load", "pengumon.line.capacity", "pengumon.wan.rate", "pengumon.router.restarts",
    )

    def prompt_context(self, config) -> str:
        cfgs = devices_with(config.devices, Capability.WAN_COUNTERS)
        poll = effective_poll_seconds(cfgs[0], DRIVERS[cfgs[0].driver]) if cfgs else 10.0
        line = config.line
        if line.down_mbps and line.up_mbps:
            capacity = f"{line.down_mbps:g} / {line.up_mbps:g} Mbps here, set in Settings"
        elif line.down_mbps or line.up_mbps:
            capacity = "partly set in Settings, partly learned from 7 days of traffic; the figures are in the section"
        else:
            capacity = "learned from 7 days of traffic; the figures are in the section"
        return (
            f"How to read it: WAN rates are the traffic through the router's internet port, from its byte counters "
            f"every {poll:g} s. Load = rate / line capacity ({capacity}). From {config.classifier.busy_load_pct:g} % "
            "the line counts as busy: latency rising then is queueing (\"bufferbloat\"), not a fault. A fault shows "
            "loss or latency while the load is low. Counters that stood still for a whole minute while the router "
            "answered mean no internet traffic at all passed the router."
        )

    async def collect(self, ctx: ContribContext) -> Optional[LoadFacts]:
        cfg = _first_device(ctx, Capability.WAN_COUNTERS)
        if cfg is None:
            return None
        busy_pct = ctx.config.classifier.busy_load_pct
        capacity, source = await _capacity(ctx, cfg)
        data = await _samples(ctx, cfg)
        lo, hi = data.span(ctx.start, ctx.end)
        events: List[ModuleEvent] = []
        # restarts and stalls: from the stored counters. A stall is a stretch of polls whose counters did not move.
        prev_ts = prev_rx = prev_tx = None
        stall_first = stall_last = 0.0
        in_stall = False
        restarts = stalls = 0
        for ts, status, rx, tx in zip(data.ts[lo:hi], data.status[lo:hi], data.wan_rx_bytes[lo:hi],
                                      data.wan_tx_bytes[lo:hi]):
            if status in _UNANSWERED or rx is None or tx is None:
                closes = True
            elif prev_ts is not None and ts - prev_ts <= MAX_GAP_SECONDS:
                if (rx < prev_rx and not _wrapped(prev_rx, rx)) or (tx < prev_tx and not _wrapped(prev_tx, tx)):
                    restarts += 1
                    events.append(ModuleEvent("router.restart", ts, ts, ROLE_ROUTER))
                    closes = True
                elif rx == prev_rx and tx == prev_tx:
                    if not in_stall:
                        in_stall, stall_first = True, prev_ts
                    stall_last = ts
                    closes = False
                else:
                    closes = True
            else:
                closes = True
            if closes and in_stall:
                in_stall = False
                if stall_last - stall_first >= STALL_SECONDS:
                    stalls += 1
                    events.append(ModuleEvent("wan.stall", stall_first, stall_last, value=stall_last - stall_first))
            if status not in _UNANSWERED and rx is not None and tx is not None:
                prev_ts, prev_rx, prev_tx = ts, rx, tx
        if in_stall and stall_last - stall_first >= STALL_SECONDS:
            stalls += 1
            events.append(ModuleEvent("wan.stall", stall_first, stall_last, value=stall_last - stall_first))
        # load: from the stored rates
        measured = [(ts, rx, tx) for ts, rx, tx in zip(data.ts[lo:hi], data.wan_rx_mbps[lo:hi], data.wan_tx_mbps[lo:hi])
                    if rx is not None and tx is not None]
        load_ts: List[float] = []
        load_pct: List[float] = []
        if capacity is not None:
            down, up = capacity
            for ts, rx, tx in measured:
                pct = max(rx / down, tx / up) * 100.0
                if pct <= LOAD_IMPLAUSIBLE_PCT:  # a bad counter reading, not load
                    load_ts.append(ts)
                    load_pct.append(pct)
        busy_seconds = 0.0
        by_day: Dict[str, float] = {}
        times = [m[0] for m in measured]
        gaps = [gap for a, b in zip(times, times[1:]) if 0 < (gap := b - a) <= MAX_GAP_SECONDS]
        step = median(gaps) or 10.0
        for first, last in _runs(load_ts, [p >= busy_pct for p in load_pct]):
            seconds = load_ts[last] - load_ts[first] + step
            busy_seconds += seconds
            if seconds >= BUSY_RUN_SECONDS:
                events.append(ModuleEvent("wan.busy", load_ts[first], load_ts[last],
                                          value=max(load_pct[first:last + 1])))
        day_of: Dict[int, str] = {}   # one date per quarter hour (zones change offset on one), not per reading
        for ts, pct in zip(load_ts, load_pct):
            index = int(ts // 900)
            day = day_of.get(index)
            if day is None:
                day = day_of[index] = day_key(index * 900.0, ctx.tz_offset_minutes, ctx.tz)
            by_day[day] = max(by_day.get(day, 0.0), pct)
        values = load_pct
        events.sort(key=lambda e: e.start)
        return LoadFacts(
            identifiers=_router_ids(cfg), readings=len(measured), capacity=capacity, capacity_source=source,
            busy_pct=busy_pct, peak_pct=max(values) if values else None, median_pct=median(values),
            busy_seconds=busy_seconds, stalls=stalls, restarts=restarts, events=tuple(events), peak_by_day=by_day)

    def emit(self, facts: Optional[LoadFacts]) -> List[ModuleEvent]:
        return list(facts.events) if facts is not None else []

    def section(self, facts: Optional[LoadFacts], detail: str) -> ModuleSection:
        if facts is None or facts.readings == 0:
            return ModuleSection(None, ["Set up, but no rate readings in this window."], None, {})
        f = facts
        findings = []
        if f.capacity is not None:
            if f.capacity_source != "set in Settings":   # the prompt text states a capacity set in Settings
                findings.append(f"Line capacity {f.capacity[0]:g} / {f.capacity[1]:g} Mbps down / up "
                                f"({f.capacity_source}).")
            peak = math.floor(f.peak_pct)                 # never rounded up to the busy level it did not reach
            busy = (f"busy ({f.busy_pct:g} % or more) for {_minutes(f.busy_seconds)}" if f.busy_seconds
                    else f"never busy ({f.busy_pct:g} % or more)")
            over = (" (over 100 %: the line is faster than the capacity Pengumon uses; set the real speed in "
                    "Settings, or busy-line figures are overstated)") if peak > 100 else ""
            findings.append(
                f"Peak load {peak} %{over}; {busy}; median {f.median_pct:.0f} %. Counters stood still "
                f"{plural(f.stalls, 'time')}; router restarts {f.restarts}.")
        else:
            findings.append("Line capacity not known yet (not set in Settings, too little traffic history to learn "
                            "it), so there is no load figure.")
            findings.append(f"Counters stood still {plural(f.stalls, 'time')}; router restarts {f.restarts}.")
        headline = None
        if f.busy_seconds >= BUSY_RUN_SECONDS or f.stalls or f.restarts:
            bits = []
            if f.busy_seconds >= BUSY_RUN_SECONDS and f.peak_pct is not None:
                bits.append(f"busy for {_minutes(f.busy_seconds)} (peak {math.floor(f.peak_pct)} %)")
            if f.stalls:
                bits.append(f"counters stood still {plural(f.stalls, 'time')}")
            if f.restarts:
                bits.append(plural(f.restarts, "router restart"))
            headline = ("Line load: " + "; ".join(bits) + ".")[:200]
        columns = {"Peak load": {d: f"{p:.0f} %" for d, p in sorted(f.peak_by_day.items())}} if f.peak_by_day else {}
        return ModuleSection(headline, findings, None, columns)


# =============================================================================================================
# Router
# =============================================================================================================

@dataclass(frozen=True)
class RouterFacts:
    identifiers: Dict[str, str]
    readings: int = 0                  # polls in the window that answered
    unreachable: int = 0
    cpu: Optional[Tuple[float, float, float]] = None    # median, 95th percentile, highest
    cpu_over: int = 0                  # readings at or above the warning level
    cpu_warning_pct: float = 90.0
    mem: Optional[Tuple[float, float]] = None
    events: Tuple[ModuleEvent, ...] = ()


class RouterContribution:
    id: ClassVar[str] = "router_system"
    title: ClassVar[str] = "Router"
    layer: ClassVar[str] = "capability"
    budget_chars: ClassVar[int] = 1200
    events: ClassVar[Tuple[EventKind, ...]] = (
        EventKind("router.unreachable", "{subject} not answering", frozenset({"router"}), 30.0,
                  "router not answering"),
        EventKind("router.cpu_high", "{subject} CPU at {value:.0f} %", frozenset({"router"}), 30.0, "high router CPU"),
        EventKind("router.memory_high", "{subject} memory at {value:.0f} %", frozenset({"router"}), 30.0,
                  "high router memory"),
    )
    redaction: ClassVar[RedactionHints] = RedactionHints(
        fields={"readings": ("keep", "keep", "keep"), "host": ("keep", "role", "role"),
                "device_name": ("keep", "role", "role"), "model": ("keep", "keep", "drop")},
        identify=lambda facts: dict(facts.identifiers),
    )
    instruments: ClassVar[Tuple[str, ...]] = ("pengumon.router.cpu", "pengumon.router.memory")

    def prompt_context(self, config) -> str:
        lv = config.router_levels
        return (
            f"How to read it: router CPU above {lv.cpu_warning_pct:g} % for minutes can delay traffic in the router "
            f"itself; memory above {lv.mem_warning_pct:g} % ({lv.mem_critical_pct:g} % critical) can come before a "
            "router restart. \"Router restarted\": its traffic counters started again from zero, as after a restart; a "
            "real restart also leaves the router not answering for a minute or two and drops everything behind it. "
            "\"Router not answering\": Pengumon could not read its status; if the Router hop's pings were fine then, "
            "the router was busy, not down."
        )

    async def collect(self, ctx: ContribContext) -> Optional[RouterFacts]:
        cfg = _first_device(ctx, Capability.ROUTER_SYSTEM)
        if cfg is None:
            return None
        lv = ctx.config.router_levels
        data = await _samples(ctx, cfg)
        lo, hi = data.span(ctx.start, ctx.end)
        statuses = data.status[lo:hi]
        events: List[ModuleEvent] = []
        for ts, status, kind in zip(data.ts[lo:hi], statuses, data.error_kind[lo:hi]):
            if status == "error" and (kind is None or kind in _UNREACHABLE_KINDS):
                events.append(ModuleEvent("router.unreachable", ts, ts, ROLE_ROUTER))
        # CPU: two readings in a row at or above the warning level
        cpu_ts, cpus = _readings(data.ts[lo:hi], data.cpu_pct[lo:hi])
        for first, last in _runs(cpu_ts, [c >= lv.cpu_warning_pct for c in cpus]):
            if last > first:
                events.append(ModuleEvent("router.cpu_high", cpu_ts[first], cpu_ts[last], ROLE_ROUTER,
                                          max(cpus[first:last + 1])))
        mem_ts, mems = _readings(data.ts[lo:hi], data.mem_pct[lo:hi])
        for first, last in _runs(mem_ts, [m >= lv.mem_critical_pct for m in mems]):
            events.append(ModuleEvent("router.memory_high", mem_ts[first], mem_ts[last], ROLE_ROUTER,
                                      max(mems[first:last + 1])))
        events.sort(key=lambda e: e.start)
        return RouterFacts(
            identifiers=_router_ids(cfg), readings=sum(st not in _UNANSWERED for st in statuses),
            unreachable=sum(e.code == "router.unreachable" for e in events),
            cpu=(median(cpus), _percentile(cpus, 0.95), max(cpus)) if cpus else None,
            cpu_over=sum(c >= lv.cpu_warning_pct for c in cpus), cpu_warning_pct=lv.cpu_warning_pct,
            mem=(min(mems), max(mems)) if mems else None, events=tuple(events))

    def emit(self, facts: Optional[RouterFacts]) -> List[ModuleEvent]:
        return list(facts.events) if facts is not None else []

    def section(self, facts: Optional[RouterFacts], detail: str) -> ModuleSection:
        if facts is None or (facts.readings == 0 and facts.unreachable == 0):
            return ModuleSection(None, ["Set up, but no readings in this window."], None, {})
        f = facts
        findings = []
        if f.cpu:
            line = (f"CPU median {f.cpu[0]:.0f} %, 95th percentile {f.cpu[1]:.0f} %, highest {f.cpu[2]:.0f} % "
                    f"({plural(f.cpu_over, 'reading')} at {f.cpu_warning_pct:g} % or more).")
            if f.mem:
                line += f" Memory {f.mem[0]:.0f}–{f.mem[1]:.0f} %."
            findings.append(line)
        if f.unreachable:
            findings.append(f"{ROLE_ROUTER} status page: no answer in {plural(f.unreachable, 'reading')}"
                            + (" (a single miss: it matters only if the Router hop's pings failed then)."
                               if f.unreachable == 1 else "."))
        if not findings:
            findings.append("The router reports no CPU or memory figures.")
        # one status page that did not answer is no news (the pings say whether the router was down)
        notable = f.cpu_over or f.unreachable > 1 or any(e.code == "router.memory_high" for e in f.events)
        headline = None
        if notable:
            bits = []
            if f.cpu_over:
                bits.append(f"CPU at {f.cpu_warning_pct:g} % or more in {plural(f.cpu_over, 'reading')}")
            if f.unreachable > 1:
                bits.append(f"status page not answering in {plural(f.unreachable, 'reading')}")
            if any(e.code == "router.memory_high" for e in f.events):
                bits.append("memory at the critical level")
            headline = ("Router: " + "; ".join(bits) + ".")[:200]
        return ModuleSection(headline, findings, None, {})


# =============================================================================================================
# Wi-Fi and mesh
# =============================================================================================================

@dataclass(frozen=True)
class NodeFacts:
    role: str                          # the node's role key
    backhaul: str                      # in words, for the table
    median: float                      # clients per minute
    lowest: float
    falls: Tuple[Tuple[float, int], ...] = ()   # (minute it fell to 0 clients, minutes at 0 from then)
    minutes_at_0: int = 0              # every minute at 0 clients, a stretch the window starts with too
    weak: str = ""                     # "weak" or "near weak": its Wi-Fi backhaul's signal; "" otherwise
    rssi: Optional[int] = None         # that backhaul's signal, dBm
    descriptor: str = ""               # what tells it apart without a name ("the only node on Wi-Fi backhaul")


@dataclass(frozen=True)
class WifiFacts:
    identifiers: Dict[str, str]
    readings: int = 0                  # polls that carried a client list
    online: Optional[float] = None     # medians
    wired: Optional[float] = None
    wifi_2g: Optional[float] = None
    wifi_5g: Optional[float] = None
    wifi_other: Optional[float] = None
    drops: int = 0
    drop_times: Tuple[Tuple[float, float, float], ...] = ()   # (start, seconds, lowest count) of each sharp drop
    nodes: Tuple[NodeFacts, ...] = ()
    events: Tuple[ModuleEvent, ...] = ()


def _weakness(node) -> str:
    """"weak" or "near weak" (fair, at most NEAR_WEAK_DB above weak) for a node's Wi-Fi backhaul; "" otherwise."""
    rssi = getattr(node, "backhaul_rssi_dbm", None) if node is not None and node.backhaul == "wifi" else None
    if rssi is None:
        return ""
    if signal_grade(rssi)["word"] == SIGNAL_WEAK[0]:
        return "weak"
    return "near weak" if rssi - SIGNAL_LEVELS[-1][0] <= NEAR_WEAK_DB else ""


def _backhaul_words(node) -> str:
    if node is None or node.backhaul is None:
        return "not known"
    if node.backhaul == "wired":
        return "wired"
    words = f"Wi-Fi {node.backhaul_band}" if node.backhaul_band else "Wi-Fi"
    if node.backhaul_rssi_dbm is not None:
        grade = signal_grade(node.backhaul_rssi_dbm)["word"].lower()
        if _weakness(node) == "near weak":
            grade += ", near weak: a backhaul this weak often drops the node"
        words += f", {node.backhaul_rssi_dbm} dBm ({grade})"
    return words


def _descriptors(ids: List[str], live_nodes: Dict[str, Any]) -> Dict[str, str]:
    """What tells a node apart without its name: the only one on a Wi-Fi (or a wired) backhaul. Said only when at most
    one node's backhaul is not known (the main router reports none), so the claim holds."""
    kinds = {nid: getattr(live_nodes.get(nid), "backhaul", None) for nid in ids}
    if len(ids) < 2 or sum(1 for k in kinds.values() if k is None) > 1:
        return {}
    out = {}
    for kind, words in (("wifi", "the only node on Wi-Fi backhaul"), ("wired", "the only node on wired backhaul")):
        alone = [nid for nid, k in kinds.items() if k == kind]
        if len(alone) == 1:
            out[alone[0]] = words
    return out


def _length(seconds: float) -> str:
    """"40 s", "5 min"."""
    return f"{round(seconds)} s" if seconds < 60 else f"{round(seconds / 60)} min"


def _timed(items: List[Tuple[float, str]], weight: List[float], count: int) -> str:
    """The `count` heaviest of `items` ((time, words)), in time order, then how many more: the pack's {minute:ts}
    token stands for the time."""
    order = sorted(range(len(items)), key=lambda k: (-weight[k], items[k][0]))[:count]
    shown = ", ".join(f"{{minute:{items[k][0]:.0f}}} ({items[k][1]})" for k in sorted(order, key=lambda k: items[k][0]))
    more = len(items) - len(order)
    return shown + (f"; {more} more, shorter" if more else "")


class WifiMeshContribution:
    id: ClassVar[str] = "wifi_mesh"
    title: ClassVar[str] = "Wi-Fi and mesh"
    layer: ClassVar[str] = "capability"
    budget_chars: ClassVar[int] = 1800
    events: ClassVar[Tuple[EventKind, ...]] = (
        EventKind("clients.drop", "online clients dropped to {value:.0f}", frozenset({"router"}), 120.0,
                  "sharp drops in online clients", by_time_of_day=True),
        EventKind("mesh.node_empty", "{subject} at 0 clients", frozenset({"router"}), 120.0,
                  "mesh nodes falling to 0 clients", by_time_of_day=True),
    )
    redaction: ClassVar[RedactionHints] = RedactionHints(
        fields={"counts": ("keep", "keep", "keep"), "backhaul": ("keep", "keep", "keep"),
                "node_label": ("keep", "role", "role"), "node_id": ("never", "never", "never"),
                "per_client": ("never", "never", "never"), "host": ("keep", "role", "role"),
                "device_name": ("keep", "role", "role")},
        identify=lambda facts: dict(facts.identifiers),
    )
    instruments: ClassVar[Tuple[str, ...]] = (
        "pengumon.clients", "pengumon.clients.wifi.signal", "pengumon.clients.wifi.weak", "pengumon.mesh.nodes",
        "pengumon.mesh.node.clients", "pengumon.mesh.backhaul.signal",
    )

    def prompt_context(self, config) -> str:
        edges = [lvl[0] for lvl in SIGNAL_LEVELS]
        words = [f"above {edges[0]} {SIGNAL_LEVELS[0][1].lower()}"]
        words += [f"{edges[i - 1]} to {edges[i]} {SIGNAL_LEVELS[i][1].lower()}" for i in range(1, len(SIGNAL_LEVELS))]
        words.append(f"below {edges[-1]} {SIGNAL_WEAK[0].lower()}")
        return (
            "How to read it: Pengumon counts the connected devices per Wi-Fi band and per mesh node, never their "
            f"names or addresses. Signal in dBm, closer to 0 is stronger: {', '.join(words)}. A mesh node joins the "
            "main router by cable (\"wired backhaul\") or by Wi-Fi; a weak Wi-Fi backhaul slows and drops everything "
            "behind that node. A sharp drop in clients, or a node falling to 0 clients, at the time of an incident "
            "points to the Wi-Fi or that node rather than the line. A node falling to 0 clients at the times of the "
            "user's problem, with no incident on the path, points to that node's link. The probes measure Pengumon's "
            "own computer's path: other devices' Wi-Fi trouble shows in them only when that computer is on Wi-Fi too."
        )

    async def collect(self, ctx: ContribContext) -> Optional[WifiFacts]:
        cfg = _first_device(ctx, Capability.CLIENT_INVENTORY)
        if cfg is None:
            return None
        identifiers = _router_ids(cfg)
        data = await _samples(ctx, cfg)
        have = [i for i, v in enumerate(data.clients_online) if v is not None]   # the polls that read the client list
        pad_ts = [data.ts[i] for i in have]
        pad_online = [data.clients_online[i] for i in have]
        lo, hi = bisect_left(pad_ts, ctx.start), bisect_right(pad_ts, ctx.end)
        events: List[ModuleEvent] = []
        # sharp drops: online clients under 70 % of the median of the 10 minutes before. (The window of each reading
        # is a slice found by bisection; the median is only worked out when the reading is under 70 % of the
        # window's highest count, which a drop must be.)
        below = []
        for k in range(lo, hi):
            ts, online = pad_ts[k], pad_online[k]
            first = bisect_left(pad_ts, ts - HISTORY_PAD_SECONDS)   # pad_ts[k] itself is excluded: ts < ts is false
            last = bisect_left(pad_ts, ts)
            if last - first < 3 or online >= CLIENT_DROP_RATIO * max(pad_online[first:last]):
                below.append(False)
                continue
            m = median(pad_online[first:last])
            below.append(bool(m and online < CLIENT_DROP_RATIO * m))
        sample_ts, sample_online = pad_ts[lo:hi], pad_online[lo:hi]
        drops = 0
        drop_times = []
        step = median([b - a for a, b in zip(sample_ts, sample_ts[1:])]) or float(MESH_BUCKET_SECONDS)
        for first, last in _runs(sample_ts, below):
            drops += 1
            lowest = float(min(sample_online[first:last + 1]))
            events.append(ModuleEvent("clients.drop", sample_ts[first], sample_ts[last], value=lowest))
            # until the first reading back up (or one reading's step, when the next one is missing or far)
            after = sample_ts[last + 1] if last + 1 < len(sample_ts) else None
            near = after is not None and after - sample_ts[last] <= MAX_GAP_SECONDS
            until = after if near else sample_ts[last] + step
            drop_times.append((sample_ts[first], until - sample_ts[first], lowest))
        # the mesh: stored per-node counts, and what the router's monitor knows now (labels, backhaul)
        live_nodes = {n.id: n for n in (getattr(getattr(ctx.live, "latest_mesh", None), "nodes", None) or ())}
        rows = await ctx.repository.get_mesh_counts(cfg.id, ctx.start, ctx.end, MESH_BUCKET_SECONDS)
        ids = sorted({r[1] for r in rows} | set(live_nodes))
        roles = {nid: mesh_role(i) for i, nid in enumerate(ids, 1)}
        labels = dict(getattr(DRIVERS[cfg.driver].options_model.model_validate(cfg.options), "mesh_node_labels", {}))
        for nid, node in live_nodes.items():
            labels[nid] = node.label or labels.get(nid, "")
        for nid, role in roles.items():
            identifiers[nid] = NEVER              # a MAC fragment: at no level, Full included
            if labels.get(nid):
                identifiers[labels[nid]] = role
        # anything per client is shown at no level: the names and MACs the router knows them by, where they are not
        # already one of this module's identifiers (a mesh node listed as a client keeps its role)
        for client in getattr(getattr(ctx.live, "latest_clients", None), "clients", None) or ():
            for value in (*client.label_parts, client.mac, client.key):
                if value and value not in identifiers:
                    identifiers[value] = NEVER
        per_node: Dict[str, List[Tuple[float, float]]] = {}
        for bucket, nid, mean in rows:
            per_node.setdefault(nid, []).append((bucket, mean))
        table = []
        described = _descriptors(ids, live_nodes)
        per_minute = MESH_BUCKET_SECONDS / 60.0
        for nid in ids:
            series = per_node.get(nid, [])
            values = [v for _, v in series]
            # each fall to 0 clients (from a count above 0) and how long it stayed at 0
            falls: List[List[float]] = []
            in_fall = False
            for k, (bucket, v) in enumerate(series):
                if v != 0:
                    in_fall = False
                    continue
                if k > 0 and series[k - 1][1] > 0:
                    falls.append([float(bucket), 0.0])
                    in_fall = True
                    events.append(ModuleEvent("mesh.node_empty", float(bucket), float(bucket), roles[nid]))
                if in_fall:
                    falls[-1][1] += per_minute
            if values:
                node = live_nodes.get(nid)
                table.append(NodeFacts(
                    roles[nid], _backhaul_words(node), median(values), min(values),
                    tuple((a, round(m)) for a, m in falls), round(sum(v == 0 for v in values) * per_minute),
                    _weakness(node), getattr(node, "backhaul_rssi_dbm", None) if node is not None else None,
                    described.get(nid, "")))
        events.sort(key=lambda e: e.start)

        window = have[lo:hi]

        def med(column):
            values = [v for v in (column[i] for i in window) if v is not None]
            return median(values)

        return WifiFacts(
            identifiers=identifiers, readings=len(window), online=med(data.clients_online), wired=med(data.clients_wired),
            wifi_2g=med(data.clients_wifi_2g), wifi_5g=med(data.clients_wifi_5g), wifi_other=med(data.clients_wifi_other),
            drops=drops, drop_times=tuple(drop_times), nodes=tuple(table), events=tuple(events))

    def emit(self, facts: Optional[WifiFacts]) -> List[ModuleEvent]:
        return list(facts.events) if facts is not None else []

    def section(self, facts: Optional[WifiFacts], detail: str) -> ModuleSection:
        if facts is None or facts.readings == 0:
            return ModuleSection(None, ["Set up, but no client counts in this window."], None, {})
        f = facts
        parts = [f"wired {f.wired:g}", f"{BAND_2G} {f.wifi_2g:g}", f"{BAND_5G} {f.wifi_5g:g}"]
        if f.wifi_other:
            parts.append(f"other bands {f.wifi_other:g}")
        findings = [f"Online clients: median {f.online:g} ({', '.join(parts)}); sharp drops {f.drops}."]
        shorter: Dict[int, Tuple[str, ...]] = {}

        def name(n: NodeFacts) -> str:
            # at Full the role becomes the node's own name; elsewhere a descriptor tells the user which node it is
            return f"{n.role} ({n.descriptor})" if n.descriptor and detail != "full" else n.role

        def listed(head: str, items: List[Tuple[float, str]], weight: List[float]) -> None:
            """A bullet listing timed items, with shorter versions down to EVENT_TIMES_SHORTEST of them."""
            findings.append(f"{head}{_timed(items, weight, EVENT_TIMES)}.")
            fewer = range(min(EVENT_TIMES, len(items)) - 1, EVENT_TIMES_SHORTEST - 1, -1)
            if fewer:
                shorter[len(findings) - 1] = tuple(f"{head}{_timed(items, weight, n)}." for n in fewer)

        nodes = list(f.nodes[:NODE_ROWS])
        empty = sorted((n for n in f.nodes if n.minutes_at_0), key=lambda n: (-n.minutes_at_0, n.role))

        def fell(n: NodeFacts, extra: str = "") -> str:
            """"Mesh node 3 fell to 0 clients 6 times (37 min in all)"; `extra` goes inside the brackets."""
            if not n.falls:          # at 0 from the window's start, no fall in it
                return f"{name(n)} at 0 clients for {n.minutes_at_0} min" + (f" ({extra})" if extra else "")
            times = "once" if len(n.falls) == 1 else f"{len(n.falls)} times"
            more = f"; {extra}" if extra else ""
            return f"{name(n)} fell to 0 clients {times} ({n.minutes_at_0} min in all{more})"

        for n in empty:
            if n.falls:
                listed(fell(n) + ": ", [(a, f"{m} min") for a, m in n.falls], [float(m) for _, m in n.falls])
        if f.drop_times:
            # a drop when a node fell to 0 clients is that fall, listed above: only the others get their times
            fall_times = [a for n in f.nodes for a, _ in n.falls]
            others = [d for d in f.drop_times if not any(d[0] - DROP_AT_FALL_SECONDS <= a <= d[0] + d[1]
                                                         + DROP_AT_FALL_SECONDS for a in fall_times)]
            head = f"{plural(f.drops, 'sharp drop')} in online clients"
            at_falls = len(f.drop_times) - len(others)
            if not others:
                findings.append(f"{head}, " + ("all at the times a node fell to 0 clients." if at_falls > 1
                                               else "at the time a node fell to 0 clients."))
            else:
                if at_falls:
                    head += f", {at_falls} at the times a node fell to 0 clients; the others"
                listed(f"{head}: ", [(a, f"{_length(sec)}, to {low:g}") for a, sec, low in others],
                       [sec for _, sec, _ in others])
        table = None
        if nodes:
            rows = [[n.role, n.backhaul, f"{n.median:g}", f"{n.lowest:g}", str(len(n.falls)), str(n.minutes_at_0)]
                    for n in nodes]
            table = (["Node", "Backhaul", "Median clients", "Lowest", "Falls to 0", "Minutes at 0"], rows)
        unknown = [n.role for n in nodes if n.backhaul == "not known"]
        if unknown:
            findings.append(f"Backhaul not known for {', '.join(unknown)} (Pengumon could not read it): it matters "
                            "only for devices on that node, not for these probes unless the monitoring computer is "
                            "on it.")
        headline = None
        if f.drops or empty:
            bits = [f"{plural(f.drops, 'sharp drop')} in online clients"] if f.drops else []
            # each node at 0 clients by name, the most minutes first, with a weak Wi-Fi backhaul said, as many as fit
            shown = 0
            for n in empty:
                bit = fell(n, f"Wi-Fi backhaul {n.weak}, {n.rssi} dBm" if n.weak else "")
                rest = len(empty) - shown - 1
                tail = [f"{plural(rest, 'more node')} at 0 clients"] if rest else []
                if len("Wi-Fi and mesh: " + "; ".join(bits + [bit] + tail) + ".") > 200 and shown:
                    break
                bits.append(bit)
                shown += 1
            if shown < len(empty):
                bits.append(f"{plural(len(empty) - shown, 'more node')} at 0 clients")
            headline = ("Wi-Fi and mesh: " + "; ".join(bits) + ".")[:200]
        return ModuleSection(headline, findings, table, {}, shorter)


# (the capabilities that activate it (any of), the class), in the order the pack shows them
CAPABILITY_CONTRIBUTIONS = (
    ((Capability.WAN_COUNTERS,), LineLoadContribution),
    ((Capability.ROUTER_SYSTEM,), RouterContribution),
    ((Capability.CLIENT_INVENTORY,), WifiMeshContribution),
)
