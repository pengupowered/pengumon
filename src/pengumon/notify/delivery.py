"""Delivery of one alert to the channels: the count and timing every delivery gets, the ntfy sender (webhooks have theirs
in webhook.py) and the test alert.

The Notifier (notifier.py) decides whether and when an alert is announced; what happens after that, the request to ntfy
and the measuring of it, is here. Every sender returns None for sent and a short reason for failed, and never raises."""

import asyncio
import base64
import logging
import time
from typing import Optional

import httpx

from pengumon.drivers.http import CappedBody, total_deadline
from pengumon.notify.webhook import Alert, alert_link, failure_words
from pengumon.observability.telemetry import alert_delivery_duration, alerts, tracer

logger = logging.getLogger("pengumon.notify")

ANSWER_MAX_BYTES = 16 * 1024  # an ntfy answer is a few hundred bytes; read this far, decoded, and no further

ALL_CHANNELS = "all"  # the channel of a decision made before any channel was asked


def count_alert(event: str, outcome: str, channel: str = ALL_CHANNELS) -> None:
    alerts.add(1, {"event": event, "channel": channel, "outcome": outcome})


async def deliver(channel: str, alert: Alert, send) -> Optional[str]:
    """Awaits `send` (a channel's coroutine) under a span, a count and a duration of its own, named by the channel:
    "ntfy", or the webhook's id. Returns what `send` returned."""
    started = time.perf_counter()
    with tracer.start_as_current_span("alert.deliver", attributes={"alert.channel": channel,
                                                                    "alert.event": alert.event}) as span:
        reason = await send  # (the channels return None for sent, a short reason for failed, and never raise)
        outcome = "sent" if reason is None else "failed"
        span.set_attribute("alert.outcome", outcome)
    count_alert(alert.event, outcome, channel)
    alert_delivery_duration.record(time.perf_counter() - started, {"channel": channel})
    return reason


def make_test_alert(now: float) -> Alert:
    """The message of the Alerts dialog's "send a test" button."""
    return Alert(event="test", severity="info", title="Pengumon: test alert",
                 message="This is a test message from the Pengumon alerts dialog.", tags="white_check_mark",
                 started_at=now)


async def send_ntfy(client: httpx.AsyncClient, url: str, token: Optional[str], public_url: Optional[str],
                    alert: Alert) -> Optional[str]:
    """Posts the alert to the ntfy topic `url`; the answer is read through the bounded reader and not used."""
    headers = {"Title": header_text(alert.title), "Priority": alert.priority, "Tags": alert.tags}
    link = alert_link(alert, public_url) if alert.page else None
    if link:
        headers["Click"] = link  # ntfy opens it when the notification is tapped
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        headers["Accept-Encoding"] = "identity"
        # (httpx's timeouts are per read: the whole exchange has a deadline of its own)
        async with asyncio.timeout(total_deadline(client.timeout)):
            async with client.stream("POST", url, content=alert.message.encode("utf-8"), headers=headers) as response:
                await CappedBody(response, ANSWER_MAX_BYTES).read()  # (the answer is not used: bounded, decoded only that far)
                response.raise_for_status()
    except Exception as e:
        # (not str(e): an HTTP error's text names the URL, and the URL holds the topic)
        status = getattr(getattr(e, "response", None), "status_code", None)
        logger.warning(f"Notification not delivered: {type(e).__name__}" + (f" (HTTP {status})" if status else ""))
        if isinstance(e, UnicodeEncodeError):  # a header (the link) with characters a header cannot carry
            return "a header has characters it cannot carry"
        return failure_words(e)
    return None


def header_text(text: str) -> str:
    """A text for an HTTP header: as it is when it is ASCII, else RFC 2047 encoded words (=?UTF-8?B?...?=), which
    ntfy decodes (its documentation: non-ASCII headers such as Title). Headers go out as ASCII, so a title with an
    accent in it (a hop named "Telefónica") was never delivered. One word holds at most 45 bytes (the limit of an
    encoded word is 75 characters) and never splits a character."""
    text = " ".join(text.split())  # a header is one line
    if text.isascii():
        return text
    words, chunk = [], b""
    for char in text:
        piece = char.encode("utf-8")
        if len(chunk) + len(piece) > 45:
            words.append(chunk)
            chunk = b""
        chunk += piece
    words.append(chunk)
    return " ".join("=?UTF-8?B?" + base64.b64encode(w).decode("ascii") + "?=" for w in words)
