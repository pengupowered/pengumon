"""The AI pack's view: reports.ai_findings.PackFacts (what the core decided from the stored data) turned into the
PackView that reports.ai_pack renders, with the sentences Pengumon writes about the facts (the core's bullets, a
module's bullets, the events that fell near an incident). Pure, like the renderer; the dataclasses and the small
formatters are the renderer's, in reports.ai_pack, which also answers for the names here (view_of, module_findings,
speed_fact) when asked for them."""

import re
from datetime import tzinfo
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from pengumon.reports.ai_pack import (
    AppFact, ChangeFact, CloseUpFact, DayFact, HopFact, IncidentFact, ModuleInput, OutageFact, PackScope, PackView,
    SpeedFact, SpeedTestFact, _count, _dur, _num, _span_words, gap_words,
)
from pengumon.reports.speed import summarize
from pengumon.speedtest.quality import headline

_SHAPE_WORDS = (("carried", "loss starts at one hop and carries on to the later hops"),
                ("one_hop", "loss at one hop only (usually ping rate-limiting, harmless)"),
                ("none", "no hop isolated by loss"))


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
        out += _pattern_bullets(f, p)
    else:
        out.append("**Incidents.** No network incident" + (" that is not expected" if f.expected_count else "")
                   + f" in this window{expected}{apps}.")
    out += _outcome_bullets(f, p)
    return out


def _pattern_bullets(f, p) -> List[str]:
    """The bullets about the pattern of the incidents: where they start, when in the day, how they recur."""
    shapes = "; ".join(f"{p.shapes[k]}: {w}" + (_latency_starts(f) if k == "none" else "")
                       for k, w in _SHAPE_WORDS if p.shapes.get(k))
    firsts = ", ".join(f"{{hop:{t}}} ({n})" for t, n in p.first_hops)
    top = p.first_hops[0][1] if p.first_hops else 0
    clear = top >= 2 and (len(p.first_hops) == 1 or p.first_hops[1][1] < top)
    where = (f" Most often at {firsts}." if clear else f" Loss started at {firsts}.") if firsts else ""
    out = [f"**Where problems start.** {shapes}.{where}"]
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
    return out


def _outcome_bullets(f, p) -> List[str]:
    """The bullets about what the incidents coincided with, Pengumon's verdicts, each hop against its normal, the first
    hop with loss and the gaps in the data."""
    out: List[str] = []
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
        out.append(f"**No data.** {_count(len(f.no_data), 'gap', 'gaps')} totaling "
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
    """The core's bullets for one module's section: how its events fell against the incidents (with no incident in
    the window, how the events of a kind marked by_time_of_day fell over the day), and the Internet's latency while
    the line was busy (an event that supports "load"). `codes`: the module's event codes."""
    out = []
    for code in codes:
        es = (getattr(f, "event_summaries", None) or {}).get(code)
        if es is None:
            continue
        blocks = sorted((b for b in getattr(es, "blocks", None) or () if b[2]), key=lambda b: (-b[2], b[0]))
        if blocks and not getattr(f, "incidents", None):
            out.append(f"{es.label[:1].upper()}{es.label[1:]}: " + ", ".join(
                (f"{n} of {es.count}" if k == 0 else str(n)) + f" between {a:02d}:00 and {b % 24:02d}:00"
                for k, (a, b, n) in enumerate(blocks)) + ".")
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


def _hop_facts_of(f, hops_identity: Mapping[str, Any]) -> tuple:
    """The hops of the view: the stored statistics with each hop's identity (name, address, reverse name)."""
    categories = getattr(f, "categories", {}) or {}
    servers = {getattr(v, "address", "") for k, v in hops_identity.items() if categories.get(k) == "dns"} - {""}
    hops = []
    for h in f.hops:
        ident = hops_identity.get(h.target_id)
        b, w = h.baseline, h.window
        address, rdns = getattr(ident, "address", "") or "", getattr(ident, "rdns", "") or ""
        hops.append(HopFact(h.target_id, h.category, getattr(ident, "name", "") or "", address,
                            rdns=rdns, ignores_ping=h.ignores_ping, probe=getattr(ident, "probe", "") or "",
                            normal_ms=b.normal_ms,
                            p90_ms=b.p90_ms, jitter_ms=b.jitter_ms, loss7_pct=b.loss_pct, window_ms=w.median_ms,
                            window_loss_pct=w.loss_pct, slow_minutes=w.slow_minutes, lossy_minutes=w.lossy_minutes,
                            note="thin history" if b.thin else "",
                            dns_server=h.category != "dns" and (address in servers or _dns_name(rdns)
                                                                or _dns_label(getattr(ident, "name", "") or ""))))
    return tuple(hops)


def _incident_facts_of(f) -> tuple:
    """The listed incidents of the view."""
    listed = set(f.listed)
    return tuple(IncidentFact(i.id, i.start, i.duration, i.lost_for, i.shape.first_hop, i.shape.kind,
                              i.kind or "unclassified", i.cause, i.confidence, i.expected, i.score,
                              _phrases(i.events), i.title, confidence_reason=i.confidence_reason,
                              latency_start=i.latency.first_hop if getattr(i, "latency", None) else None,
                              latency_shape=i.latency.kind if getattr(i, "latency", None) else "",
                              latency_normal=tuple(getattr(i, "latency_normal", ()) or ()))
                 for i in f.incidents if i.id in listed)


def _closeup_facts_of(f) -> tuple:
    """The close-ups of the view: the minutes around the incidents as cells."""
    by_id = {i.id: i for i in f.incidents}
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
    return tuple(closeups)


def _time_table_of(f) -> Optional[tuple]:
    """The time-of-day table (blocks of the day by key hop), None without a profile."""
    time_table = None
    if f.profile:
        cols = [p_["target_id"] for p_ in f.profile]
        rows = []
        for k, blk in enumerate(f.profile[0]["blocks"]):
            a, b_ = blk["hours"]
            rows.append((f"{a:02d}:00-{b_ % 24:02d}:00",) + tuple(
                f"{_num(p_['blocks'][k]['median_ms'])} ms, {_num(p_['blocks'][k]['loss_pct'], 2)} %" for p_ in f.profile))
        time_table = (("Hours",) + tuple(f"{{hop:{c}}}" for c in cols), tuple(rows))
    return time_table


def view_of(f, hops_identity: Mapping[str, Any], modules: Sequence[ModuleInput], provider: str, tz: tzinfo,
            label: str, version: str, *, apps: Sequence[AppFact] = (), environment: Sequence[str] = (),
            provider_source: str = "", zone: str = "", zone_kind: str = "",
            speed: Optional[SpeedFact] = None) -> PackView:
    """reports.ai_findings.PackFacts -> PackView. `hops_identity`: target id -> object with name, address, rdns.
    `apps` and `environment` (sentences, tokens allowed) are what the caller knows beyond the stored facts."""
    kinds = {"path": "The path changed", "settings": "Settings changed", "route": "The route changed"}
    return PackView(
        scope=PackScope(f.scope, label, f.start, f.end, tz, zone, zone_kind), version=version, provider=provider,
        hops=_hop_facts_of(f, hops_identity),
        incidents=_incident_facts_of(f), closeups=_closeup_facts_of(f), findings=tuple(_findings(f)),
        time_table=_time_table_of(f),
        days=tuple(DayFact(d["day"], d["incidents"], d["lost_minutes"]) for d in f.per_day), apps=tuple(apps),
        changes=tuple(ChangeFact(c["at"], kinds.get(c["kind"], c["kind"]), c["kind"]) for c in f.changes),
        environment=tuple(environment), modules=tuple(modules), incident_total=len(f.incidents),
        provider_source=provider_source, last_incident_end=max((i.end for i in f.incidents), default=None),
        speed=speed,
        outages=tuple(OutageFact(o["id"], o["start_time"], o["duration_seconds"], o["down_seconds"], o["drops"],
                                 o["critical_drops"], tuple(o["places"]), o["likely_maintenance"],
                                 tuple(o["incident_ids"]), o["ongoing"]) for o in getattr(f, "outages", ())),
        outage_gap_minutes=getattr(f, "outage_gap_minutes", 0))


def speed_fact(rows: Sequence[Mapping[str, Any]], plan_down: Optional[float], plan_up: Optional[float]) -> Optional[SpeedFact]:
    """The stored speed tests of a window (any order) -> what the pack shows; None without any."""
    summary = summarize(rows, plan_down, plan_up)
    if summary is None:
        return None
    ran = sorted((r for r in rows if r["status"] != "skipped"), key=lambda r: r["ts"])
    best = [(r, headline(r)) for r in ran]
    return SpeedFact(summary, tuple(
        SpeedTestFact(r["ts"], r["status"], r["provider"], r.get("server") or "", h["down_mbps"], h["up_mbps"],
                      r.get("idle_ms"), r.get("loaded_down_ms"), r.get("loaded_up_ms"), r.get("reason") or "",
                      h["measured_by"] or "")
        for r, h in best))


def _dns_name(rdns: str) -> bool:
    """A reverse name that says it is a name server ("cdns01.example-isp.net", "resolver1.example.net")."""
    first = (rdns or "").lower().split(".")[0]
    return bool(re.fullmatch(r"[a-z]*dns\d*[a-z]?|resolver\d*|ns\d*", first)) if first else False


def _dns_label(name: str) -> bool:
    """A hop's own name that says it is a name server ("Provider DNS", "Public resolver 1")."""
    return bool(re.search(r"\b(?:dns|resolver)s?\b", name, re.I))
