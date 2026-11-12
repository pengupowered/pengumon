"""What a configured device brings to the AI troubleshooting pack: the module-contribution contract (the interface, the
types it uses and small helpers the contributions share). It imports nothing of pengumon, so the implementations
(lines/docsis_contrib.py, devices/contrib.py) and the drivers (`pack_notes`) can import it at module level. The
registry of the built-in contributions and their activation from the configured devices are reports/contributions.py.

A contribution is a line technology (DOCSIS) or a capability (line load, router, Wi-Fi and mesh) that has something to
say to the pack: the prompt text that explains its readings to a model, a small pack section, coincidence events the
core matches against incidents without knowing what a modem is, redaction hints and the OpenTelemetry instruments it
owns. A driver adds `pack_notes`: text about itself, nothing more. Everything is built in (like `DRIVERS`: no plugin
loading, so the dependency gate sees everything that runs), and a contribution is active only when a configured device
provides it. Activation reads the configuration alone, so it works offline and builds no driver.

Texts that name a person's things never contain the names: a contribution renders with role keys (`ROLE_LINE`,
`ROLE_ROUTER`, `mesh_role(n)`) where a device or a mesh node would be named, and the pack's Redactor turns each key into
the string of the level chosen. A role key stands in the text as it is written here ("line_device", "mesh_node:2").
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import (
    TYPE_CHECKING, Any, Callable, ClassVar, Dict, FrozenSet, List, Literal, Mapping, Optional, Protocol, Sequence, Tuple,
)
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from pengumon.devices.settings import DeviceConfig

ROLE_LINE = "line_device"      # the modem or gateway that reads the access line
ROLE_ROUTER = "router_device"  # the router
NEVER = "never"                # an identifier shown at no level, Full included (mesh node ids, anything per client)


def mesh_role(n: int) -> str:
    """The role key of the n-th (1-based) mesh node, numbered by node id order."""
    return f"mesh_node:{n}"


def field_key(name: str) -> str:
    """The identifier value of a string that is one of the hints' `fields` (the model): the Redactor applies that
    field's (full, vague, anonymized) actions to it instead of a role."""
    return f"field:{name}"


# What a redaction hint can say about a field at a level.
HINT_ACTIONS = frozenset({"keep", "role", "generic", "drop", "never"})
DETAILS = ("full", "vague", "anonymized")


@dataclass(frozen=True)
class EventKind:
    code: str                    # "line.error_burst"; codes are technology-neutral where they can be
    phrase: str                  # "codewords +{value:,.0f}", "{subject} at 0 clients"; {subject} is a role key
    supports: FrozenSet[str]     # CAUSE_CATEGORIES it corroborates (incidents/taxonomy.py:20), e.g. {"line"}
    reach_seconds: float         # how far from an incident it still counts (about one reading interval)
    label: str = ""              # what the events are, in a few plain words, plural ("codeword bursts"): the pack's
    #                              "What coincided" names them by it (empty: the code's words)
    by_time_of_day: bool = False  # in a window with no incident, the pack relates these events to the time of day
    #                              ("6 of 6 between 18:00 and 00:00") instead of saying none was during an incident

    def render(self, event: "ModuleEvent") -> str:
        return self.phrase.format(subject=event.subject, value=event.value)


@dataclass(frozen=True)
class ModuleEvent:
    code: str
    start: float
    end: float
    subject: str = ""            # a role key, never a name
    value: Optional[float] = None


@dataclass(frozen=True)
class ModuleSection:
    headline: Optional[str]      # one Findings bullet, 200 characters at most
    findings: List[str]          # bullets, role keys and numbers only
    table: Optional[Tuple[List[str], List[List[str]]]]   # header, rows; the core renders it and cuts rows to the budget
    day_columns: Dict[str, Dict[str, str]]                # column -> day (YYYY-MM-DD) -> cell, at most 2 columns
    shorter: Mapping[int, Tuple[str, ...]] = field(default_factory=dict)   # index into findings -> shorter versions
    #                                         of that bullet, longest first: over its budget, the core uses them in
    #                                         turn before it leaves anything out (a timed list down to its top 2)


@dataclass(frozen=True)
class RedactionHints:
    """`fields`: field -> (full, vague, anonymized), each of keep | role | generic | drop | never.
    `identify(facts)` -> {value: role key | NEVER | field_key(field)}: the strings of this module's devices that the
    core cannot know (the modem's host, the router's, mesh node labels and ids, device config names, models).
    `identifiers` gives the Redactor the role keys and NEVER; `field_values` the field ones with that field's actions,
    so what happens to a model at each level is decided by `fields["model"]` alone."""

    fields: Mapping[str, Tuple[str, str, str]]
    identify: Optional[Callable[[Any], Dict[str, str]]] = None

    def _all(self, facts) -> Dict[str, str]:
        return dict(self.identify(facts)) if self.identify is not None and facts is not None else {}

    def identifiers(self, facts) -> Dict[str, str]:
        return {v: k for v, k in self._all(facts).items() if not k.startswith("field:")}

    def field_values(self, facts) -> Dict[str, Tuple[str, str, str]]:
        """value -> its field's (full, vague, anonymized) actions."""
        return {v: tuple(self.fields[k[6:]]) for v, k in self._all(facts).items()
                if k.startswith("field:") and k[6:] in self.fields}


@dataclass(frozen=True)
class DriverNotes:
    """Driver level: text only, and only what the driver's own code establishes. `{poll}`, `{min_poll}`,
    `{poll_limit}` (" (as often as Pengumon allows for it)" at the driver's minimum, else " (never more often than
    every {min_poll})") and, for
    drivers that have them, `{clientlist_interval}` are filled in from the device's configuration, and `{aimesh}` is
    ", on an AiMesh network" when a mesh was seen (see `driver_note`)."""

    full: str
    anonymized: Optional[str]    # None: no note at Anonymized
    extra_events: Tuple[EventKind, ...] = ()   # a vendor event, e.g. a gateway's own reboot log (none yet)


@dataclass
class ContribContext:
    """What a contribution may read: no state, no HTTP, no driver."""

    repository: Any
    config: Any                  # the AppConfig
    start: float
    end: float
    devices: Tuple["DeviceConfig", ...]
    live: Optional[Any] = None   # the router's monitor (latest_snapshot / latest_clients / latest_mesh) while the app
    #                              runs; None offline
    tz_offset_minutes: int = 0   # the day columns' days start at local midnight in this offset, unless `tz` is given
    cache: Dict[Any, Any] = field(default_factory=dict)   # shared by the contributions of one pack build
    tz: Optional[Any] = None     # the local time zone (a tzinfo; a named zone follows its daylight-saving changes)


class Contribution(Protocol):
    id: ClassVar[str]                              # "docsis", "wan_load", "router_system", "wifi_mesh"
    title: ClassVar[str]                           # "Cable line (DOCSIS)"
    layer: ClassVar[Literal["technology", "capability"]]
    budget_chars: ClassVar[int]                    # prompt context + section + driver note, rendered
    events: ClassVar[Tuple[EventKind, ...]]
    redaction: ClassVar[RedactionHints]
    instruments: ClassVar[Tuple[str, ...]]         # the OTel names it owns (section 7.2 of the design)

    def prompt_context(self, config) -> str: ...   # the levels in force are filled in from config
    async def collect(self, ctx: ContribContext) -> Any: ...   # facts, from the stored history of the window
    def emit(self, facts) -> List[ModuleEvent]: ...
    def section(self, facts, detail: str) -> ModuleSection: ...


# --- small helpers the contributions share --------------------------------------------------------------------------

def host_of(cfg: "DeviceConfig") -> str:
    return urlsplit(cfg.url).hostname or ""


def effective_poll_seconds(cfg: "DeviceConfig", driver_cls) -> float:
    """How often the device is really read: the configured interval, raised to the driver's minimum
    (devices/registry.py `build_device` does the same)."""
    return max(float(cfg.poll_interval_seconds or driver_cls.default_poll_seconds),
               float(getattr(driver_cls, "min_poll_seconds", 0.0)))


def plural(n: int, word: str) -> str:
    """"1 reading", "3 readings"."""
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def duration_words(seconds: float) -> str:
    """"10 s", "30 minutes" (whole numbers where they are whole)."""
    if seconds < 120:
        return f"{seconds:g} s"
    minutes = seconds / 60.0
    return f"{minutes:g} minutes"


def median(values: Sequence[float]) -> Optional[float]:
    ordered = sorted(values)
    n = len(ordered)
    if not n:
        return None
    return float(ordered[n // 2]) if n % 2 else (ordered[n // 2 - 1] + ordered[n // 2]) / 2.0


def day_key(ts: float, tz_offset_minutes: int = 0, tz=None) -> str:
    """The local day (YYYY-MM-DD) of a time: in the zone `tz` (a tzinfo) when given, else in a fixed offset from
    UTC."""
    if tz is not None:
        return datetime.fromtimestamp(ts, tz).strftime("%Y-%m-%d")
    return (datetime.fromtimestamp(ts, timezone.utc) + timedelta(minutes=tz_offset_minutes)).strftime("%Y-%m-%d")


def render_section(section: ModuleSection) -> str:
    """The section as the core writes it into the pack (Markdown), for sizing; day columns are not part of it."""
    def one_line(text) -> str:  # a value cannot start a new bullet or row
        return " ".join(str(text).split())

    def cell(text) -> str:
        return one_line(text).replace("|", "\\|")

    lines = [f"- {one_line(section.headline)}"] if section.headline else []
    lines += [f"- {one_line(f)}" for f in section.findings]
    if section.table is not None:
        header, rows = section.table
        lines.append("| " + " | ".join(cell(h) for h in header) + " |")
        lines.append("|" + "---|" * len(header))
        lines += ["| " + " | ".join(cell(c) for c in row) + " |" for row in rows]
    return "\n".join(lines)
