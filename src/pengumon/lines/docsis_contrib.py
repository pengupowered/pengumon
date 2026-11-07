"""The DOCSIS line technology's contribution to the AI troubleshooting pack: the prompt text that explains a cable
modem's readings, a short section from the stored line readings, the coincidence events (the neutral `line.*` codes
every line technology emits) and the redaction hints. See pengumon.capabilities.contrib for the interface."""

import dataclasses
from dataclasses import dataclass
from typing import Any, ClassVar, Dict, List, Optional, Tuple

from pengumon.capabilities import Capability
from pengumon.capabilities.contrib import (
    ROLE_LINE, ContribContext, EventKind, ModuleEvent, ModuleSection, RedactionHints, day_key,
    effective_poll_seconds, field_key, host_of, median, plural,
)
from pengumon.drivers import DRIVERS, devices_with
from pengumon.grades import GOOD
from pengumon.lines import docsis
from pengumon.lines.docsis import (
    NO_READING_STATUSES, TECHNOLOGY, channel_finding, channel_findings, decode_channels, decode_summary, grade_snapshot,
    is_degraded, judge_errors, snapshot_from_sample,
)

# Error kinds that mean the modem could not be reached at all (analysis/incident.py UNREACHABLE_KINDS); a refused
# login or an unreadable page says the modem answered. A row without a kind (from before schema v7) counts.
_UNREACHABLE_KINDS = frozenset({"timeout", "unreachable"})
_POLL_WORDS = {"auth": "auth refused", "unreachable": "unreachable", "timeout": "timed out",
               "protocol": "unreadable answer", "unsupported": "unsupported", "unconfigured": "not configured",
               "internal": "internal error"}
TABLE_ROWS = 6  # channels listed, worst first
_CHANNEL_HEADER = ["Dir", "Freq MHz", "Power dBmV", "SNR dB", "New uncorr.", "Finding"]
DEFAULT_POLL_SECONDS = 1800.0
# A reading tells what happened since the one before: its events span that interval (at most SPAN_POLLS polls back,
# so a reading after a long gap does not claim the whole gap), and reach only REACH_SECONDS beyond it.
SPAN_POLLS = 2.0
REACH_SECONDS = 60.0
# New uncorrectables are counted between two readings, so they coincide with an incident that overlaps that interval:
# their events span the whole interval and reach only a second further (the stamps are to the second).
COUNTED_REACH_SECONDS = 1.0
ERROR_TIMES = 4  # readings with new uncorrectables listed with their time, the largest counts
ERROR_TIMES_SHORTEST = 2  # over its budget, the section shortens that list to this before it leaves anything out
AT_LIMIT_SHOWN = 3  # channels at a healthy limit described one by one


@dataclass(frozen=True)
class DocsisFacts:
    """What the stored readings of the window say. Names live only in `identifiers` (value -> role key)."""

    identifiers: Dict[str, str]
    readings: int = 0                 # rows in the window
    good: int = 0                     # rows with a reading
    unreachable: int = 0              # rows where the modem could not be reached
    restarts: int = 0
    new_errors: int = 0               # new uncorrectable codewords, summed
    error_readings: int = 0           # readings with new uncorrectable codewords
    ds_snr: Optional[Tuple[float, float, float]] = None     # min, median, max of the per-reading averages
    ds_power: Optional[Tuple[float, float]] = None          # min, max
    us_power: Optional[Tuple[float, float]] = None
    last_ts: Optional[float] = None   # the last reading with a value
    last_poll_kind: Optional[str] = None   # how the last row of the window ended (None: a reading)
    degraded: Tuple[Tuple[Any, ...], ...] = ()   # rows of the last reading's channels outside typical levels
    degraded_count: int = 0
    events: Tuple[ModuleEvent, ...] = ()
    errors_by_day: Dict[str, int] = dataclasses.field(default_factory=dict)
    poll_seconds: float = DEFAULT_POLL_SECONDS
    error_times: Tuple[Tuple[float, float, int], ...] = ()   # (reading before, reading, new uncorrectables) of each
    #                                                           reading with some
    at_limit: Tuple[str, ...] = ()    # the last reading's channels at a healthy limit, described
    spacing: Optional[Tuple[float, float, float]] = None   # seconds between the window's readings: median, 10th, 90th %


def _finding(c, lv) -> str:
    """Every way the channel is outside the levels `lv`, worst first (docsis.channel_findings, by rank): "SNR below
    30 dB (errors likely); high uncorrectable count since the modem started". A critical level says its limit (the
    levels themselves are in the columns before); the uncorrectable count is the channel's own since the modem started,
    not the new ones of the table's column."""
    found = sorted(channel_findings(c, lv), key=lambda f: f.rank)
    if not found:
        return channel_finding(c, lv)
    text = "; ".join(f.words for f in found)
    return text[:1].upper() + text[1:]


def _as_shown(channel):
    """The channel with its levels rounded as the pack shows them (0.1), so a value shown at a limit is judged at it."""
    return channel.model_copy(update={"power_dbmv": round(channel.power_dbmv, 1),
                                      "snr_db": None if channel.snr_db is None else round(channel.snr_db, 1)})


def _limit(c, lv) -> Optional[Tuple[str, str]]:
    """(metric, side) when a shown channel sits exactly at a healthy limit of the levels `lv`: ("snr", "low"),
    ("power", "high")..."""
    if c.direction == "downstream":
        if c.snr_db is not None and c.snr_db == round(lv.ds_snr_warning_db, 1):
            return "snr", "low"
        if abs(c.power_dbmv) == round(lv.ds_power_warning_abs_dbmv, 1):
            return "power", "high" if c.power_dbmv > 0 else "low"
        return None
    if c.power_dbmv == round(lv.us_power_warning_dbmv, 1):
        return "power", "high"
    if c.power_dbmv == round(lv.us_power_low_warning_dbmv, 1):
        return "power", "low"
    return None


def _at_limit(channels, rows) -> Tuple[str, ...]:
    """"DS 573 MHz, SNR 33.0 dB (lowest in the window 32.4 dB), no new uncorrectables" for each channel at a limit,
    with the window's worst value of that level on that channel (`rows`: the window's readings)."""
    if not channels:
        return ()

    def level(c, metric: str) -> Optional[float]:
        return c.snr_db if metric == "snr" else c.power_dbmv

    seen: Dict[Tuple[str, float, str], List[float]] = {(c.direction, c.frequency_mhz, m): [] for c, (m, _) in channels}
    for row in rows:
        for c in decode_channels(row.detail):
            for metric in ("snr", "power"):
                values = seen.get((c.direction, c.frequency_mhz, metric))
                if values is not None and level(c, metric) is not None:
                    values.append(level(c, metric))
    out = []
    for c, (metric, side) in channels:
        values = seen[(c.direction, c.frequency_mhz, metric)] or [level(c, metric)]
        worst = min(values) if side == "low" else max(values)
        if metric == "snr":
            now, low = f"SNR {c.snr_db:.1f} dB", f"{worst:.1f} dB"
        else:
            sign = "+" if c.direction == "downstream" else ""
            now, low = f"power {c.power_dbmv:{sign}.1f} dBmV", f"{worst:{sign}.1f} dBmV"
        new = ""          # codewords are counted downstream only
        if c.direction == "downstream":
            new = f", {c.uncorrectables_recent:,} new uncorrectables" if c.uncorrectables_recent else \
                ", no new uncorrectables"
        out.append(f"{'DS' if c.direction == 'downstream' else 'US'} {c.frequency_mhz:g} MHz, {now} "
                   f"({'lowest' if side == 'low' else 'highest'} in the window {low}){new}")
    return tuple(out)


def _identifiers(facts: DocsisFacts) -> Dict[str, str]:
    return dict(facts.identifiers)


class DocsisContribution:
    id: ClassVar[str] = "docsis"
    title: ClassVar[str] = "Cable line (DOCSIS)"
    layer: ClassVar[str] = "technology"
    budget_chars: ClassVar[int] = 3000
    events: ClassVar[Tuple[EventKind, ...]] = (
        # the modem itself not answering or restarting supports a fault at the Gateway; codeword bursts and a signal out
        # of range support one on the line (loss from ISP hop 1 on while the Gateway answers)
        EventKind("line.offline", "{subject} not answering", frozenset({"gateway"}), REACH_SECONDS,
                  "readings with the modem not answering"),
        EventKind("line.restart", "{subject} restarted", frozenset({"gateway"}), REACH_SECONDS, "modem restarts"),
        EventKind("line.error_burst", "uncorrectable codewords +{value:,.0f}", frozenset({"line"}),
                  COUNTED_REACH_SECONDS, "bursts of uncorrectable codewords"),
        # fewer new uncorrectables than a burst (judge_errors): they coincide, but support nothing
        EventKind("line.errors", "uncorrectable codewords +{value:,.0f}", frozenset(), COUNTED_REACH_SECONDS,
                  "smaller counts of new uncorrectable codewords"),
        EventKind("line.signal_out_of_range", "line signal outside typical levels", frozenset({"line"}),
                  REACH_SECONDS, "readings with the signal outside typical levels"),
    )
    redaction: ClassVar[RedactionHints] = RedactionHints(
        fields={
            "levels": ("keep", "keep", "keep"),            # channel numbers, frequencies, levels, codeword counts
            "host": ("role", "role", "role"),               # the modem's host (config): role "Gateway"
            "device_name": ("role", "role", "role"),        # the device's config name
            "error_message": ("never", "never", "never"),
            "model": ("keep", "keep", "drop"),
        },
        identify=_identifiers,
    )
    instruments: ClassVar[Tuple[str, ...]] = (
        "pengumon.line.docsis.power", "pengumon.line.docsis.snr", "pengumon.line.docsis.channels",
        "pengumon.line.errors", "pengumon.line.restarts",
    )

    # --- prompt ---

    def prompt_context(self, config) -> str:
        lv = config.line_signal   # the read interval is not said here: the section states it from the readings
        return (
            "How to read it: the modem talks to the provider over the coaxial cable on many channels at once, "
            "downstream (to the home) and upstream (from the home).\n"
            f"- Downstream power, healthy within ±{lv.ds_power_warning_abs_dbmv:g} dBmV. Far below: a weak signal "
            "(long cable, splitters, a bad connector). Far above: too strong.\n"
            f"- Downstream SNR (signal-to-noise ratio), healthy above {lv.ds_snr_warning_db:g} dB; below "
            f"{lv.ds_snr_critical_db:g} dB errors are likely. Low on a few neighbouring channels: interference at "
            "those frequencies. Low on all: the cable or the provider's network.\n"
            f"- Upstream power, healthy from {lv.us_power_low_warning_dbmv:g} to {lv.us_power_warning_dbmv:g} dBmV. "
            "High means the modem must \"shout\" to be heard, often a damaged cable, a loose connector or too many "
            f"splitters; above {lv.us_power_critical_dbmv:g} dBmV the modem may drop off the network.\n"
            "- Codewords are the data blocks on downstream channels. Corrected ones were fixed (fine in moderation); "
            "uncorrectable ones are lost data. Each reading counts the new ones since the reading before: every burst "
            "is counted, only its time within that interval is unknown. Downstream only: these counters do not see the "
            "upload direction. Loss starting at the Gateway is the modem unit or the cable from the router to it, not "
            "the coaxial line; a modem restart at that time means the unit. A coaxial (RF) problem shows as loss "
            "starting at ISP hop 1 while the Gateway answers, often with a burst of new uncorrectables; a few without "
            "loss are common.\n"
            "- \"Modem restarted\": its error counters went down, as after a restart (a change in its set of channels "
            "can look the same). \"Modem not answering\": Pengumon could not read its status page (down, restarting or "
            "busy); compare the Gateway hop's pings at that time.\n"
            "- The provider usually owns the line up to the modem; the cables and splitters inside the home are "
            "usually the user's."
        )

    # --- facts ---

    async def collect(self, ctx: ContribContext) -> Optional[DocsisFacts]:
        cfgs = devices_with(ctx.devices, Capability.ACCESS_LINE, TECHNOLOGY)
        if not cfgs:
            return None
        cfg = cfgs[0]
        poll = effective_poll_seconds(cfg, DRIVERS[cfg.driver])
        identifiers = {v: ROLE_LINE for v in (host_of(cfg), cfg.id) if v}
        if cfg.model:
            identifiers.setdefault(cfg.model, field_key("model"))   # kept or dropped by fields["model"]
        levels = getattr(ctx.config, "line_signal", None) or docsis.DEFAULT_LEVELS
        # One row of padding on each side tells whether the first reading of the window is a restart or a burst.
        rows = [s for s in await ctx.repository.get_line_samples(cfg.id, ctx.start, ctx.end, padding_seconds=2 * poll)
                if s.technology == TECHNOLOGY]
        facts: Dict[str, Any] = dict(identifiers=identifiers, poll_seconds=poll)
        events: List[ModuleEvent] = []
        snr, dsp, usp = [], [], []
        by_day: Dict[str, int] = {}
        error_times: List[Tuple[float, int]] = []
        prev = None  # the previous row with a reading
        prev_row = None  # the previous row of any kind

        def since(earlier):
            """Where an event of the reading at s.ts starts: the earlier reading, at most SPAN_POLLS polls back."""
            floor = s.ts - SPAN_POLLS * poll
            return max(earlier.ts, floor) if earlier is not None else s.ts - poll
        in_window = [s for s in rows if ctx.start <= s.ts <= ctx.end]
        counts = dict(readings=len(in_window), good=0, unreachable=0, restarts=0, new_errors=0, error_readings=0)
        last_good = None
        for s in rows:
            inside = ctx.start <= s.ts <= ctx.end
            if s.status in NO_READING_STATUSES:
                if inside and s.status == "error" and (s.error_kind is None or s.error_kind in _UNREACHABLE_KINDS):
                    counts["unreachable"] += 1
                    events.append(ModuleEvent("line.offline", since(prev_row), s.ts, ROLE_LINE))
                prev_row = s
                continue
            recent = s.errors_recent or 0
            if prev is not None and s.errors_total is not None and prev.errors_total is not None \
                    and s.errors_total < prev.errors_total:
                if inside:
                    counts["restarts"] += 1
                    events.append(ModuleEvent("line.restart", since(prev), s.ts, ROLE_LINE))
            if inside:
                counts["good"] += 1
                summary = decode_summary(s.summary)
                for values, key in ((snr, "downstream_snr_avg"), (dsp, "downstream_power_avg"),
                                    (usp, "upstream_power_avg")):
                    if summary[key] is not None:
                        values.append(summary[key])
                if recent > 0:
                    counts["new_errors"] += recent
                    counts["error_readings"] += 1
                    counted_from = prev.ts if prev is not None else s.ts - poll   # the counters' own interval
                    error_times.append((counted_from, s.ts, int(recent)))
                    day = day_key(s.ts, ctx.tz_offset_minutes, ctx.tz)
                    by_day[day] = by_day.get(day, 0) + recent
                    interval = (s.ts - prev.ts) if prev is not None else None
                    recurring = prev is not None and (prev.errors_recent or 0) > 0
                    # The modem's error-free codeword counts are not stored, so the ratio is unknown here: the
                    # per-minute rule judges (judge_errors).
                    burst = judge_errors(recent, interval, None, recurring, levels)[0] != GOOD
                    events.append(ModuleEvent("line.error_burst" if burst else "line.errors", counted_from, s.ts,
                                              ROLE_LINE, float(recent)))
                snapshot = grade_snapshot(snapshot_from_sample(dataclasses.replace(s, detail=None)), levels)
                if any(snapshot.grades.get(k, GOOD) != GOOD for k in ("ds_power", "ds_snr", "us_power")):
                    worst = [c.snr_db for c in decode_channels(s.detail) if c.direction == "downstream"
                             and c.snr_db is not None]
                    events.append(ModuleEvent("line.signal_out_of_range", since(prev_row), s.ts, ROLE_LINE,
                                              min(worst) if worst else None))
                last_good = s
            prev = prev_row = s
        degraded_rows: Tuple[Tuple[Any, ...], ...] = ()
        degraded_count = 0
        at_limit: Tuple[str, ...] = ()
        if last_good is not None:
            shown = [_as_shown(c) for c in decode_channels(last_good.detail)]
            bad = [c for c in shown if is_degraded(c, levels)]
            limits = [(c, _limit(c, levels)) for c in shown if not is_degraded(c, levels)]
            at_limit = _at_limit([(c, lim) for c, lim in limits if lim],
                                 [s for s in in_window if s.status not in NO_READING_STATUSES])
            bad.sort(key=lambda c: (-c.uncorrectables_recent, c.snr_db if c.snr_db is not None else 99.0))
            degraded_count = len(bad)
            degraded_rows = tuple(
                ("DS" if c.direction == "downstream" else "US", f"{c.frequency_mhz:g}", f"{c.power_dbmv:+.1f}",
                 f"{c.snr_db:.1f}" if c.snr_db is not None else "-", f"{c.uncorrectables_recent:,}",
                 _finding(c, levels)) for c in bad[:TABLE_ROWS])
        last_row = in_window[-1] if in_window else None
        stat3 = (min(snr), median(snr), max(snr)) if snr else None
        gaps = sorted(b.ts - a.ts for a, b in zip(in_window, in_window[1:]) if b.ts > a.ts)
        spacing = (median(gaps), gaps[int(0.1 * (len(gaps) - 1))], gaps[int(0.9 * (len(gaps) - 1))]) if gaps else None
        return DocsisFacts(
            **facts, **counts,
            ds_snr=stat3,
            ds_power=(min(dsp), max(dsp)) if dsp else None,
            us_power=(min(usp), max(usp)) if usp else None,
            last_ts=last_good.ts if last_good is not None else None,
            last_poll_kind=(None if last_row is None or last_row.status not in NO_READING_STATUSES
                            else (last_row.error_kind or last_row.status)),
            degraded=degraded_rows, degraded_count=degraded_count, at_limit=at_limit,
            events=tuple(events), errors_by_day=by_day, error_times=tuple(error_times), spacing=spacing,
        )

    def emit(self, facts: Optional[DocsisFacts]) -> List[ModuleEvent]:
        return list(facts.events) if facts is not None else []

    # --- section ---

    def section(self, facts: Optional[DocsisFacts], detail: str) -> ModuleSection:
        if facts is None or facts.good == 0:
            if facts is None or facts.readings == 0:
                last = "no poll recorded"
            else:
                kind = facts.last_poll_kind or "error"
                last = f"last poll: {_POLL_WORDS.get(kind, kind)}"
            return ModuleSection(None, [f"Set up, but no readings in this window ({last})."], None, {})
        f = facts
        findings = []
        parts = []
        if f.ds_snr:
            parts.append(f"downstream SNR {f.ds_snr[0]:.1f}–{f.ds_snr[2]:.1f} dB (median {f.ds_snr[1]:.1f})")
        if f.ds_power:
            parts.append(f"downstream power {f.ds_power[0]:+.1f} to {f.ds_power[1]:+.1f} dBmV")
        if f.us_power:
            parts.append(f"upstream power {f.us_power[0]:.1f} to {f.us_power[1]:.1f} dBmV")
        spacing = ""
        if f.spacing:
            mid, low, high = (round(x / 60) for x in f.spacing)
            spacing = (f"{plural(f.readings, 'reading')} in this window, a median of {plural(mid, 'minute')} apart"
                       + (f" (mostly {low} to {high})" if high - low >= 2 else "") + ". ")
        if parts or spacing:
            findings.append(spacing + ("Averages over the channels, per reading: " + "; ".join(parts) + "." if parts
                                       else ""))
        errors = (f"{f.new_errors:,} new uncorrectable codewords in {f.error_readings} of {plural(f.good, 'reading')}"
                  if f.new_errors else f"No new uncorrectable codewords in {plural(f.good, 'reading')}")
        shorter: Dict[int, Tuple[str, ...]] = {}

        def timed(count: int) -> str:
            # the largest counts, in time order; {counted:a:b} is the pack's token for the interval between two
            # readings and the incidents in it
            shown = sorted(sorted(f.error_times, key=lambda e: -e[2])[:count])
            more = len(f.error_times) - len(shown)
            return (f"{errors}: " + "; ".join(f"+{n:,} {{counted:{a:.0f}:{b:.0f}}}" for a, b, n in shown)
                    + (f"; {plural(more, 'more reading')} with fewer" if more else "") + ".")
        if f.error_times:
            findings.append(timed(ERROR_TIMES))
            fewer = range(min(ERROR_TIMES, len(f.error_times)) - 1, ERROR_TIMES_SHORTEST - 1, -1)
            shorter = {len(findings) - 1: tuple(timed(n) for n in fewer)}
        else:
            findings.append(f"{errors}.")
        if f.at_limit:
            findings.append(f"{plural(len(f.at_limit), 'channel')} at a healthy limit at the last reading, worth "
                            "watching, not a fault: " + "; ".join(f.at_limit[:AT_LIMIT_SHOWN])
                            + (f"; {len(f.at_limit) - AT_LIMIT_SHOWN} more" if len(f.at_limit) > AT_LIMIT_SHOWN else "")
                            + ".")
        table = None
        if f.degraded:
            findings.append(f"{plural(f.degraded_count, 'channel')} outside typical levels at the last reading:")
            table = (list(_CHANNEL_HEADER), [list(r) for r in f.degraded])
        # the restarts and the readings it did not answer are always in the headline, zeros too: no trim can hide
        # them, and a model that reads "0 modem restarts" does not send the user to look for one
        bits = [f"{f.new_errors:,} new uncorrectable codewords in {f.error_readings} of {plural(f.good, 'reading')}"
                if f.new_errors else f"no new uncorrectable codewords in {plural(f.good, 'reading')}",
                plural(f.restarts, "modem restart"),
                f"not answering in {plural(f.unreachable, 'reading')}" if f.unreachable else "answered every reading"]
        if f.degraded_count:
            bits.append(f"{plural(f.degraded_count, 'channel')} outside typical levels at the last reading")
        headline = ("Cable line: " + "; ".join(bits) + ".")[:200]
        columns = {"Uncorr. codewords": {d: f"{n:,}" for d, n in sorted(f.errors_by_day.items())}} \
            if f.errors_by_day else {}
        return ModuleSection(headline, findings, table, columns, shorter)
