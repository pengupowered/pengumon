"""Webhook delivery of alerts: the same events ntfy gets, as JSON (Pengumon's own payload), Slack or Discord.

The Notifier (notify/notifier.py) decides what is announced (quiet rules, rate limits, dedup) and hands each alert to every
channel; this module builds the request body for a webhook and sends it. The URL comes from the owner's config, so it
is trusted, but only http(s) is spoken and a redirect is followed only within the same host. Header values and URLs
(a webhook id in the path or a token in the query is a secret) are never logged: a failure is logged as the webhook's
name and a short reason.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Optional
from urllib.parse import urljoin, urlsplit

import httpx

from pengumon.notify.settings import WebhookConfig

logger = logging.getLogger("pengumon.notify")

PAYLOAD_VERSION = 1
DISCORD_LIMIT = 2000  # characters in a Discord message's content
MAX_REDIRECTS = 3
RETRY_DELAY_SECONDS = 2.0


def failure_words(error: Exception) -> str:
    """Why a delivery failed, in plain words (the Alerts dialog shows it): no class names, and never the URL, which
    httpx puts into its messages."""
    status = getattr(getattr(error, "response", None), "status_code", None)
    if status:
        return f"the server answered {status}"
    if isinstance(error, httpx.TimeoutException):
        return "timed out"
    if isinstance(error, httpx.ConnectError):
        return "could not connect"
    return "the connection failed"


@dataclass
class Alert:
    """One announcement, with everything any channel shows. `event`: incident_started, incident_resolved,
    service_down, service_up, path_changed, speed_slow or test. `priority` and `tags` are ntfy's. `page` is the part of the
    dashboard the link should open ("#live": Path health), added to the configured public URL."""
    event: str
    severity: str  # info, warning or critical
    title: str
    message: str
    priority: str = "default"
    tags: str = ""
    incident_id: Optional[int] = None
    cause: Optional[str] = None
    started_at: Optional[float] = None  # epoch seconds
    ended_at: Optional[float] = None
    duration_seconds: Optional[float] = None
    load_pct: Optional[float] = None
    service_id: Optional[str] = None
    page: Optional[str] = None
    outage_id: Optional[int] = None  # the outage a network incident alert is about (incidents.outages), with `drops`
    drops: Optional[int] = None      # how many incidents the outage joined so far


def alert_link(alert: Alert, public_url: Optional[str]) -> Optional[str]:
    """The dashboard link of an alert: the public URL, with the alert's page when it names one."""
    if not public_url:
        return None
    return public_url.rstrip("/") + "/" + alert.page if alert.page else public_url


def _iso(epoch: Optional[float]) -> Optional[str]:
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def json_payload(alert: Alert, link: Optional[str]) -> dict:
    """The stable payload of format `json` (documented in docs/webhooks.md; new fields may be added, never changed)."""
    return {
        "version": PAYLOAD_VERSION,
        "source": "pengumon",
        "event": alert.event,
        "severity": alert.severity,
        "title": alert.title,
        "message": alert.message,
        "incident_id": alert.incident_id,
        "outage_id": alert.outage_id,
        "drops": alert.drops,
        "cause": alert.cause,
        "service_id": alert.service_id,
        "started_at": _iso(alert.started_at),
        "ended_at": _iso(alert.ended_at),
        "duration_seconds": alert.duration_seconds,
        "load_pct": alert.load_pct,
        "link": link,
    }


def _slack_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def slack_payload(alert: Alert, link: Optional[str]) -> dict:
    text = f"*{_slack_escape(alert.title)}*\n{_slack_escape(alert.message)}"
    if link:
        text += f"\n<{link}|Open the dashboard>"
    return {"text": text}


def discord_payload(alert: Alert, link: Optional[str]) -> dict:
    tail = f"\n{link}" if link else ""
    head = f"**{alert.title}**\n{alert.message}"
    # allowed_mentions: nothing in a message (a service name, a description) can ping @everyone
    return {"content": _truncate(head, DISCORD_LIMIT - len(tail)) + tail, "allowed_mentions": {"parse": []}}


_BUILDERS = {"json": json_payload, "slack": slack_payload, "discord": discord_payload}


class WebhookSender:
    """One configured webhook, with its header values resolved (secrets looked up by the Notifier's factory)."""

    def __init__(self, config: WebhookConfig, headers: Dict[str, str], public_url: Optional[str] = None,
                 retry_delay: float = RETRY_DELAY_SECONDS):
        self.config = config
        self.name = config.name or "webhook"
        self.format = config.format
        self.host = urlsplit(config.url).hostname or ""  # the only part of the URL that is shown anywhere
        self._headers = headers
        self._public_url = public_url
        self._retry_delay = retry_delay

    def payload(self, alert: Alert) -> dict:
        return _BUILDERS[self.format](alert, alert_link(alert, self._public_url))

    async def send(self, client: httpx.AsyncClient, alert: Alert) -> Optional[str]:
        """Posts the alert; one retry after a transport error, a timeout, a 429 or a 5xx. Returns None when it was
        delivered, else a short reason (also logged). Never raises."""
        body = self.payload(alert)
        reason = "not sent"
        for attempt in (1, 2):
            reason = await self._post(client, body)
            if reason is None:
                return None
            if attempt == 1 and self._transient(reason):
                await asyncio.sleep(self._retry_delay)
                continue
            break
        logger.warning(f"Webhook {self.name} not delivered: {reason}")
        return reason

    @staticmethod
    def _transient(reason: str) -> bool:
        if reason.startswith("the server answered "):
            code = int(reason[20:23])
            return code == 429 or code >= 500
        return not reason.startswith("refused")

    async def _post(self, client: httpx.AsyncClient, body: dict) -> Optional[str]:
        url = self.config.url
        try:
            for _ in range(MAX_REDIRECTS + 1):
                response = await client.post(url, json=body, headers=self._headers, follow_redirects=False,
                                             timeout=self.config.timeout_seconds)
                if response.status_code not in (301, 302, 303, 307, 308):
                    break
                target = urljoin(url, response.headers.get("location", ""))
                if not self._same_host(target):
                    return "refused: redirect to another host"
                url = target
            else:
                return "refused: too many redirects"
            if response.status_code >= 400:
                return f"the server answered {response.status_code}"
            return None
        except Exception as e:  # httpx messages can hold the URL, so only these words are kept
            return failure_words(e)

    def _same_host(self, target: str) -> bool:
        a, b = urlsplit(self.config.url), urlsplit(target)
        return b.scheme in ("http", "https") and (b.hostname or "").lower() == (a.hostname or "").lower() \
            and b.port == a.port
