"""The evidence dossier: facts decided from the stored data, then rendered as Markdown.

Every sentence in the dossier follows from the data in the window: loss figures come from the per-minute
rollups, RF wording appears only when DOCSIS evidence exists, and the failing hop is named only when its loss
stands out from the hops before it. Where loss starts is said in the provider's terms: the gateway is probed on its
home-side address, which does not use the line, so loss starting there is the gateway's, never called a line fault;
loss starting at the provider's first router while the gateway answers is the line or that router."""

import re
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone, tzinfo
from typing import Any, Dict, Iterable, List, Optional, Tuple

from pengumon.lines import docsis
from pengumon.lines.docsis import channel_finding, is_degraded as _is_degraded, uncorrectable_increase as _uncorrectable_increase
from pengumon.analysis.incident import UNREACHABLE_KINDS
from pengumon.incidents.taxonomy import incident_cause
from pengumon.timeofday import viewer_zone
from pengumon.lines.docsis_model import DocsisChannel, DocsisSignalSnapshot
from pengumon.lines.levels import LineSignalConfig
from pengumon.reports.common import (
    _canonical_order, _hop_label, _split_incidents, _targets, find_primary_failing_hop, material_loss_points,
)
from pengumon.reports.speed import (SpeedSummary, counts_text, direction_text, latency_text, method_text, summarize,
                                    under_text)


@dataclass(frozen=True)
class DossierFacts:
    """Everything the dossier states, decided from the stored data; render_dossier_markdown only formats it."""
    hours: int
    now: float
    total_incidents: int
    total_dur_sec: float
    total_dur_min: float
    excluded: int                              # expected incidents left out of the totals
    busy_pct: float
    under_load: int                            # incidents at or above busy_pct line load
    by_cause: List[Tuple[str, int, float]]     # (cause category, count, minutes), in render order (most frequent
    #                                            first); CAUSE_WORDS names them
    hop_stats: List[Dict[str, Any]]            # canonical order
    primary: Optional[Dict[str, Any]]          # first hop with elevated loss
    later_lossy: List[Dict[str, Any]]          # hops after `primary` that also lose >= material_loss_points
    labels: Dict[str, str]                     # target id -> "Name (address)"
    names: Dict[str, str]
    max_lan_loss: Optional[float]              # worst loss over local and gateway hops that sent probes
    docsis_snapshot: Optional[DocsisSignalSnapshot]
    snap_ok: bool                              # the latest snapshot holds modem readings
    has_window_snaps: bool
    unc_increase: int                          # new uncorrectable codewords across the window's snapshots
    degraded: List[Tuple[DocsisChannel, str]]  # channels outside typical levels, worst first, with the finding
    rf_evidence: bool
    local_name: str
    gw_name: str
    ignoring: frozenset = frozenset()          # hops that ignore ping (engine.path_rules): listed, never counted
    material: float = material_loss_points(None)  # percentage points that make a hop's loss material (reports:)
    primary_place: Optional[str] = None        # where `primary` sits: PLACES key (None: no primary, or another hop)
    window_readings: int = 0                   # modem readings in the window ...
    modem_unanswered: int = 0                  # ... and those where the modem could not be reached at all
    line_levels: Optional[LineSignalConfig] = None  # the config's line levels, "typical" in the text (None: defaults)
    zone: tzinfo = timezone.utc                # the clock "Generated" is shown on (timeofday.viewer_zone)
    speed: Optional[SpeedSummary] = None       # the window's speed tests (None: there were none)


# The incidents' cause categories (incidents.taxonomy) in the provider's terms.
CAUSE_WORDS = {
    "router": "home network (router or Wi-Fi)",
    "gateway": "gateway (modem) not answering on its home side",
    "line": "line or the provider's first router",
    "isp": "provider's network beyond its first router",
    "internet": "public servers only",
    "dns": "DNS lookups",
    "load": "busy line",
    "unknown": "not attributed",
}


def place_of(primary: Optional[Dict[str, Any]], categories: Dict[str, Optional[str]],
             hop_stats: List[Dict[str, Any]]) -> Optional[str]:
    """Where the first hop with elevated loss sits: "home" (the router), "gateway" (the gateway's home-side address),
    "first_isp" (the provider's first hop, so the gateway answered), "isp" (a later provider hop), "internet", or None
    (no such hop, or a custom or DNS one)."""
    if primary is None:
        return None
    cat = categories.get(primary["target_id"])
    if cat == "isp":
        first_isp = next((h for h in hop_stats if categories.get(h["target_id"]) == "isp"), None)
        return "first_isp" if first_isp is primary else "isp"
    return {"local": "home", "gateway": "gateway", "public": "internet"}.get(cat)


def hop_note(h: Dict[str, Any], primary: Optional[Dict[str, Any]], ignoring=frozenset(),
             material: float = material_loss_points(None)) -> str:
    if h["target_id"] in ignoring:
        return "Ignores ping (later hops answer, so traffic passes); not counted"
    if primary and h["target_id"] == primary["target_id"]:
        return "First hop with elevated loss"
    if h["loss_pct"] >= material:
        return "Elevated loss"
    return "No material loss"


async def collect_dossier_facts(repository, config, hours: int, now: float, line=None,
                                ignoring: Iterable[str] = (), *, start: Optional[float] = None,
                                zone: tzinfo = timezone.utc) -> DossierFacts:
    """`line` is the line device's DocsisHistory (pengumon.lines.docsis); without one the dossier has no modem
    readings. `ignoring`: the hops that ignore ping now (engine.path_rules); their loss counts in no finding.
    `start`: the window begins here instead of `hours` before `now`, and its hop totals stop at `now` (the AI pack's
    windows end at their own end time)."""
    ignoring = frozenset(ignoring)
    start_time = now - hours * 3600 if start is None else start

    rows = await repository.get_incidents_since(start_time)
    incidents, excluded = _split_incidents(rows)
    total_incidents = len(incidents)

    def lasted(r):  # an incident still open has no duration yet: it has lasted until now
        return r["duration_seconds"] if r["duration_seconds"] is not None else max(0.0, now - r.get("start_time", now))

    total_dur_sec = sum(lasted(r) for r in incidents)
    total_dur_min = round(total_dur_sec / 60.0, 1)
    busy_pct = getattr(getattr(config, "classifier", None), "busy_load_pct", 60.0)
    under_load = sum(1 for r in incidents if (r.get("load_pct") or 0) >= busy_pct)
    by_cause: Dict[str, List[float]] = defaultdict(lambda: [0, 0.0])
    for r in incidents:
        cause = incident_cause(r.get("kind"), r["cause_code"], r.get("description"))
        by_cause[cause][0] += 1
        by_cause[cause][1] += lasted(r) / 60.0
    causes = [(c, n, m) for c, (n, m) in sorted(by_cause.items(), key=lambda kv: -kv[1][0])]

    cfg_targets = _targets(config)
    names = {t.id: t.name for t in cfg_targets}
    addresses = {t.id: getattr(t, "address", "") or "" for t in cfg_targets}
    categories = {t.id: getattr(t, "category", None) for t in cfg_targets}
    order = _canonical_order(cfg_targets)

    # end_time is passed only with `start`: a repository that takes none (the characterization fake) is called as before
    hop_stats = await (repository.get_hop_stats(start_time) if start is None
                       else repository.get_hop_stats(start_time, now))
    hop_stats.sort(key=lambda h: order.index(h["target_id"]) if h["target_id"] in order else 999)
    material = material_loss_points(config)
    primary = find_primary_failing_hop(hop_stats, ignoring, material)
    later_lossy = []
    if primary:
        later_lossy = [h for h in hop_stats[hop_stats.index(primary) + 1:]
                       if h["sent"] and h["loss_pct"] >= material and h["target_id"] not in ignoring]
    labels = {h["target_id"]: _hop_label(h["target_id"], names, addresses) for h in hop_stats}

    local_hops = [h for h in hop_stats if categories.get(h["target_id"]) in ("local", "gateway") and h["sent"]
                  and h["target_id"] not in ignoring]
    max_lan_loss = max((h["loss_pct"] for h in local_hops), default=None)

    docsis_snapshot = None
    window_snaps: List[DocsisSignalSnapshot] = []
    if line is not None:
        docsis_snapshot = await line.get_latest_docsis_snapshot()
        window_snaps = await line.get_docsis_snapshots_in_range(start_time, now, padding_seconds=0)
    unc_increase = _uncorrectable_increase(window_snaps)

    snap_ok = bool(docsis_snapshot and docsis_snapshot.status not in ("unconfigured", "error"))
    line_levels = getattr(config, "line_signal", None) or docsis.DEFAULT_LEVELS
    degraded_channels = []
    if snap_ok:
        degraded_channels = sorted(
            (c for c in docsis_snapshot.channels if _is_degraded(c, line_levels)),
            key=lambda c: (c.uncorrectables_recent or 0, c.uncorrectables or 0,
                           -(c.snr_db if c.snr_db is not None else 99.0)),
            reverse=True,
        )
    rf_evidence = bool(degraded_channels or unc_increase > 0)

    speed = await _speed_summary(repository, config, start_time, now)

    return DossierFacts(
        hours=hours,
        now=now,
        total_incidents=total_incidents,
        total_dur_sec=total_dur_sec,
        total_dur_min=total_dur_min,
        excluded=excluded,
        busy_pct=busy_pct,
        under_load=under_load,
        by_cause=causes,
        hop_stats=hop_stats,
        ignoring=ignoring,
        material=material,
        primary=primary,
        primary_place=place_of(primary, categories, hop_stats),
        later_lossy=later_lossy,
        labels=labels,
        names=names,
        max_lan_loss=max_lan_loss,
        docsis_snapshot=docsis_snapshot,
        snap_ok=snap_ok,
        has_window_snaps=bool(window_snaps),
        window_readings=len(window_snaps),
        modem_unanswered=sum(1 for s in window_snaps if s.status == "error"
                             and (s.error_kind is None or s.error_kind in UNREACHABLE_KINDS)),
        unc_increase=unc_increase,
        degraded=[(c, channel_finding(c, line_levels)) for c in degraded_channels],
        rf_evidence=rf_evidence,
        local_name=next((t.name for t in cfg_targets if categories.get(t.id) == "local"), "the local router"),
        gw_name=next((t.name for t in cfg_targets if categories.get(t.id) == "gateway"), "the gateway"),
        line_levels=line_levels,
        zone=zone,
        speed=speed,
    )


async def _speed_summary(repository, config, start_time: float, end_time: float) -> Optional[SpeedSummary]:
    """The speed tests that started in the window against the plan speed (Settings > Your line); None without any."""
    rows = await repository.get_speed_tests(start_time, end_time, limit=10_000)
    line = getattr(config, "line", None)
    return summarize(rows, getattr(line, "down_mbps", None), getattr(line, "up_mbps", None))


# The ticket's sentence after "Loss is first elevated at <hop>: <n>%.", by where that hop sits.
_TICKET_PLACE = {
    "gateway": " That is the gateway's home-side address, which does not use the line: the gateway stopped answering "
               "while the router answered.",
    "first_isp": " That is the provider's first router; the gateway answered, so the loss starts on the line or at "
                 "that router.",
}


def _plural(n: int, one: str, many: Optional[str] = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def _span(seconds: float) -> str:
    """A duration in words: "none", "45 seconds", "2 minutes 5 seconds", "1 hour 5 minutes"."""
    total = round(seconds)
    if total <= 0:
        return "none"
    h, rest = divmod(total, 3600)
    m, sec = divmod(rest, 60)
    parts = [_plural(h, "hour")] if h else []
    if m:
        parts.append(_plural(m, "minute"))
    if sec and not h:
        parts.append(_plural(sec, "second"))
    return " ".join(parts)


def _hop_name(name: str) -> str:
    """A target name without a leading "Hop 3:" (the table's own Hop column carries the number)."""
    return re.sub(r"^\s*Hop\s+\d+\s*[:\-–]\s*", "", name) or name


def _place_text(f: DossierFacts) -> str:
    """The rest of "- Loss first appears at <hop>": what that place means, without claiming more than it shows."""
    place = f.primary_place
    if place == "gateway":
        return (f", the gateway probed on its home-side address, while {f.local_name} answers. These probes do not "
                "use the line, so the gateway itself stopped answering (restarting, overloaded, or the link from the "
                "router to it); a line fault shows here only if it made the gateway restart."
                + (" The modem's RF signs above are a separate finding." if f.rf_evidence else ""))
    if place == "first_isp":
        return (", the provider's first router, while the gateway answers: the line or that router."
                + (" The modem's RF signs in the same window point to the line itself." if f.rf_evidence else ""))
    if place == "isp":
        return ", inside the provider's network past its first router; the hops before it show no comparable loss."
    if place == "internet":
        return ", past the provider's routers."
    return "."


def _speed_lines(s: SpeedSummary) -> List[str]:
    """The "Speed tests" section: what ran, the speeds against the plan, the delay under load."""
    L = ["", "## Speed tests", f"- **Tests in the window:** {counts_text(s)}"]
    for text in (direction_text("Download", s.down_median, s.down_low, s.plan_down),
                 direction_text("Upload", s.up_median, s.up_low, s.plan_up), method_text(s), under_text(s),
                 latency_text(s)):
        if text:
            L.append(f"- {text}")
    if not s.measured:
        L.append("- No test measured a speed.")
    if not (s.plan_down or s.plan_up):
        L.append("- No plan speed is set in Pengumon (Settings > Your line), so the speeds are not compared with one.")
    return L


def _speed_ticket(s: SpeedSummary) -> str:
    """The ticket's one line about the speed tests."""
    parts = [t for t in (direction_text("download", s.down_median, None, s.plan_down),
                         direction_text("upload", s.up_median, None, s.plan_up), under_text(s)) if t]
    return f"Speed tests ({counts_text(s)}): " + "; ".join(parts) + "."


def render_dossier_markdown(f: DossierFacts) -> str:
    """The dossier text: formatting only, no data access and no decisions beyond wording."""
    hours, primary, docsis_snapshot = f.hours, f.primary, f.docsis_snapshot
    degraded_channels = [c for c, _ in f.degraded]
    rf_evidence, snap_ok = f.rf_evidence, f.snap_ok
    unc_increase, max_lan_loss = f.unc_increase, f.max_lan_loss

    L: List[str] = [
        "# Broadband Packet Loss Evidence Report",
        f"**Generated:** {datetime.fromtimestamp(f.now, f.zone).strftime('%Y-%m-%d %H:%M:%S %Z')} | "
        f"**Observation window:** past {_plural(hours, 'hour')}",
    ]
    if max_lan_loss is None:
        L.append("**Local hardware:** no local or gateway probes in this window")
    else:
        L.append(f"**Local hardware:** {max_lan_loss}% worst loss across local and gateway hops "
                 f"({'below' if max_lan_loss < f.material else 'at or above'} the {f.material}% "
                 f"threshold)")

    L += ["", "## 1. Summary",
          f"- **Recorded incidents:** {f.total_incidents}",
          f"- **Cumulative duration:** {_span(f.total_dur_sec)}"]
    if f.by_cause:
        parts = ", ".join(f"{CAUSE_WORDS.get(c, c)}: {n} ({_span(m * 60)})" for c, n, m in f.by_cause)
        L.append(f"- **By cause:** {parts}")
    if f.under_load:
        L.append(f"- {f.under_load} of these occurred while the line was at {f.busy_pct:g}% load or more.")
    if f.excluded:
        L.append(f"- {_plural(f.excluded, 'event')} during heavy line load {'was' if f.excluded == 1 else 'were'} excluded (expected).")
    if primary:
        L.append(f"- **First hop with elevated loss:** {f.labels[primary['target_id']]} "
                 f"({primary['loss_pct']}% loss).")
    else:
        L.append("- No single failing hop was isolated: no hop's loss exceeds that of the hops before it by "
                 f"{f.material} points or more.")

    L += ["", "## 2. Loss per hop",
          f"Probes are ICMP/DNS requests sent about once per second per target; the table counts every probe "
          f"in the window, including those during heavy line load. Hops are listed from the premises outward.",
          "",
          "| Hop | Target | Sent | Lost | Loss % | Avg RTT | Max RTT | Note |",
          "| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |"]
    for n, h in enumerate(f.hop_stats, 1):  # the position from the premises outward, not the internal target id
        tid = h["target_id"]
        L.append(f"| Hop {n} | {_hop_name(f.names.get(tid, tid))} | {h['sent']} | {h['lost']} | {h['loss_pct']}% | "
                 f"{h['avg_ms']} ms | {h['max_ms']} ms | {hop_note(h, primary, f.ignoring, f.material)} |")
    if primary:
        if f.later_lossy:
            L += ["", f"Loss of {f.material} points or more is also present at later hops "
                      f"({', '.join(f.names.get(h['target_id'], h['target_id']) for h in f.later_lossy)}), so it continues past that point."]
        else:
            L += ["", f"Later hops show no comparable loss. Loss at a single intermediate hop can also be "
                      f"that router rate-limiting or deprioritizing ping, rather than traffic being dropped."]

    L += ["", "## 3. Modem (DOCSIS) telemetry"]
    if docsis_snapshot is None or docsis_snapshot.status == "unconfigured":
        L.append("- No modem data: modem credentials are not configured in Pengumon.")
    elif docsis_snapshot.status == "error":
        L.append("- The modem could not be read at the last poll.")
    else:
        s, lv = docsis_snapshot, f.line_levels or docsis.DEFAULT_LEVELS  # "typical": the configured warning levels
        L += [f"- Downstream average power: {s.downstream_power_avg} dBmV (typical range "
              f"-{lv.ds_power_warning_abs_dbmv:g} to +{lv.ds_power_warning_abs_dbmv:g})",
              f"- Downstream average SNR: {s.downstream_snr_avg} dB (typically above {lv.ds_snr_warning_db:g})",
              f"- Upstream average power: {s.upstream_power_avg} dBmV (typically {lv.us_power_low_warning_dbmv:g} "
              f"to {lv.us_power_warning_dbmv:g})",
              f"- Uncorrectable codewords since modem boot: {s.uncorrectables_total:,}"]
        if f.has_window_snaps:
            L.append(f"- New uncorrectable codewords recorded in the window: {unc_increase:,}")
    if f.modem_unanswered:
        L.append(f"- The modem's status page did not answer in {f.modem_unanswered} of {f.window_readings} readings "
                 f"in the window.")

    if degraded_channels:
        worst = degraded_channels[0]
        L += ["", "### Channels outside typical levels", "",
              "| Direction | Channel | Frequency | Modulation | Power | SNR | Corrected | Uncorrectable | Finding |",
              "| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |"]
        for c, finding in f.degraded:
            freq = f"{c.frequency_mhz:.1f} MHz" if c.frequency_mhz else "-"
            snr = f"{c.snr_db:.1f} dB" if c.snr_db is not None else "-"
            L.append(f"| {c.direction.title()} | `{c.channel_id}` | {freq} | {c.modulation or '-'} | "
                     f"{c.power_dbmv:.1f} dBmV | {snr} | {c.correcteds or 0:,} | {c.uncorrectables or 0:,} | {finding} |")
        L += ["", f"{len(degraded_channels)} of {len(docsis_snapshot.channels)} channels are outside typical levels; "
                  f"the most affected is {worst.direction} channel {worst.channel_id}."]
    elif snap_ok and docsis_snapshot.channels:
        L += ["", "- All bonded channels are within typical power and SNR ranges at the last poll."]

    if f.speed is not None:
        L += _speed_lines(f.speed)

    L += ["", "## 4. What the data shows"]
    if rf_evidence:
        bits = []
        if degraded_channels:
            bits.append(f"{len(degraded_channels)} channel(s) outside typical levels")
        if unc_increase > 0:
            bits.append(f"{unc_increase:,} new uncorrectable codewords in the window")
        L.append(f"- The modem reports RF-level signs: {'; '.join(bits)}. These are consistent with a "
                 f"cable-plant (RF) problem and justify a line check, but the probes alone do not prove the link.")
    elif snap_ok:
        L.append("- The modem shows no RF impairment in the window: no channel outside typical levels and no new "
                 "uncorrectable codewords. Any loss seen above is not explained by the modem's RF data.")
    else:
        L.append("- No modem data is available, so nothing can be said about the RF side.")
    if primary:
        L.append(f"- Loss first appears at {f.labels[primary['target_id']]}{_place_text(f)}")
    else:
        L.append("- The probes did not isolate a single failing hop.")

    L += ["", "## 5. Summary for a support ticket", "```text",
          f"Over the past {_plural(hours, 'hour')} Pengumon recorded {_plural(f.total_incidents, 'incident')} totaling {_span(f.total_dur_sec)}"
          + (f" ({_plural(f.excluded, 'further event')} during heavy line load {'was' if f.excluded == 1 else 'were'} excluded as expected)." if f.excluded else "."),
          ]
    if max_lan_loss is not None:
        L.append(f"Loss to {f.local_name} and {f.gw_name}: at most {max_lan_loss}%.")
    if primary:
        L.append(f"Loss is first elevated at {f.labels[primary['target_id']]}: {primary['loss_pct']}%."
                 + _TICKET_PLACE.get(f.primary_place, ""))
    else:
        L.append("No single failing hop could be isolated from the probe data.")
    if f.speed is not None and f.speed.measured:
        L.append(_speed_ticket(f.speed))
    if rf_evidence:
        if degraded_channels:
            w = degraded_channels[0]
            freq = f" at {w.frequency_mhz:.1f} MHz" if w.frequency_mhz else ""
            L.append(f"Modem: {w.direction} channel {w.channel_id}{freq} is outside typical levels "
                     f"({len(degraded_channels)} channel(s) in total).")
        if unc_increase > 0:
            L.append(f"Modem: {unc_increase:,} new uncorrectable codewords in the window.")
        if f.primary_place == "gateway":
            L.append("Request: please check the gateway, which stopped answering on its home side; the modem's RF "
                     "readings also justify a line check (tap, drop, connectors).")
        else:
            L.append("Request: please check the line (tap, drop, connectors) for RF impairment.")
    elif f.primary_place == "gateway":
        L.append("Request: if you supplied this gateway, please check it; it stopped answering on its home side.")
    L += ["```"]

    return "\n".join(L)


async def build_dossier(repository, config, hours: int, now: Optional[float] = None, line=None,
                        ignoring: Iterable[str] = (), tz_name: Optional[str] = None,
                        tz_offset_minutes: int = 0) -> Dict[str, Any]:
    """`tz_name` / `tz_offset_minutes`: the viewer's zone, the fallbacks of timeofday.viewer_zone (Settings wins)."""
    now = now or time.time()
    facts = await collect_dossier_facts(repository, config, hours, now, line, ignoring,
                                        zone=viewer_zone(config, tz_name, tz_offset_minutes)[0])
    return {
        "observation_hours": hours,
        "total_incidents": facts.total_incidents,
        "total_duration_seconds": facts.total_dur_sec,
        "total_duration_minutes": facts.total_dur_min,
        "excluded_expected": facts.excluded,
        "primary_failing_hop": facts.primary["target_id"] if facts.primary else None,
        "hop_stats": facts.hop_stats,
        "docsis": facts.docsis_snapshot,
        "formatted_markdown": render_dossier_markdown(facts),
    }
