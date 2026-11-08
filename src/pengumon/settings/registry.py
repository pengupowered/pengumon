"""The registry: the one list of what the Settings page shows, in its order, with the words it uses.

A `Setting` names a dotted key of the settings document, which is its config.yaml path ("classifier.down_loss_pct").
Type, range, choices and default are *not* repeated here: they are read from the pydantic field the key names
(`Setting.field()`, `.default()`, `.constraints()`), so a range exists in one place, the section model.

Also here: the sections (`SECTIONS`), the five lists (`LISTS`), the secret slots (`SECRET_SLOTS`) and the fields of
the stored sections that are deliberately not on the page (`NOT_ON_PAGE`). tests/test_settings_registry.py fails
when a section model gains a field that is in none of them.
"""

import dataclasses
import types
import typing
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Literal, Optional, Tuple, Type

from pydantic import BaseModel
from pydantic.fields import FieldInfo

from pengumon.config import AppConfig
from pengumon.devices.settings import DeviceConfig
from pengumon.engine.netinfo import default_dns_probes
from pengumon.engine.prober_settings import DnsProbeConfig
from pengumon.notify.settings import WebhookItem
from pengumon.services.settings import ServiceCheckConfig
from pengumon.settings import env as settings_env
from pengumon.web import themes as theme_rules
from pengumon.web.settings import ThemeItem

@dataclass(frozen=True)
class Section:
    id: str
    title: str
    order: int


SECTIONS: Tuple[Section, ...] = tuple(Section(id, title, n) for n, (id, title) in enumerate((
    ("outage", "Outage rules"),
    ("alerts", "Alerts"),
    ("line", "Your line"),
    ("speedtest", "Speed test"),
    ("devices", "Devices and services"),
    ("probing", "Probing"),
    ("retention", "Retention"),
    ("access", "Sign-in"),
    ("privacy", "Privacy"),
    ("discovery", "Discovery"),
    ("display", "Display"),
    ("advanced", "Advanced"),
), start=1))
SECTION_IDS = tuple(s.id for s in SECTIONS)


def _resolve(key: str) -> Tuple[Type[BaseModel], str, FieldInfo]:
    """(section model, field name, field) of a dotted key, walking AppConfig's nested models. KeyError when the key
    names no field."""
    model: Type[BaseModel] = AppConfig
    parts = key.split(".")
    for part in parts[:-1]:
        info = model.model_fields.get(part)
        inner = unwrap_optional(info.annotation)[0] if info is not None else None
        if not (isinstance(inner, type) and issubclass(inner, BaseModel)):
            raise KeyError(key)
        model = inner
    info = model.model_fields.get(parts[-1])
    if info is None:
        raise KeyError(key)
    return model, parts[-1], info


def unwrap_optional(annotation) -> Tuple[Any, bool]:
    """(the annotation without Optional, whether None is allowed)."""
    origin = typing.get_origin(annotation)
    if origin is typing.Union or origin is types.UnionType:
        args = [a for a in typing.get_args(annotation) if a is not type(None)]
        return (args[0] if len(args) == 1 else annotation), len(args) < len(typing.get_args(annotation))
    return annotation, False


def field_constraints(info: FieldInfo) -> Dict[str, Any]:
    """What the page needs to know of a field: {type, nullable, min, max, exclusive_min, exclusive_max, max_length,
    step, choices}. type is number | integer | boolean | text | choice | time | timezone | layout | theme | hours | list |
    object |
    map (the last three only for list-item fields). Read from the annotation and its pydantic metadata."""
    annotation, nullable = unwrap_optional(info.annotation)
    extra = info.json_schema_extra if isinstance(info.json_schema_extra, dict) else {}
    origin = typing.get_origin(annotation)
    choices: Optional[List[Any]] = None
    if extra.get("ui_type"):
        kind = extra["ui_type"]
    elif origin is Literal:
        kind, choices = "choice", list(typing.get_args(annotation))
    elif annotation is bool:
        kind = "boolean"
    elif annotation is int:
        kind = "integer"
    elif annotation is float:
        kind = "number"
    elif annotation is str:
        kind = "text"
    elif origin in (list, List):
        kind = "list"
    elif origin in (dict, Dict):
        kind = "map"
    elif isinstance(annotation, type) and issubclass(annotation, BaseModel):
        kind = "object"
    else:
        kind = "text"
    out: Dict[str, Any] = {"type": kind, "nullable": nullable, "min": None, "max": None, "exclusive_min": False,
                           "exclusive_max": False, "max_length": None, "step": 1 if kind == "integer" else None,
                           "choices": choices}
    for meta in info.metadata:
        for attr, target, exclusive in (("ge", "min", False), ("gt", "min", True), ("le", "max", False),
                                        ("lt", "max", True)):
            value = getattr(meta, attr, None)
            if value is not None:
                out[target] = value
                out[f"exclusive_{target}"] = exclusive
        if getattr(meta, "max_length", None) is not None:
            out["max_length"] = meta.max_length
    return out


def field_default(info: FieldInfo) -> Any:
    """The field's default as JSON-ready data (a model default is dumped)."""
    value = info.get_default(call_default_factory=True)
    return value.model_dump() if isinstance(value, BaseModel) else value


@dataclass(frozen=True)
class Setting:
    key: str              # dotted path in the settings document, = the config.yaml path ("classifier.down_loss_pct")
    section: str          # one of SECTION_IDS
    group: Optional[str]  # sub-heading in the section ("Loss", "Latency spike levels", "Hop colors", "Quiet hours")
    label: str            # "Loss counts as down from"
    effect: str           # one line: what changing it does
    unit: Optional[str] = None   # shown unit: "%", "ms", "s", "min", "days", "Mbps", "dB", "dBmV", "codewords/min"
    scale: float = 1.0    # shown = stored * scale (a fraction shown as %: 100; seconds shown as minutes: 1/60)
    rounds: bool = False  # stored in probe rounds, shown in seconds (x round_seconds)
    live: bool = True     # False: applies at the next start (the page says so)
    env: Optional[str] = None    # the variable that overrides it (pengumon.settings.env.SETTING_ENV)
    advanced: bool = False
    export: bool = True
    order: int = 0        # place within its section (set from the registry's order)
    related: Optional[str] = None  # a key shown beside it (the incident level next to a hop color), with a link to it
    also_in: Tuple[str, ...] = ()  # other sections that render the same value (asn_lookup: privacy too)
    choice_labels: Optional[Dict[str, str]] = None  # stored choice -> the words the page shows ("critical": "Outages only")
    empty: Optional[str] = None  # what an empty (None) value means, in words ("learned from the router's traffic")

    def resolve(self) -> Tuple[Type[BaseModel], str, FieldInfo]:
        """(section model, field name, field)."""
        return _resolve(self.key)

    def field(self) -> FieldInfo:
        return _resolve(self.key)[2]

    def default(self) -> Any:
        return field_default(self.field())

    def constraints(self) -> Dict[str, Any]:
        return field_constraints(self.field())


@dataclass(frozen=True)
class ListSpec:
    """One of the five lists. `path` is its place in AppConfig / config.yaml; `item_model` validates one item;
    `default` gives the items of a list nobody set."""
    name: str
    path: str
    section: str
    label: str
    item_model: Type[BaseModel]
    default: Callable[[], List[BaseModel]]


@dataclass(frozen=True)
class SecretSlot:
    """A named place in secrets.json, written through the Settings page and never read back. A `pattern` slot
    stands for one per item: "webhook.{id}.url" per webhook, "webhook_header.{name}" per header secret."""
    slot: str
    label: str
    kind: Literal["url", "token"]
    section: str
    env: Optional[str] = None
    pattern: bool = False


def _entries(section: str, rows: List[Setting]) -> List[Setting]:
    return [dataclasses.replace(s, section=section, order=n, advanced=section == "advanced")
            for n, s in enumerate(rows, start=1)]


def _s(key: str, label: str, effect: str, group: Optional[str] = None, **kw) -> Setting:
    return Setting(key=key, section="", group=group, label=label, effect=effect, **kw)


def _theme_names() -> Dict[str, str]:
    """The built-in themes' names by id (the custom ones are named on the page, from the list)."""
    return {t.id: t.name for t in theme_rules.builtin_themes()}


_ROUNDS = dict(unit="s", rounds=True)
_MINUTES = dict(unit="min", scale=1 / 60)
_PCT_OF_FRACTION = dict(unit="%", scale=100.0)
_PER_MILLION = dict(unit="per million", scale=1e6)  # a tiny ratio (0.000001) shown as "1 per million"

SETTINGS: Tuple[Setting, ...] = tuple(
    _entries("outage", [
        _s("classifier.window_probes", "Judge each hop by its last",
           "Loss, latency and jitter are measured over each hop's probes from this time, one a round. Latency and "
           "jitter take the middle value, so they count as high only when more than half the probes are: one stalled "
           "probe is no spike. Hop colors judge loss over it too.", **_ROUNDS),
        _s("classifier.degraded_loss_pct", "Loss counts as degraded from",
           "Share of a stage's judged probes lost from which that stage is degraded (a warning).", "Loss", unit="%"),
        _s("classifier.down_loss_pct", "Loss counts as down from",
           "Share lost from which the stage is down: a critical outage.", "Loss", unit="%"),
        _s("classifier.consecutive_failures_for_incident", "Loss outage starts after",
           "How long loss must last before an incident opens.", "When incidents start and end", **_ROUNDS),
        _s("classifier.consecutive_failures_for_latency_incident", "Latency incident starts after",
           "How long a latency or jitter spike alone must last before an incident opens.",
           "When incidents start and end", **_ROUNDS),
        _s("classifier.consecutive_successes_for_recovery", "Recovery confirmed after",
           "How long the line must stay healthy before an incident ends. Applies to loss and latency incidents.",
           "When incidents start and end", **_ROUNDS),
        _s("classifier.local_latency_spike_threshold_ms", "Router",
           "Median latency to the router hops above this is a local latency fault.", "Latency spike levels",
           unit="ms"),
        _s("classifier.gateway_latency_spike_threshold_ms", "Modem / gateway",
           "Median latency to the modem or gateway above this is a fault there.", "Latency spike levels",
           unit="ms"),
        _s("classifier.isp_latency_spike_threshold_ms", "ISP hops",
           "Median latency to the ISP's hops above this is an ISP fault.", "Latency spike levels", unit="ms",
           empty="same as internet"),
        _s("classifier.public_latency_spike_threshold_ms", "Internet",
           "Median latency to the internet targets above this is a fault upstream.", "Latency spike levels",
           unit="ms"),
        _s("classifier.local_jitter_spike_threshold_ms", "Router",
           "Jitter to the router hops above this is a local fault.", "Jitter spike levels", unit="ms"),
        _s("classifier.gateway_jitter_spike_threshold_ms", "Modem / gateway",
           "Jitter to the modem or gateway above this is a fault there.", "Jitter spike levels", unit="ms"),
        _s("classifier.public_jitter_spike_threshold_ms", "Internet",
           "Jitter to the internet targets above this is a fault upstream.", "Jitter spike levels", unit="ms"),
        _s("classifier.dns_timeout_threshold_ms", "Slow DNS above",
           "Median DNS lookup time above which DNS counts as slow.", "DNS", unit="ms"),
    ])
    + _entries("alerts", [
        _s("notifications.public_url", "Dashboard address in alerts",
           "The link alerts carry back to this dashboard.", "Channels", empty="no link"),
        _s("alerts.min_severity", "Send alerts for", "For network incidents only. With outages only, a warning that "
           "turns into an outage is sent when it does. Host app alerts ignore this.", "Rules",
           choice_labels={"warning": "Every incident", "critical": "Outages only"}),
        _s("alerts.delay_seconds", "Wait before alerting",
           "A network incident shorter than this sends nothing, not even a resolved message.", "Rules", **_MINUTES),
        _s("alerts.repeat_window_seconds", "Don't repeat the same cause within",
           "For network incidents only: one with the same cause inside this time is not announced again. 0: every one "
           "is. Host app alerts ignore this.", "Rules",
           **_MINUTES),
        _s("alerts.quiet_hours.enabled", "Quiet hours",
           "Alerts wait for the end of the window; what is still open then goes out once.", "Quiet hours"),
        _s("alerts.quiet_hours.start", "From", "Start of the window, in the time zone under Display.",
           "Quiet hours"),
        _s("alerts.quiet_hours.end", "Until", "End of the window; it may cross midnight.", "Quiet hours"),
        _s("alerts.quiet_hours.critical_still", "Still send critical incidents",
           "A critical network incident goes out at once, even in quiet hours.", "Quiet hours"),
        _s("service_checks.notify_after_seconds", "Alert when a host app has been down for",
           "A host app must fail this long before an alert goes out, so restarts stay quiet.", "Host apps",
           **_MINUTES),
    ])
    + _entries("line", [
        _s("line.isp_name", "ISP name", "Names the outage report for your ISP."),
        _s("line.down_mbps", "Download speed", "Your plan's speed, which the load is a share of.", unit="Mbps",
           empty="learned from traffic"),
        _s("line.up_mbps", "Upload speed", "Your plan's speed, which the load is a share of.", unit="Mbps",
           empty="learned from traffic"),
        _s("classifier.busy_load_pct", "Busy line from",
           "From this load, latency (and loss seen only at ISP hops) counts as expected, not as a fault.",
           unit="%"),
    ])
    + _entries("speedtest", [
        _s("speedtest.enabled", "Run speed tests",
           "Measures download and upload speed on a schedule. Each test uses the data below."),
        _s("speedtest.provider", "Test against",
           "Who the test data goes to. Cloudflare sees your address and the transfer. M-Lab publishes every result, with "
           "your public IP address, as open data. Your own LibreSpeed server is the private choice.",
           choice_labels={"cloudflare": "Cloudflare (speed.cloudflare.com)",
                          "mlab": "M-Lab NDT7 (publishes your IP address with each result)",
                          "librespeed": "Your LibreSpeed server"}),
        _s("speedtest.librespeed_url", "LibreSpeed server",
           "The folder that holds garbage.php and empty.php. For the official Docker image that is "
           "http://<host>/backend, not http://<host> (the bare host answers 404). Needed when the test runs against it.",
           empty="not set"),
        _s("speedtest.interval_hours", "Run every",
           "How often a scheduled test runs: any number of hours, days or weeks, up to 365 days (a week for weekly, 30 "
           "days for about monthly). The moment is not the same for every install. A restart never adds a test.",
           "Schedule", unit="h"),
        _s("speedtest.window_start", "Only from",
           "Scheduled tests run only between these times, in the time zone under Display. Leave both empty for any "
           "time.", "Schedule", empty="any time"),
        _s("speedtest.window_end", "Until", "End of the window; it may cross midnight.", "Schedule", empty="any time"),
        _s("speedtest.busy_load_pct", "Wait while the line is busy from",
           "A scheduled test waits (10 minutes, up to 3 times) while the line's load is at or above this, then is skipped. "
           "Unknown load does not block it.", "Schedule", unit="%"),
        _s("speedtest.download_mb", "Download at most",
           "Hard limit per test; the download stops at this much data or after about 8 seconds of it. To measure a "
           "line for the full 8 seconds this needs about its speed in Mbps (1000 MB for a gigabit line).", "Data use",
           unit="MB", choice_labels={"10": "10 MB", "25": "25 MB", "50": "50 MB", "100": "100 MB", "250": "250 MB",
                                     "500": "500 MB", "1000": "1000 MB"}),
        _s("speedtest.upload_mb", "Upload at most",
           "Hard limit per test; the upload stops at this much data or after about 8 seconds of it.", "Data use", unit="MB",
           choice_labels={"5": "5 MB", "10": "10 MB", "25": "25 MB", "50": "50 MB"}),
        _s("speedtest.monthly_cap_mb", "Monthly data limit",
           "Scheduled tests are skipped once this calendar month's tests have moved this much. 0 means no limit.",
           "Data use", unit="MB"),
        _s("speedtest.slow_alert_pct", "Alert when under",
           "Alert when a test measures under this share of your plan speed. Needs the plan speed under Your line. 0: off.",
           "Alert", unit="%"),
    ])
    + _entries("devices", [
        _s("service_checks.interval_seconds", "Check host apps every",
           "How often each host app's health address is fetched.", "Host app checks", unit="s"),
        _s("service_checks.failures_for_incident", "Failed checks before an incident",
           "Failed checks in a row before a host app counts as not responding.", "Host app checks", unit="checks"),
    ])
    + _entries("probing", [
        _s("classifier.path_trace_target", "Trace to when an incident starts",
           "The host traced to record the path at the start of each incident."),
    ])
    + _entries("retention", [
        _s("database.retention_days", "Keep raw samples for",
           "1-second samples, line, router and mesh readings. Line-capacity learning needs 7 days. Older ones are deleted "
           "at the next hourly cleanup.", unit="days"),
        _s("database.rollup_retention_days", "Keep per-minute history for",
           "Long-range charts, uptime strips, the report and the dossier. Older ones are deleted at the next hourly "
           "cleanup.", unit="days"),
    ])
    + _entries("access", [
        # the section's password controls are the page's own (dialogs/settings/access.js): they apply at once
        _s("access.allow_viewers", "Others may look, but not change anything",
           "While a password is set: people without it see the dashboard, read only. Off: they get the sign-in page. "
           "No effect without a password.", "Without the password", env="PENGUMON_VIEW_WITHOUT_TOKEN"),
        _s("mcp.enabled", "Allow AI assistants to read (MCP)",
           "Opens /mcp, a read-only endpoint an AI assistant can ask about the network (status, incidents, reports). "
           "It uses the same sign-in as the dashboard and changes nothing.", "AI assistants (MCP)"),
    ])
    + _entries("privacy", [
        _s("privacy.client_details", "Show connected clients' names and addresses",
           "Signed-in viewers only: to people signed in with the password (to no one while no password is set). "
           "Always: to everyone who can open the page. Never: a client shows as its maker and the last 4 characters "
           "of its hardware address.",
           choice_labels={"token": "Signed-in viewers only", "full": "Always", "masked": "Never"}),
    ])
    + _entries("discovery", [
        _s("discovery.background", "Background route checks",
           "Rerun discovery once a day and after an incident ends, and report route changes."),
        _s("discovery.asn_lookup", "Ask who runs each public hop",
           "Background runs send the public hop addresses to Team Cymru to name their operators.",
           also_in=("privacy",)),
        _s("discovery.daily_check_time", "Daily check at",
           "In the time zone under Display.", empty="a random minute, picked at each start"),
    ])
    + _entries("display", [
        _s("general.timezone", "Time zone",
           "Quiet hours and the daily route check follow this clock. The reports count their days and hours in it "
           "too, unless it is UTC: then each browser uses its own zone."),
        _s("display.theme", "Theme",
           "The colors and look every browser starts with. A browser's own choice in the header's theme menu wins.",
           choice_labels=_theme_names()),
        _s("display.effects", "Animated effects",
           "Themes that have one show a slow animation, such as falling characters. It stops while the tab is hidden and "
           "when the system asks for reduced motion; a browser can turn it off in the header's theme menu."),
        _s("display.default_latency_range", "Latency chart opens on",
           "The range the latency chart shows first. A browser's own choice still wins.",
           choice_labels={"live": "Live", "15m": "15 minutes", "1h": "1 hour", "24h": "24 hours"}),
        # the page shows these three as one row, "Layout for new browsers" (dialogs/settings/fields.js layoutRowHtml)
        _s("dashboard.order", "Panel order for new browsers",
           "The panel order a browser starts with; a browser's own Layout choice wins."),
        _s("dashboard.hidden", "Hidden panels for new browsers",
           "The panels a browser starts without; a browser's own Layout choice wins."),
        _s("dashboard.colors", "Card colors for new browsers",
           "The colors each card starts with; a browser's own Layout choice wins."),
        _s("display.grafana_url", "Grafana address",
           "Adds Dashboard, Logs and Traces links to the page header, at once.", env="PENGUMON_GRAFANA_URL",
           empty="no links"),
    ])
    + _entries("advanced", [
        _s("prober.hop_status.down_loss_fraction", "Hop shows down above",
           "Share of a hop's judged probes lost above which its dot turns red.", "Hop colors",
           related="classifier.down_loss_pct", **_PCT_OF_FRACTION),
        _s("prober.hop_status.degraded_loss_fraction", "Hop shows degraded above",
           "Share lost above which a hop's dot turns amber.", "Hop colors",
           related="classifier.degraded_loss_pct", **_PCT_OF_FRACTION),
        _s("prober.hop_status.local_latency_ms", "Router hop latency", "A router hop turns amber above this.",
           "Hop colors", unit="ms", related="classifier.local_latency_spike_threshold_ms"),
        _s("prober.hop_status.local_jitter_ms", "Router hop jitter", "A router hop turns amber above this.",
           "Hop colors", unit="ms", related="classifier.local_jitter_spike_threshold_ms"),
        _s("prober.hop_status.gateway_latency_ms", "Modem hop latency", "A modem or gateway hop turns amber above "
           "this.", "Hop colors", unit="ms", related="classifier.gateway_latency_spike_threshold_ms"),
        _s("prober.hop_status.gateway_jitter_ms", "Modem hop jitter", "A modem or gateway hop turns amber above "
           "this.", "Hop colors", unit="ms", related="classifier.gateway_jitter_spike_threshold_ms"),
        _s("prober.hop_status.public_latency_ms", "Internet target latency", "An internet target turns amber "
           "above this (a hop's own \"warn above\" wins).", "Hop colors", unit="ms",
           related="classifier.public_latency_spike_threshold_ms"),
        _s("prober.hop_status.public_jitter_ms", "Internet target jitter", "An internet target turns amber above "
           "this.", "Hop colors", unit="ms", related="classifier.public_jitter_spike_threshold_ms"),
        _s("prober.hop_status.dns_latency_ms", "DNS lookup time", "A DNS lookup turns amber above this.",
           "Hop colors", unit="ms", related="classifier.dns_timeout_threshold_ms"),
        _s("prober.hop_status.dns_jitter_ms", "DNS lookup jitter", "A DNS lookup turns amber above this.",
           "Hop colors", unit="ms"),
        _s("classifier.dns_degraded_loss_pct", "DNS degraded from",
           "Share of DNS lookups lost from which DNS counts as degraded.", "DNS rules", unit="%"),
        _s("classifier.dns_cooldown_seconds", "DNS faults wait after line trouble",
           "After any upstream trouble, DNS faults count only once this time has passed.", "DNS rules", unit="s"),
        _s("prober.timeout_seconds", "Ping timeout",
           "A ping without an answer in this time counts as lost. At most one probe round.", "Timeouts", unit="s"),
        _s("prober.dns_timeout_seconds", "DNS lookup timeout",
           "A lookup without an answer in this time counts as lost.", "Timeouts", unit="s",
           empty="the ping timeout"),
        _s("uptime.outage_dead_minutes", "Strip outage after",
           "Minutes with every probe lost that make an uptime-strip block an outage.", "Uptime strips",
           unit="min"),
        _s("uptime.degraded_loss_pct", "Strip degraded from",
           "Loss in a block from which its strip block is degraded (so is one dead minute).", "Uptime strips",
           unit="%"),
        _s("reports.material_loss_points", "A hop counts as lossy from",
           "Percentage points of loss above the hops before it; the report and dossier blame such a hop.",
           "Report", unit="points"),
        _s("line_signal.ds_power_warning_abs_dbmv", "Downstream power warning beyond ±",
           "Downstream channel power outside this band warns.", "Line signal levels", unit="dBmV"),
        _s("line_signal.ds_snr_warning_db", "Downstream SNR warning below", "Signal-to-noise below this warns.",
           "Line signal levels", unit="dB"),
        _s("line_signal.ds_snr_critical_db", "Downstream SNR critical below",
           "Signal-to-noise below this is critical.", "Line signal levels", unit="dB"),
        _s("line_signal.us_power_warning_dbmv", "Upstream power warning above",
           "The modem shouting this loud warns.", "Line signal levels", unit="dBmV"),
        _s("line_signal.us_power_critical_dbmv", "Upstream power critical above",
           "Upstream power above this is critical.", "Line signal levels", unit="dBmV"),
        _s("line_signal.us_power_low_warning_dbmv", "Upstream power weak below",
           "Upstream power below this warns.", "Line signal levels", unit="dBmV"),
        _s("line_signal.us_power_low_critical_dbmv", "Upstream power critical below",
           "Upstream power below this is critical.", "Line signal levels", unit="dBmV"),
        _s("line_signal.uncorrectable_ratio_warning", "Lost codewords: warning from",
           "Codewords lost after error correction, out of every million received, from which the line warns.",
           "Line signal levels", **_PER_MILLION),
        _s("line_signal.uncorrectable_ratio_critical", "Lost codewords: critical from",
           "Codewords lost out of every million received from which the line is critical.", "Line signal levels",
           **_PER_MILLION),
        _s("line_signal.uncorrectable_warning_per_minute", "Lost codewords a minute: warning from",
           "Without codeword counts: new uncorrectable codewords a minute from which the line warns.",
           "Line signal levels", unit="codewords/min"),
        _s("line_signal.uncorrectable_critical_per_minute", "Lost codewords a minute: critical above",
           "Without codeword counts: a minute rate above which the line is critical.", "Line signal levels",
           unit="codewords/min"),
        _s("router_levels.cpu_warning_pct", "Router CPU warning above", "The router's CPU gauge warns above this.",
           "Router levels", unit="%"),
        _s("router_levels.mem_warning_pct", "Router memory warning above",
           "Many routers use most of their memory as cache.", "Router levels", unit="%"),
        _s("router_levels.mem_critical_pct", "Router memory critical above", "Out-of-memory risk.",
           "Router levels", unit="%"),
        _s("router_levels.wan_critical_pct", "Line load critical above",
           "A line this full is saturated. Must be above the busy line level.", "Router levels", unit="%",
           related="classifier.busy_load_pct"),
        _s("server.log_level", "Log level", "How much Pengumon writes to its log.", "Logging", env="PENGUMON_LOG_LEVEL",
           choice_labels={"debug": "Everything (for debugging)", "info": "Normal", "warning": "Warnings and errors",
                          "error": "Errors only", "critical": "Critical errors only"}),
        _s("server.log_levels", "Per-logger levels",
           "For experts debugging one part, e.g. pengumon.devices=debug,uvicorn.access=warning.", "Logging",
           env="PENGUMON_LOG_LEVELS"),
    ])
)

BY_KEY: Dict[str, Setting] = {s.key: s for s in SETTINGS}

LISTS: Dict[str, ListSpec] = {spec.name: spec for spec in (
    ListSpec("devices", "devices", "devices", "Devices", DeviceConfig, list),
    ListSpec("services", "service_checks.services", "devices", "Host app checks", ServiceCheckConfig, list),
    ListSpec("webhooks", "notifications.webhooks", "alerts", "Webhooks", WebhookItem, list),
    ListSpec("dns_probes", "dns_probes", "probing", "DNS lookups", DnsProbeConfig, default_dns_probes),
    ListSpec("themes", "display.themes", "display", "Custom themes", ThemeItem, list),
)}

SECRET_SLOTS: Tuple[SecretSlot, ...] = (
    SecretSlot("ntfy.url", "ntfy topic address", "url", "alerts", env=settings_env.SECRET_ENV["ntfy.url"]),
    SecretSlot("ntfy.token", "ntfy access token", "token", "alerts", env=settings_env.SECRET_ENV["ntfy.token"]),
    SecretSlot("webhook.{id}.url", "Webhook address", "url", "alerts", pattern=True),
    SecretSlot("webhook_header.{name}", "Webhook header secret", "token", "alerts", pattern=True),
)

# Fields of AppConfig the page does not show, and why. Everything else is a Setting or one of LISTS.
NOT_ON_PAGE: Dict[str, str] = {
    "server.host": "bootstrap: the listen address is needed before the database opens (PENGUMON_HOST)",
    "server.port": "bootstrap: needed before the database opens (PENGUMON_PORT)",
    "database.db_path": "bootstrap: where the settings themselves are stored",
    "prober.interval_seconds": "bootstrap: the probe round; rules are shown in seconds as rounds x round_seconds",
    "prober.ping_count_per_probe": "does nothing; the one-time import drops it",
    "targets": "the probe path has its own store and editor (engine/path.py)",
}


def get(key: str) -> Setting:
    """The Setting for `key`; KeyError for a key the page does not have."""
    return BY_KEY[key]


def in_section(section: str) -> List[Setting]:
    """The settings a section renders, in order: its own, then those it shows too (`also_in`)."""
    own = [s for s in SETTINGS if s.section == section]
    return own + [s for s in SETTINGS if section in s.also_in]


def list_spec_for_path(path: str) -> Optional[ListSpec]:
    return next((spec for spec in LISTS.values() if spec.path == path), None)


def stored_leaf_paths(root: Type[BaseModel] = AppConfig) -> List[str]:
    """Every leaf field path of AppConfig, descending into nested section models; a list (of items) is one leaf."""
    found: List[str] = []

    def walk(model: Type[BaseModel], prefix: str) -> None:
        for name, info in model.model_fields.items():
            inner = unwrap_optional(info.annotation)[0]
            path = f"{prefix}{name}"
            if isinstance(inner, type) and issubclass(inner, BaseModel):
                walk(inner, path + ".")
            else:
                found.append(path)

    walk(root, "")
    return found


def unplaced_fields(root: Type[BaseModel] = AppConfig) -> List[str]:
    """Leaf fields that are neither a Setting, nor one of LISTS, nor in NOT_ON_PAGE: a field someone added to a
    section model without deciding whether the page shows it. Empty for this build (a test pins it)."""
    return [path for path in stored_leaf_paths(root)
            if path not in BY_KEY and path not in NOT_ON_PAGE and list_spec_for_path(path) is None]
