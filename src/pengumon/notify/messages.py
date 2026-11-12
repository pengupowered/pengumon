"""The texts of the alerts: what each announcement says, built from the incident, the outage or the service it is about.

The Notifier (notifier.py) decides whether and when an alert goes out and calls these when its time has come; nothing
here sends or schedules anything."""

from typing import Any, Dict, Optional, Sequence

from pengumon.incidents.model import Incident
from pengumon.incidents.outages import drop_of
from pengumon.notify.policy import AlertPolicy, Timing
from pengumon.notify.webhook import Alert


def duration_text(seconds: Optional[float]) -> str:
    seconds = seconds or 0
    if seconds < 90:
        return f"{seconds:.0f} s"
    return f"{seconds / 60:.1f} min"


def ongoing_title(policy: AlertPolicy, title: str, timing: Timing, since: float) -> str:
    """An alert that quiet hours held says since when it has been going on."""
    if not timing.held:
        return title
    return title.replace("Pengumon: ", f"Pengumon: Ongoing since {policy.clock_text(since)}: ", 1)


def _alone(incident: Incident) -> Dict[str, Any]:
    """The outage fields of an alert about an incident that is its own outage (grouping off)."""
    return {"outage_id": incident.id, "drops": 1} if drop_of(incident) is not None else {}


def incident_started(policy: AlertPolicy, incident: Incident, timing: Timing) -> Alert:
    load = f"\nLine load: {incident.load_pct:.0f}%" if incident.load_pct is not None else ""
    return Alert(
        event="incident_started", severity=incident.severity,
        title=ongoing_title(policy, f"Pengumon: {incident.title}", timing, incident.start_time),
        message=f"Cause: {incident.title}\n{incident.description}{load}",
        priority="high" if incident.severity == "critical" else "default",
        tags="rotating_light",
        incident_id=incident.id, cause=incident.cause_code, started_at=incident.start_time,
        load_pct=incident.load_pct, **_alone(incident),
    )


def outage_started(policy: AlertPolicy, first: Incident, severity: str, drops: int, timing: Timing,
                   outage_id: int) -> Alert:
    load = f"\nLine load: {first.load_pct:.0f}%" if first.load_pct is not None else ""
    more = f"\nDrops so far: {drops}" if drops > 1 else ""
    return Alert(
        event="incident_started", severity=severity,
        title=ongoing_title(policy, f"Pengumon: {first.title}", timing, first.start_time),
        message=f"Cause: {first.title}\n{first.description}{load}{more}",
        priority="high" if severity == "critical" else "default",
        tags="rotating_light",
        incident_id=first.id, cause=first.cause_code, started_at=first.start_time, load_pct=first.load_pct,
        outage_id=outage_id, drops=drops,
    )


def incident_resolved(incident: Incident) -> Alert:
    load = f"\nPeak line load: {incident.load_pct:.0f}%" if incident.load_pct is not None else ""
    return Alert(
        event="incident_resolved", severity="info",
        title=f"Pengumon: resolved, {incident.title}",
        message=f"Cause: {incident.title}\nDuration: {duration_text(incident.duration_seconds)}{load}",
        priority="default",
        tags="white_check_mark",
        incident_id=incident.id, cause=incident.cause_code, started_at=incident.start_time,
        ended_at=incident.end_time, duration_seconds=incident.duration_seconds, load_pct=incident.load_pct,
        **_alone(incident),
    )


def outage_resolved(policy: AlertPolicy, summary: Dict[str, Any], outage_id: int, load_pct: Optional[float]) -> Alert:
    """`summary` is OutageBuilder.summary: the span, the drops and the downtime of the whole outage."""
    zone_text = policy.clock_text
    span = f"{zone_text(summary['start_time'])}-{zone_text(summary['end_time'])} ({duration_text(summary['duration_seconds'])})"
    lines = [f"Cause: {summary['title']}", f"Span: {span}"]
    if summary["drops"] > 1:
        lines.append(f"Drops: {summary['drops']} ({summary['critical_drops']} critical)")
        lines.append(f"Down: {duration_text(summary['down_seconds'])}")
    if summary["likely_maintenance"]:
        lines.append("Likely maintenance (a guess from the hour: provider hops, in the maintenance hours). "
                     "The downtime counts in full.")
    if load_pct is not None:
        lines.append(f"Peak line load: {load_pct:.0f}%")
    return Alert(
        event="incident_resolved", severity="info",
        title=f"Pengumon: resolved, {summary['title']}",
        message="\n".join(lines),
        priority="default",
        tags="white_check_mark",
        incident_id=outage_id, cause=summary["cause_code"], started_at=summary["start_time"],
        ended_at=summary["end_time"], duration_seconds=summary["duration_seconds"], load_pct=load_pct,
        outage_id=outage_id, drops=summary["drops"],
    )


def service_down(policy: AlertPolicy, service, error, timing: Timing, created: float, seconds_down: float,
                 down_for: float) -> Alert:
    return Alert(
        event="service_down", severity="warning",
        title=ongoing_title(policy, f"Pengumon: {service.name} not responding", timing, created - seconds_down),
        message=f"{service.name} not responding: {error.message}\nDown for {duration_text(down_for)}",
        priority="default",
        tags="warning",
        started_at=created - seconds_down, duration_seconds=down_for, service_id=service.id,
    )


def service_up(service, seconds_down: float, now: float) -> Alert:
    return Alert(
        event="service_up", severity="info",
        title=f"Pengumon: {service.name} is back",
        message=f"{service.name} is responding again after {duration_text(seconds_down)}",
        priority="default",
        tags="white_check_mark",
        started_at=now - seconds_down, ended_at=now, duration_seconds=seconds_down, service_id=service.id,
    )


def path_changed(changes: Sequence[str], created: float) -> Alert:
    lines = list(changes[:6]) + ([f"...and {len(changes) - 6} more"] if len(changes) > 6 else [])
    return Alert(
        event="path_changed", severity="info",
        title="Pengumon: your network path changed",
        message="\n".join(lines) + "\nReview it on Path health (Your path changed · Review).",
        priority="default",
        tags="world_map",
        started_at=created,
        page="#live",
    )


def speed_slow(down_mbps: Optional[float], up_mbps: Optional[float], plan_down: Optional[float],
               plan_up: Optional[float], alert_pct: float, measured_by: Optional[str], note: Optional[str],
               created: float) -> Alert:
    def line(label: str, value: Optional[float], plan: Optional[float]) -> Optional[str]:
        if value is None or not plan:
            return None
        share = value / plan * 100.0
        return f"{label}: {value:.0f} of {plan:.0f} Mbps ({share:.0f} %)" + (" - under the alert level" if share < alert_pct else "")

    lines = [x for x in (line("Download", down_mbps, plan_down), line("Upload", up_mbps, plan_up)) if x]
    if measured_by == "router":
        lines.append("Measured by your router: what the line carried during the test.")
    elif measured_by == "test":
        lines.append("Measured by the test alone (one server; your router gave no reading of the line).")
    if note:
        lines.append(note)
    return Alert(
        event="speed_slow", severity="info",
        title="Pengumon: your line measured slow",
        message="\n".join(lines) + f"\nThe alert level is {alert_pct:.0f} % of your plan speed.",
        priority="default",
        tags="turtle",
        started_at=created,
    )
