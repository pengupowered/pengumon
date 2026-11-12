"""Push notifications for incidents: ntfy (https://ntfy.sh) and webhooks (JSON, Slack, Discord), either or both.

PENGUMON_NOTIFY_URL is the ntfy topic URL (for example https://ntfy.sh/my-topic or a self-hosted server);
PENGUMON_NOTIFY_TOKEN, when set, is sent as a Bearer token. Webhooks are set on the Settings page (the
`notifications.webhooks` list; notify/webhook.py, docs/webhooks.md). Every channel hears the same alerts, after the same
rules: expected conditions and app outages inside their grace period are quiet, and the `alerts:` settings
(notify/policy.py) say when the rest goes out: a severity filter, a delay, quiet hours that hold an alert until they end, and how often one cause repeats.
Delivery runs in background tasks with a short timeout, so the classifier never waits for the network.

This module decides whether and when an alert is announced. The timers are scheduler.py, the texts messages.py, the
request to ntfy and the delivery count delivery.py, the channels the settings describe channels.py.

A saved change to the channels or the rules reaches the notifier through `reconfigure`, with no restart; what it
already announced, what it sent lately and what is waiting for its time all carry over (notify/component.py).
"""

import asyncio
import logging
import time
from urllib.parse import urlsplit
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Mapping, Optional, Sequence, Set

import httpx

from pengumon.incidents.taxonomy import is_service_kind
from pengumon.incidents.model import Incident
from pengumon.incidents.outages import Drop, OutageBuilder, drop_of
from pengumon.notify import messages
from pengumon.notify.channels import build_channels
from pengumon.notify.delivery import count_alert as _count, deliver, make_test_alert, send_ntfy
from pengumon.notify.policy import SEVERITY_RANK, AlertPolicy, Timing
from pengumon.notify.scheduler import AlertScheduler
from pengumon.notify.settings import NotificationsConfig
from pengumon.notify.webhook import Alert, WebhookSender
from pengumon.secrets import SECRETS_DIR, SecretStore

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger("pengumon.notify")

MIN_INTERVAL_SECONDS = 300.0  # the default of alerts.repeat_window_seconds: one start message per cause in this time
SEND_TIMEOUT_SECONDS = 5.0


@dataclass
class _Live:
    """An outage the Notifier follows (incidents.outages: drops joined while the gap holds). One start alert, one
    resolved alert, however many drops. Forgotten with the resolved alert, and by a restart."""
    outage: OutageBuilder
    first: Incident                  # the drop that started it: the start alert is about it
    severity: str
    load_pct: Optional[float] = None
    dropped: bool = False            # the start alert was refused (severity filter, or the outage ended inside the delay)
    suppressed: bool = False         # the repeat window held it back: not asked again

    @property
    def id(self) -> int:
        return self.outage.id


class Notifier:
    def __init__(
        self,
        url: Optional[str],
        token: Optional[str] = None,
        client: Optional[httpx.AsyncClient] = None,
        clock: Callable[[], float] = time.monotonic,
        webhooks: Sequence[WebhookSender] = (),
        wall: Callable[[], float] = time.time,
        public_url: Optional[str] = None,
        policy: Optional[AlertPolicy] = None,
        schedule: Optional[Callable[[float, Callable[[], None]], Any]] = None,
    ):
        """`policy`: when alerts go out (default: the defaults of `alerts:`, UTC). `schedule(delay, fn)` runs `fn`
        after `delay` seconds and returns a handle with cancel() (default: the event loop's call_later); tests pass
        their own so time is theirs to move."""
        self.url = url  # the ntfy topic; None: ntfy is not used
        self.public_url = public_url  # the dashboard's address for links (notifications.public_url)
        self.webhooks: List[WebhookSender] = list(webhooks)
        self.policy = policy or AlertPolicy()
        self.clock = clock
        self._wall = wall
        self._timers = AlertScheduler(wall, schedule or self._call_later)
        self._token = token
        self._client = client or httpx.AsyncClient(timeout=SEND_TIMEOUT_SECONDS)
        self._last_sent: Dict[str, float] = {}
        self._announced: Set[int] = set()  # incident ids (the first one's, for an outage) whose start message went out
        self._current: Optional[_Live] = None  # the outage a new drop may still join (only with grouping on)
        self._member: Dict[int, _Live] = {}  # incident id -> the outage it belongs to
        self._tasks: Set[asyncio.Task] = set()


    @staticmethod
    def _call_later(delay: float, fn: Callable[[], None]):
        return asyncio.get_running_loop().call_later(delay, fn)

    @classmethod
    def build(cls, settings: Optional[NotificationsConfig] = None, store: Optional[SecretStore] = None,
              env: Optional[Mapping[str, str]] = None, secrets_dir: "Optional[Path]" = SECRETS_DIR,
              policy: Optional[AlertPolicy] = None, secret_value: Optional[Callable[[str], Optional[str]]] = None,
              **kw) -> "Notifier":
        """A Notifier for the current settings, with or without a channel (a channel added later arrives through
        `reconfigure`)."""
        settings = settings or NotificationsConfig()
        channels = build_channels(settings, store, env, secrets_dir, secret_value)
        return cls(channels.url, channels.token, webhooks=channels.senders, public_url=settings.public_url,
                   policy=policy, **kw)

    @classmethod
    def from_env(cls, settings: Optional[NotificationsConfig] = None, store: Optional[SecretStore] = None,
                 env: Optional[Mapping[str, str]] = None,
                 secrets_dir: "Optional[Path]" = SECRETS_DIR) -> Optional["Notifier"]:
        """`build`, or None when there is no channel."""
        notifier = cls.build(settings, store, env, secrets_dir)
        return notifier if notifier.channels() else None

    def reconfigure(self, ntfy_url: Optional[str], ntfy_token: Optional[str], senders: Sequence[WebhookSender],
                    public_url: Optional[str], rules: AlertPolicy) -> None:
        """Replaces the channels and the rules in place. What was announced, what was sent lately and what is
        waiting stay; the waiting announcements are timed again under the new rules (quiet hours switched off send
        them now, a longer delay holds them longer), and one the new severity filter refuses is dropped."""
        self.url, self._token, self.public_url = ntfy_url, ntfy_token, public_url
        self.webhooks = list(senders)
        self.policy = rules
        self._timers.reschedule_all()

    def channels(self) -> List[dict]:
        """What is configured, for the alerts dialog: names, formats and hosts, no secrets and no paths."""
        found = [{"name": "ntfy", "kind": "ntfy", "format": None, "host": urlsplit(self.url).hostname}] if self.url else []
        found += [{"name": w.name, "kind": "webhook", "format": w.format, "host": w.host} for w in self.webhooks]
        return found

    async def send_test(self) -> List[dict]:
        """Sends a test message to every channel now (no quiet rules) and says how each went."""
        alert = make_test_alert(self._wall())
        names = [c["name"] for c in self.channels()]  # (before the sends: a Save meanwhile may change the channels)
        results = await asyncio.gather(*self._deliveries(alert))
        return [{"name": name, "ok": reason is None, "detail": reason} for name, reason in zip(names, results)]

    # --- the rules: when an alert goes out ---------------------------------------------------------------------

    @staticmethod
    def _incident_key(incident: Incident) -> str:
        return f"incident:{incident.id}" if incident.id is not None else f"incident:cause:{incident.cause_code}"

    def on_incident_started(self, incident: Incident) -> None:
        if incident.expected or is_service_kind(incident.kind):  # app outages: notify_service_down, after a grace period
            return
        rules = self.policy.outage_rules
        drop = drop_of(incident)
        if drop is None or not rules.grouping:  # not groupable, or grouping is off: each incident is announced alone
            self._consider(incident, incident.start_time)
            return
        current = self._current
        if current is not None and current.outage.joins(drop, rules.gap_seconds, self._wall()):
            self._join(current, incident, drop)
            return
        live = _Live(OutageBuilder(drop), incident, incident.severity, incident.load_pct)
        self._current = live
        self._member[drop.id] = live
        self._consider_outage(live, incident.start_time)

    def _join(self, live: _Live, incident: Incident, drop: Drop) -> None:
        """A drop that belongs to an outage that is live: it announces nothing of its own. The outage's resolved alert
        (waiting for the gap to pass) is canceled. An outage the start alert was refused for is considered again, as
        an escalation is."""
        live.outage.add(drop)
        self._member[drop.id] = live
        self._timers.drop(f"resolved:{live.id}")
        self._rank_up(live, incident)
        self._reconsider(live)

    @staticmethod
    def _rank_up(live: _Live, incident: Incident) -> None:
        if SEVERITY_RANK.get(incident.severity, 0) > SEVERITY_RANK.get(live.severity, 0):
            live.severity = incident.severity

    def _reconsider(self, live: _Live) -> None:
        key = f"incident:{live.id}"
        if key in self._timers:
            self._timers.reschedule(key)  # timed again: the outage may now be exempt from quiet hours
        elif live.id not in self._announced and live.dropped and not live.suppressed \
                and self.policy.passes_severity(live.severity):
            self._consider_outage(live, self._wall())

    def _consider_outage(self, live: _Live, start_time: float) -> None:
        if not self.policy.passes_severity(live.severity):
            live.dropped = True
            _count("incident_started", "dropped")
            return
        live.dropped = False

        def due() -> Optional[Timing]:
            if not self.policy.passes_severity(live.severity) or not live.outage.ongoing and live.id not in self._announced:
                live.dropped = True  # (over before the delay was up: a drop that joins later asks again)
                return None
            return self.policy.incident_time(start_time, live.severity)

        self._timers.schedule(f"incident:{live.id}", due, lambda timing: self._announce_outage(live, timing))

    def on_incident_escalated(self, incident: Incident) -> None:
        """A warning incident became critical. With `min_severity: critical` it was not announced; it is considered
        now, as if it had started now. One that is announced or waiting keeps its place (a waiting one is timed
        again, since it may now be exempt from quiet hours)."""
        if incident.expected or is_service_kind(incident.kind):
            return
        live = self._member.get(incident.id)
        if live is not None:
            drop = drop_of(incident)
            if drop is not None:
                live.outage.replace(drop)
            self._rank_up(live, incident)
            self._reconsider(live)
            return
        key = self._incident_key(incident)
        if key in self._timers:
            self._timers.reschedule(key)
        elif incident.id is None or incident.id not in self._announced:
            self._consider(incident, self._wall())

    def _consider(self, incident: Incident, start_time: float) -> None:
        if not self.policy.passes_severity(incident.severity):
            _count("incident_started", "dropped")
            return

        def due() -> Optional[Timing]:
            if not self.policy.passes_severity(incident.severity):
                return None
            return self.policy.incident_time(start_time, incident.severity)

        self._timers.schedule(self._incident_key(incident), due, lambda timing: self._announce_incident(incident, timing))

    def _announce_incident(self, incident: Incident, timing: Timing) -> None:
        now = self.clock()
        key = incident.cause_code
        if not self.policy.repeat_allowed(self._last_sent.get(key), now):
            _count("incident_started", "suppressed")
            return
        self._last_sent[key] = now
        if incident.id is not None:
            self._announced.add(incident.id)
        self._dispatch(messages.incident_started(self.policy, incident, timing))

    def _announce_outage(self, live: _Live, timing: Timing) -> None:
        first = live.first
        now = self.clock()
        if not self.policy.repeat_allowed(self._last_sent.get(first.cause_code), now):
            live.suppressed = True
            _count("incident_started", "suppressed")
            return
        self._last_sent[first.cause_code] = now
        self._announced.add(live.id)
        self._dispatch(messages.outage_started(self.policy, first, live.severity, len(live.outage.members), timing,
                                               live.id))

    def on_incident_resolved(self, incident: Incident) -> None:
        if incident.expected or is_service_kind(incident.kind):
            return
        live = self._member.get(incident.id)
        if live is not None:
            self._member_resolved(live, incident)
            return
        if self._timers.drop(self._incident_key(incident)):
            return  # still waiting for its time: it was never announced, so there is nothing to resolve
        if incident.id not in self._announced:
            return
        self._announced.discard(incident.id)
        at = self._wall()
        alert = messages.incident_resolved(incident)
        # quiet hours hold a "resolved" too, unless the incident was exempt (critical, critical_still): the person
        # who heard about it at night hears that it is over
        self._timers.schedule(f"resolved:{incident.id}", lambda: self.policy.quiet_time(at, incident.severity),
                       lambda timing: self._dispatch(alert))

    def _member_resolved(self, live: _Live, incident: Incident) -> None:
        """A drop of a live outage ended. When none is open, the outage's resolved alert goes out once the gap has
        passed with no new drop (a drop that joins cancels it): quiet hours hold it as they hold any resolved alert."""
        end = incident.end_time if incident.end_time is not None else self._wall()
        member = next((m for m in live.outage.members if m.id == incident.id), None)
        if member is not None:
            live.outage.replace(Drop(member.id, member.start, end, incident.severity, member.place, member.title,
                                     member.cause_code))
        self._rank_up(live, incident)
        if incident.load_pct is not None:
            live.load_pct = max(live.load_pct or 0.0, incident.load_pct)
        if live.outage.ongoing:
            return
        gap = self.policy.outage_rules.gap_seconds

        def due() -> Optional[Timing]:
            return self.policy.quiet_time(live.outage.latest_end(self._wall()) + gap, live.severity)

        self._timers.schedule(f"resolved:{live.id}", due, lambda timing: self._finish_outage(live))

    def _finish_outage(self, live: _Live) -> None:
        """The gap passed after the outage's last drop: it is over. Sends its one resolved alert if it was announced."""
        self._forget(live)
        self._timers.drop(f"incident:{live.id}")  # never announced (still waiting): there is nothing to resolve
        if live.id not in self._announced:
            return
        self._announced.discard(live.id)
        summary = live.outage.summary(self._wall(), self.policy.outage_rules)
        self._dispatch(messages.outage_resolved(self.policy, summary, live.id, live.load_pct))

    def _forget(self, live: _Live) -> None:
        for m in live.outage.members:
            self._member.pop(m.id, None)
        if self._current is live:
            self._current = None

    def notify_service_down(self, service, error, seconds_down: float) -> None:
        """A host app has not answered for the configured grace period (ServiceChecker decides when). Normal
        priority: it is an app, not the network, and a restart is not an emergency. Only quiet hours apply; a held
        one is sent at their end if the app is still down."""
        created = self._wall()

        def build(timing: Timing) -> Alert:
            down_for = seconds_down + max(0.0, self._wall() - created)
            return messages.service_down(self.policy, service, error, timing, created, seconds_down, down_for)

        self._timers.schedule(f"service_down:{service.id}", lambda: self.policy.quiet_time(created, "warning"),
                       lambda timing: self._dispatch(build(timing)))

    def notify_service_up(self, service, seconds_down: float) -> None:
        """Sent only for an outage notify_service_down announced (ServiceChecker guarantees it); when that message
        is still waiting for the end of quiet hours, both are dropped: the app came back before anyone was told."""
        if self._timers.drop(f"service_down:{service.id}"):
            return
        now = self._wall()
        alert = messages.service_up(service, seconds_down, now)
        self._timers.schedule(f"service_up:{service.id}", lambda: self.policy.quiet_time(now, "info"),
                       lambda timing: self._dispatch(alert))

    def forget_service(self, service_id: str) -> None:
        """A service was removed or changed: its waiting messages are stale."""
        self._timers.drop(f"service_down:{service_id}")
        self._timers.drop(f"service_up:{service_id}")

    def notify_path_changed(self, changes: Sequence[str]) -> None:
        """The background route-change check (engine/discovery/drift.py) found that the route differs from the applied
        path; it deduplicates, so this is sent once per set of changes. Normal priority: nothing is down. Only quiet
        hours apply; a newer set replaces one that is still waiting."""
        created = self._wall()
        alert = messages.path_changed(changes, created)
        self._timers.schedule("path_changed", lambda: self.policy.quiet_time(created, "info"),
                       lambda timing: self._dispatch(alert))

    def notify_speed_slow(self, down_mbps: Optional[float], up_mbps: Optional[float], plan_down: Optional[float],
                          plan_up: Optional[float], alert_pct: float, measured_by: Optional[str] = None,
                          note: Optional[str] = None) -> None:
        """A speed test measured under `alert_pct` percent of the plan speed (speedtest/runner.py decides, and sends
        this once until a test is above the line again). The speeds are the test's headline (speedtest/quality.py):
        `measured_by` "router" when they are the line's rate the router measured during the test, "test" when they
        are the test's own; `note` says when the household's traffic shared the line. Normal priority: nothing is
        down, and no incident is opened. Only quiet hours apply; a newer one replaces one that is still waiting."""
        created = self._wall()
        alert = messages.speed_slow(down_mbps, up_mbps, plan_down, plan_up, alert_pct, measured_by, note, created)
        self._timers.schedule("speed_slow", lambda: self.policy.quiet_time(created, "info"),
                       lambda timing: self._dispatch(alert))

    def pending_count(self) -> int:
        return self._timers.count()

    def _dispatch(self, alert: Alert) -> None:
        """Hands the alert to every channel, each in its own background task, so one slow channel delays no other."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        for coroutine in self._deliveries(alert):
            task = loop.create_task(coroutine)
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    def _deliveries(self, alert: Alert) -> list:
        """One coroutine per channel that sends `alert`, each counted and timed (and a span of its own) under the
        channel's name: "ntfy", or the webhook's id. Never the address, the topic or the text."""
        found = [deliver("ntfy", alert, self._send_ntfy(alert))] if self.url else []
        return found + [deliver(w.config.id or w.name, alert, w.send(self._client, alert)) for w in self.webhooks]

    async def _send_ntfy(self, alert: Alert) -> Optional[str]:
        return await send_ntfy(self._client, self.url, self._token, self.public_url, alert)

    async def aclose(self) -> None:
        self._timers.cancel_all()
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self._client.aclose()
