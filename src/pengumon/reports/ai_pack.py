"""The AI troubleshooting pack as Markdown: `render_pack` renders the facts at one of three detail levels, through one
Redactor (reports.redact). reports.contributions.build_ai_pack collects them (the core's facts from reports.ai_findings,
the modules' sections from the contributions) and calls it.

Pure: no database, no clock, no network. The facts are the dataclasses below. Anything in them that is a sentence of
Pengumon's own refers to hops as `{hop:<target id>}` and to module devices as `{role:<key>}`; the renderer turns those
into role strings, so a name never enters a sentence. Descriptions, probe errors, URLs and raw node ids are fields of
the facts for completeness and are never rendered."""

import copy
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from pengumon.reports.redact import ANONYMIZED, FULL, LEVELS, VAGUE, WHAT_IT_IS, Redactor
from pengumon.reports.speed import (SpeedSummary, counts_text, direction_text, latency_text, method_text,
                                    under_text)
from pengumon.speedtest.grade import bufferbloat_grade
from pengumon.timeofday import clock_changes

HARD_CAP_CHARS = 32_000
HEADLINE_CHARS = 200
CAPS = {"compact": {"incidents": 20, "closeups": 3, "speed": 12}, "detailed": {"incidents": 40, "closeups": 6, "speed": 30}}
MIN_INCIDENTS, MIN_CLOSEUPS, MIN_SPEED = 10, 1, 5
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
  - Pengumon cannot read the gateway's event log (a restart: the gateway; timeouts or lost sync: the line). Suggest it when loss starting at the Gateway, or at ISP hop 1 with no codeword burst or signal fault then, repeats or matches the user's problem. A Gateway that answered every probe during the loss did not restart: do not ask about one.
  - Do not ask the user to repeat what Pengumon already measures over time (pinging the router, the gateway or internet servers); suggest only tests it cannot do.
  - "No data" means Pengumon was not measuring, not that the network was down.
- Verdicts: *gateway* = loss starts at the Gateway while the Router answers: the gateway or the link to it (a modem restart then: the unit), not the line; *line* = loss starts at ISP hop 1 while the Gateway answers: the line or the provider's first router (codeword bursts or a signal out of range at the same time point to the line itself); *isp* = the provider's network beyond its first router.
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
SPEED_PROVIDERS = {"cloudflare": "Cloudflare", "mlab": "M-Lab", "librespeed": "own LibreSpeed server"}
SPEED_NOTE = ("Pengumon runs a speed test on a schedule (it moves a capped amount of data each way, to one speed test "
              "server) and times the delay of its pings while the line is full: *Idle ms* is the median before the test, "
              "*Loaded* during the download and the upload, *Bufferbloat* the grade of the delay the load added (A+ best, "
              "F worst). The speeds are the line's rate the router measured during the test (*By* router: the test and "
              "the household's traffic, what the line carried while full) when the router reports it, else the test's "
              "own (*By* test: one server, which may not fill the line). A slow result is not a fault by itself, "
              "repeated slow ones are. Delay during a test is the test's own and is not an incident.")
# titles that claim no more than the evidence: a gateway that stopped answering may be the gateway or the cable to it
TABLE_ROWS_KEPT = 3                # a module table's first rows outlast its extra bullets (Renderer.module)
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
    probe: str = ""                 # how it is probed when not by ping: "TCP port 443", "HTTPS request" (never a path)
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
class OutageFact:
    """Drops joined into one outage (incidents.outages); only the fields the pack states."""
    id: int
    start: float
    duration_s: float
    down_s: float
    drops: int
    critical_drops: int
    places: Tuple[str, ...]         # cause categories, path order
    likely_maintenance: bool
    incident_ids: Tuple[int, ...]
    ongoing: bool = False


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
class SpeedTestFact:
    """One speed test that ran (a skipped one is only counted in the summary)."""
    ts: float
    status: str                     # ok | partial | failed
    provider: str                   # cloudflare | mlab | librespeed
    server: str = ""                # a colo or machine name; never rendered for librespeed (the host of the user's server)
    down_mbps: Optional[float] = None
    up_mbps: Optional[float] = None
    idle_ms: Optional[float] = None
    loaded_down_ms: Optional[float] = None
    loaded_up_ms: Optional[float] = None
    reason: str = ""                # why it failed or was partial (the server's host, for librespeed, is left out)
    measured_by: str = ""           # router | test (the speeds are the headline's: speedtest/quality.headline)


@dataclass(frozen=True)
class SpeedFact:
    summary: SpeedSummary           # over every test of the window
    tests: Tuple[SpeedTestFact, ...] = ()   # the tests that ran, time order


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
    speed: Optional[SpeedFact] = None              # the window's speed tests (None: there were none)
    outages: Tuple[OutageFact, ...] = ()           # time order
    outage_gap_minutes: int = 0                    # 0: grouping is off


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
_TIME_TOKEN = re.compile(r"\{(when|minute|counted):(-?[0-9.]+)(?::(-?[0-9.]+))?\}")


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


def _count(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _span_words(seconds: float) -> str:
    """"24 hours", "7 days", "1 hour", "2 h 30 min"."""
    s = int(round(seconds))
    if s % 86400 == 0 and s > 86400:
        return f"{s // 86400} days"
    if s % 3600 == 0:
        return _count(s // 3600, "hour", "hours")
    return _dur(s)


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
        try:
            ts = float(m.group(2))
            if m.group(1) == "when":
                return self.rx.when(ts, "dayclock" if self.span > 86400 else "clock")
            if m.group(1) == "minute":       # to the minute: a client list read once a minute has no seconds to give
                return self.rx.when(ts, "dayminute" if self.span > 86400 else "clock")
            return self.counted(ts, float(m.group(3) or ts))
        except (ValueError, OverflowError, OSError):  # not a time ("1.2.3", year 10000): the text, without its braces
            return m.group(0)[1:-1]

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
            probed = ""
            if h.probe:
                probed = f"probed by {h.probe}" + (" (time to the server's answer, not a round trip)"
                                                   if h.probe.endswith("request") else "")
            note = "; ".join(x for x in (probed, self.t(h.note) if h.note else "",
                                         ("ignores its probe" if h.probe else "ignores ping") if h.ignores_ping else "",
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

    def outages(self) -> str:
        """The outages that join more than one incident, with the incidents' numbers in the table above."""
        f = self.f
        if not f.outages:
            return ""
        many = [o for o in f.outages if o.drops > 1]
        if not many:
            return ("## Outages\n" + (f"Each incident above is an outage of its own: no two drops at neighboring places of the path "
                                      f"followed each other within {f.outage_gap_minutes} min.\n\n" if f.outage_gap_minutes else
                                      "Grouping is off (Settings): every drop is its own outage.\n\n"))
        head = f"## Outages ({_count(len(f.outages), 'outage', 'outages')}, {len(many)} of them joining several incidents)"
        what = (f"Drops at neighboring places of the path that follow each other within {f.outage_gap_minutes} min are "
                "one outage: one provider event often shows as several incidents above. Count outages, not incidents, "
                "for how often the line went down. Down: the critical drops' time, overlaps once. Likely maintenance: "
                "a guess from the hour alone (the line or provider's network, in the maintenance hours set in Pengumon); "
                "it changes no figure.")
        rows = []
        for o in many[-10:]:
            nums = [self.number[i] for i in o.incident_ids if i in self.number]
            hidden = len(o.incident_ids) - len(nums)
            listed = ", ".join(f"#{n}" for n in nums) + (f" and {hidden} not in the table" if hidden and nums else
                                                         f"{hidden} not in the table" if hidden else "")
            rows.append([self.stamp(o.start), _dur(o.duration_s) + (" (ongoing)" if o.ongoing else ""), _dur(o.down_s),
                         f"{o.drops} ({o.critical_drops} critical)", ", ".join(o.places),
                         "yes" if o.likely_maintenance else "no", listed])
        table = _table(["Start", "Lasted", "Down", "Drops", "Places", "Likely maintenance", "Incidents"], rows)
        return f"{head}\n{what}\n{table}\n\n"

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
        out = ["## This week by time of day and by day"]
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

    def _module_parts(self, m: ModuleInput, tables: bool):
        """What a contribution is made of, longest versions first: its bullets (each with its shorter versions, the
        core's findings among them), its table, the table's rows, the table's lead-in bullet and the driver note."""
        sec = m.section
        own = list(getattr(sec, "findings", None) or [])
        shorter = dict(getattr(sec, "shorter", None) or {})
        versions = [[text] + list(shorter.get(k, ())) for k, text in enumerate(own)]   # each bullet, longest first
        table = getattr(sec, "table", None) if tables else None
        rows = list(table[1]) if table else []
        # a table's lead-in is its section's last finding, ending in ":" ("1 channel outside typical levels ...:")
        lead = versions.pop()[0] if table and own and own[-1].rstrip().endswith(":") else None
        # the section's first finding, then what coincided with the incidents (the core's bullets: the strongest
        # evidence), then the rest: the trimmer cuts from the end, and the detail it cuts first is in the headline
        items = versions[:1] + [[c] for c in m.core_findings] + versions[1:]
        note = ""
        if m.driver_note:
            full, anon = m.driver_note
            note = full if self.rx.level != ANONYMIZED else (anon or "")
        return items, table, rows, lead, note

    @staticmethod
    def _module_cut_note(pick: List[int], nr: int, n_rows: int, nb: int, n_items: int, over: bool) -> str:
        """What was cut from a contribution, in words ("" when nothing)."""
        cut = []
        if any(pick):
            cut.append(f"bullets shortened {sum(1 for p in pick if p)}")
        if nr < n_rows:
            cut.append(f"table rows {nr} of {n_rows}")
        if nb < n_items:
            cut.append(f"bullets {nb} of {n_items}")
        if over:
            cut.append("over budget by the prompt context")
        return "; ".join(cut)

    def module(self, m: ModuleInput, tables: bool = True) -> Tuple[str, str]:
        """(text, trimmed note) of one contribution, cut to its budget. First a bullet with shorter versions (the
        section's `shorter`: a timed list down to its largest few) takes them in turn, so nothing is left out while a
        list can still shrink. A table is often the evidence (the channels outside typical levels), so its first
        TABLE_ROWS_KEPT rows outlast every bullet past the second: rows past those go next, then those bullets, then
        the last rows. The table's lead-in bullet stays with it and says when rows were left out, so it never
        introduces nothing. The prompt context is never cut."""
        items, table, rows, lead, note = self._module_parts(m, tables)
        pick = [0] * len(items)       # the version of each bullet in use

        def lead_in(nr: int) -> Optional[str]:
            if lead is None:
                return None
            head = lead.rstrip().rstrip(":")
            if nr == len(rows):
                return lead
            if nr == 0:
                return f"{head} (the list is left out for length)."
            return f"{head} (the first {nr} of {len(rows)}):"

        def build(nb: int, nr: int, with_lead: bool = True) -> str:
            parts = [f"### {m.title}", self.t(m.prompt_context)]
            bullets = [v[p] for v, p in zip(items, pick)]
            shown = bullets[:nb] + ([lead_in(nr)] if lead is not None and with_lead else [])
            if shown:
                parts.append("\n".join(f"- {self.t(b)}" for b in shown))
            if table and nr:
                parts.append(_table([self.t(h) for h in table[0]], [[self.t(c, table=True) for c in r] for r in rows[:nr]]))
            if note:
                parts.append(self.t(note))
            return "\n\n".join(parts) + "\n\n"

        nb, nr = len(items), len(rows)
        text = build(nb, nr)
        while len(text) > m.budget_chars:      # the last such bullet first, as the trimmer cuts from the end
            k = next((k for k in reversed(range(len(items))) if pick[k] < len(items[k]) - 1), None)
            if k is None:
                break
            pick[k] += 1
            text = build(nb, nr)
        keep = min(TABLE_ROWS_KEPT, len(rows))
        while len(text) > m.budget_chars and nr > keep:
            nr -= 1
            text = build(nb, nr)
        while len(text) > m.budget_chars and nb > 2:
            nb -= 1
            text = build(nb, nr)
        while len(text) > m.budget_chars and nr > 0:
            nr -= 1
            text = build(nb, nr)
        if len(text) > m.budget_chars and lead is not None:   # no rows left: its lead-in goes too, as before
            text = build(nb, nr, with_lead=False)
        return text, self._module_cut_note(pick, nr, len(rows), nb, len(items), len(text) > m.budget_chars)

    def speed(self, cap: int) -> Tuple[str, int]:
        """The speed tests: the summary of all of the window's, then the newest `cap` that ran as a table."""
        sp = self.f.speed
        if sp is None:
            return "", 0
        sm = sp.summary
        bullets = [counts_text(sm).capitalize()]
        bullets += [t for t in (direction_text("Download", sm.down_median, sm.down_low, sm.plan_down),
                                direction_text("Upload", sm.up_median, sm.up_low, sm.plan_up), method_text(sm),
                                under_text(sm), latency_text(sm)) if t]
        if not (sm.plan_down or sm.plan_up):
            bullets.append("No plan speed is set in Pengumon, so the speeds are not compared with one")
        kept = list(sp.tests[-cap:]) if cap < len(sp.tests) else list(sp.tests)
        rows = []
        for t in kept:
            own = t.provider != "librespeed"      # the user's own server is named by its host: never shown
            # (in any case, and before the reason is cut: the label is lowercased, a 404's hint repeats the address)
            reason = t.reason if own or not t.server else re.sub(re.escape(t.server), "the server", t.reason,
                                                                  flags=re.IGNORECASE)
            grade = bufferbloat_grade(t.idle_ms, t.loaded_down_ms, t.loaded_up_ms)
            # the reason is swept, then cut: an address cut in two is no longer one the final scan could find
            rows.append([self.stamp(t.ts), t.status, SPEED_PROVIDERS.get(t.provider, t.provider),
                         self.t(t.server) if own and t.server else "-", _num(t.down_mbps), _num(t.up_mbps),
                         t.measured_by or "-", _num(t.idle_ms), _num(t.loaded_down_ms), _num(t.loaded_up_ms),
                         grade or "-", self.t(reason)[:80] if reason else "-"])
        head = ["Start", "Result", "Provider", "Server", "Down Mbps", "Up Mbps", "By", "Idle ms", "Loaded down ms",
                "Loaded up ms", "Bufferbloat", "Note"]
        what = ("" if len(kept) == len(sp.tests) else f" (the {len(kept)} newest of {len(sp.tests)} that ran)")
        out = ["## Speed tests", SPEED_NOTE, "\n".join(f"- {b}" for b in bullets)]
        if rows:
            out.append(f"Tests{what}:\n" + _table(head, rows))
        return "\n\n".join(out) + "\n\n", len(kept)

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
    inc_cap, up_cap, speed_cap, tables = caps["incidents"], caps["closeups"], caps["speed"], True
    trimmed: List[str] = []
    built = _redactor(facts, level)   # its sweep pattern is built once; each pass counts on its own

    def render(tokens: int):
        rx = copy.copy(built)
        rx.hits, rx.swept, rx.used_roles = {}, {}, {}
        r = _Render(facts, rx, detail)
        inc_text, shown = r.incidents(inc_cap)
        dev_text, info = r.devices(tables)
        body = (r.header(tokens) + FIXED_TEXT + "\n\n" + r.hops() + r.findings() + inc_text + r.outages() + r.closeups(up_cap) + r.week()
                + r.speed(speed_cap)[0] + dev_text + r.tail() + "\n" + SYMPTOM_TEXT)
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
        elif speed_cap > MIN_SPEED and facts.speed is not None and len(facts.speed.tests) > MIN_SPEED:
            speed_cap = MIN_SPEED
            trimmed.append(f"speed test rows cut to {MIN_SPEED}")
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


def __getattr__(name: str):
    """view_of, module_findings and speed_fact live in reports.ai_pack_view, which imports this module; they are
    looked up there on first use so that `from pengumon.reports.ai_pack import view_of` keeps working."""
    if name in ("view_of", "module_findings", "speed_fact"):
        from pengumon.reports import ai_pack_view
        return getattr(ai_pack_view, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
