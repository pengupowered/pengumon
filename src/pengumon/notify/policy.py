"""When an alert may go out (the alerts rules, docs/webhooks.md). Pure: no I/O and no clock of its own; every method
is told the time, so the Notifier and the replay preview use the same code and tests drive it with a fake clock.

Network incidents: the severity filter (`min_severity`), the delay (`delay_seconds`), quiet hours and the repeat
window. Host-app and route-change alerts: quiet hours only. The test alert: never asked.
"""

from dataclasses import dataclass
from typing import Optional

from pengumon.notify.settings import AlertsConfig
from pengumon.timeofday import clock_text, window_end, zone

SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}


@dataclass(frozen=True)
class Timing:
    """`at`: the wall time to send (epoch seconds); `held`: quiet hours moved it from `moved_from`."""
    at: float
    held: bool = False
    moved_from: Optional[float] = None


class AlertPolicy:
    def __init__(self, alerts: Optional[AlertsConfig] = None, timezone: str = "UTC"):
        alerts = alerts or AlertsConfig()
        self.min_severity = alerts.min_severity
        self.delay_seconds = float(alerts.delay_seconds)
        self.repeat_window_seconds = float(alerts.repeat_window_seconds)
        self.quiet = alerts.quiet_hours.model_copy()
        self.timezone = timezone
        self._tz = zone(timezone)

    def passes_severity(self, severity: str) -> bool:
        """Whether a network incident of this severity is announced at all."""
        return SEVERITY_RANK.get(severity, 0) >= SEVERITY_RANK[self.min_severity]

    def exempt(self, severity: Optional[str]) -> bool:
        """A critical alert is not held while `critical_still` is on."""
        return bool(self.quiet.critical_still and severity == "critical")

    def incident_time(self, start_time: float, severity: str) -> Timing:
        """When a network incident that began at `start_time` is announced: after the delay, and not inside quiet
        hours unless it is exempt."""
        return self.quiet_time(start_time + self.delay_seconds, severity)

    def quiet_time(self, at: float, severity: Optional[str] = None) -> Timing:
        """`at`, or the end of the quiet hours when `at` falls in them and the alert is not exempt."""
        if not self.quiet.enabled or self.exempt(severity):
            return Timing(at)
        end = window_end(at, self.quiet.start, self.quiet.end, self._tz)
        return Timing(at) if end is None else Timing(end, True, at)

    def repeat_allowed(self, last_sent: Optional[float], now: float) -> bool:
        """Whether an incident of the same cause may be announced again (`last_sent` and `now` on one clock)."""
        return last_sent is None or now - last_sent >= self.repeat_window_seconds

    def clock_text(self, epoch: float) -> str:
        """"03:12" in the configured zone, for "Ongoing since 03:12"."""
        return clock_text(epoch, self._tz)
