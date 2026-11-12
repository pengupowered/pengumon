"""Device events: what happened to a router that its readings show, kept in `device_events` (schema v19).

* `restarted`: its uptime went back, so it started again (`boot_at`, the time it started; `previous_boot_at`). The
  boot time is compared, not the uptime, so a restart while Pengumon was not running is found at its next reading;
  the uptime of a device read every minute moves by a few seconds against the clock, which BOOT_SLACK_SECONDS absorbs.
  An uptime outside 0..MAX_UPTIME_SECONDS is ignored (a bad number, not a boot time). For a device whose uptime counter
  wraps (SystemReading.uptime_wrap_s: SNMP sysUpTime every ~497 days), a boot time that moved on by whole wraps (within
  BOOT_SLACK_SECONDS) is the counter wrapping, not a restart.
* `wan_link`: the WAN port's link speed changed, went down (0) or came back (`old`, `new`, in Mb/s).

They are not incident kinds: a restart already shows as an outage, and a second incident would double the alert. They
appear where an incident or a window is explained: the incident inspector, the dossier, the AI pack and
GET /api/devices/{id}/events. Their readers take a stored row as it is: a `detail` value that is not what it should be
(an older version's, a hand-edited row) gives the plain words ("restarted", "unknown"), never an error.
"""

from dataclasses import dataclass
from datetime import datetime, tzinfo
from typing import Any, Dict, List, Optional, Tuple

from pengumon.capabilities import valid_uptime

RESTARTED = "restarted"
WAN_LINK = "wan_link"
KINDS = (RESTARTED, WAN_LINK)
BOOT_SLACK_SECONDS = 120.0
# Events this far outside an incident still belong to it (a WAN link that dropped a minute before the probes noticed).
EVENT_REACH_SECONDS = 300.0


@dataclass(frozen=True)
class DeviceEvent:
    device_id: str
    ts: float
    kind: str
    detail: Dict[str, Any]

    def as_dict(self, tz: Optional[tzinfo] = None) -> Dict[str, Any]:
        return {"device_id": self.device_id, "ts": self.ts, "kind": self.kind, "detail": self.detail,
                "text": event_text(self, tz)}


def link_words(mbps: Optional[int]) -> str:
    """"down", "100 Mb/s", "2.5 Gb/s"; "unknown" for None or anything that is not a speed."""
    if isinstance(mbps, bool) or not isinstance(mbps, int) or mbps < 0:
        return "unknown"
    if mbps == 0:
        return "down"
    return f"{mbps / 1000:g} Gb/s" if mbps >= 1000 else f"{mbps} Mb/s"


def event_text(event: DeviceEvent, tz: Optional[tzinfo] = None) -> str:
    """The event in a few words: "restarted (up since 08:14)", "WAN link 2.5 Gb/s -> down"."""
    d = event.detail
    if event.kind == RESTARTED:
        boot = d.get("boot_at")
        if isinstance(boot, (int, float)) and not isinstance(boot, bool):
            try:
                return f"restarted (up since {datetime.fromtimestamp(boot, tz).strftime('%Y-%m-%d %H:%M')})"
            except (ValueError, OverflowError, OSError):  # a stored boot time no clock can show (NaN, year 10000)
                pass
        return "restarted"
    if event.kind == WAN_LINK:
        old, new = d.get("old"), d.get("new")
        if new == 0:
            return f"WAN link went down (was {link_words(old)})"
        if old == 0:
            return f"WAN link came back at {link_words(new)}"
        return f"WAN link changed from {link_words(old)} to {link_words(new)}"
    return event.kind


class EventTracker:
    """Decides the events of one device from its fresh readings. The first reading after a start compares with the
    newest stored one (`restore`), so what changed while Pengumon was not running is found too."""

    def __init__(self, device_id: str):
        self.device_id = device_id
        self._boot_at: Optional[float] = None
        self._link: Optional[int] = None
        self.restored = False

    def restore(self, uptime: Optional[Tuple[float, int]], link: Optional[Tuple[float, int]]) -> None:
        """The newest stored (ts, uptime_s) and (ts, wan_link_mbps), or None for one never read. A stored uptime that is
        not one (valid_uptime) is left out."""
        self.restored = True
        if uptime is not None and self._boot_at is None and valid_uptime(uptime[1]) is not None:
            self._boot_at = uptime[0] - valid_uptime(uptime[1])
        if link is not None and self._link is None:
            self._link = int(link[1])

    def check(self, ts: float, uptime_s: Optional[int], link_mbps: Optional[int],
              wrap_s: Optional[float] = None) -> List[DeviceEvent]:
        """The events this reading shows. `wrap_s`: the device's uptime counter goes back to 0 after this long."""
        events: List[DeviceEvent] = []
        uptime_s = valid_uptime(uptime_s)
        if uptime_s is not None:
            boot = ts - uptime_s
            if self._boot_at is not None and boot > self._boot_at + BOOT_SLACK_SECONDS:
                wraps = round((boot - self._boot_at) / wrap_s) if wrap_s else 0
                if wraps >= 1 and abs(boot - self._boot_at - wraps * wrap_s) <= BOOT_SLACK_SECONDS:
                    boot -= wraps * wrap_s  # the counter wrapped: still the same boot
                else:
                    events.append(DeviceEvent(self.device_id, ts, RESTARTED, {
                        "boot_at": round(boot), "previous_boot_at": round(self._boot_at), "uptime_s": uptime_s}))
            self._boot_at = boot  # each reading's own estimate: the clock and the uptime drift apart over months
        if link_mbps is not None:
            if self._link is not None and link_mbps != self._link:
                events.append(DeviceEvent(self.device_id, ts, WAN_LINK, {"old": self._link, "new": link_mbps}))
            self._link = link_mbps
        return events


async def events_between(repository, device_id: Optional[str], start: float, end: float) -> List[DeviceEvent]:
    """The stored events of one device (every device's for None) with start <= ts <= end, oldest first."""
    rows = await repository.get_device_events(device_id, start, end)
    return [DeviceEvent(d, ts, kind, detail) for d, ts, kind, detail in rows]
