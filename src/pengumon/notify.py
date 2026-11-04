"""Push notifications for incidents: ntfy (https://ntfy.sh) and webhooks (JSON, Slack, Discord), either or both.

PENGUMON_NOTIFY_URL is the ntfy topic URL (for example https://ntfy.sh/my-topic or a self-hosted server);
PENGUMON_NOTIFY_TOKEN, when set, is sent as a Bearer token. Webhooks are listed under `notifications:` in config.yaml
(webhook.py, docs/webhooks.md). Every channel hears the same alerts, after the same rules: expected conditions and
app outages inside their grace period are quiet, and the `alerts:` settings (notify_policy.py) say when the rest goes
out: a severity filter, a delay, quiet hours that hold an alert until they end, and how often one cause repeats.
Delivery runs in background tasks with a short timeout, so the classifier never waits for the network.

As a component (register), the notifier is always built, from the environment, the Settings secrets and the config,
and left on the context for the classifier and the service checks (with no channel it announces nothing). A saved
change to the channels or the rules reaches it through `reconfigure`, with no restart; what it already announced, what
it sent lately and what is waiting for its time all carry over.
"""

import asyncio
import logging
import os
import time
from urllib.parse import urlsplit
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple

import httpx

from pengumon.components import Component, Context
from pengumon.incidents.taxonomy import is_service_kind
from pengumon.incidents.model import Incident
from pengumon.notify_policy import AlertPolicy, Timing
from pengumon.notify_settings import SECRET_PREFIX, NotificationsConfig, WebhookConfig
from pengumon.observability import get_logger
from pengumon.observability.telemetry import alert_delivery_duration, alerts, tracer
from pengumon.secrets import SECRETS_DIR, SecretStore, resolve_webhook_secret
from pengumon.settings.env import secret_override
from pengumon.webhook import Alert, WebhookSender, alert_link, failure_words

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger("pengumon.notify")

MIN_INTERVAL_SECONDS = 300.0  # the default of alerts.repeat_window_seconds: one start message per cause in this time
SEND_TIMEOUT_SECONDS = 5.0


_ALL = "all"  # the channel of a decision made before any channel was asked
# The Alert.event of what a schedule key announces
_KEY_EVENTS = {"incident": "incident_started", "resolved": "incident_resolved", "service_down": "service_down",
               "service_up": "service_up", "path_changed": "path_changed"}


def _count(event: str, outcome: str, channel: str = _ALL) -> None:
    alerts.add(1, {"event": event, "channel": channel, "outcome": outcome})


def _duration_text(seconds: Optional[float]) -> str:
    seconds = seconds or 0
    if seconds < 90:
        return f"{seconds:.0f} s"
    return f"{seconds / 60:.1f} min"


@dataclass
class Channels:
    """What the settings say the Notifier talks to now: the ntfy topic and token, and one sender per usable webhook."""
    url: Optional[str]
    token: Optional[str]
    senders: List[WebhookSender]


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
        self._schedule_fn = schedule or self._call_later
        self._token = token
        self._client = client or httpx.AsyncClient(timeout=SEND_TIMEOUT_SECONDS)
        self._last_sent: Dict[str, float] = {}
        self._announced: Set[int] = set()  # incident ids whose start message went out
        self._pending: Dict[str, _Pending] = {}  # waiting for the delay or the end of quiet hours
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
        for key in list(self._pending):
            self._reschedule(key)

    def channels(self) -> List[dict]:
        """What is configured, for the alerts dialog: names, formats and hosts, no secrets and no paths."""
        found = [{"name": "ntfy", "kind": "ntfy", "format": None, "host": urlsplit(self.url).hostname}] if self.url else []
        found += [{"name": w.name, "kind": "webhook", "format": w.format, "host": w.host} for w in self.webhooks]
        return found

    async def send_test(self) -> List[dict]:
        """Sends a test message to every channel now (no quiet rules) and says how each went."""
        alert = Alert(event="test", severity="info", title="Pengumon: test alert",
                      message="This is a test message from the Pengumon alerts dialog.", tags="white_check_mark",
                      started_at=self._wall())
        results = await asyncio.gather(*self._deliveries(alert))
        names = [c["name"] for c in self.channels()]
        return [{"name": name, "ok": reason is None, "detail": reason} for name, reason in zip(names, results)]

    # --- the rules: when an alert goes out ---------------------------------------------------------------------

    def _schedule(self, key: str, due: "Callable[[], Optional[Timing]]", emit: "Callable[[Timing], None]",
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
                _count(event, "dropped")
            return
        delay = timing.at - self._wall()
        if delay <= 0:
            emit(timing)
            return
        if first:
            _count(event, "held" if timing.held else "delayed")
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
        self._schedule(key, entry.due, entry.emit, first=False)  # asked again: a timer that fired early waits again

    def _reschedule(self, key: str) -> None:
        entry = self._pending.pop(key, None)
        if entry is not None:
            entry.cancel()
            self._schedule(key, entry.due, entry.emit, first=False)

    def _drop(self, key: str) -> bool:
        entry = self._pending.pop(key, None)
        if entry is not None:
            entry.cancel()
        return entry is not None

    @staticmethod
    def _incident_key(incident: Incident) -> str:
        return f"incident:{incident.id}" if incident.id is not None else f"incident:cause:{incident.cause_code}"

    def on_incident_started(self, incident: Incident) -> None:
        if incident.expected or is_service_kind(incident.kind):  # app outages: notify_service_down, after a grace period
            return
        self._consider(incident, incident.start_time)

    def on_incident_escalated(self, incident: Incident) -> None:
        """A warning incident became critical. With `min_severity: critical` it was not announced; it is considered
        now, as if it had started now. One that is announced or waiting keeps its place (a waiting one is timed
        again, since it may now be exempt from quiet hours)."""
        if incident.expected or is_service_kind(incident.kind):
            return
        key = self._incident_key(incident)
        if key in self._pending:
            self._reschedule(key)
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

        self._schedule(self._incident_key(incident), due, lambda timing: self._announce_incident(incident, timing))

    def _announce_incident(self, incident: Incident, timing: Timing) -> None:
        now = self.clock()
        key = incident.cause_code
        if not self.policy.repeat_allowed(self._last_sent.get(key), now):
            _count("incident_started", "suppressed")
            return
        self._last_sent[key] = now
        if incident.id is not None:
            self._announced.add(incident.id)
        load = f"\nLine load: {incident.load_pct:.0f}%" if incident.load_pct is not None else ""
        self._dispatch(Alert(
            event="incident_started", severity=incident.severity,
            title=self._ongoing(f"Pengumon: {incident.title}", timing, incident.start_time),
            message=f"Cause: {incident.title}\n{incident.description}{load}",
            priority="high" if incident.severity == "critical" else "default",
            tags="rotating_light",
            incident_id=incident.id, cause=incident.cause_code, started_at=incident.start_time,
            load_pct=incident.load_pct,
        ))

    def _ongoing(self, title: str, timing: Timing, since: float) -> str:
        """An alert that quiet hours held says since when it has been going on."""
        if not timing.held:
            return title
        return title.replace("Pengumon: ", f"Pengumon: Ongoing since {self.policy.clock_text(since)}: ", 1)

    def on_incident_resolved(self, incident: Incident) -> None:
        if incident.expected or is_service_kind(incident.kind):
            return
        if self._drop(self._incident_key(incident)):
            return  # still waiting for its time: it was never announced, so there is nothing to resolve
        if incident.id not in self._announced:
            return
        self._announced.discard(incident.id)
        at = self._wall()
        load = f"\nPeak line load: {incident.load_pct:.0f}%" if incident.load_pct is not None else ""
        alert = Alert(
            event="incident_resolved", severity="info",
            title=f"Pengumon: resolved, {incident.title}",
            message=f"Cause: {incident.title}\nDuration: {_duration_text(incident.duration_seconds)}{load}",
            priority="default",
            tags="white_check_mark",
            incident_id=incident.id, cause=incident.cause_code, started_at=incident.start_time,
            ended_at=incident.end_time, duration_seconds=incident.duration_seconds, load_pct=incident.load_pct,
        )
        # quiet hours hold a "resolved" too, unless the incident was exempt (critical, critical_still): the person
        # who heard about it at night hears that it is over
        self._schedule(f"resolved:{incident.id}", lambda: self.policy.quiet_time(at, incident.severity),
                       lambda timing: self._dispatch(alert))

    def notify_service_down(self, service, error, seconds_down: float) -> None:
        """A host app has not answered for the configured grace period (ServiceChecker decides when). Normal
        priority: it is an app, not the network, and a restart is not an emergency. Only quiet hours apply; a held
        one is sent at their end if the app is still down."""
        created = self._wall()

        def build(timing: Timing) -> Alert:
            down_for = seconds_down + max(0.0, self._wall() - created)
            return Alert(
                event="service_down", severity="warning",
                title=self._ongoing(f"Pengumon: {service.name} not responding", timing, created - seconds_down),
                message=f"{service.name} not responding: {error.message}\nDown for {_duration_text(down_for)}",
                priority="default",
                tags="warning",
                started_at=created - seconds_down, duration_seconds=down_for, service_id=service.id,
            )

        self._schedule(f"service_down:{service.id}", lambda: self.policy.quiet_time(created, "warning"),
                       lambda timing: self._dispatch(build(timing)))

    def notify_service_up(self, service, seconds_down: float) -> None:
        """Sent only for an outage notify_service_down announced (ServiceChecker guarantees it); when that message
        is still waiting for the end of quiet hours, both are dropped: the app came back before anyone was told."""
        if self._drop(f"service_down:{service.id}"):
            return
        now = self._wall()
        alert = Alert(
            event="service_up", severity="info",
            title=f"Pengumon: {service.name} is back",
            message=f"{service.name} is responding again after {_duration_text(seconds_down)}",
            priority="default",
            tags="white_check_mark",
            started_at=now - seconds_down, ended_at=now, duration_seconds=seconds_down, service_id=service.id,
        )
        self._schedule(f"service_up:{service.id}", lambda: self.policy.quiet_time(now, "info"),
                       lambda timing: self._dispatch(alert))

    def forget_service(self, service_id: str) -> None:
        """A service was removed or changed: its waiting messages are stale."""
        self._drop(f"service_down:{service_id}")
        self._drop(f"service_up:{service_id}")

    def notify_path_changed(self, changes: Sequence[str]) -> None:
        """The background route-change check (engine/discovery/drift.py) found that the route differs from the applied
        path; it deduplicates, so this is sent once per set of changes. Normal priority: nothing is down. Only quiet
        hours apply; a newer set replaces one that is still waiting."""
        lines = list(changes[:6]) + ([f"...and {len(changes) - 6} more"] if len(changes) > 6 else [])
        created = self._wall()
        alert = Alert(
            event="path_changed", severity="info",
            title="Pengumon: your network path changed",
            message="\n".join(lines) + "\nReview it on Path health (Your path changed · Review).",
            priority="default",
            tags="world_map",
            started_at=created,
            page="#live",
        )
        self._schedule("path_changed", lambda: self.policy.quiet_time(created, "info"),
                       lambda timing: self._dispatch(alert))

    def pending_count(self) -> int:
        return len(self._pending)

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
        found = [self._deliver("ntfy", alert, self._send_ntfy(alert))] if self.url else []
        return found + [self._deliver(w.config.id or w.name, alert, w.send(self._client, alert)) for w in self.webhooks]

    @staticmethod
    async def _deliver(channel: str, alert: Alert, send) -> Optional[str]:
        started = time.perf_counter()
        with tracer.start_as_current_span("alert.deliver", attributes={"alert.channel": channel,
                                                                        "alert.event": alert.event}) as span:
            reason = await send  # (the channels return None for sent, a short reason for failed, and never raise)
            outcome = "sent" if reason is None else "failed"
            span.set_attribute("alert.outcome", outcome)
        _count(alert.event, outcome, channel)
        alert_delivery_duration.record(time.perf_counter() - started, {"channel": channel})
        return reason

    async def _send_ntfy(self, alert: Alert) -> Optional[str]:
        headers = {"Title": alert.title, "Priority": alert.priority, "Tags": alert.tags}
        link = alert_link(alert, self.public_url) if alert.page else None
        if link:
            headers["Click"] = link  # ntfy opens it when the notification is tapped
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        try:
            response = await self._client.post(
                self.url,
                content=alert.message.encode("utf-8"),
                headers=headers,
            )
            response.raise_for_status()
        except Exception as e:
            # (not str(e): an HTTP error's text names the URL, and the URL holds the topic)
            status = getattr(getattr(e, "response", None), "status_code", None)
            logger.warning(f"Notification not delivered: {type(e).__name__}" + (f" (HTTP {status})" if status else ""))
            return failure_words(e)
        return None

    async def aclose(self) -> None:
        for entry in list(self._pending.values()):
            entry.cancel()
        self._pending.clear()
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self._client.aclose()


def build_channels(settings: NotificationsConfig, store: Optional[SecretStore] = None,
                   env: Optional[Mapping[str, str]] = None,
                   secrets_dir: "Optional[Path]" = SECRETS_DIR,
                   secret_value: Optional[Callable[[str], Optional[str]]] = None) -> Channels:
    """The channels the settings and secrets describe. `secret_value(slot)` is the settings manager's
    (state.settings.secret_value: environment, secret file, then secrets.json); without it the SecretStore is read
    directly (tests, a start without the manager). ntfy: PENGUMON_NOTIFY_URL / _TOKEN win, else the Settings
    secrets `ntfy.url` / `ntfy.token`. A webhook's URL: the secret slot `webhook.<id>.url` (secrets.json
    webhook_urls.<id>), else the `url` of a config.yaml webhook. A webhook with no URL, or whose header names a
    secret nobody has, is left out and logged (it would only be refused)."""
    env = os.environ if env is None else env
    store = store or SecretStore()
    stored = store.data()
    ntfy = stored.get("ntfy") if isinstance(stored.get("ntfy"), dict) else {}
    if secret_value is not None:
        url, token = _text(secret_value("ntfy.url")), _text(secret_value("ntfy.token"))
    else:
        url = secret_override("ntfy.url", env) or _text(ntfy.get("url"))
        token = secret_override("ntfy.token", env) or _text(ntfy.get("token"))
    urls = stored.get("webhook_urls") if isinstance(stored.get("webhook_urls"), dict) else {}
    senders = []
    for hook in settings.webhooks:
        label = hook.name or hook.id or "webhook"
        stored_url = secret_value(f"webhook.{hook.id}.url") if secret_value is not None else urls.get(hook.id)
        address = _text(stored_url) or _text(getattr(hook, "url", None))
        if address is None:
            logger.error(f"Webhook {label} is not used: its URL is not set")
            continue
        if not isinstance(hook, WebhookConfig) or hook.url != address:
            try:
                hook = WebhookConfig(**hook.model_dump(exclude={"url"}), url=address)
            except ValueError:
                logger.error(f"Webhook {label} is not used: its URL is not a valid http(s) address")
                continue
        headers = _resolve_headers(label, hook.headers, env, secrets_dir, store, secret_value)
        if headers is not None:
            senders.append(WebhookSender(hook, headers, settings.public_url))
    return Channels(url, token, senders)


def _text(value: Any) -> Optional[str]:
    return value.strip() or None if isinstance(value, str) else None


def _resolve_headers(name: str, headers: Mapping[str, str], env: Mapping[str, str],
                     secrets_dir: "Optional[Path]", store: Optional[SecretStore],
                     secret_value: Optional[Callable[[str], Optional[str]]] = None) -> Optional[Dict[str, str]]:
    """The headers to send, with each "secret:<name>" value replaced by the secret; None (and a log line naming the
    webhook and the secret, never a value) when one cannot be found."""
    resolved = {}
    for header, value in headers.items():
        if value.startswith(SECRET_PREFIX):
            secret_name = value[len(SECRET_PREFIX):]
            secret = (secret_value(f"webhook_header.{secret_name}") if secret_value is not None else
                      resolve_webhook_secret(secret_name, env=env, secrets_dir=secrets_dir, store=store))
            if secret is None:
                logger.error(f"Webhook {name} is not used: header {header} names the secret {secret_name!r}, which "
                             "is not set (environment, secrets directory or data/secrets.json)")
                return None
            value = secret
        resolved[header] = value
    return resolved


def _describe(notifier: Notifier) -> str:
    return ", ".join(c["name"] if c["kind"] == "ntfy" else f"webhook {c['name']} ({c['format']})"
                     for c in notifier.channels()) or "no channel"


async def register(ctx: Context) -> Optional[Component]:
    config, store = ctx.config, ctx.state.secret_store

    def policy() -> AlertPolicy:
        return AlertPolicy(config.alerts, config.general.timezone)

    def secret_value():
        manager = getattr(ctx.state, "settings", None)  # W1's SettingsManager; None where the store is not running (tests)
        return manager.secret_value if manager is not None else None

    notifier = Notifier.build(config.notifications, store, policy=policy(), secret_value=secret_value())
    channels = notifier.channels()
    log = get_logger("pengumon")  # the app's lifecycle logger name
    log.info(f"Incident notifications enabled: {_describe(notifier)}." if channels
             else "Incident notifications: no channel configured yet.")
    ctx.notifier = ctx.state.notifier = notifier

    hub = getattr(ctx, "settings", None)  # the settings hub (W1); without it the notifier is set at start only
    if hub is not None:
        @hub.on(keys=["notifications.public_url", "general.timezone"], prefixes=["alerts."], lists=["webhooks"],
                slots=["ntfy.", "webhook.", "webhook_header."], name="notify.reconfigure")
        def _reconfigure(change) -> None:
            built = build_channels(config.notifications, store, secret_value=secret_value())
            notifier.reconfigure(built.url, built.token, built.senders, config.notifications.public_url, policy())
            log.info(f"Notifications reconfigured: {_describe(notifier)}.")

    return Component("notify", stop=notifier.aclose)
