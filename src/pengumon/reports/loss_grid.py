"""The loss grid ("when it breaks"): probe loss per calendar day and hour."""

import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, Optional

from pengumon.reports.queries import loss_grid_rows


async def build_loss_grid(repository, days: int = 7, now: Optional[float] = None,
                          tz_offset_minutes: int = 0, ignoring: Iterable[str] = ()) -> Dict[str, Any]:
    """Probe loss per calendar day and hour for the last `days` days ("when it breaks"), all targets together but the
    hops that ignore ping (`ignoring`, engine.path_rules: their unanswered pings are no loss), from the rollups. `days` lists the dates oldest first (today last, in the time zone tz_offset_minutes east of UTC), `weekdays`
    their day of the week (0 Monday); `sent`, `lost` and `loss_pct` are one row of 24 hours per day. An hour with no
    probes (before the monitor ran, or not yet happened) is null in loss_pct and 0 in sent."""
    now = now or time.time()
    local_now = datetime.fromtimestamp(now, timezone.utc) + timedelta(minutes=tz_offset_minutes)
    dates = [(local_now - timedelta(days=days - 1 - i)).date() for i in range(days)]
    first_midnight = datetime(dates[0].year, dates[0].month, dates[0].day, tzinfo=timezone.utc)
    start_time = first_midnight.timestamp() - tz_offset_minutes * 60
    names = [d.isoformat() for d in dates]
    row_of = {name: i for i, name in enumerate(names)}
    sent = [[0] * 24 for _ in names]
    lost = [[0] * 24 for _ in names]
    for r in await loss_grid_rows(repository, start_time, tz_offset_minutes, exclude=ignoring):
        i = row_of.get(r["day"])
        if i is not None and 0 <= r["hour"] < 24:
            sent[i][r["hour"]], lost[i][r["hour"]] = r["sent"], r["lost"]
    return {
        "days": names,
        "weekdays": [d.weekday() for d in dates],
        "tz_offset_minutes": tz_offset_minutes,
        "generated_at": now,
        "sent": sent,
        "lost": lost,
        "loss_pct": [[round(l * 100.0 / s, 3) if s else None for s, l in zip(srow, lrow)]
                     for srow, lrow in zip(sent, lost)],
    }
