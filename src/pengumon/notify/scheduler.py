"""The timers of the Notifier: announcements waiting for their delay or for the end of quiet hours.

What is announced, and when, is the Notifier's decision (notifier.py, policy.py); this keeps what is waiting. One waiting
announcement per key; each asks again for its time when its timer fires and when the rules change, so it always follows
the current rules."""

from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from pengumon.notify.delivery import count_alert
from pengumon.notify.policy import Timing

# The Alert.event of what a schedule key announces
_KEY_EVENTS = {"incident": "incident_started", "resolved": "incident_resolved", "service_down": "service_down",
               "service_up": "service_up", "path_changed": "path_changed", "speed_slow": "speed_slow"}


@dataclass
class _Pending:
    """An announcement waiting for its time. `due` is asked again when it fires and when the rules change, so it
    always follows the current rules; `emit` sends it."""
    due: "Callable[[], Optional[Timing]]"
    emit: "Callable[[Timing], None]"
    handle: Any = None

    def cancel(self) -> None:
        if self.handle is not None:
            self.handle.cancel()
            self.handle = None


class AlertScheduler:
    """`wall()` is the time of day in epoch seconds; `schedule_fn(delay, fn)` runs `fn` after `delay` seconds and
    returns a handle with cancel()."""

    def __init__(self, wall: Callable[[], float], schedule_fn: Callable[[float, Callable[[], None]], Any]):
        self._wall = wall
        self._schedule_fn = schedule_fn
        self._pending: Dict[str, _Pending] = {}  # waiting for the delay or the end of quiet hours

    def __contains__(self, key: str) -> bool:
        return key in self._pending

    def count(self) -> int:
        return len(self._pending)

    def schedule(self, key: str, due: "Callable[[], Optional[Timing]]", emit: "Callable[[Timing], None]",
                 first: bool = True) -> None:
        """Sends now, or at the time `due()` says (None: never). One waiting announcement per key. `first` is False
        when a waiting one is timed again (its timer fired, or the rules changed): the telemetry counted it already."""
        previous = self._pending.pop(key, None)
        if previous is not None:
            previous.cancel()
        event = _KEY_EVENTS.get(key.split(":", 1)[0], "other")
        timing = due()
        if timing is None:
            if first:
                count_alert(event, "dropped")
            return
        delay = timing.at - self._wall()
        if delay <= 0:
            emit(timing)
            return
        if first:
            count_alert(event, "held" if timing.held else "delayed")
        entry = _Pending(due, emit)
        try:
            entry.handle = self._schedule_fn(delay, lambda: self._fire(key, entry))
        except RuntimeError:  # no event loop (a sync caller): nothing can wait, as _dispatch sends nothing either
            return
        self._pending[key] = entry

    def _fire(self, key: str, entry: _Pending) -> None:
        if self._pending.get(key) is not entry:
            return
        del self._pending[key]
        self.schedule(key, entry.due, entry.emit, first=False)  # asked again: a timer that fired early waits again

    def reschedule(self, key: str) -> None:
        entry = self._pending.pop(key, None)
        if entry is not None:
            entry.cancel()
            self.schedule(key, entry.due, entry.emit, first=False)

    def reschedule_all(self) -> None:
        for key in list(self._pending):
            self.reschedule(key)

    def drop(self, key: str) -> bool:
        entry = self._pending.pop(key, None)
        if entry is not None:
            entry.cancel()
        return entry is not None

    def cancel_all(self) -> None:
        for entry in list(self._pending.values()):
            entry.cancel()
        self._pending.clear()
