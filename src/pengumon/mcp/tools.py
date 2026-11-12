"""The MCP tools: read-only questions about the network, answered from the feature functions the API routes call.

A tool is `Tool(name, description, input_schema, handler)`; the handler takes the app's AppState and the validated
arguments and returns plain data (dicts, lists, numbers, strings). It raises ToolFailure for a failure of its own
(an unknown incident id, a part that is not ready: the caller sees `isError` and the sentence) and BadArguments for
arguments the schema cannot catch (a JSON-RPC -32602). Nothing here imports the web framework or the api package
(docs/architecture.md: what the routes and the tools share lives in the feature packages).

Nothing a tool returns holds a secret: devices are summarized from their latest snapshot (never their configuration or
credentials), clients are counts only (`traffic` and the traffic hours of `incident` describe a device by its kind, maker
and main app, never by name or address), and the AI pack carries its own redaction. No device host either: the
classifier's titles and descriptions name the router and gateway with their addresses ("reaching Mesh (192.168.88.1)"),
so they pass the hops' redactor at Vague (a hop by its role word, reports.contributions.hop_redactor), and every string
of a tool's answer passes the Vague scan's patterns (`scrubbed`: home addresses, MACs, home host names, secrets in
URLs), whatever field carries it (a target id made from an address, a hop named after its host name, a device's own
words). `services` (the owner's own check URLs) and `ai_pack` (its own levels) are left to themselves. Times are ISO 8601
in the zone set in Settings (general.timezone; UTC when none).
"""

import ipaddress
import re
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, tzinfo
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from pydantic_core import to_jsonable_python

from pengumon.analysis.incident import inspect_incident as run_inspection
from pengumon.capabilities import Capability
from pengumon.devices.samples import line_history, router_history
from pengumon.devices import traffic as router_traffic
from pengumon.devices.summary import POLL_FAILED, POLL_FAILURES, client_counts, device_label, summarize_snapshot
from pengumon.engine.path_rules import ignoring_ping_ids
from pengumon.incidents.outage_read import outages_since, with_outage_ids
from pengumon.incidents.outages import Rules
from pengumon.reports.common import pack_building
from pengumon.reports.redact import SCAN_RULES, VAGUE
from pengumon.reports.queries import DNS_NAME
from pengumon.reports.reliability import build_report
from pengumon.reports.speed import summarize
from pengumon.reports.uptime import build_uptime
from pengumon.services.summary import service_list
from pengumon.speedtest.grade import bufferbloat_grade
from pengumon.speedtest.schedule import month_bounds
from pengumon.timeofday import viewer_zone, zone as named_zone
from pengumon.speedtest.quality import confidence, headline


class ToolFailure(Exception):
    """A tool's own failure, told to the caller as a plain sentence (isError)."""


class BadArguments(Exception):
    """Arguments that fit the schema but not the situation (a JSON-RPC invalid-params error)."""


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: Dict[str, Any]
    handler: Callable[[Any, Dict[str, Any]], Awaitable[Any]]


DEFAULT_LIMIT = 20
MAX_LIMIT = 200
PERIODS = {"24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400}
_STAGES = ("local", "gateway", "isp", "public", "dns", "custom")
_TIME_KEYS = frozenset({
    "timestamp", "start_time", "end_time", "since", "last_checked", "last_ok", "last_poll", "window_start",
    "window_end", "event_start", "event_end", "generated_at", "as_of", "loss_start",
    "ts", "hour", "boot_at", "previous_boot_at"})  # (the last four: device events and traffic hours of `incident`)


def plain(value: Any) -> Any:
    """JSON-ready data: models and dataclasses to dicts, NaN and infinity to null."""
    return to_jsonable_python(value, inf_nan_mode="null")


def zone_of(state) -> tzinfo:
    return viewer_zone(state.config)[0]


def iso(ts: Optional[float], tz: tzinfo) -> Optional[str]:
    return None if ts is None else datetime.fromtimestamp(ts, tz).isoformat(timespec="seconds")


def localize(value: Any, tz: tzinfo) -> Any:
    """`value` with every epoch-seconds field named in _TIME_KEYS turned into an ISO 8601 string in `tz`."""
    if isinstance(value, dict):
        return {k: (iso(v, tz) if k in _TIME_KEYS and isinstance(v, (int, float)) and not isinstance(v, bool)
                    else localize(v, tz)) for k, v in value.items()}
    if isinstance(value, list):
        return [localize(v, tz) for v in value]
    return value


def _limit_schema() -> Dict[str, Any]:
    return {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT, "description":
            f"How many to return (default {DEFAULT_LIMIT}, at most {MAX_LIMIT})."}


def _period_schema(default: str) -> Dict[str, Any]:
    return {"type": "string", "enum": list(PERIODS), "description": f"How far back to look (default {default})."}


def _schema(**properties: Dict[str, Any]) -> Dict[str, Any]:
    return {"type": "object", "properties": properties, "additionalProperties": False}


def _repository(state):
    if state.repository is None:
        raise ToolFailure("Pengumon is still starting: the history database is not ready yet.")
    return state.repository


# --- what no answer carries: home addresses and names ---------------------------------------------------------------

_NET = tuple((re.compile(pattern, re.IGNORECASE), repl) for _, pattern, repl in SCAN_RULES[VAGUE])


def scrubbed(value: Any) -> Any:
    """`value` (plain data) with every string passed through the Vague scan's patterns."""
    if isinstance(value, str):
        for pattern, repl in _NET:
            value = pattern.sub(repl, value)
        return value
    if isinstance(value, dict):
        return {k: scrubbed(v) for k, v in value.items()}
    if isinstance(value, list):
        return [scrubbed(v) for v in value]
    return value


def _scrubbing(handler: Callable[[Any, Dict[str, Any]], Awaitable[Any]]):
    async def run(state, args):
        return scrubbed(plain(await handler(state, args)))
    return run


async def _hop_words(state):
    """The hops' redactor at Vague: free text (titles, descriptions) names a hop by its role word."""
    from pengumon.reports.contributions import hop_redactor  # imported when asked, as the AI pack's build is
    return await hop_redactor(state.repository, state.config, VAGUE)


def _said(rx, text: Optional[str]) -> Optional[str]:
    return rx.text(text) if rx is not None and text else text


def incident_summary(incident, tz: tzinfo, outage_ids: Optional[Dict[int, int]] = None, rx=None) -> Dict[str, Any]:
    """`outage_ids` (incident id -> outage id): given, the summary names the outage the incident belongs to (null: none).
    `rx`: the hops' redactor the title passes (_hop_words)."""
    extra = {} if outage_ids is None else {"outage_id": outage_ids.get(incident.id)}
    return {**extra, "id": incident.id, "start": iso(incident.start_time, tz), "end": iso(incident.end_time, tz),
            "duration_seconds": incident.duration_seconds, "resolved": incident.resolved, "kind": incident.kind,
            "cause": incident.cause, "title": _said(rx, incident.title), "severity": incident.severity,
            "load_pct": incident.load_pct, "expected_from_load": incident.expected}


# --- status -------------------------------------------------------------------------------------------------------

_RANK = {"up": 0, "degraded": 1, "down": 2}


def _stages(state) -> Dict[str, Any]:
    stages: Dict[str, Any] = {}
    stats = list(state.prober.stats.values()) if state.prober is not None else []
    for name in _STAGES:
        hops = [s for s in stats if s.category == name and s.enabled]
        if not hops:
            continue
        measured = [s for s in hops if not (s.ignores_ping or s.ipv6_unavailable or s.probing_unavailable)]
        latencies = [s.last_ms for s in measured if s.last_ms is not None]
        jitters = [s.jitter_ms for s in measured if s.jitter_ms is not None]
        stages[name] = {
            "hops": len(hops),
            "status": max((s.status for s in measured), key=_RANK.__getitem__) if measured else "unknown",
            "latency_ms": round(sum(latencies) / len(latencies), 1) if latencies else None,
            "loss_pct": round(max((s.loss_recent_pct for s in measured), default=0.0), 1) if measured else None,
            "jitter_ms": round(max(jitters), 1) if jitters else None,
        }
    return stages


async def _status(state, args):
    if state.classifier is None:
        raise ToolFailure("Pengumon is still starting: there is no diagnosis yet.")
    tz = zone_of(state)
    diagnosis = state.classifier.last_diagnosis
    rx = await _hop_words(state)
    return {
        "status": diagnosis.status, "cause": diagnosis.cause_code, "title": _said(rx, diagnosis.title),
        "description": _said(rx, diagnosis.description), "load_pct": diagnosis.load_pct,
        "as_of": iso(diagnosis.timestamp, tz),
        "active_incident": incident_summary(diagnosis.active_incident, tz, rx=rx) if diagnosis.active_incident else None,
        "stages": _stages(state),
        "stage_note": "Per stage of the path (local network, gateway/modem, ISP, public internet, DNS): worst hop "
                      "status, mean latest latency, worst recent loss and jitter over the stage's hops.",
    }


# --- incidents ----------------------------------------------------------------------------------------------------

_INCIDENT_SCAN = 1000


async def _incidents(state, args):
    repo = _repository(state)
    tz = zone_of(state)
    period = args.get("period", "7d")
    limit = args.get("limit", DEFAULT_LIMIT)
    rows = await repo.get_recent_incidents(limit=_INCIDENT_SCAN, include_expected=True, scope="network")
    now = time.time()
    since = now - PERIODS[period]
    inside = [i for i in rows if i.start_time >= since]
    rules = Rules.from_config(state.config)
    shown = inside[:limit]
    ids = await _outage_ids(repo, rules, shown, now)
    found = list(reversed(await outages_since(repo, rules, since, now)))
    rx = await _hop_words(state)
    outages = [_outage_summary(o.summary(now, rules), tz, rx) for o in found[:limit]]
    return {"period": period, "total_in_period": len(inside), "returned": len(shown),
            "may_be_incomplete": len(rows) == _INCIDENT_SCAN and len(inside) == len(rows),
            "incidents": [incident_summary(i, tz, ids, rx=rx) for i in shown],
            "outages_total_in_period": len(found), "outages": outages,
            "outage_note": "An outage joins incidents at neighboring places of the path that follow each other within "
                           f"{int(rules.gap_seconds // 60)} minutes (0: grouping is off): one event at the provider "
                           "often shows as several incidents. Count outages for how often the line went down. "
                           "likely_maintenance is a guess from the hour of the day, not a fact."}


async def _outage_ids(repo, rules: Rules, incidents, now: float) -> Dict[int, int]:
    """Incident id -> outage id for these incidents (an incident in no outage is left out)."""
    if not incidents:
        return {}
    grouped = await with_outage_ids(repo, rules, list(incidents), now)
    return {i.id: i.outage_id for i in grouped if i.outage_id is not None}


def _outage_summary(o: Dict[str, Any], tz: tzinfo, rx) -> Dict[str, Any]:
    return {"id": o["id"], "start": iso(o["start_time"], tz), "end": iso(o["end_time"], tz), "ongoing": o["ongoing"],
            "duration_seconds": round(o["duration_seconds"]), "down_seconds": round(o["down_seconds"]),
            "drops": o["drops"], "critical_drops": o["critical_drops"], "severity": o["severity"],
            "places": o["places"], "title": _said(rx, o["title"]), "likely_maintenance": o["likely_maintenance"],
            "incident_ids": o["incident_ids"]}


_DROPPED_EVIDENCE = ("incident", "samples", "docsis_snapshots", "router_snapshots")


# The household's own address ranges (RFC 1918, IPv6 unique local). ISP-internal ranges (CGNAT 100.64/10) stay visible.
_LAN_NETS = tuple(ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7"))


def _ip(value: Any):
    try:
        return ipaddress.ip_address(str(value or "").strip().strip("[]"))
    except ValueError:
        return None


def _addresses(targets, categories: Tuple[str, ...]) -> frozenset:
    return frozenset(ip for t in targets if getattr(t, "category", None) in categories
                     for ip in [_ip(getattr(t, "address", None))] if ip is not None)


def _trace_without_lan(hops, targets=()):
    """The incident's path trace with every hop on the household's side shown as "private" and without its name: the
    first hops are the router and modem, whose addresses are the devices' hosts (no device host in any tool's output).
    That side is every private, link-local or loopback hop, a hop at the address of the router or gateway target (a
    router's global IPv6 address, a gateway's public WAN address behind double NAT, whose reverse name embeds it), and,
    when the trace reaches a hop at an ISP target's address, every hop before that one. Hops from the provider on keep
    their address and name: they are what an ISP ticket needs, when the name has the shape of a host name (the rule of
    reports.queries.path_trace_names: a reverse name is text from outside, so one with spaces, or longer than DNS
    allows, is left out)."""
    if not isinstance(hops, list):
        return hops
    home, provider = _addresses(targets, ("local", "gateway")), _addresses(targets, ("isp",))
    first_provider = next((k for k, hop in enumerate(hops)
                           if isinstance(hop, dict) and _ip(hop.get("ip")) in provider), None)
    out = []
    for k, hop in enumerate(hops):
        ip = _ip(hop.get("ip")) if isinstance(hop, dict) else None
        if ip is None:
            local = True  # "*" or anything unparsed: nothing to show
        else:
            local = (ip.is_link_local or ip.is_loopback or ip.is_unspecified or any(ip in net for net in _LAN_NETS)
                     or ip in home or (first_provider is not None and k < first_provider))
        if local:
            out.append({**hop, "ip": "private", "hostname": None})
        elif isinstance(hop, dict) and hop.get("hostname") is not None and not _is_host_name(hop["hostname"]):
            out.append({**hop, "hostname": None})  # reverse DNS is whatever the zone's owner wrote: not text for a model
        else:
            out.append(hop)
    return out


def _is_host_name(name: Any) -> bool:
    return isinstance(name, str) and len(name) <= 254 and DNS_NAME.fullmatch(name) is not None


async def _incident(state, args):
    repo = _repository(state)
    tz = zone_of(state)
    incident = await repo.get_incident_by_id(args["id"])
    if incident is None:
        raise ToolFailure(f"There is no incident with id {args['id']}.")
    cfg = state.config
    targets = [*cfg.targets, *cfg.dns_probes] if cfg else []
    line = state.devices.primary(Capability.ACCESS_LINE) if state.devices is not None else None
    poll = line.interval_seconds if line else None
    reach = {"snapshot_reach_seconds": poll} if poll else {}
    inspection = await run_inspection(repo, targets, incident, **reach,
                                      line=line_history(state.devices, repo), router=router_history(state.devices, repo),
                                      classifier=getattr(cfg, "classifier", None),
                                      traffic=router_traffic.traffic_source(state.devices, "generic"),
                                      tz=tz)
    evidence = plain(inspection.to_response())
    out = {k: v for k, v in evidence.items() if k not in _DROPPED_EVIDENCE}
    correlation = out.get("docsis_correlation")
    if isinstance(correlation, dict) and isinstance(correlation.get("nearest_snapshot"), dict):
        correlation["nearest_snapshot"].pop("channels", None)  # per-channel tables: the dashboard has them
    out["probe_samples_omitted"] = len(evidence.get("samples", ()))
    out["modem_snapshots_omitted"] = len(evidence.get("docsis_snapshots", ()))
    out["router_snapshots_omitted"] = len(evidence.get("router_snapshots", ()))
    rx = await _hop_words(state)
    return localize({
        "incident": {**incident_summary(incident, tz, rx=rx), "description": _said(rx, incident.description),
                     "affected_targets": incident.affected_targets, "loss_seconds": incident.loss_seconds,
                     "path_trace": _trace_without_lan(plain(incident.path_trace), targets)},
        "evidence": out,
        "evidence_note": "Per-hop impact over the incident, the modem correlation, and counts of the raw samples "
                         "left out; the dashboard has the charts.",
    }, tz)


# --- report -------------------------------------------------------------------------------------------------------

async def _report(state, args):
    repo = _repository(state)
    tz = zone_of(state)
    period = args.get("period", "7d")
    ignoring = ignoring_ping_ids(state.prober)
    uptime = await build_uptime(repo, state.config, period, tz=tz)
    reliability = await build_report(repo, state.config, PERIODS[period] // 86400, tz=tz, ignoring=ignoring)
    hops = []
    for target in uptime["targets"]:
        cells = target["cells"]
        hops.append({"name": target["name"], "availability_pct": target["availability_pct"],
                     "outage_blocks": cells.count("outage"), "degraded_blocks": cells.count("degraded"),
                     "busy_blocks": cells.count("busy")})
    out = localize({
        "period": period, "zone": uptime["zone"], "window_start": uptime["start"], "window_end": uptime["end"],
        "block_seconds": uptime["block_seconds"], "uptime_by_hop": hops,
        "daily_availability": reliability["availability"], "incidents": reliability["incidents"],
        "worst_hours_of_day": None, "generated_at": reliability["generated_at"],
    }, tz)
    out["worst_hours_of_day"] = reliability["worst_hours"]  # its "hour" is the hour of the day (0-23), not a time
    return out


# --- devices, services --------------------------------------------------------------------------------------------

_CLIENT_KEYS = ("online", "wired", "by_band")
_SEE_DASHBOARD = " The dashboard shows the device's own message."


def _device(device) -> Dict[str, Any]:
    snapshot = device.latest_snapshot
    latest = summarize_snapshot(snapshot) if snapshot is not None else None
    if latest is not None and latest.get("error_message") and snapshot.status != "unconfigured":
        latest["error_message"] = POLL_FAILURES.get(snapshot.error_kind, POLL_FAILED) + _SEE_DASHBOARD
    out: Dict[str, Any] = {
        "id": device.id, "label": device_label(device), "kind": getattr(device.driver, "label", None) or device.driver.name,
        "model": getattr(device.config, "model", None), "enabled": device.enabled,
        "offers": sorted(c.value for c in device.driver.offers),
        "line_technology": (getattr(device.driver, "line_technology", None)
                            if Capability.ACCESS_LINE in device.driver.offers else None),
        "poll_interval_seconds": device.interval_seconds, "status": snapshot.status if snapshot else None,
        "latest": latest,
    }
    if snapshot is not None and snapshot.error_kind:
        out["error_kind"] = snapshot.error_kind
    counts = client_counts(device.monitor) if device.enabled else None
    if counts is not None:  # how many, never who
        out["clients"] = {**{k: counts[k] for k in _CLIENT_KEYS}, "mesh_nodes": len(counts["nodes"]),
                          "as_of": counts.get("ts")}
    return out


async def _devices(state, args):
    if state.devices is None:
        raise ToolFailure("Pengumon is still starting: the devices are not set up yet.")
    tz = zone_of(state)
    devices = [_device(d) for d in state.devices.all()]
    return localize({"devices": devices, "note": None if devices else "No modem or router is configured."}, tz)


async def _services(state, args):
    tz = zone_of(state)
    services = await service_list(state.service_checker, state.repository)
    return localize({"services": services, "note": None if services else "No host app checks are configured."}, tz)


# --- speed tests --------------------------------------------------------------------------------------------------

_SPEED_PROVIDERS = {"cloudflare": "Cloudflare", "mlab": "M-Lab NDT7", "librespeed": "your LibreSpeed server"}
_OWN_SERVER = "your LibreSpeed server"


def _speed_row(row: Dict[str, Any], tz: tzinfo) -> Dict[str, Any]:
    """One stored test. A LibreSpeed test names its provider, never the server's address: the server label is that
    address's host, and an error sentence can repeat it (in any case: the label is lowercased, a 404's hint repeats the
    address as entered)."""
    own = row["provider"] == "librespeed"
    level, note = confidence(row)
    server = row.get("server")
    reason = row.get("reason")
    if own and server and reason:
        reason = re.sub(re.escape(server), _OWN_SERVER, reason, flags=re.IGNORECASE)
    best = headline(row)
    return {
        "time": iso(row["ts"], tz), "trigger": row["trigger"], "status": row["status"], "reason": reason,
        "provider": _SPEED_PROVIDERS.get(row["provider"], row["provider"]), "server": None if own else server,
        # the headline: the line's rate the router measured during the test ("router"), else the test's own ("test")
        "download_mbps": best["down_mbps"], "upload_mbps": best["up_mbps"], "measured_by": best["measured_by"],
        "test_download_mbps": row.get("down_mbps"), "test_upload_mbps": row.get("up_mbps"),
        "traffic_note": best["note"],
        "idle_latency_ms": row.get("idle_ms"), "latency_during_download_ms": row.get("loaded_down_ms"),
        "latency_during_upload_ms": row.get("loaded_up_ms"),
        "bufferbloat_grade": bufferbloat_grade(row.get("idle_ms"), row.get("loaded_down_ms"), row.get("loaded_up_ms")),
        # "low": a direction lasted too short to measure the line; the note says how much data per test would
        "confidence": level, "note": note,
        "downloaded_mb": round((row.get("bytes_down") or 0) / 1e6, 1),
        "uploaded_mb": round((row.get("bytes_up") or 0) / 1e6, 1),
    }


async def _speed_tests(state, args):
    repo = _repository(state)
    config = state.config
    if config is None:
        raise ToolFailure("Pengumon is still starting: the settings are not loaded yet.")
    tz = zone_of(state)
    period = args.get("period", "7d")
    limit = args.get("limit", DEFAULT_LIMIT)
    now = time.time()
    rows = await repo.get_speed_tests(now - PERIODS[period], now, limit=10_000)
    cfg, line = config.speedtest, config.line
    start, end = month_bounds(now, named_zone(config.general.timezone))
    used = await repo.speed_test_bytes(start, end)
    runner = state.speedtest
    summary = summarize(rows, line.down_mbps, line.up_mbps)
    return {
        "enabled": cfg.enabled, "provider": _SPEED_PROVIDERS.get(cfg.provider, cfg.provider),
        "running_now": bool(runner and runner.running),
        "next_scheduled_run": iso(await runner.next_run(), tz) if runner is not None else None,
        "data_used_this_month_mb": round(used / 1e6, 1),
        "monthly_cap_mb": cfg.monthly_cap_mb or None,
        "plan_download_mbps": line.down_mbps, "plan_upload_mbps": line.up_mbps,
        "period": period, "tests_in_period": len(rows), "returned": min(limit, len(rows)),
        "summary": None if summary is None else {
            "ok": summary.ok, "partial": summary.partial, "failed": summary.failed, "skipped": summary.skipped,
            "download_median_mbps": summary.down_median, "download_lowest_mbps": summary.down_low,
            "upload_median_mbps": summary.up_median, "upload_lowest_mbps": summary.up_low,
            "tests_under_half_of_plan": summary.under if (line.down_mbps or line.up_mbps) else None,
            "median_added_latency_under_load_ms": summary.added_ms, "bufferbloat_grade": summary.grade},
        "tests": [_speed_row(r, tz) for r in rows[:limit]],
        "note": None if rows else ("The speed test is off: turn it on in Settings > Speed test." if not cfg.enabled
                                   else "No speed test has run in this period."),
    }


# --- the AI pack --------------------------------------------------------------------------------------------------

# At most PACK_RATE builds a minute through MCP, as GET /api/ai-pack allows (rate_limit("ai_pack", 6, 60)): without
# it the endpoint's 120 calls a minute could build packs back to back, keep the shared lock busy and refuse the
# dashboard's own.
PACK_RATE = (6, 60.0)
_pack_starts: deque = deque()  # monotonic times of the builds started within the last minute


def reset_pack_rate() -> None:
    """Forgets the recent builds (tests)."""
    _pack_starts.clear()


def _count_pack_build() -> None:
    calls, per_seconds = PACK_RATE
    now = time.monotonic()
    while _pack_starts and now - _pack_starts[0] > per_seconds:
        _pack_starts.popleft()
    if len(_pack_starts) >= calls:
        wait = max(1, int(per_seconds - (now - _pack_starts[0])))
        raise ToolFailure(f"At most {calls} packs a minute; try again in {wait} s.")
    _pack_starts.append(now)


async def _ai_pack(state, args):
    from pengumon.reports.contributions import build_ai_pack  # imported when asked, as GET /api/ai-pack does

    repo = _repository(state)
    scope = args.get("scope", "window")
    hours = args.get("hours", 24)
    incident_id = args.get("incident_id")
    if (scope == "incident") != (incident_id is not None):
        raise BadArguments("incident_id goes with scope 'incident', and only with it.")
    if incident_id is not None and await repo.get_incident_by_id(incident_id) is None:
        raise ToolFailure(f"There is no incident with id {incident_id}.")
    if pack_building.locked():
        raise ToolFailure("Another pack is being built; try again in a few seconds.")
    _count_pack_build()
    level = args.get("level", "full")
    async with pack_building:
        pack = await build_ai_pack(
            repo, state.config, scope=scope, hours=hours, incident_id=incident_id, level=level,
            detail=args.get("detail", "short"), devices=tuple(state.config.devices) if state.config else (),
            live=state.devices, ignoring=ignoring_ping_ids(state.prober), prober=state.prober)
    return {"scope": scope, "hours": hours if scope == "window" else None, "incident_id": incident_id,
            "level": level, "markdown": pack["markdown"], "modules": pack["modules"]}


# --- traffic ------------------------------------------------------------------------------------------------------

TRAFFIC_TOP_DEFAULT = 10


async def _traffic(state, args):
    repo = _repository(state)
    tz = zone_of(state)
    device = router_traffic.traffic_device(state.devices)
    if device is None:
        return {"available": False, "reason": "No configured router reports traffic per client.", "clients": []}
    hours = int(args.get("hours", 24))
    top = int(args.get("top", TRAFFIC_TOP_DEFAULT))
    namer = router_traffic.Namer("generic", getattr(device.monitor, "latest_clients", None))
    status = router_traffic.status_of(device)
    body = await router_traffic.summary(repo, device.id, hours, namer, top=top, tz=tz)

    def apps(rows):
        return [{"app": a["app"], "download_bytes": a["down"], "upload_bytes": a["up"]} for a in rows]

    clients = []
    for c in body["clients"]:
        clients.append({"device": c["label"], "vendor": c["vendor"], "download_bytes": c["down"],
                        "upload_bytes": c["up"], "top_apps": apps(c["apps"]), "main_app": c["traffic_hint"]})
    busiest = sorted((h for h in body["wan"] if h["down"] is not None), key=lambda h: -h["down"])[:3]
    return {
        "device": device.id, "available": status.available, "reason": status.reason,
        "hours": hours, "from": iso(body["start"], tz), "to": iso(body["end"], tz),
        "newest_hour_ends": iso(body["window_end"], tz),
        "totals": {"download_bytes": body["totals"]["down"], "upload_bytes": body["totals"]["up"]},
        "busiest_hours": [{"hour": iso(h["hour"], tz), "download_bytes": h["down"], "upload_bytes": h["up"]}
                          for h in busiest],
        "clients": clients,
        "apps": apps(body["apps"]),
        "apps_window_ends": iso(body["apps_window_end"], tz),
        "apps_days": body["apps_days"],
        "note": "The router's own count (its Traffic Analyzer), per clock hour, published after the hour ends. App "
                "figures: for 47 hours or less, the router's latest 24-hour window; for more, the sum of the whole local "
                "days (midnight to midnight) up to the last midnight, apps_days saying which and how many had figures. "
                "Clients are ranked by download over the hours asked.",
    }


# --- the list ------------------------------------------------------------------------------------------------------

TOOLS: List[Tool] = [
    Tool("status",
         "Is the internet OK right now? Returns the current diagnosis (healthy, busy, degraded, down or unknown), the "
         "likely cause in words, the line load, the open incident if there is one, and for each stage of the path "
         "(local network, gateway or modem, ISP, public internet, DNS) the latest latency, recent packet loss and "
         "jitter. Start here; it is cheap.",
         _schema(), _scrubbing(_status)),
    Tool("incidents",
         "Lists recent network incidents (outages and slow spells), newest first, with when they started and ended, "
         "how long they lasted, the kind and cause, severity and the line load at the time. Each has an `outage_id`: an "
         "outage joins incidents at neighboring places of the path that follow each other within a few minutes (a "
         "flapping line or one provider event is several incidents but one outage), and `outages` lists them with "
         "their drops, down time and whether they are likely maintenance (a guess from the hour). Use it to see how "
         "stable the network has been, then call `incident` with an id for the evidence.",
         _schema(period=_period_schema("7d"), limit=_limit_schema()), _scrubbing(_incidents)),
    Tool("incident",
         "One incident in full: its description, the path trace taken when it began, and the evidence for its cause "
         "(per-hop loss and latency over the incident, whether the modem's signal explains it). Use it to answer "
         "'why did the internet drop at that time?'. Raw samples are left out; their counts are given.",
         {"type": "object", "properties": {"id": {"type": "integer", "minimum": 1, "maximum": 2**53,
                                                  "description": "The incident id from `incidents`."}},
          "required": ["id"], "additionalProperties": False}, _scrubbing(_incident)),
    Tool("report",
         "Uptime and reliability over a day, a week or a month: availability of every hop of the path, how many "
         "outage and degraded blocks it had, availability per day, incidents by cause and the worst hours of the "
         "day for packet loss. Use it for 'how reliable has my internet been?'.",
         _schema(period=_period_schema("7d")), _scrubbing(_report)),
    Tool("devices",
         "The modem and router Pengumon polls: what each is, whether the last poll worked, its headline readings "
         "(cable line signal levels and error counts, WAN rates, CPU and memory load) with Pengumon's grades, and "
         "counts of connected clients (never who they are). No passwords or addresses.",
         _schema(), _scrubbing(_devices)),
    Tool("services",
         "The apps on the machine running Pengumon whose health it checks: each one's name, whether it is up, "
         "degraded or down, response time, the error if it is not up, and its last outages. Empty when none are "
         "configured.",
         _schema(), _services),
    Tool("speed_tests",
         "Is my line as fast as I pay for? The speed tests Pengumon runs on a schedule: whether they are on, which "
         "provider, when the next one runs, how much data they used this month against the monthly limit, the plan "
         "speed the owner set, a summary of the period (median and lowest download and upload, how many tests fell "
         "under half the plan speed, the bufferbloat grade: how much a full line adds to latency) and the tests "
         "newest first with time, status, why one failed or was skipped, download and upload Mbps, idle and loaded "
         "latency and data moved. Each test's Mbps are the line's rate the router measured during the test "
         "(measured_by \"router\": the test and the household's traffic, what the line carried while full) when "
         "the router reports WAN counters, else the test's own (measured_by \"test\": one server, which may not fill "
         "the line); the test's own figures are kept beside them. Repeated slow results are the finding. Empty when "
         "the speed test is off.",
         _schema(period=_period_schema("7d"), limit=_limit_schema()), _scrubbing(_speed_tests)),
    Tool("traffic",
         "Who used the internet? The router's own count of the bytes each connected device downloaded and uploaded "
         "(its Traffic Analyzer), per clock hour, over the last `hours`: the totals, the busiest hours, the devices "
         "ranked by download with their top apps (and the app a device mostly uses), and the apps of all devices "
         "together. Use it to explain a busy line or a slow evening. A device is described by its kind and maker "
         "(\"game console\", \"phone\"), never by name or address. Empty when no router reports it.",
         _schema(hours={"type": "integer", "minimum": 1, "maximum": 720,
                        "description": "How many complete hours back (default 24)."},
                 top={"type": "integer", "minimum": 1, "maximum": 50,
                      "description": f"How many devices and apps to list (default {TRAFFIC_TOP_DEFAULT})."}),
         _scrubbing(_traffic)),
    Tool("ai_pack",
         "A written troubleshooting summary of the network (Markdown) for a period or one incident: the findings, "
         "the timeline, the path and the devices, ready to reason over or to hand to a person. The slowest tool; "
         "one build at a time. `level` full names the household's devices and hops, `vague` and `anonymized` "
         "remove more; use `anonymized` if the text will leave the household.",
         _schema(scope={"type": "string", "enum": ["window", "incident"],
                        "description": "A time window (default) or one incident."},
                 hours={"type": "integer", "minimum": 1, "maximum": 168,
                        "description": "For scope window: how many hours back (default 24)."},
                 incident_id={"type": "integer", "minimum": 1, "maximum": 2**53,
                              "description": "For scope incident, and only then: the incident id."},
                 level={"type": "string", "enum": ["full", "vague", "anonymized"],
                        "description": "How much of the household to name (default full)."},
                 detail={"type": "string", "enum": ["short", "detailed"], "description": "Default short."}),
         _ai_pack),
]
