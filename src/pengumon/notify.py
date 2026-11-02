"""Push notifications for incidents: ntfy (https://ntfy.sh) and webhooks (JSON, Slack, Discord), either or both.

PENGUMON_NOTIFY_URL is the ntfy topic URL (for example https://ntfy.sh/my-topic or a self-hosted server);
PENGUMON_NOTIFY_TOKEN, when set, is sent as a Bearer token. Webhooks are listed under `notifications:` in config.yaml
(webhook.py, docs/webhooks.md). Every channel hears the same alerts, after the same rules: expected conditions and
app outages inside their grace period are quiet, and a cause is announced at most once per MIN_INTERVAL_SECONDS.
Delivery runs in background tasks with a short timeout, so the classifier never waits for the network.

As a component (register), the notifier is built from the environment and the config and left on the context for the
classifier and the service checks; with no ntfy URL and no webhook there is none and nothing is announced.
"""

import asyncio
import logging
import os
import time
from urllib.parse import urlsplit
from typing import TYPE_CHECKING, Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple

import httpx

from pengumon.components import Component, Context
from pengumon.incidents.taxonomy import is_service_kind
from pengumon.incidents.model import Incident
from pengumon.notify_settings import SECRET_PREFIX, NotificationsConfig
from pengumon.observability import get_logger
from pengumon.secrets import SECRETS_DIR, SecretStore, resolve_webhook_secret
from pengumon.webhook import Alert, WebhookSender, alert_link

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger("pengumon.notify")

MIN_INTERVAL_SECONDS = 300.0  # at most one start message per cause in this time
SEND_TIMEOUT_SECONDS = 5.0


def _duration_text(seconds: Optional[float]) -> str:
    seconds = seconds or 0
    if seconds < 90:
        return f"{seconds:.0f} s"
    return f"{seconds / 60:.1f} min"


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
    ):
        self.url = url  # the ntfy topic; None: ntfy is not used
        self.public_url = public_url  # the dashboard's address for links (notifications.public_url)
        self.webhooks: List[WebhookSender] = list(webhooks)
        self.clock = clock
        self._wall = wall
        self._token = token
        self._client = client or httpx.AsyncClient(timeout=SEND_TIMEOUT_SECONDS)
        self._last_sent: Dict[str, float] = {}
        self._announced: Set[int] = set()  # incident ids whose start message went out
        self._tasks: Set[asyncio.Task] = set()

    @classmethod
    def from_env(cls, settings: Optional[NotificationsConfig] = None, store: Optional[SecretStore] = None,
                 env: Optional[Mapping[str, str]] = None,
                 secrets_dir: "Optional[Path]" = SECRETS_DIR) -> Optional["Notifier"]:
        """ntfy from PENGUMON_NOTIFY_URL / _TOKEN, the webhooks from `settings` (config.yaml `notifications:`). A
        webhook whose header names a secret nobody has is left out and logged (it would only be refused); None
        when no channel is left."""
        env = os.environ if env is None else env
        url = env.get("PENGUMON_NOTIFY_URL", "").strip() or None
        settings = settings or NotificationsConfig()
        senders = []
        for hook in settings.webhooks:
            headers = _resolve_headers(hook.name or "webhook", hook.headers, env, secrets_dir, store)
            if headers is not None:
                senders.append(WebhookSender(hook, headers, settings.public_url))
        if not url and not senders:
            return None
        return cls(url, env.get("PENGUMON_NOTIFY_TOKEN", "").strip() or None, webhooks=senders,
                   public_url=settings.public_url)

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
        sends = ([self._send_ntfy(alert)] if self.url else []) + [w.send(self._client, alert) for w in self.webhooks]
        results = await asyncio.gather(*sends)
        names = [c["name"] for c in self.channels()]
        return [{"name": name, "ok": reason is None, "detail": reason} for name, reason in zip(names, results)]

    def on_incident_started(self, incident: Incident) -> None:
        if incident.expected or is_service_kind(incident.kind):  # app outages: notify_service_down, after a grace period
            return
        now = self.clock()
        key = incident.cause_code
        last = self._last_sent.get(key)
        if last is not None and now - last < MIN_INTERVAL_SECONDS:
            return
        self._last_sent[key] = now
        if incident.id is not None:
            self._announced.add(incident.id)
        load = f"\nLine load: {incident.load_pct:.0f}%" if incident.load_pct is not None else ""
        self._dispatch(Alert(
            event="incident_started", severity=incident.severity,
            title=f"Pengumon: {incident.title}",
            message=f"Cause: {incident.cause_code}\n{incident.description}{load}",
            priority="high" if incident.severity == "critical" else "default",
            tags="rotating_light",
            incident_id=incident.id, cause=incident.cause_code, started_at=incident.start_time,
            load_pct=incident.load_pct,
        ))

    def on_incident_resolved(self, incident: Incident) -> None:
        if incident.expected or is_service_kind(incident.kind) or incident.id not in self._announced:
            return
        self._announced.discard(incident.id)
        load = f"\nPeak line load: {incident.load_pct:.0f}%" if incident.load_pct is not None else ""
        self._dispatch(Alert(
            event="incident_resolved", severity="info",
            title=f"Pengumon: resolved, {incident.title}",
            message=f"Cause: {incident.cause_code}\nDuration: {_duration_text(incident.duration_seconds)}{load}",
            priority="default",
            tags="white_check_mark",
            incident_id=incident.id, cause=incident.cause_code, started_at=incident.start_time,
            ended_at=incident.end_time, duration_seconds=incident.duration_seconds, load_pct=incident.load_pct,
        ))

    def notify_service_down(self, service, error, seconds_down: float) -> None:
        """A host app has not answered for the configured grace period (ServiceChecker decides when). Normal
        priority: it is an app, not the network, and a restart is not an emergency."""
        now = self._wall()
        self._dispatch(Alert(
            event="service_down", severity="warning",
            title=f"Pengumon: {service.name} not responding",
            message=f"{service.name} not responding: {error.message}\nDown for {_duration_text(seconds_down)}",
            priority="default",
            tags="warning",
            started_at=now - seconds_down, duration_seconds=seconds_down, service_id=service.id,
        ))

    def notify_service_up(self, service, seconds_down: float) -> None:
        """Sent only for an outage notify_service_down announced (ServiceChecker guarantees it)."""
        now = self._wall()
        self._dispatch(Alert(
            event="service_up", severity="info",
            title=f"Pengumon: {service.name} is back",
            message=f"{service.name} is responding again after {_duration_text(seconds_down)}",
            priority="default",
            tags="white_check_mark",
            started_at=now - seconds_down, ended_at=now, duration_seconds=seconds_down, service_id=service.id,
        ))

    def notify_path_changed(self, changes: Sequence[str]) -> None:
        """The background route-change check (engine/discovery/drift.py) found that the route differs from the applied
        path; it deduplicates, so this is sent once per set of changes. Normal priority: nothing is down."""
        lines = list(changes[:6]) + ([f"...and {len(changes) - 6} more"] if len(changes) > 6 else [])
        self._dispatch(Alert(
            event="path_changed", severity="info",
            title="Pengumon: your provider's route changed",
            message="\n".join(lines) + "\nReview it on Path health (Your path changed · Review).",
            priority="default",
            tags="world_map",
            started_at=self._wall(),
            page="#live",
        ))

    def _dispatch(self, alert: Alert) -> None:
        """Hands the alert to every channel, each in its own background task, so one slow channel delays no other."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        coroutines = ([self._send_ntfy(alert)] if self.url else []) + [w.send(self._client, alert) for w in self.webhooks]
        for coroutine in coroutines:
            task = loop.create_task(coroutine)
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

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
            logger.warning(f"Notification not delivered: {type(e).__name__}: {e}")
            return type(e).__name__
        return None

    async def aclose(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self._client.aclose()


def _resolve_headers(name: str, headers: Mapping[str, str], env: Mapping[str, str],
                     secrets_dir: "Optional[Path]", store: Optional[SecretStore]) -> Optional[Dict[str, str]]:
    """The headers to send, with each "secret:<name>" value replaced by the secret; None (and a log line naming the
    webhook and the secret, never a value) when one cannot be found."""
    resolved = {}
    for header, value in headers.items():
        if value.startswith(SECRET_PREFIX):
            secret_name = value[len(SECRET_PREFIX):]
            secret = resolve_webhook_secret(secret_name, env=env, secrets_dir=secrets_dir, store=store)
            if secret is None:
                logger.error(f"Webhook {name} is not used: header {header} names the secret {secret_name!r}, which "
                             "is not set (environment, secrets directory or data/secrets.json)")
                return None
            value = secret
        resolved[header] = value
    return resolved


async def register(ctx: Context) -> Optional[Component]:
    notifier = Notifier.from_env(ctx.config.notifications, ctx.state.secret_store)
    if notifier is None:
        return None
    kinds = ", ".join(c["name"] if c["kind"] == "ntfy" else f"webhook {c['name']} ({c['format']})"
                      for c in notifier.channels())
    get_logger("pengumon").info(f"Incident notifications enabled: {kinds}.")  # the app's lifecycle logger name
    ctx.notifier = ctx.state.notifier = notifier
    return Component("notify", stop=notifier.aclose)
