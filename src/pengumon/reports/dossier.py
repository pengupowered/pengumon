"""The evidence dossier: facts decided from the stored data, then rendered as Markdown.

Every sentence in the dossier follows from the data in the window: loss figures come from the per-minute
rollups, RF wording appears only when DOCSIS evidence exists, and the failing hop is named only when its loss
stands out from the hops before it."""

import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

from pengumon.lines.docsis import channel_finding, is_degraded as _is_degraded, uncorrectable_increase as _uncorrectable_increase
from pengumon.incidents.taxonomy import current_cause
from pengumon.lines.docsis_model import DocsisChannel, DocsisSignalSnapshot
from pengumon.reports.common import (
    LOSS_MATERIAL_PCT, _canonical_order, _hop_label, _split_incidents, _targets, find_primary_failing_hop,
)


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
    by_cause: List[Tuple[str, int, float]]     # (cause, count, minutes), in render order (most frequent first)
    hop_stats: List[Dict[str, Any]]            # canonical order
    primary: Optional[Dict[str, Any]]          # first hop with elevated loss
    later_lossy: List[Dict[str, Any]]          # hops after `primary` that also lose >= LOSS_MATERIAL_PCT
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


def hop_note(h: Dict[str, Any], primary: Optional[Dict[str, Any]], ignoring=frozenset()) -> str:
    if h["target_id"] in ignoring:
        return "Ignores ping (later hops answer, so traffic passes); not counted"
    if primary and h["target_id"] == primary["target_id"]:
        return "First hop with elevated loss"
    if h["loss_pct"] >= LOSS_MATERIAL_PCT:
        return "Elevated loss"
    return "No material loss"


async def collect_dossier_facts(repository, config, hours: int, now: float, line=None,
                                ignoring: Iterable[str] = ()) -> DossierFacts:
    """`line` is the line device's DocsisHistory (pengumon.lines.docsis); without one the dossier has no modem
    readings. `ignoring`: the hops that ignore ping now (engine.path_rules); their loss counts in no finding."""
    ignoring = frozenset(ignoring)
    start_time = now - hours * 3600

    rows = await repository.get_incidents_since(start_time)
    incidents, excluded = _split_incidents(rows)
    total_incidents = len(incidents)
    total_dur_sec = sum(r["duration_seconds"] or 0 for r in incidents)
    total_dur_min = round(total_dur_sec / 60.0, 1)
    busy_pct = getattr(getattr(config, "classifier", None), "busy_load_pct", 60.0)
    under_load = sum(1 for r in incidents if (r.get("load_pct") or 0) >= busy_pct)
    by_cause: Dict[str, List[float]] = defaultdict(lambda: [0, 0.0])
    for r in incidents:
        by_cause[current_cause(r["cause_code"])][0] += 1
        by_cause[current_cause(r["cause_code"])][1] += (r["duration_seconds"] or 0) / 60.0
    causes = [(c, n, m) for c, (n, m) in sorted(by_cause.items(), key=lambda kv: -kv[1][0])]

    cfg_targets = _targets(config)
    names = {t.id: t.name for t in cfg_targets}
    addresses = {t.id: getattr(t, "address", "") or "" for t in cfg_targets}
    categories = {t.id: getattr(t, "category", None) for t in cfg_targets}
    order = _canonical_order(cfg_targets)

    hop_stats = await repository.get_hop_stats(start_time)
    hop_stats.sort(key=lambda h: order.index(h["target_id"]) if h["target_id"] in order else 999)
    primary = find_primary_failing_hop(hop_stats, ignoring)
    later_lossy = []
    if primary:
        later_lossy = [h for h in hop_stats[hop_stats.index(primary) + 1:]
                       if h["sent"] and h["loss_pct"] >= LOSS_MATERIAL_PCT and h["target_id"] not in ignoring]
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
    degraded_channels = []
    if snap_ok:
        degraded_channels = sorted(
            (c for c in docsis_snapshot.channels if _is_degraded(c)),
            key=lambda c: (c.uncorrectables_recent or 0, c.uncorrectables or 0,
                           -(c.snr_db if c.snr_db is not None else 99.0)),
            reverse=True,
        )
    rf_evidence = bool(degraded_channels or unc_increase > 0)

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
        primary=primary,
        later_lossy=later_lossy,
        labels=labels,
        names=names,
        max_lan_loss=max_lan_loss,
        docsis_snapshot=docsis_snapshot,
        snap_ok=snap_ok,
        has_window_snaps=bool(window_snaps),
        unc_increase=unc_increase,
        degraded=[(c, channel_finding(c)) for c in degraded_channels],
        rf_evidence=rf_evidence,
        local_name=next((t.name for t in cfg_targets if categories.get(t.id) == "local"), "the local router"),
        gw_name=next((t.name for t in cfg_targets if categories.get(t.id) == "gateway"), "the gateway"),
    )


def render_dossier_markdown(f: DossierFacts) -> str:
    """The dossier text: formatting only, no data access and no decisions beyond wording."""
    hours, primary, docsis_snapshot = f.hours, f.primary, f.docsis_snapshot
    degraded_channels = [c for c, _ in f.degraded]
    rf_evidence, snap_ok = f.rf_evidence, f.snap_ok
    unc_increase, max_lan_loss = f.unc_increase, f.max_lan_loss

    L: List[str] = [
        "# Broadband Packet Loss Evidence Report",
        f"**Generated:** {datetime.fromtimestamp(f.now).strftime('%Y-%m-%d %H:%M:%S')} | "
        f"**Observation window:** past {hours} hours",
    ]
    if max_lan_loss is None:
        L.append("**Local hardware:** no local or gateway probes in this window")
    else:
        L.append(f"**Local hardware:** {max_lan_loss}% worst loss across local and gateway hops "
                 f"({'below' if max_lan_loss < LOSS_MATERIAL_PCT else 'at or above'} the {LOSS_MATERIAL_PCT}% "
                 f"threshold)")

    L += ["", "## 1. Summary",
          f"- **Recorded incidents:** {f.total_incidents}",
          f"- **Cumulative duration:** {f.total_dur_min} minutes ({round(f.total_dur_sec)} seconds)"]
    if f.by_cause:
        parts = ", ".join(f"{c}: {n} ({m:.1f} min)" for c, n, m in f.by_cause)
        L.append(f"- **By cause:** {parts}")
    if f.under_load:
        L.append(f"- {f.under_load} of these occurred while the line was at {f.busy_pct:g}% load or more.")
    if f.excluded:
        L.append(f"- {f.excluded} events during heavy line load were excluded (expected).")
    if primary:
        L.append(f"- **First hop with elevated loss:** {f.labels[primary['target_id']]} "
                 f"({primary['loss_pct']}% loss).")
    else:
        L.append("- No single failing hop was isolated: no hop's loss exceeds that of the hops before it by "
                 f"{LOSS_MATERIAL_PCT} points or more.")

    L += ["", "## 2. Loss per hop",
          f"Probes are ICMP/DNS requests sent about once per second per target; the table counts every probe "
          f"in the window, including those during heavy line load. Hops are listed from the premises outward.",
          "",
          "| Hop | Target | Sent | Lost | Loss % | Avg RTT | Max RTT | Note |",
          "| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |"]
    for h in f.hop_stats:
        tid = h["target_id"]
        L.append(f"| `{tid}` | {f.names.get(tid, tid)} | {h['sent']} | {h['lost']} | {h['loss_pct']}% | "
                 f"{h['avg_ms']} ms | {h['max_ms']} ms | {hop_note(h, primary, f.ignoring)} |")
    if primary:
        if f.later_lossy:
            L += ["", f"Loss of {LOSS_MATERIAL_PCT} points or more is also present at later hops "
                      f"({', '.join(h['target_id'] for h in f.later_lossy)}), so it continues past that point."]
        else:
            L += ["", f"Later hops show no comparable loss. Loss at a single intermediate hop can also be "
                      f"that router rate-limiting or deprioritising ping, rather than traffic being dropped."]

    L += ["", "## 3. Modem (DOCSIS) telemetry"]
    if docsis_snapshot is None or docsis_snapshot.status == "unconfigured":
        L.append("- No modem data: modem credentials are not configured in Pengumon.")
    elif docsis_snapshot.status == "error":
        L.append("- The modem could not be read at the last poll.")
    else:
        s = docsis_snapshot
        L += [f"- Downstream average power: {s.downstream_power_avg} dBmV (typical range -7 to +7)",
              f"- Downstream average SNR: {s.downstream_snr_avg} dB (typically above 33)",
              f"- Upstream average power: {s.upstream_power_avg} dBmV (typically 38 to 48)",
              f"- Uncorrectable codewords since modem boot: {s.uncorrectables_total:,}"]
        if f.has_window_snaps:
            L.append(f"- New uncorrectable codewords recorded in the window: {unc_increase:,}")

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
        L.append(f"- Loss first appears at {f.labels[primary['target_id']]}.")
    else:
        L.append("- The probes did not isolate a single failing hop.")

    L += ["", "## 5. Summary for a support ticket", "```text",
          f"Over the past {hours} hours Pengumon recorded {f.total_incidents} incidents totalling {f.total_dur_min} minutes"
          + (f" ({f.excluded} further events during heavy line load were excluded as expected)." if f.excluded else "."),
          ]
    if max_lan_loss is not None:
        L.append(f"Loss to {f.local_name} and {f.gw_name}: at most {max_lan_loss}%.")
    if primary:
        L.append(f"Loss is first elevated at {f.labels[primary['target_id']]}: {primary['loss_pct']}%.")
    else:
        L.append("No single failing hop could be isolated from the probe data.")
    if rf_evidence:
        if degraded_channels:
            w = degraded_channels[0]
            freq = f" at {w.frequency_mhz:.1f} MHz" if w.frequency_mhz else ""
            L.append(f"Modem: {w.direction} channel {w.channel_id}{freq} is outside typical levels "
                     f"({len(degraded_channels)} channel(s) in total).")
        if unc_increase > 0:
            L.append(f"Modem: {unc_increase:,} new uncorrectable codewords in the window.")
        L.append("Request: please check the line (tap, drop, connectors) for RF impairment.")
    L += ["```"]

    return "\n".join(L)


async def build_dossier(repository, config, hours: int, now: Optional[float] = None, line=None,
                        ignoring: Iterable[str] = ()) -> Dict[str, Any]:
    now = now or time.time()
    facts = await collect_dossier_facts(repository, config, hours, now, line, ignoring)
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
