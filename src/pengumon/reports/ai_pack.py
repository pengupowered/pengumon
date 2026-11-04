"""The AI troubleshooting pack as Markdown: `build_ai_pack` collects the facts (reports.ai_findings) and the
modules' sections (capabilities.contrib), `render_pack` renders them at one of three detail levels, through one Redactor (reports.redact).

Pure: no database, no clock, no network. The facts are the dataclasses below. Anything in them that is a sentence of
Pengumon's own refers to hops as `{hop:<target id>}` and to module devices as `{role:<key>}`; the renderer turns those
into role strings, so a name never enters a sentence. Descriptions, probe errors, URLs and raw node ids are fields of
the facts for completeness and are never rendered."""

import dataclasses
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone, tzinfo
from types import SimpleNamespace
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from pengumon.reports.redact import (
    ANONYMIZED, FULL, LEVELS, NEVER, VAGUE, WHAT_IT_IS, Redactor, is_home_hop, module_role_word,
)

HARD_CAP_CHARS = 32_000
HEADLINE_CHARS = 200
CAPS = {"compact": {"incidents": 20, "closeups": 3}, "detailed": {"incidents": 40, "closeups": 6}}
MIN_INCIDENTS, MIN_CLOSEUPS = 10, 1
NO_MODULE_TEXT = (
    "What Pengumon cannot see here: no modem or router is set up in Pengumon, so there are no line signal, line load or\n"
    "Wi-Fi readings, only the pings. If the problem starts at Router or Gateway, ask the user for what their internet box\n"
    "shows (its status page or lights), or to set up the box in Pengumon if it is supported.")

LEVEL_BLURB = {
    FULL: "**Full detail:** it names your devices and their addresses, your provider's routers and all measurements. "
          "Pengumon sent it nowhere.",
    VAGUE: "**Vague detail:** your own addresses, device names and MAC addresses are replaced by roles such as "
           "\"Router\"; your provider's routers and its name are kept. Pengumon sent it nowhere.",
    ANONYMIZED: "**Anonymized:** addresses, names, dates and the provider's name are replaced by roles and relative "
                "days (\"Day 1\" is the first day of the window). Pengumon sent it nowhere.",
}

# The fixed text: sections 1 to 3 of the pack (design 3).
FIXED_TEXT = """## For the AI assistant

You are helping someone who is not a network expert find out why their home internet misbehaves. Pengumon, a monitor on a computer in their home, measured the network and wrote the summary below. You cannot reach Pengumon or the network; the user can run tests and report back.

- Base every statement on this text and cite it (section, time, hop). Never invent figures, times or devices.
- Give each conclusion a confidence (high, medium, low) and the reason. Where the data cannot decide, say so and name the test that would.
- If a missing fact would change your conclusion, say which and how the user can get it. Do not ask for anything that would not change your answer, or that the data already rules out.
- If the user described no problem (last section), answer from the data first, then ask once, at the end, what goes wrong, on which device and when.
- Write for a non-expert: short sentences, explain each technical term once, concrete steps.
- For a complaint to the provider, tell the user to send Pengumon's "ISP dossier" (Pengumon: Dossier); do not write the evidence yourself.

## How Pengumon measures

- About once a second it pings every hop in "The path" and makes one lookup per DNS target; no reply within 2 s is a lost probe. Latency and jitter in ms, loss in % of probes, rates in Mbps, line signal in dBmV and dB, 24-hour times in the zone the first lines name.
- Every probe goes from the computer Pengumon runs on: it measures that computer's path to the internet, not other devices'.
- Roles: *Router* = the home router or mesh; *Gateway* = the modem or gateway, pinged on its home-side address (those pings do not use the provider's line); *ISP hop n* = the provider's routers, in order (unless marked as a DNS server); *Internet n* = public servers, pinged side by side (not a chain); *DNS lookup* = name lookups.
- "Normal" is the hop's own median over the last 7 days, of one-minute averages; "slow minutes" are minutes above twice normal (and at least 10 ms more).
- Read before concluding:
  - Provider routers often limit replies to pings. Loss at one hop that does not carry on to the later hops is usually harmless; only loss that reaches the Internet hops affects the user. Pengumon labels each incident with where the loss starts and whether it carries on.
  - A hop marked "ignores ping" never answers but passes traffic; it is counted nowhere.
  - Latency that rises with line load is a busy line ("bufferbloat"), not a fault. Incidents during heavy load, and loss that stays at the provider's routers, are marked "expected".
  - An incident's "Lasted" is how long Pengumon saw a problem; "Lost for" is how long probes were actually lost (0 s: it was about latency). One-minute averages hide short spikes: the close-ups give the peaks.
  - Loss already at the Router points inside the home (Wi-Fi, the router, or the monitoring computer's own link).
  - For loss starting at the Gateway or at ISP hop 1, the gateway's own event log at that time (a restart: the gateway; timeouts or lost sync with the provider: the line), or the provider's line check, is what tells the gateway, the line and the provider apart. Pengumon cannot read either: suggest it when such loss repeats or matches the user's problem.
  - Do not ask the user to repeat what Pengumon already measures over time (pinging the router, the gateway or internet servers); suggest only tests it cannot do.
  - "No data" means Pengumon was not measuring, not that the network was down.
- Verdicts: *gateway* = loss starts at the Gateway while the Router answers: the gateway itself or the link to it, not the line; *line* = loss starts at ISP hop 1 while the Gateway answers: the line or the provider's first router (codeword bursts or a signal out of range at the same time point to the line itself); *isp* = the provider's network beyond its first router.
- The verdicts and confidences are Pengumon's own rule-based reading of the data, not proof. For loss: *high*: 1-second data, loss starts where the verdict says, and a device reading at the same time supports it; *medium*: the same without such a reading; *low*: minute data only, an expected incident, loss at one hop only, or loss starting elsewhere. Latency only (no probe lost): *low* if under a minute, no hop clearly slower, one provider hop only, or rising elsewhere; else as for loss. The reason is in brackets.

## Questions

Answer with these headings:

1. **What is most likely wrong?** The cause, your confidence, and the figures behind it. Start with how much went wrong ("Overall").
2. **Where is it?** Inside the home (Wi-Fi or router), the gateway (modem) itself, the line into the home or the provider's first router, or the provider's network beyond; what rules out the others.
3. **What should I test next?** One to three simple tests that tell the remaining options apart, and what each result would mean.
4. **What can I fix myself, and when should I call my provider?** If it points to the line or the provider, say so and tell the user to send the ISP dossier.
5. **Would anything change your answer?** Only data that would change the conclusion, and how to get it. If nothing would, write just "Nothing essential is missing." Never list things that would not change it."""

# The user's own slot, the pack's last section.
SYMPTOM_TEXT = ("## What went wrong for you?\n\n"
                "(For you: one line on what goes wrong, on which device, roughly when; or leave it empty.)\n")
FOR_YOU = ("> **For you:** paste this whole text into a chat with any AI assistant (ChatGPT, Gemini, Claude, ...) and "
           "send it. Before sending, add one line in the last section: what goes wrong, on which device, and roughly "
           "when.")
UTC_TEXT = ("All times are UTC, which may differ from your local time: convert before you compare them with when you "
            "noticed a problem.")
# what loss starting at a hop of this role means, said in the close-up of a gateway or line verdict whose loss starts
# elsewhere than the verdict's own place (VERDICT_START)
START_MEANING = {
    "gateway": "Starting at the Gateway: the gateway itself or the cable from the router to it, not the line.",
    "isp": "Starting at ISP hop 1 while the Gateway answers: the line or the provider's first router.",
}
VERDICT_START = {"gateway": "gateway", "line": "isp"}
LINK_CLEAN_TEXT = ("The Router hop answered every probe in this window (0 % loss, no slow minutes), so the monitoring "
                   "computer's own link is ruled out and it does not matter whether that computer uses a cable or "
                   "Wi-Fi; do not ask about it. These probes say nothing about other devices' Wi-Fi: for a problem on "
                   "another device, suggest testing that device next to the router or on a cable.")
LINK_UNKNOWN_TEXT = ("The Router hop {what} in this window, so the monitoring computer's own link (cable or Wi-Fi) "
                     "may play a part: Pengumon does not know how that computer connects; worth asking the user.")
IPV6_NOTE = "This matters only for a problem limited to IPv6 services."
APPS_NOTE = "Their outages are apps on the monitoring computer, not network events, and do not matter here."
CHANGE_WORDS = ("\"Path\": the hops Pengumon pings were changed in Settings; \"route\": Pengumon's daily route check "
                "found the traffic taking a different way.")
# titles that claim no more than the evidence: a gateway that stopped answering may be the gateway or the cable to it
KIND_TITLE = {"gateway_down": "Gateway or its link not answering"}
CLOCK_CHANGE_NEAR_SECONDS = 600.0   # an incident starting this close to a daylight-saving change says so
COUNTED_NEAR_SECONDS = 3 * 3600.0   # a reading interval with no incident names the nearest one this close to it


@dataclass(frozen=True)
class PackScope:
    kind: str                       # "window" | "incident"
    label: str                      # "Past 7 days" | "Incident 14, with an hour either side"
    start: float
    end: float
    tz: tzinfo = timezone.utc
    zone: str = ""                  # the zone's name ("America/New_York") or its offset ("UTC+01:00")
    zone_kind: str = ""             # named | offset | utc; "": from tz (a fixed offset, or UTC at offset 0)


@dataclass(frozen=True)
class HopFact:
    id: str                         # the target id; may embed an address ("isp-198-51-100-1"); never rendered
    category: str                   # local | gateway | isp | public | dns | custom
    name: str
    address: str
    rdns: str = ""
    ignores_ping: bool = False
    normal_ms: Optional[float] = None
    p90_ms: Optional[float] = None
    jitter_ms: Optional[float] = None
    loss7_pct: Optional[float] = None
    window_ms: Optional[float] = None
    window_loss_pct: Optional[float] = None
    slow_minutes: int = 0
    lossy_minutes: int = 0
    note: str = ""                  # Pengumon's sentence (tokens allowed)
    error: str = ""                 # never rendered
    dns_server: bool = False        # a DNS target's name server is at this hop's address (or its name says so)


@dataclass(frozen=True)
class IncidentFact:
    id: int
    start: float
    duration_s: Optional[float]
    lost_s: Optional[float]
    start_hop: Optional[str]        # target id where loss starts; None: no hop isolated
    shape: str                      # carried | one_hop | none
    kind: str
    cause: str                      # a CAUSE_CATEGORIES value
    confidence: str                 # high | medium | low
    expected: bool
    score: float
    at_same_time: Tuple[str, ...]   # phrases of the coinciding events (tokens allowed), at most 4
    title: str                      # free text: swept at Vague, replaced by taxonomy words at Anonymized
    description: str = ""           # never rendered
    confidence_reason: str = ""     # reports.ai_findings.confidence's reason code
    latency_start: Optional[str] = None   # no hop isolated by loss: where latency rises (target id), None: nowhere
    latency_shape: str = ""         # carried | one_hop | none; "": not looked at
    latency_normal: Tuple[str, ...] = ()  # the hops before latency_start, checked and normal


@dataclass(frozen=True)
class CloseUpFact:
    incident_id: int
    hop_loss: Tuple[Tuple[str, float], ...]                    # (target id, loss % during the episode)
    at_same_time: Tuple[str, ...]
    header: Tuple[str, ...]                                    # column names after the time column (tokens allowed)
    rows: Tuple[Tuple[float, Tuple[str, ...]], ...]            # 1-minute rows, +-5 minutes
    fine_rows: Optional[Tuple[Tuple[float, Tuple[str, ...]], ...]] = None   # 10-second rows (incident scope)
    peaks: Tuple[Tuple[str, float, Optional[float]], ...] = ()   # (target id, highest probe in the episode, normal)


@dataclass(frozen=True)
class DayFact:
    date: str                       # local calendar day, ISO
    incidents: int
    lost_min: float


@dataclass(frozen=True)
class AppFact:
    name: str
    up: bool
    url: str = ""                   # never rendered
    error: str = ""                 # never rendered
    outages: Optional[int] = None   # outages in the window (stored incidents); None: say up / down instead
    down_s: float = 0.0


@dataclass(frozen=True)
class ChangeFact:
    ts: float
    text: str                       # Pengumon's sentence (tokens allowed)
    kind: str = ""                  # path | settings | route


@dataclass(frozen=True)
class ModuleInput:
    """One active contribution, ready to render: what capabilities.contrib's Contribution produced, plus the pieces
    the Redactor needs (hints). `section` has the ModuleSection attributes: headline, findings, table, day_columns."""
    id: str
    title: str
    layer: str                      # technology | capability
    budget_chars: int
    prompt_context: str
    section: Any
    driver_note: Optional[Tuple[str, Optional[str]]] = None   # (full, anonymized); None: no note
    role_labels: Mapping[str, str] = field(default_factory=dict)          # role key -> the configured label (Full)
    identifiers: Mapping[str, str] = field(default_factory=dict)          # value -> role key | "never"
    field_values: Mapping[str, Tuple[str, str, str]] = field(default_factory=dict)  # value -> (full, vague, anon)
    core_findings: Tuple[str, ...] = ()    # the core's bullets about this module's events (tokens allowed)


@dataclass(frozen=True)
class PackView:
    scope: PackScope
    version: str
    provider: str
    hops: Tuple[HopFact, ...]
    incidents: Tuple[IncidentFact, ...]            # time order
    closeups: Tuple[CloseUpFact, ...]              # most significant first
    findings: Tuple[str, ...]                      # core bullets (tokens allowed)
    time_table: Optional[Tuple[Tuple[str, ...], Tuple[Tuple[str, ...], ...]]]   # time of day x key hops (tokens allowed)
    days: Tuple[DayFact, ...]
    apps: Tuple[AppFact, ...]
    changes: Tuple[ChangeFact, ...]
    environment: Tuple[str, ...]                   # sentences (tokens allowed)
    modules: Tuple[ModuleInput, ...] = ()
    incident_total: int = 0                        # every incident of the window; 0: len(incidents)
    provider_source: str = ""                      # where the provider's name comes from ("named in Settings")
    known_hops: Tuple[HopFact, ...] = ()           # not shown, only swept: targets switched off, earlier path versions
    last_incident_end: Optional[float] = None      # the end of the window's last incident (listed or not); None: from
    #                                                the listed ones


@dataclass
class AiPack:
    markdown: str
    level: str
    detail: str
    chars: int
    tokens: int                                    # chars / 4
    trimmed: List[str]
    modules: List[Dict[str, Any]]                  # {id, title, chars, budget_chars, trimmed}
    roles: Dict[str, str]                          # "Who is who": role string -> the configured name
    swept: Dict[str, int]                          # what the sweep replaced (the counts line)
    hits: Dict[str, int]                           # what the final scan still found: empty when the rules held


# -- small formatting ---------------------------------------------------------------------------------------------
def _num(v: Optional[float], digits: int = 1) -> str:
    if v is None:
        return "-"
    return f"{v:.{digits}f}".rstrip("0").rstrip(".") if digits else str(int(round(v)))


def _dur(s: Optional[float]) -> str:
    if s is None:
        return "-"
    s = int(round(s))
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min {s % 60} s" if s % 60 else f"{s // 60} min"
    return f"{s // 3600} h {(s % 3600) // 60} min"


def _cell(s: str) -> str:
    return " ".join(str(s).replace("|", "/").split())


def _table(header: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    out = ["| " + " | ".join(_cell(h) for h in header) + " |", "|" + "---|" * len(header)]
    out += ["| " + " | ".join(_cell(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


_SHAPE = {"carried": "{}, carried on", "one_hop": "{} only"}
_REASON = {"minute_data": "minute data only", "expected": "expected", "one_hop": "one hop only",
           "place": "starts elsewhere", "no_hop": "no hop isolated", "corroborated": "a device reading agrees",
           "brief": "latency only, under a minute", "no_rise": "no hop clearly slower"}
_TIME_TOKEN = re.compile(r"\{(when|counted):(-?[0-9.]+)(?::(-?[0-9.]+))?\}")


def _and(words: Sequence[str]) -> str:
    """"Router", "Router and Gateway", "Router, Gateway and ISP hop 1"."""
    words = list(words)
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1]


def _ago(seconds: float) -> str:
    """A distance in time, to the minute from a minute on: "29 s", "46 min", "2 h 5 min"."""
    seconds = abs(seconds)
    return _dur(seconds if seconds < 60 else round(seconds / 60) * 60)


def gap_words(gap: float) -> str:
    """" (46 min before)" / " (5 min after)" for an event's distance from an incident (> 0: before it); "" when
    they overlap or are less than a minute apart."""
    if abs(gap) < 60:
        return ""
    return f" ({_ago(gap)} {'before' if gap > 0 else 'after'})"


def clock_changes(tz: tzinfo, start: float, end: float) -> List[Tuple[float, float, float]]:
    """The daylight-saving changes of `tz` in [start, end]: (the first second of the new offset, the offset before,
    the offset after), offsets in seconds. A fixed offset has none."""
    if isinstance(tz, timezone):
        return []

    def offset(t: float) -> float:
        return (datetime.fromtimestamp(t, tz).utcoffset() or timedelta(0)).total_seconds()

    out = []
    t, before = int(start), offset(int(start))
    while t < end:
        nxt = int(min(end, t + 3600))
        after = offset(nxt)
        if after != before:
            lo, hi = t, nxt           # the offset at lo is the old one, at hi the new one
            while hi - lo > 1:
                mid = (lo + hi) // 2
                lo, hi = (mid, hi) if offset(mid) == before else (lo, mid)
            out.append((float(hi), before, after))
            before = after
        t = nxt if nxt > t else t + 1
    return out


class _Render:
    """One rendering pass at one set of caps."""

    def __init__(self, facts: PackView, rx: Redactor, detail: str):
        self.f, self.rx, self.detail = facts, rx, detail
        self.span = facts.scope.end - facts.scope.start
        self.category = {h.id: h.category for h in facts.hops}
        self.number: Dict[int, int] = {}          # incident id -> its number in the table (set by incidents())
        self.kind, self.zone_name = _zone_of(facts.scope)
        self.changes = clock_changes(facts.scope.tz, facts.scope.start, facts.scope.end)

    def t(self, s: str, table: bool = False) -> str:
        if s and "{" in s:
            s = _TIME_TOKEN.sub(self._time_token, s)
        return self.rx.text(s, table=table)

    def _time_token(self, m) -> str:
        ts = float(m.group(2))
        if m.group(1) == "when":
            return self.rx.when(ts, "dayclock" if self.span > 86400 else "clock")
        return self.counted(ts, float(m.group(3) or ts))

    def stamp(self, ts: float) -> str:
        return self.rx.when(ts, "dayclock" if self.span > 86400 else "clock_s")

    def counted(self, a: float, b: float) -> str:
        """"counted between 22:40 and 23:10, with incident #1 (23:06:04) in that interval": what a device counted
        between two readings, and the incidents that overlap that interval; else the nearest one within
        COUNTED_NEAR_SECONDS of it."""
        wide = self.span > 86400
        same_day = self.rx.when(a, "day") == self.rx.when(b, "day")
        first = self.rx.when(a, "dayclock" if wide else "clock")
        second = self.rx.when(b, ("clock_s" if same_day else "dayclock") if wide else "clock")
        text = f"counted between {first} and {second}"

        def which(i: IncidentFact) -> str:
            return f"incident #{self.number[i.id]}" if i.id in self.number else "an incident not listed"

        inside = [i for i in self.f.incidents if i.start <= b and i.start + (i.duration_s or 0.0) >= a]
        if inside:
            return text + ", with " + _and([f"{which(i)} ({self.stamp(i.start)})" for i in inside[:3]]) \
                + " in that interval"
        if not self.f.incidents:
            return text

        def distance(i: IncidentFact) -> float:
            return i.start - b if i.start > b else a - (i.start + (i.duration_s or 0.0))
        i = min(self.f.incidents, key=distance)
        if distance(i) > COUNTED_NEAR_SECONDS:
            return f"{text}, no incident within {_span_words(COUNTED_NEAR_SECONDS)}"
        how = f"started {_ago(i.start - b)} after it" if i.start > b else f"ended {_ago(distance(i))} before it"
        return f"{text}, no incident in that interval ({which(i)} {how})"

    def change_words(self, change: Tuple[float, float, float]) -> str:
        at, before, after = change
        delta = after - before
        amount = "one hour" if abs(delta) == 3600 else f"{_num(abs(delta) / 60, 0)} minutes"
        wall = datetime.fromtimestamp(at, timezone(timedelta(seconds=before))).strftime("%H:%M")
        to = datetime.fromtimestamp(at, timezone(timedelta(seconds=after))).strftime("%H:%M")
        text = f"clocks went {'forward' if delta > 0 else 'back'} {amount} on {self.rx.when(at, 'day')} at {wall}, to {to}"
        if delta < 0:      # the hour before the change happened twice: its times carry the redactor's marks
            first, second = self.rx.repeated_mark(at - 1).strip(), self.rx.repeated_mark(at).strip()
            if not first.startswith("("):      # a zone's abbreviations say which pass; "(1st)" says it itself
                first, second = f"{first} (first)", f"{second} (second)"
            text += f", so {to} to {wall} that night happened twice: times in it are marked {first} or {second}"
        return text

    def near_change(self, ts: float) -> str:
        """"starts 29 s after the clocks went back" for an incident close to a daylight-saving change."""
        for at, before, after in self.changes:
            d = ts - at
            if abs(d) <= CLOCK_CHANGE_NEAR_SECONDS:
                return (f"starts {_ago(d)} {'after' if d >= 0 else 'before'} the clocks went "
                        f"{'forward' if after > before else 'back'}")
        return ""

    # -- sections -----------------------------------------------------------------------------------------------
    def header(self, tokens: int) -> str:
        f, rx = self.f, self.rx
        sc = f.scope
        span = f"{rx.when(sc.start)} to {rx.when(sc.end)}"
        if self.kind == "utc":
            times = f"{span}. {UTC_TEXT}"
        else:
            bits = []
            if rx.level != ANONYMIZED:   # a zone or an offset says roughly where the home is
                bits.append(self.zone_name if self.kind == "named"
                            else f"{rx.zone(sc.start)}, a fixed offset: daylight-saving changes are not applied")
            bits += [self.change_words(c) for c in self.changes]
            times = f"{span}, local time" + (f" ({'; '.join(bits)})" if bits else "") + "."
        return (f"# Network troubleshooting pack from Pengumon\n\n{FOR_YOU}\n> {LEVEL_BLURB[rx.level]}\n\n"
                f"{sc.label}, {times} Pengumon {f.version}. About {tokens:,} tokens.\n\n")

    def _what(self, h: HopFact) -> str:
        what = WHAT_IT_IS.get(h.category, "other")
        if h.dns_server and h.category in ("isp", "public"):
            what = "provider DNS server" if h.category == "isp" else "public DNS server"
        return what

    def _cache_note(self, h: HopFact) -> str:
        """A DNS lookup answered faster than the first provider router cannot have crossed the provider."""
        if h.category != "dns" or h.normal_ms is None:
            return ""
        ref = next((x for cat in ("isp", "public") for x in self.f.hops
                    if x.category == cat and x.normal_ms is not None), None)
        if ref is None or h.normal_ms >= ref.normal_ms:
            return ""
        return (f"answered in the home (faster than {self.rx.hop(ref.id)}), likely from a cache: it does not test "
                "the internet path")

    def hops(self) -> str:
        rx = self.rx
        rows = []
        for h in self.f.hops:
            addr, name = rx.hop_fields(h)
            where = addr if name in ("-", "", addr) else (name if addr == "-" else f"{addr} ({name})")
            note = "; ".join(x for x in (self.t(h.note) if h.note else "", "ignores ping" if h.ignores_ping else "",
                                         self._cache_note(h)) if x)
            rows.append([rx.hop(h.id), self._what(h), where, _num(h.normal_ms), _num(h.p90_ms),
                         _num(h.jitter_ms), _num(h.loss7_pct, 2), _num(h.window_ms), _num(h.window_loss_pct, 2),
                         str(h.slow_minutes), str(h.lossy_minutes), note])
        head = ["Hop", "What it is", "Address (name)", "Normal ms", "P90", "Jitter", "7-day loss %", "Window ms",
                "Window loss %", "Slow min", "Lossy min", "Notes"]
        provider = ""
        if self.f.provider and rx.level != ANONYMIZED:
            source = f" ({self.f.provider_source})" if self.f.provider_source else ""
            provider = f"The provider: {self.t(rx.provider_text())}{source}.\n\n"
        return "## The path and each hop's normal\n" + _table(head, rows) + "\n\n" + provider

    def findings(self) -> str:
        """The scale first ("Overall"), then each device's headline (or that it saw nothing notable), then the rest of
        Pengumon's bullets."""
        core = [f"- {self.t(x)}" for x in self.f.findings]
        first = [x for x in core if x.startswith("- **Overall.**")]
        devices = []
        for m in self.f.modules:
            head = getattr(m.section, "headline", None)
            if head:
                devices.append(f"- {self.t(head)[:HEADLINE_CHARS]}")
            else:
                own = list(getattr(m.section, "findings", None) or [])
                quiet = "no readings" if own and own[0].startswith("Set up, but no") else "nothing notable"
                devices.append(f"- {m.title}: {quiet} in this window.")
        return "## Findings\n" + "\n".join(first + devices + [x for x in core if x not in first]) + "\n\n"

    def _latency_where(self, i: IncidentFact) -> str:
        lead = "latency only" if not i.lost_s else "no hop isolated by loss"
        if not i.latency_shape:
            return "no hop isolated"
        if i.latency_shape == "none" or i.latency_start is None:
            return f"{lead}, no hop clearly slower"
        hop = self.rx.hop(i.latency_start)
        if i.latency_shape == "one_hop":
            return f"{lead}, at {hop} only (later hops normal)"
        normal = [self.rx.hop(t) for t in i.latency_normal]
        tail = f" ({_and(normal)} normal)" if normal else ""
        if self.category.get(i.latency_start) == "public":
            return f"{lead}, at the Internet hops{tail}"
        return f"{lead}, rising from {hop} onward{tail}"

    def _where(self, i: IncidentFact) -> str:
        if i.start_hop is None or i.shape == "none":
            return self._latency_where(i)
        return _SHAPE.get(i.shape, "{}").format(self.rx.hop(i.start_hop))

    def _at_same_time(self, i: IncidentFact, phrases: Sequence[str]) -> List[str]:
        near = self.near_change(i.start)
        return ([near] if near else []) + [self.t(p) for p in phrases[:4]]

    def incidents(self, cap: int) -> Tuple[str, int]:
        inc = list(self.f.incidents)
        kept = sorted(inc, key=lambda i: (-i.score, -i.start))[:cap] if len(inc) > cap else inc
        kept.sort(key=lambda i: i.start)
        self.number = {i.id: n for n, i in enumerate(kept, 1)}
        rows = []
        for n, i in enumerate(kept, 1):
            verdict = i.cause + (", expected" if i.expected else "")
            why = _REASON.get(i.confidence_reason, "")
            rows.append([str(n), self.stamp(i.start), _dur(i.duration_s), _dur(i.lost_s), self._where(i), verdict,
                         f"{i.confidence} ({why})" if why else i.confidence,
                         "; ".join(self._at_same_time(i, i.at_same_time)) or "-",
                         KIND_TITLE.get(i.kind) or self.rx.title(i.title, i.kind, i.cause)])
        head = ["#", "Start", "Lasted", "Lost for", "Where it starts", "Verdict", "Confidence", "At the same time", "Title"]
        total = max(self.f.incident_total, len(inc))
        what = f"{total}, in time order" if len(kept) == total else f"the {len(kept)} most significant of {total}, in time order"
        body = _table(head, rows) if rows else "No incidents in this window."
        return f"## Incidents ({what})\n{body}\n\n", len(kept)

    def closeups(self, cap: int) -> str:
        ups = list(self.f.closeups)[:cap]
        if not ups:
            return ""
        by_id = {i.id: i for i in self.f.incidents}
        out = [f"## Close-ups (the {len(ups)} most significant)"]
        for c in ups:
            i = by_id.get(c.incident_id)
            n = f" {self.number[c.incident_id]}" if c.incident_id in self.number else ""
            title = (f"### Incident{n} at {self.stamp(i.start)}, {self._where(i)}" if i
                     else f"### Incident {c.incident_id}")
            out.append(title)
            start = self.category.get(i.start_hop or "", "") if i is not None and i.shape != "none" else ""
            if (i is not None and i.cause in VERDICT_START and start != VERDICT_START[i.cause]
                    and start in START_MEANING):
                out.append(START_MEANING[start])
            if c.hop_loss and not any(v for _, v in c.hop_loss):
                out.append("No probe to any hop was lost in the episode.")
            elif c.hop_loss:
                out.append(_table(["Hop", "Loss % in the episode"], [[self.rx.hop(h), _num(v, 1)] for h, v in c.hop_loss]))
            same = self._at_same_time(i, c.at_same_time) if i is not None else [self.t(p) for p in c.at_same_time[:4]]
            if same:
                out.append("At the same time: " + "; ".join(same))
            if c.peaks:
                lasted = f"; the incident lasted {_dur(i.duration_s)}" if i is not None and i.duration_s else ""
                out.append("Highest single probe: " + ", ".join(
                    f"{self.rx.hop(t)} {_num(p)} ms" + (f" (normal {_num(nm)})" if nm is not None else "")
                    for t, p, nm in c.peaks) + f". Each is one probe, not a lasting delay{lasted}.")
            head = ["Time"] + [self.t(h) for h in c.header]
            out.append("1-minute rows, 5 minutes either side:\n" + _table(head, [[self.rx.when(ts, "clock")] + [self.t(x) for x in r]
                                                                                 for ts, r in c.rows]))
            if c.fine_rows:
                out.append("10-second rows:\n" + _table(head, [[self.rx.when(ts, "clock_s")] + [self.t(x) for x in r]
                                                                for ts, r in c.fine_rows]))
        return "\n\n".join(out) + "\n\n"

    def week(self) -> str:
        f = self.f
        if not f.days and not f.time_table:
            return ""
        out = ["## A normal week"]
        if f.time_table:
            out.append(_table([self.t(h) for h in f.time_table[0]], [[self.t(c) for c in r] for r in f.time_table[1]]))
        if f.days:
            cols: List[Tuple[str, Mapping[str, str]]] = []
            for m in f.modules:
                for name, per_day in list((getattr(m.section, "day_columns", None) or {}).items())[:2]:
                    cols.append((name, per_day))
            rows = [[self.rx.day(d.date), str(d.incidents), _num(d.lost_min)] + [self.t(c.get(d.date, "-"), table=True)
                                                                                 for _, c in cols] for d in f.days]
            out.append(_table(["Day", "Incidents", "Lost min"] + [n for n, _ in cols], rows))
        return "\n\n".join(out) + "\n\n"

    def module(self, m: ModuleInput, tables: bool = True) -> Tuple[str, str]:
        """(text, trimmed note) of one contribution, cut to its budget: table rows first, then bullets past the
        second; the prompt context is never cut."""
        sec = m.section
        own = list(getattr(sec, "findings", None) or [])
        bullets = own[:2] + list(m.core_findings) + own[2:]   # a table's lead-in bullet stays last
        table = getattr(sec, "table", None) if tables else None
        rows = list(table[1]) if table else []
        note = ""
        if m.driver_note:
            full, anon = m.driver_note
            note = full if self.rx.level != ANONYMIZED else (anon or "")

        def build(nb: int, nr: int) -> str:
            parts = [f"### {m.title}", self.t(m.prompt_context)]
            if bullets[:nb]:
                parts.append("\n".join(f"- {self.t(b)}" for b in bullets[:nb]))
            if table and nr:
                parts.append(_table([self.t(h) for h in table[0]], [[self.t(c, table=True) for c in r] for r in rows[:nr]]))
            if note:
                parts.append(self.t(note))
            return "\n\n".join(parts) + "\n\n"

        nb, nr = len(bullets), len(rows)
        text = build(nb, nr)
        cut = []
        while len(text) > m.budget_chars and nr > 0:
            nr -= 1
            text = build(nb, nr)
        if nr < len(rows):
            cut.append(f"table rows {nr} of {len(rows)}")
        while len(text) > m.budget_chars and nb > 2:
            nb -= 1
            text = build(nb, nr)
        if nb < len(bullets):
            cut.append(f"bullets {nb} of {len(bullets)}")
        if len(text) > m.budget_chars:
            cut.append("over budget by the prompt context")
        return text, "; ".join(cut)

    def devices(self, tables: bool) -> Tuple[str, List[Dict[str, Any]]]:
        mods = sorted(self.f.modules, key=lambda m: 0 if m.layer == "technology" else 1)
        if not mods:
            return f"## Your devices\n{NO_MODULE_TEXT}\n\n", []
        out, info = ["## Your devices\n\n"], []
        for m in mods:
            text, cut = self.module(m, tables)
            out.append(text)
            info.append({"id": m.id, "title": m.title, "chars": len(text), "budget_chars": m.budget_chars, "trimmed": cut})
        return "".join(out), info

    def _link(self) -> str:
        """The monitoring computer's own link: ruled out by a clean Router hop, else unknown and worth asking about."""
        r = next((h for h in self.f.hops if h.category == "local"), None)
        if r is not None and r.window_loss_pct is not None and not r.lossy_minutes and not r.slow_minutes:
            return LINK_CLEAN_TEXT
        if r is None:
            return ("There is no Router hop in the path, so the monitoring computer's own link (cable or Wi-Fi) cannot "
                    "be told apart from the rest: worth asking the user how that computer connects.")
        bits = []
        if r.window_loss_pct is None:
            bits.append("has no measurements")
        if r.lossy_minutes:
            bits.append(f"lost probes in {_count(r.lossy_minutes, 'minute', 'minutes')}")
        if r.slow_minutes:
            bits.append(f"had {_count(r.slow_minutes, 'slow minute', 'slow minutes')}")
        return LINK_UNKNOWN_TEXT.format(what=" and ".join(bits))

    def tail(self) -> str:
        f, rx = self.f, self.rx
        out = ["## Host apps, changes, environment"]
        if f.apps:
            def state(a: AppFact) -> str:
                if a.outages is None:
                    return "up" if a.up else "down"
                if not a.outages:
                    return "no outage in this window"
                return f"{_count(a.outages, 'outage', 'outages')}, {_dur(a.down_s)} in all"
            note = f" {APPS_NOTE}" if any(a.outages for a in f.apps) else ""
            out.append("Host apps (other apps on the monitoring computer that Pengumon checks): " + "; ".join(
                f"{rx.text(a.name) if rx.level == FULL else rx.app(a.name)} ({state(a)})" for a in f.apps) + "." + note)
        if f.changes:
            last = f.last_incident_end
            if last is None and f.incidents:
                last = max(i.start + (i.duration_s or 0.0) for i in f.incidents)
            style = "dayclock" if self.span > 86400 else "clock"
            lines = [f"- {rx.when(c.ts, style)}: {self.t(c.text)}"
                     + (" (after the last incident)" if last is not None and c.ts > last else "") for c in f.changes]
            words = f"\n\n{CHANGE_WORDS}" if any(c.kind in ("path", "route") for c in f.changes) else ""
            out.append("Changes:\n" + "\n".join(lines) + words)
        env = [self.t(e) + (f" {IPV6_NOTE}" if e.startswith("IPv6:") else "") for e in f.environment]
        env.append(self._link())
        out.append("Environment:\n" + "\n".join(f"- {e}" for e in env))
        return "\n\n".join(out) + "\n"


def _zone_of(scope: PackScope) -> Tuple[str, str]:
    """(kind, name) of the scope's zone: named (an IANA zone, which follows daylight saving), offset (a fixed one) or
    utc (no zone known)."""
    if scope.zone_kind:
        return scope.zone_kind, scope.zone
    if isinstance(scope.tz, timezone):
        off = scope.tz.utcoffset(None) or timedelta(0)
        return ("offset" if off else "utc"), scope.zone
    return "named", scope.zone or str(scope.tz)


def _redactor(facts: PackView, level: str) -> Redactor:
    identifiers: Dict[str, str] = {}
    labels: Dict[str, str] = {}
    fields: Dict[str, Tuple[str, str, str]] = {}
    for m in facts.modules:
        identifiers.update(m.identifiers)
        labels.update(m.role_labels)
        fields.update(m.field_values)
    return Redactor(level, hops=facts.hops, provider=facts.provider, apps=[a.name for a in facts.apps],
                    module_identifiers=identifiers, module_labels=labels, field_values=fields, tz=facts.scope.tz,
                    day_zero=facts.scope.start, known_hops=facts.known_hops, vocabulary=_vocabulary(facts, level))


def _vocabulary(facts: PackView, level: str) -> str:
    """Pengumon's own text at this level, which names nobody: an identifier that is a word or phrase of it ("modem")
    is not swept. Not the modules' findings or tables, which must carry role keys only."""
    parts = [FIXED_TEXT, NO_MODULE_TEXT, " ".join(WHAT_IT_IS.values()), FOR_YOU, *LEVEL_BLURB.values(), SYMPTOM_TEXT,
             UTC_TEXT, *START_MEANING.values(), LINK_CLEAN_TEXT, LINK_UNKNOWN_TEXT, IPV6_NOTE,
             APPS_NOTE, CHANGE_WORDS, *KIND_TITLE.values()]
    for m in facts.modules:
        parts += [m.title, m.prompt_context]
        if m.driver_note:
            parts.append((m.driver_note[0] if level != ANONYMIZED else m.driver_note[1]) or "")
    return "\n".join(parts)


def render_pack(facts: PackView, level: str = FULL, detail: str = "compact") -> AiPack:
    """Render the pack. `level`: full | vague | anonymized. `detail`: compact | detailed (the caps of 2.4). Over the
    hard cap, the trimming order of 4.1: incident rows to 10, close-ups to 1, the module tables."""
    if level not in LEVELS:
        raise ValueError(f"unknown detail level {level!r}")
    caps = CAPS[detail]
    inc_cap, up_cap, tables = caps["incidents"], caps["closeups"], True
    trimmed: List[str] = []

    def render(tokens: int):
        rx = _redactor(facts, level)
        r = _Render(facts, rx, detail)
        inc_text, shown = r.incidents(inc_cap)
        dev_text, info = r.devices(tables)
        body = (r.header(tokens) + FIXED_TEXT + "\n\n" + r.hops() + r.findings() + inc_text + r.closeups(up_cap) + r.week()
                + dev_text + r.tail() + "\n" + SYMPTOM_TEXT)
        for h in facts.hops:       # register every role for the key, used or not
            rx.hop(h.id)
        body = rx.scan(body)
        return body, rx, info, shown

    while True:
        body, rx, info, shown = render(99_999)   # the widest size the header can state: the final pack is no longer
        if len(body) <= HARD_CAP_CHARS:
            break
        if inc_cap > MIN_INCIDENTS and len(facts.incidents) > MIN_INCIDENTS:
            inc_cap = MIN_INCIDENTS
            trimmed.append(f"incident rows cut to {MIN_INCIDENTS}")
        elif up_cap > MIN_CLOSEUPS and len(facts.closeups) > MIN_CLOSEUPS:
            up_cap = MIN_CLOSEUPS
            trimmed.append(f"close-ups cut to {MIN_CLOSEUPS}")
        elif tables and any(getattr(m.section, "table", None) for m in facts.modules):
            tables = False
            trimmed.append("module tables dropped")
        else:
            trimmed.append("still over the hard cap")
            break
    tokens = round(len(body) / 4 / 100) * 100
    body, rx, info, shown = render(tokens)       # the header states the size, which it adds to
    for m in info:
        if m["trimmed"]:
            trimmed.append(f"{m['id']}: {m['trimmed']}")
    return AiPack(markdown=body, level=level, detail=detail, chars=len(body), tokens=len(body) // 4, trimmed=trimmed,
                  modules=info, roles=dict(rx.used_roles), swept=dict(rx.swept), hits=dict(rx.hits))


# -- from the facts to the view, and the async entry point -------------------------------------------------------------
_SHAPE_WORDS = (("carried", "loss starts at one hop and carries on to the later hops"),
                ("one_hop", "loss at one hop only (usually ping rate-limiting, harmless)"),
                ("none", "no hop isolated by loss"))


def _span_words(seconds: float) -> str:
    """"24 hours", "7 days", "1 hour", "2 h 30 min"."""
    s = int(round(seconds))
    if s % 86400 == 0 and s > 86400:
        return f"{s // 86400} days"
    if s % 3600 == 0:
        return _count(s // 3600, "hour", "hours")
    return _dur(s)


def _overall(f) -> str:
    """The scale of the window before any verdict: the seconds of lost probes and their share of the window, the
    incidents that were about latency only, and how the Router and Gateway hops fared."""
    real = [i for i in f.incidents if not i.expected]
    lost = sum(i.lost_for for i in real if i.lost_for > 0)
    lossy = [i for i in real if i.lost_for > 0]
    latency = [i for i in real if not i.lost_for > 0]
    window = max(1.0, f.end - f.start)
    if lossy:
        share = lost * 100.0 / window
        text = (f"{_dur(lost)} of lost probes in {_span_words(window)} ({share:.2g} % of the time), in "
                f"{_count(len(lossy), 'incident', 'incidents')}")
    else:
        text = f"No lost probes in an incident in {_span_words(window)}"
    if latency:
        shortest, longest = (_dur(d) for d in (min(i.duration for i in latency), max(i.duration for i in latency)))
        size = shortest if shortest == longest else f"{shortest} to {longest}"
        each = " each" if len(latency) > 1 else ""
        text += f"; {_count(len(latency), 'incident was', 'incidents were')} about latency only ({size}{each})"
    homes = []
    for cat in ("local", "gateway"):
        h = next((h for h in f.hops if h.category == cat), None)
        if h is None:
            continue
        w = h.window
        if w.loss_pct is None:
            homes.append(f"{{hop:{h.target_id}}} not measured")
        elif not w.lossy_minutes and not w.slow_minutes:
            homes.append(f"{{hop:{h.target_id}}} clean (no loss, no slow minute)")
        else:
            pct = _num(w.loss_pct, 2) if w.loss_pct >= 0.01 else "under 0.01"
            bits = [f"{pct} % loss in {_count(w.lossy_minutes, 'minute', 'minutes')}"] if w.lossy_minutes else []
            bits += [_count(w.slow_minutes, "slow minute", "slow minutes")] if w.slow_minutes else []
            homes.append(f"{{hop:{h.target_id}}} {', '.join(bits)}")
    pings = f". Pings: {'; '.join(homes)}" if homes else ""
    return f"**Overall.** {text}{pings}."


def _count(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _deviations(f, limit: int = 4) -> List[str]:
    """The hops whose window is notably off their own normal (reports.ai_findings.hop_window_vs_normal), path order."""
    out = []
    for h in f.hops:
        w, b = h.window, h.baseline
        bits = []
        if "slower" in w.notable:
            bits.append(f"median {_num(w.median_ms)} ms against {_num(b.normal_ms)} ms normally")
        if "slow_minutes" in w.notable:
            share = w.slow_minutes * 100.0 / max(1, w.minutes)
            bits.append(f"{_count(w.slow_minutes, 'slow minute', 'slow minutes')} ({share:.0f} % of the window)")
        if "lossier" in w.notable:
            bits.append(f"loss {_num(w.loss_pct, 2)} % against {_num(b.loss_pct, 2)} % normally")
        if bits:
            out.append(f"{{hop:{h.target_id}}}: " + ", ".join(bits))
    return out[:limit] + ([f"{len(out) - limit} more hops"] if len(out) > limit else [])


def _findings(f) -> List[str]:
    """Pengumon's own bullets (design 2.1, section 5) from reports.ai_findings.PackFacts: roles and numbers only; a
    hop is a `{hop:<id>}` token."""
    p = f.patterns
    out: List[str] = [_overall(f)]
    apps = f"; {_count(f.service_count, 'host-app outage', 'host-app outages')} (not the network)" if f.service_count else ""
    expected = (f", and {f.expected_count} marked expected (a busy line, or loss only at the provider's routers)"
                if f.expected_count else "")
    if p.count:
        out.append(f"**Incidents.** {_count(p.count, 'network incident', 'network incidents')} ({p.per_day_avg:.1f} a day)"
                   f"{expected}{apps}.")
        shapes = "; ".join(f"{p.shapes[k]}: {w}" + (_latency_starts(f) if k == "none" else "")
                           for k, w in _SHAPE_WORDS if p.shapes.get(k))
        firsts = ", ".join(f"{{hop:{t}}} ({n})" for t, n in p.first_hops)
        top = p.first_hops[0][1] if p.first_hops else 0
        clear = top >= 2 and (len(p.first_hops) == 1 or p.first_hops[1][1] < top)
        where = (f" Most often at {firsts}." if clear else f" Loss started at {firsts}.") if firsts else ""
        out.append(f"**Where problems start.** {shapes}.{where}")
        if p.blocks:
            ranked = sorted((b[2] for b in p.blocks), reverse=True)
            top_b = max(p.blocks, key=lambda b: b[2])
            if ranked[0] >= 2 and (len(ranked) == 1 or ranked[1] < ranked[0]):
                when = f"{top_b[2]} of {p.count} started between {top_b[0]:02d}:00 and {top_b[1] % 24:02d}:00"
            else:
                when = "No 6-hour block of the day stands out"
            peak = (f"; busiest hour {p.peak_hour:02d}:00 ({p.peak_count} of {p.count})"
                    if p.peak_hour is not None else "")
            lost = (f"{p.with_loss} lost probes: median {_dur(p.median_lost)}, longest {_dur(p.longest_lost)}"
                    if p.with_loss else "none lost probes")
            latency = f"; {p.count - p.with_loss} only about latency" if p.with_loss and p.with_loss < p.count else ""
            out.append(f"**Time of day.** {when}{peak}. Of the {p.count}, {lost}{latency}.")
        if p.median_gap is not None and p.count >= 3:     # two starts make one gap: no pattern
            limit = _dur(f.rules.get("cluster_gap_seconds", 900.0))
            close = (f"; {p.close_together} started within {limit} of the one before" if p.close_together else "")
            groups = (f" ({_count(p.clusters, 'cluster', 'clusters')}, the largest of {p.largest_cluster})"
                      if p.clusters else "")
            every = f"; about every {_dur(p.about_every)}" if p.about_every else ""
            out.append(f"**Recurrence.** Median gap {_dur(p.median_gap)}{close}{groups}{every}.")
    else:
        out.append("**Incidents.** No network incident" + (" that is not expected" if f.expected_count else "")
                   + f" in this window{expected}{apps}.")
    labels = getattr(f, "labels_of_events", {}) or {}
    if f.coincided:
        out.append("**What coincided.** " + "; ".join(
            f"{labels.get(c) or c.replace('.', ' ').replace('_', ' ')} with {n} of {p.count}"
            for c, n in sorted(f.coincided.items(), key=lambda kv: (-kv[1], kv[0]))) + " incidents.")
    elif p.count:
        out.append("**What coincided.** No device reading or change coincided with an incident.")
    if f.verdicts:
        out.append("**Pengumon's verdicts.** " + "; ".join(
            f"{c} {v['total']} (" + ", ".join(f"{k} {v[k]}" for k in ("high", "medium", "low") if v[k]) + ")"
            for c, v in sorted(f.verdicts.items(), key=lambda kv: -kv[1]["total"])) + ". Rule-based, not proof.")
    devs = _deviations(f)
    if devs:
        out.append("**Against each hop's normal.** " + "; ".join(devs) + ".")
    if f.primary_hop:
        later = f" and later at {', '.join('{hop:%s}' % t for t in f.later_lossy)}" if f.later_lossy else ""
        out.append(f"**First hop with elevated loss in the window.** {{hop:{f.primary_hop}}}{later}.")
    if f.no_data:
        out.append(f"**No data.** {_count(len(f.no_data), 'gap', 'gaps')} totalling "
                   f"{_dur(sum(b - a for a, b in f.no_data))}: Pengumon was not measuring then (times at the end).")
    return out


def _latency_starts(f) -> str:
    """" (latency only: rising from {hop:isp1} onward in 7, no hop clearly slower in 2)" for the unexpected incidents
    with no hop isolated by loss."""
    counts: Dict[Optional[str], int] = {}
    for i in f.incidents:
        if not i.expected and i.shape.kind == "none" and i.latency is not None:
            key = i.latency.first_hop if i.latency.kind == "carried" else (
                f"one:{i.latency.first_hop}" if i.latency.kind == "one_hop" else None)
            counts[key] = counts.get(key, 0) + 1
    if not counts:
        return ""
    words = []
    for key, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0] or "~")):
        if key is None:
            words.append(f"no hop clearly slower in {n}")
        elif key.startswith("one:"):
            words.append(f"slower at {{hop:{key[4:]}}} only in {n}")
        else:
            words.append(f"latency rising from {{hop:{key}}} on in {n}")
    return f" ({', '.join(words)})"


def module_findings(f, codes: Sequence[str]) -> List[str]:
    """The core's bullets for one module's section: how its events fell against the incidents, and the Internet's
    latency while the line was busy (an event that supports "load"). `codes`: the module's event codes."""
    out = []
    for code in codes:
        es = (getattr(f, "event_summaries", None) or {}).get(code)
        if es is None:
            continue
        where = "; ".join(("loss starting at {hop:%s} in %d" % (t, n)) if t else f"no hop isolated in {n}"
                          for t, n in es.starts)
        if not es.during:
            during = f"none of {es.count} during an incident" if es.count > 1 else "not during an incident"
        elif es.during == es.count:
            during = f"all {es.count} during an incident" if es.count > 1 else "during an incident"
        else:
            during = f"{es.during} of {es.count} during an incident"
        out.append(f"{es.label[:1].upper()}{es.label[1:]}: {during}"
                   + (f" ({where})" if es.during and any(t for t, _ in es.starts) else "") + ".")
    busy = getattr(f, "busy_latency", None)
    if busy is not None and any(c in (getattr(f, "load_codes", None) or ()) for c in codes):
        other = f", {_num(busy.other_ms)} ms otherwise" if busy.other_ms is not None else ""
        out.append(f"{{hop:{busy.target_id}}} latency while the line was busy: {_num(busy.busy_ms)} ms "
                   f"(median of {_count(busy.busy_minutes, 'minute', 'minutes')}){other}.")
    return out


def _phrases(events) -> Tuple[str, ...]:
    out = []
    for e in events[:4]:
        try:
            text = e.phrase.format(subject=e.subject, value=e.value if e.value is not None else 0)
        except (KeyError, IndexError, ValueError):
            text = e.code
        more = ""
        if e.count > 1:   # one phrase for several readings: its value is the largest of them
            more = f" (largest of {e.count})" if e.value is not None else f" ({e.count} times)"
        out.append(text + more + gap_words(getattr(e, "gap_s", 0.0) or 0.0))
    return tuple(out)


def view_of(f, hops_identity: Mapping[str, Any], modules: Sequence[ModuleInput], provider: str, tz: tzinfo,
            label: str, version: str, *, apps: Sequence[AppFact] = (), environment: Sequence[str] = (),
            provider_source: str = "", zone: str = "", zone_kind: str = "") -> PackView:
    """reports.ai_findings.PackFacts -> PackView. `hops_identity`: target id -> object with name, address, rdns.
    `apps` and `environment` (sentences, tokens allowed) are what the caller knows beyond the stored facts."""
    by_id = {i.id: i for i in f.incidents}
    categories = getattr(f, "categories", {}) or {}
    servers = {getattr(v, "address", "") for k, v in hops_identity.items() if categories.get(k) == "dns"} - {""}
    hops = []
    for h in f.hops:
        ident = hops_identity.get(h.target_id)
        b, w = h.baseline, h.window
        address, rdns = getattr(ident, "address", "") or "", getattr(ident, "rdns", "") or ""
        hops.append(HopFact(h.target_id, h.category, getattr(ident, "name", "") or "", address,
                            rdns=rdns, ignores_ping=h.ignores_ping, normal_ms=b.normal_ms,
                            p90_ms=b.p90_ms, jitter_ms=b.jitter_ms, loss7_pct=b.loss_pct, window_ms=w.median_ms,
                            window_loss_pct=w.loss_pct, slow_minutes=w.slow_minutes, lossy_minutes=w.lossy_minutes,
                            note="thin history" if b.thin else "",
                            dns_server=h.category != "dns" and (address in servers or _dns_name(rdns)
                                                                or _dns_label(getattr(ident, "name", "") or ""))))
    listed = set(f.listed)
    incidents = tuple(IncidentFact(i.id, i.start, i.duration, i.lost_for, i.shape.first_hop, i.shape.kind,
                                   i.kind or "unclassified", i.cause, i.confidence, i.expected, i.score,
                                   _phrases(i.events), i.title, confidence_reason=i.confidence_reason,
                                   latency_start=i.latency.first_hop if getattr(i, "latency", None) else None,
                                   latency_shape=i.latency.kind if getattr(i, "latency", None) else "",
                                   latency_normal=tuple(getattr(i, "latency_normal", ()) or ()))
                      for i in f.incidents if i.id in listed)
    normals = {h.target_id: h.baseline.normal_ms for h in f.hops}
    closeups = []
    for c in f.closeups:
        inc = by_id.get(c.incident_id)
        header = tuple(x for col in c.columns for x in (f"{{hop:{col}}} loss %", f"{{hop:{col}}} ms"))

        def cells(per_col):
            return tuple(x for col in c.columns for x in (
                (_num(per_col[col][0]), _num(per_col[col][1])) if col in per_col else ("-", "-")))
        closeups.append(CloseUpFact(
            c.incident_id, tuple((h["target_id"], h["loss_pct"]) for h in (inc.hops or [])) if inc else (),
            _phrases(inc.events) if inc else (), header, tuple((ts_, cells(pc)) for ts_, pc in c.minutes),
            tuple((ts_, cells(pc)) for ts_, pc in c.rows_10s) if c.rows_10s else None,
            peaks=tuple((col, (inc.peaks or {})[col], normals.get(col)) for col in c.columns
                        if inc is not None and (getattr(inc, "peaks", None) or {}).get(col) is not None)))
    time_table = None
    if f.profile:
        cols = [p_["target_id"] for p_ in f.profile]
        rows = []
        for k, blk in enumerate(f.profile[0]["blocks"]):
            a, b_ = blk["hours"]
            rows.append((f"{a:02d}:00-{b_ % 24:02d}:00",) + tuple(
                f"{_num(p_['blocks'][k]['median_ms'])} ms, {_num(p_['blocks'][k]['loss_pct'], 2)} %" for p_ in f.profile))
        time_table = (("Hours",) + tuple(f"{{hop:{c}}}" for c in cols), tuple(rows))
    kinds = {"path": "The path changed", "settings": "Settings changed", "route": "The route changed"}
    return PackView(
        scope=PackScope(f.scope, label, f.start, f.end, tz, zone, zone_kind), version=version, provider=provider,
        hops=tuple(hops),
        incidents=incidents, closeups=tuple(closeups), findings=tuple(_findings(f)), time_table=time_table,
        days=tuple(DayFact(d["day"], d["incidents"], d["lost_minutes"]) for d in f.per_day), apps=tuple(apps),
        changes=tuple(ChangeFact(c["at"], kinds.get(c["kind"], c["kind"]), c["kind"]) for c in f.changes),
        environment=tuple(environment), modules=tuple(modules), incident_total=len(f.incidents),
        provider_source=provider_source, last_incident_end=max((i.end for i in f.incidents), default=None))


def _dns_name(rdns: str) -> bool:
    """A reverse name that says it is a name server ("cdns01.example-isp.net", "resolver1.example.net")."""
    first = (rdns or "").lower().split(".")[0]
    return bool(re.fullmatch(r"[a-z]*dns\d*[a-z]?|resolver\d*|ns\d*", first)) if first else False


def _dns_label(name: str) -> bool:
    """A hop's own name that says it is a name server ("Provider DNS", "Public resolver 1")."""
    return bool(re.search(r"\b(?:dns|resolver)s?\b", name, re.I))


def _label(scope: str, hours: int, incident_id: Optional[int]) -> str:
    if scope == "incident":
        return f"Incident {incident_id}, with an hour either side"
    if hours == 1:
        return "Past hour"
    return f"Past {hours // 24} days" if hours % 24 == 0 and hours > 24 else f"Past {hours} hours"


def _mesh_labels(identifiers: Mapping[str, str]) -> Dict[str, str]:
    """Full label of each mesh node key (node ids are NEVER identifiers, so a mesh key's value is its label)."""
    out: Dict[str, str] = {}
    for value, key in identifiers.items():
        if key.startswith("mesh_node:") and key not in out:
            out[key] = value
    return out


def _live_device(live, capabilities) -> Optional[Any]:
    """The monitor of the first live device offering one of the capabilities; None without a registry or a monitor."""
    if live is None:
        return None
    for cap in capabilities:
        found = live.providing(cap) if hasattr(live, "providing") else ()
        if found:
            return getattr(found[0], "monitor", None)
    return None


# The names a UTC clock goes by: a Settings zone that is one of them is the container's default, not the household's.
UTC_NAMES = frozenset({"UTC", "UCT", "GMT", "GMT0", "GMT+0", "GMT-0", "Greenwich", "Universal", "Zulu", "+00:00",
                       "-00:00", "Etc/UTC", "Etc/UCT", "Etc/GMT", "Etc/GMT0", "Etc/GMT+0", "Etc/GMT-0", "Etc/Greenwich",
                       "Etc/Universal", "Etc/Zulu"})


def pack_zone(config, tz_name: Optional[str] = None, tz_offset_minutes: int = 0) -> Tuple[tzinfo, str, str]:
    """(tzinfo, kind, name) the pack's times are in. In this order: the zone set in Settings (general.timezone), when
    it is not UTC (the default in a container that runs in UTC); the IANA zone the caller names (the viewer's
    browser); a fixed offset the caller gives; else UTC. kind: named (follows daylight saving) | offset | utc."""
    from pengumon.settings.general import known_zone
    from pengumon.timeofday import zone
    setting = (getattr(getattr(config, "general", None), "timezone", None) or "").strip()
    if setting and setting not in UTC_NAMES:
        if setting[:1] in "+-":
            return zone(setting), "offset", f"UTC{setting}"
        if known_zone(setting):
            return zone(setting), "named", setting
    name = (tz_name or "").strip()
    if name and name not in UTC_NAMES and known_zone(name):
        return zone(name), "named", name
    if tz_offset_minutes:
        return timezone(timedelta(minutes=tz_offset_minutes)), "offset", ""
    return timezone.utc, "utc", "UTC"


async def build_ai_pack(repository, config, *, scope: str, hours: int, incident_id: Optional[int], level: str,
                        detail: str, devices, live, ignoring=frozenset(), tz_offset_minutes: int = 0,
                        prober=None, tz_name: Optional[str] = None) -> Dict[str, Any]:
    """The pack for the API: collects the modules' sections and the core's facts, renders at `level`. `detail`:
    "short" or "detailed". `live`: the app's device registry, or None (no monitors: the live-only parts say so).
    `prober`: the app's prober, for how pings are sent now (None: the environment says only what is stored).
    `tz_name` (the viewer's IANA zone) and `tz_offset_minutes`: the fallbacks of pack_zone."""
    from pengumon.capabilities import Capability
    from pengumon.capabilities.contrib import ContribContext, activate, capability_contributions, driver_note, technology_contributions
    from pengumon.drivers import DRIVERS
    from pengumon.reports.ai_findings import collect_pack_facts
    from pengumon import __version__

    if level not in LEVELS:
        raise ValueError(f"unknown detail level {level!r}")
    render_detail = "detailed" if detail == "detailed" else "compact"
    now = time.time()
    if scope == "incident":
        inc = await repository.get_incident_by_id(incident_id)
        if inc is None:
            raise ValueError(f"no incident {incident_id}")
        start, end = inc.start_time - 3600, min(now, (inc.end_time or inc.start_time) + 3600)
    else:
        start, end = now - hours * 3600, now
    tz, zone_kind, zone_name = pack_zone(config, tz_name, tz_offset_minutes)

    act = activate(config)
    caps_of = {cls: caps for caps, cls in capability_contributions()}
    tech_of = {cls: tech for tech, cls in technology_contributions().items()}
    cache: Dict[Any, Any] = {}
    events, kinds, loaded = [], [], []
    for c in act.contributions:
        caps = caps_of.get(type(c), (Capability.ACCESS_LINE,))
        ctx = ContribContext(repository, config, start, end, tuple(devices), live=_live_device(live, caps),
                             tz_offset_minutes=tz_offset_minutes, cache=cache, tz=tz)
        data = await c.collect(ctx)
        events += c.emit(data)
        kinds += list(c.events)
        loaded.append((c, data))
    for _, notes in act.notes:
        kinds += list(notes.extra_events)
    facts = await collect_pack_facts(repository, config, start=start, end=end, events=events, event_kinds=kinds,
                                     ignoring=ignoring, detail=render_detail, scope=scope, incident_id=incident_id,
                                     tz_offset_minutes=tz_offset_minutes, now=now, tz=tz)

    # A driver's notes close one section only: its line technology's, else the router's, else the first it feeds.
    def feeds(driver, c) -> bool:
        return (getattr(driver, "line_technology", None) == tech_of.get(type(c)) if c.layer == "technology"
                else any(cap in driver.offers for cap in caps_of.get(type(c), ())))

    note_home: Dict[int, Tuple[Any, Any]] = {}
    for cfg, notes in act.notes:
        fed = [k for k, (c, _) in enumerate(loaded) if feeds(DRIVERS[cfg.driver], c)]
        if fed:
            home = next((k for k in fed if loaded[k][0].layer == "technology"), None)
            if home is None:
                home = next((k for k in fed if Capability.ROUTER_SYSTEM in caps_of.get(type(loaded[k][0]), ())), fed[0])
            note_home.setdefault(home, (cfg, notes))
    mesh = any(key.startswith("mesh_node:") for c, data in loaded for key in c.redaction.identifiers(data).values())
    modules = []
    for k, (c, data) in enumerate(loaded):
        identifiers = c.redaction.identifiers(data)
        note = None
        if k in note_home:
            cfg, notes = note_home[k]
            note = (driver_note(cfg, notes, "full", mesh=mesh), driver_note(cfg, notes, "anonymized", mesh=mesh))
            note = note if note[0] else None
        modules.append(ModuleInput(c.id, c.title, c.layer, c.budget_chars, c.prompt_context(config),
                                   c.section(data, level), note, _mesh_labels(identifiers), identifiers,
                                   c.redaction.field_values(data),
                                   core_findings=tuple(module_findings(facts, [e.code for e in c.events]))))

    from pengumon.reports.queries import path_trace_names
    names = await path_trace_names(repository)
    identity = {t.id: SimpleNamespace(name=t.name, address=_address(t), rdns=names.get(_address(t), ""))
                for t in list(config.targets) + list(config.dns_probes)}
    provider, source = _provider(config, identity)
    view = view_of(facts, identity, modules, provider, tz, _label(scope, hours, incident_id), __version__,
                   apps=_apps(config, facts), environment=_environment(config, facts, prober, tz),
                   provider_source=source, zone=zone_name, zone_kind=zone_kind)
    view = dataclasses.replace(view, known_hops=await _known_hops(repository, config))
    pack = render_pack(view, level, render_detail)
    return {"markdown": pack.markdown, "modules": pack.modules, "roles": pack.roles, "level": level, "scope": scope,
            "detail": detail, "chars": pack.chars, "tokens_estimate": pack.tokens, "trimmed": pack.trimmed,
            "redactions": {**pack.swept, **{f"scan:{k}": v for k, v in pack.hits.items()}}}


def _provider(config, identity: Mapping[str, Any]) -> Tuple[str, str]:
    """(the provider's name, where it comes from): the ISP name set in Settings (line.isp_name, which also names the
    dossier), else the domain most of the provider's hops' reverse names share (as discovery groups them), else none."""
    from pengumon.engine.discovery.addresses import registrable_domain
    named = getattr(getattr(config, "line", None), "isp_name", None)
    if named:
        return named, "named in Settings"
    domains: Dict[str, int] = {}
    for t in config.targets:
        if getattr(t, "category", None) == "isp":
            d = registrable_domain(getattr(identity.get(t.id), "rdns", "") or "")
            if d:
                domains[d] = domains.get(d, 0) + 1
    if domains:
        return max(sorted(domains), key=lambda d: domains[d]), "from its routers' names"
    return "", ""


def _apps(config, facts) -> Tuple[AppFact, ...]:
    """The host apps Pengumon checks, with their outages in the window (the stored incidents)."""
    services = getattr(getattr(config, "service_checks", None), "services", None) or ()
    outages = getattr(facts, "service_outages", None) or {}
    return tuple(AppFact(s.name, True, outages=outages.get(s.id, (0, 0.0))[0], down_s=outages.get(s.id, (0, 0.0))[1])
                 for s in services if getattr(s, "enabled", True))


def _environment(config, facts, prober, tz: tzinfo) -> List[str]:
    """How pings are sent and whether IPv6 is probed (from the prober, when the app runs), and the stretches with no
    probe at all."""
    out = []
    if prober is not None:
        method = prober.probe_method()
        if method.get("backend"):
            out.append(f"Pings are sent with {method['backend']}, timed to {method['resolution_ms']:g} ms.")
        if prober.probing_problem() is not None:
            out.append("Pengumon cannot send pings from this computer now: it is measuring nothing until that is "
                       "fixed.")
        v6 = [t for t in config.targets if getattr(t, "family", 4) == 6 and getattr(t, "enabled", True)]
        if v6:
            reason = getattr(getattr(prober, "ipv6", None), "reason", None)
            out.append("IPv6: this computer has no IPv6 route now, so the IPv6 hops are not probed." if reason
                       else "IPv6: probed.")
        else:
            out.append("IPv6: no IPv6 hop is set up; only IPv4 is measured.")
    gaps = list(getattr(facts, "no_data", None) or ())
    if gaps:
        shown = ", ".join(f"{{when:{a:.0f}}} to {{when:{b:.0f}}}" for a, b in gaps[:5])
        more = f" and {len(gaps) - 5} more" if len(gaps) > 5 else ""
        out.append("Not measured (no probe at all: Pengumon was stopped or could not send pings, or the computer was "
                   f"off or asleep): {shown}{more}.")
    return out


def _address(target) -> str:
    """A target's address; a DNS probe's is its name server (or, with the system resolver, the name it looks up)."""
    return getattr(target, "address", None) or getattr(target, "nameserver", None) or getattr(target, "domain", "") or ""


async def _known_hops(repository, config) -> Tuple[HopFact, ...]:
    """Every hop the pack may meet in an old title without showing it: the configured targets (switched off ones
    too), the removed ones and those of the kept path versions (a hop's earlier name)."""
    found = [(t.id, getattr(t, "category", "dns"), t.name, _address(t)) for t in list(config.targets) + list(config.dns_probes)]
    try:
        found += [(t.id, t.category, t.name, t.address) for t in await repository.removed_path_hops()]
        for v in await repository.path_versions():
            found += [(h.get("id", ""), h.get("category", "custom"), h.get("name", ""), h.get("address", ""))
                      for h in (await repository.path_version(v["revision"])) or ()]
    except AttributeError:       # a repository without the stored path
        pass
    return tuple(HopFact(i or "", c or "custom", n or "", a or "") for i, c, n, a in dict.fromkeys(found))
