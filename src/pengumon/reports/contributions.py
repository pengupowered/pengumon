"""The AI pack's registry and build: which contributions exist (the technology ones and the capability ones), which of
them and which drivers' notes the configured devices activate, and `build_ai_pack`, which collects the contributions'
sections and the core's facts (reports.ai_findings) from the repository and renders them (reports.ai_pack).

The contract the contributions implement is capabilities/contrib.py, which imports nothing of pengumon; this module
imports every implementation and the drivers at module level. The renderer (reports.ai_pack) stays pure: no database,
no clock, no network.
"""

import asyncio
import dataclasses
import time
from dataclasses import dataclass
from datetime import tzinfo
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Dict, List, Mapping, Optional, Tuple

import pengumon
from pengumon.capabilities import Capability
from pengumon.capabilities.contrib import ContribContext, DriverNotes, duration_words, effective_poll_seconds
from pengumon.devices.contrib import CAPABILITY_CONTRIBUTIONS
from pengumon.drivers import DRIVERS
from pengumon.engine.discovery.addresses import registrable_domain
from pengumon.engine.probe_tcp import KERNEL_TIMING
from pengumon.lines import docsis
from pengumon.lines.docsis_contrib import DocsisContribution
from pengumon.models.probes import TargetConfig, probe_words
from pengumon.reports.ai_findings import collect_pack_facts
from pengumon.reports.ai_pack import AppFact, HopFact, ModuleInput, render_pack
from pengumon.reports.ai_pack_view import module_findings, speed_fact, view_of
from pengumon.reports.queries import path_trace_names
from pengumon.reports.redact import LEVELS, Redactor
from pengumon.timeofday import viewer_zone

if TYPE_CHECKING:
    from pengumon.devices.settings import DeviceConfig


# --- registry and activation ----------------------------------------------------------------------------------------

# line technology -> its contribution class
TECHNOLOGY_CONTRIBUTIONS: Dict[str, type] = {docsis.TECHNOLOGY: DocsisContribution}


def technology_contributions() -> Dict[str, type]:
    return dict(TECHNOLOGY_CONTRIBUTIONS)


def capability_contributions() -> Tuple[Tuple[Tuple[Any, ...], type], ...]:
    """(the capabilities that activate it (any of), the contribution class), in the order the pack shows them."""
    return CAPABILITY_CONTRIBUTIONS


def all_contributions() -> Tuple[type, ...]:
    """Every registered contribution class: technology ones first, then capability ones."""
    return tuple(technology_contributions().values()) + tuple(cls for _, cls in capability_contributions())


def all_driver_notes() -> Dict[str, DriverNotes]:
    """driver name -> its pack_notes, for the drivers that have them."""
    return {name: d.pack_notes for name, d in DRIVERS.items() if getattr(d, "pack_notes", None) is not None}


@dataclass(frozen=True)
class Activation:
    contributions: Tuple[Any, ...]                       # instances, in pack order
    notes: Tuple[Tuple["DeviceConfig", DriverNotes], ...]  # (device, its driver's notes), config order, once per driver


def activate(config) -> Activation:
    """The contributions and driver notes the configured devices bring (disabled devices count too)."""
    active: List[Any] = []
    devices = list(config.devices)
    technologies = {getattr(DRIVERS[c.driver], "line_technology", None) for c in devices
                    if Capability.ACCESS_LINE in DRIVERS[c.driver].offers}
    for technology, cls in technology_contributions().items():
        if technology in technologies:
            active.append(cls())
    for capabilities, cls in capability_contributions():
        if any(cap in DRIVERS[c.driver].offers for c in devices for cap in capabilities):
            active.append(cls())
    notes, seen = [], set()
    for cfg in devices:
        pack_notes = getattr(DRIVERS[cfg.driver], "pack_notes", None)
        if pack_notes is not None and cfg.driver not in seen:
            seen.add(cfg.driver)
            notes.append((cfg, pack_notes))
    return Activation(tuple(active), tuple(notes))


def driver_note(cfg: "DeviceConfig", notes: DriverNotes, detail: str, *, mesh: bool = False) -> Optional[str]:
    """The note for one device at a detail level, its placeholders filled from the device's configuration. `mesh`:
    a mesh was seen (the `{aimesh}` clause). Anonymized uses the `anonymized` text, or no note."""
    driver = DRIVERS[cfg.driver]
    text = notes.anonymized if detail == "anonymized" else notes.full
    if text is None:
        return None
    options = driver.options_model.model_validate(cfg.options)
    poll = effective_poll_seconds(cfg, driver)
    min_poll = float(getattr(driver, "min_poll_seconds", driver.default_poll_seconds))
    return text.format(
        poll=duration_words(poll),
        min_poll=duration_words(min_poll),
        poll_limit=(" (as often as Pengumon allows for it)" if poll <= min_poll
                    else f" (never more often than every {duration_words(min_poll)})"),
        clientlist_interval=duration_words(float(getattr(options, "clientlist_interval_seconds", 0.0) or 0.0)),
        aimesh=", on an AiMesh network" if mesh else "",
    )


# --- the build ------------------------------------------------------------------------------------------------------

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


async def _pack_window(repository, scope: str, hours: int, incident_id: Optional[int],
                       now: float) -> Tuple[float, float]:
    """The pack's window: the past `hours`, or an hour either side of the incident (not past now)."""
    if scope == "incident":
        inc = await repository.get_incident_by_id(incident_id)
        if inc is None:
            raise ValueError(f"no incident {incident_id}")
        return inc.start_time - 3600, min(now, (inc.end_time or inc.start_time) + 3600)
    return now - hours * 3600, now


async def _collect_contributions(act, caps_of, repository, config, start: float, end: float, devices, live,
                                 tz_offset_minutes: int, tz: tzinfo):
    """Each active contribution's data, read once; with the events they emit and the event kinds they and the drivers'
    notes declare."""
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
    return events, kinds, loaded


def _module_inputs(act, loaded, caps_of, facts, config, level: str) -> List[ModuleInput]:
    """One ModuleInput per contribution: its section for `level`, the core's findings about its events and, for the
    one section each driver's notes close, those notes."""
    tech_of = {cls: tech for tech, cls in technology_contributions().items()}

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
    return modules


async def build_ai_pack(repository, config, *, scope: str, hours: int, incident_id: Optional[int], level: str,
                        detail: str, devices, live, ignoring=frozenset(), tz_offset_minutes: int = 0,
                        prober=None, tz_name: Optional[str] = None) -> Dict[str, Any]:
    """The pack for the API: collects the modules' sections and the core's facts, renders at `level`. `detail`:
    "short" or "detailed". `live`: the app's device registry, or None (no monitors: the live-only parts say so).
    `prober`: the app's prober, for how pings are sent now (None: the environment says only what is stored).
    `tz_name` (the viewer's IANA zone) and `tz_offset_minutes`: the fallbacks of timeofday.viewer_zone."""
    if level not in LEVELS:
        raise ValueError(f"unknown detail level {level!r}")
    render_detail = "detailed" if detail == "detailed" else "compact"
    now = time.time()
    start, end = await _pack_window(repository, scope, hours, incident_id, now)
    tz, zone_kind, zone_name = viewer_zone(config, tz_name, tz_offset_minutes)

    act = activate(config)
    caps_of = {cls: caps for caps, cls in capability_contributions()}
    events, kinds, loaded = await _collect_contributions(act, caps_of, repository, config, start, end, devices, live,
                                                         tz_offset_minutes, tz)
    facts = await collect_pack_facts(repository, config, start=start, end=end, events=events, event_kinds=kinds,
                                     ignoring=ignoring, detail=render_detail, scope=scope, incident_id=incident_id,
                                     tz_offset_minutes=tz_offset_minutes, now=now, tz=tz)

    modules = _module_inputs(act, loaded, caps_of, facts, config, level)

    names = await path_trace_names(repository)
    identity = {t.id: SimpleNamespace(name=t.name, address=_address(t), rdns=names.get(_address(t), ""),
                                      probe=probe_words(t) if isinstance(t, TargetConfig) else "")
                for t in list(config.targets) + list(config.dns_probes)}
    provider, source = _provider(config, identity)
    line = getattr(config, "line", None)
    speed = speed_fact(await repository.get_speed_tests(start, end, limit=10_000),
                       getattr(line, "down_mbps", None), getattr(line, "up_mbps", None))
    view = view_of(facts, identity, modules, provider, tz, _label(scope, hours, incident_id), pengumon.__version__,
                   apps=_apps(config, facts), environment=_environment(config, facts, prober, tz),
                   provider_source=source, zone=zone_name, zone_kind=zone_kind, speed=speed)
    view = dataclasses.replace(view, known_hops=await _known_hops(repository, config))
    # pure, and a second or so of regexes for a long window: off the event loop, so the probes keep their timing
    pack = await asyncio.to_thread(render_pack, view, level, render_detail)
    return {"markdown": pack.markdown, "modules": pack.modules, "roles": pack.roles, "level": level, "scope": scope,
            "detail": detail, "chars": pack.chars, "tokens_estimate": pack.tokens, "trimmed": pack.trimmed,
            "redactions": {**pack.swept, **{f"scan:{k}": v for k, v in pack.hits.items()}}}


def _provider(config, identity: Mapping[str, Any]) -> Tuple[str, str]:
    """(the provider's name, where it comes from): the ISP name set in Settings (line.isp_name, which also names the
    dossier), else the domain most of the provider's hops' reverse names share (as discovery groups them), else none."""
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
    tcp = sum(1 for t in config.targets if getattr(t, "probe", "icmp") == "tcp" and getattr(t, "enabled", True))
    http = sum(1 for t in config.targets if getattr(t, "probe", "icmp") == "http" and getattr(t, "enabled", True))
    if tcp:
        out.append(f"{tcp} hop{'s' if tcp != 1 else ''} {'are' if tcp != 1 else 'is'} probed by a TCP connection "
                   f"instead of a ping ({'timed by the kernel' if KERNEL_TIMING else 'timed by Pengumon'}).")
    if http:
        out.append(f"{http} {'are' if http != 1 else 'is'} probed by an HTTP request (the time to the server's "
                   "answer, not a round trip).")
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


async def hop_redactor(repository, config, level: str) -> Redactor:
    """A redactor that knows only the path's hops (configured, switched off, removed, earlier versions), for free text
    outside the pack: the MCP tools pass the classifier's titles and descriptions through it at Vague, so a home hop's
    name, id, address and reverse name become its role word ("Router", "Gateway") and the scan's patterns remove any
    other home address, MAC or home host name. No modules: their identifiers are the pack's. `repository` may be None
    (the app still starting): the configured hops only."""
    targets = list(getattr(config, "targets", None) or ()) + list(getattr(config, "dns_probes", None) or ())
    names = await path_trace_names(repository) if repository is not None else {}
    hops = [HopFact(t.id, getattr(t, "category", "dns"), t.name or "", _address(t), rdns=names.get(_address(t), ""))
            for t in targets if getattr(t, "enabled", True)]
    known = await _known_hops(repository, config) if repository is not None and config is not None else ()
    provider = _provider(config, {h.id: h for h in hops})[0] if config is not None else ""
    return Redactor(level, hops=hops, provider=provider, known_hops=known)


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
