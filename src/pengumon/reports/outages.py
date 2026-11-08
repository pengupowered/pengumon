"""Outages in the reports: the incident rows of a window, grouped by the same rule as the API (incidents.outages)."""

from datetime import datetime, tzinfo
from typing import Any, Dict, Iterable, List

from pengumon.incidents.outages import Rules, group_outages


def outages_of(rows: Iterable[Dict[str, Any]], config, now: float) -> List[Dict[str, Any]]:
    """The outage summaries (incidents.outages.OutageBuilder.summary) of the window's incident rows, oldest first.
    The caller passes the real network rows (reports.common._split_incidents)."""
    return group_outages(rows, Rules.from_config(config), now)


def outage_totals(outages: List[Dict[str, Any]]) -> Dict[str, Any]:
    """{count, down_minutes, longest_minutes, maintenance}: how many outages, the minutes the line was down in them
    (critical drops, overlaps once), the longest one's minutes (first drop to last), and how many are likely maintenance."""
    return {
        "count": len(outages),
        "down_minutes": round(sum(o["down_seconds"] for o in outages) / 60.0, 1),
        "longest_minutes": round(max((o["duration_seconds"] for o in outages), default=0.0) / 60.0, 1),
        "maintenance": sum(1 for o in outages if o["likely_maintenance"]),
    }


def outage_line(o: Dict[str, Any], zone: tzinfo, now: float) -> str:
    """"Nov 8 03:12-03:37, 25 min, 7 drops, down 14 min, likely maintenance" (the label only when set; the end
    carries its date when it is not the start's day; an open outage says it is ongoing)."""
    start = datetime.fromtimestamp(o["start_time"], zone)
    text = f"{start.strftime('%b')} {start.day} {start.strftime('%H:%M')}"
    if o["end_time"] is None:
        text += " (ongoing)"
    else:
        end = datetime.fromtimestamp(o["end_time"], zone)
        text += "-" + (end.strftime("%H:%M") if end.date() == start.date()
                       else f"{end.strftime('%b')} {end.day} {end.strftime('%H:%M')}")
    parts = [text, f"{round(o['duration_seconds'] / 60.0)} min", f"{o['drops']} drops",
             f"down {round(o['down_seconds'] / 60.0)} min"]
    if o["likely_maintenance"]:
        parts.append("likely maintenance")
    return ", ".join(parts)


def gap_minutes(config) -> int:
    """incidents.group_gap_minutes of the config (0: grouping is off)."""
    return int(Rules.from_config(config).gap_seconds // 60)
