"""The settings hub: how a component hears that a saved setting changed, so it can apply it without a restart.

Most settings need no applier: after a save the settings manager copies the new values into the live config objects
in place (every holder of `state.config` sees them at once). A component whose running parts keep a copy (the
prober's timeout, the notifier's channels, the device tasks) subscribes in its `register`:

    @ctx.settings.on(keys=["prober.timeout_seconds"], name="probing.timeout")
    def _timeout(change):
        prober.ping_timeout = change.new.value("prober.timeout_seconds", default)

After every commit the manager calls `publish(change)`. Each applier whose subscription the change touches is
called once, in registration order (= component order), and each is contained: one that raises is logged and
named in the result, so the save reports its parts as "applies after a restart" (the store already holds them).

A subscription matches a change by exact `keys`, by key `prefixes` ("alerts." matches "alerts.quiet_hours.start"),
by `lists` (devices, services, webhooks, dns_probes), by `slots` (a secret slot such as "ntfy.url"; an entry ending
in "." matches by prefix, "webhook." matches "webhook.home.url") and by `events`, named happenings that are not a
save (W5's "devices_rebuilt", published by the device supervisor after it rebuilt the devices).
"""

import inspect
import os
import traceback
from dataclasses import dataclass, field
from typing import Awaitable, Callable, FrozenSet, Iterable, List, Optional, Union

from pengumon.observability import get_logger
from pengumon.observability.telemetry import settings_live_failures
from pengumon.settings.document import SettingsDocument

_log = get_logger("pengumon.settings")


@dataclass(frozen=True)
class Change:
    """What one save (or event) changed. `keys`: registry keys whose effective value changed; `lists`: list names;
    `slots`: secret slots written or cleared (never their values); `events`: event names. `old` and `new` are the
    whole documents before and after (`old` is None for the start-up load)."""

    keys: FrozenSet[str] = frozenset()
    lists: FrozenSet[str] = frozenset()
    slots: FrozenSet[str] = frozenset()
    events: FrozenSet[str] = frozenset()
    old: Optional[SettingsDocument] = None
    new: SettingsDocument = field(default_factory=SettingsDocument)

    @classmethod
    def of(cls, *, keys: Iterable[str] = (), lists: Iterable[str] = (), slots: Iterable[str] = (),
           events: Iterable[str] = (), old: Optional[SettingsDocument] = None,
           new: Optional[SettingsDocument] = None) -> "Change":
        return cls(frozenset(keys), frozenset(lists), frozenset(slots), frozenset(events), old,
                   new if new is not None else SettingsDocument())

    @property
    def empty(self) -> bool:
        return not (self.keys or self.lists or self.slots or self.events)


ApplyFn = Callable[[Change], Union[Awaitable[None], None]]


@dataclass(frozen=True)
class Subscription:
    name: str
    apply: ApplyFn
    keys: FrozenSet[str]
    prefixes: tuple
    lists: FrozenSet[str]
    slots: FrozenSet[str]
    events: FrozenSet[str]

    def touched(self, change: Change) -> List[str]:
        """The parts of `change` this subscription covers, sorted: keys, list names, slots and events."""
        keys = {k for k in change.keys if k in self.keys or k.startswith(self.prefixes)}
        slots = {s for s in change.slots
                 if s in self.slots or any(p.endswith(".") and s.startswith(p) for p in self.slots)}
        return sorted(keys | (change.lists & self.lists) | slots | (change.events & self.events))


class SettingsHub:
    def __init__(self) -> None:
        self._subscriptions: List[Subscription] = []

    def on(self, keys: Iterable[str] = (), *, prefixes: Iterable[str] = (), lists: Iterable[str] = (),
           slots: Iterable[str] = (), events: Iterable[str] = (), name: str) -> Callable[[ApplyFn], ApplyFn]:
        """Decorator: subscribes the function to the changes that touch any of these. `name` is what logs and the
        failed list call it ("probing.timeout"); names are unique."""
        keys, prefixes = frozenset([keys] if isinstance(keys, str) else keys), tuple(prefixes)
        if not (keys or prefixes or lists or slots or events):
            raise ValueError(f"settings applier {name!r} subscribes to nothing")
        if any(s.name == name for s in self._subscriptions):
            raise ValueError(f"settings applier {name!r} is registered twice")

        def register(fn: ApplyFn) -> ApplyFn:
            self._subscriptions.append(Subscription(name, fn, keys, prefixes, frozenset(lists), frozenset(slots),
                                                    frozenset(events)))
            return fn
        return register

    @property
    def names(self) -> List[str]:
        """The appliers' names, in registration order."""
        return [s.name for s in self._subscriptions]

    def touched_by(self, name: str, change: Change) -> List[str]:
        """The parts of `change` the applier `name` covers (for the save's `live_failed` list)."""
        for subscription in self._subscriptions:
            if subscription.name == name:
                return subscription.touched(change)
        raise KeyError(name)

    def subscribers(self, change: Change) -> List[str]:
        """The names of the appliers `change` would call, in order."""
        return [s.name for s in self._subscriptions if s.touched(change)]

    async def publish(self, change: Change) -> List[str]:
        """Calls every applier the change touches, in registration order; returns the names of those that raised."""
        failed: List[str] = []
        for subscription in self._subscriptions:
            if not subscription.touched(change):
                continue
            try:
                result = subscription.apply(change)
                if inspect.isawaitable(result):
                    await result
            except Exception as e:
                failed.append(subscription.name)
                section = subscription.name.split(".", 1)[0]
                settings_live_failures.add(1, {"section": section, "applier": subscription.name})
                # parts: the setting keys, list names and secret slot names (never a value) the applier was told of.
                # The error text and the traceback can quote a value (a URL a library refused), so the ERROR line has
                # the type and where it was raised; the traceback is for DEBUG.
                frame = traceback.extract_tb(e.__traceback__)[-1]
                _log.error(f"Settings applier {subscription.name} failed: {type(e).__name__}; the change applies at "
                           "the next start", applier=subscription.name, section=section, error=type(e).__name__,
                           parts=",".join(subscription.touched(change)), where=f"{os.path.basename(frame.filename)}:"
                           f"{frame.lineno}")
                _log.debug(f"Settings applier {subscription.name} failed", applier=subscription.name, exc_info=True)
        return failed
