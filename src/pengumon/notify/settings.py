"""The alert config sections: `notifications:` (the dashboard link and the webhooks, next to the ntfy topic) and
`alerts:` (the rules for when an alert goes out).

A webhook's URL can carry a token (Discord, Slack and Home Assistant put one in the path), so the Settings store keeps
it with the secrets (slot `webhook.<id>.url`, pengumon.settings.registry), never in the item: `WebhookItem` is the
stored item. `WebhookConfig` is the config.yaml shape (the item plus its `url`), which the one-time import reads and
today's Notifier still runs on."""

import re
from typing import Dict, Iterable, List, Literal, Optional
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from pengumon.settings.general import time_of_day

SECRET_PREFIX = "secret:"  # a header value "secret:ha_token" names a secret instead of holding it (pengumon.secrets)
_SECRET_NAME = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
WEBHOOK_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


def http_url(value: str, what: str, *, allow_query: bool) -> str:
    """`value` stripped, when it is an http(s) URL with a host and no credentials. The message never repeats it: a
    webhook URL can hold a token."""
    value = value.strip()
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"{what} must be an http or https URL with a host")
    if parts.username is not None or parts.password is not None:
        raise ValueError(f"{what} must not contain credentials (use headers for a token)")
    try:
        port = parts.port
    except ValueError:
        port = 0
    if port == 0:
        raise ValueError(f"{what} has a port that is not a valid port (1 to 65535)")
    if not allow_query and ("?" in value or "#" in value):
        raise ValueError(f"{what} must not have a query string")
    return value


def webhook_id(name: Optional[str], taken: Iterable[str] = ()) -> str:
    """The id a webhook gets from its name: letters, digits, '_' and '-' (anything else becomes '-'), at most 64,
    "webhook" for a name with none of those, and "-2", "-3", ... when the id is already in `taken`."""
    base = re.sub(r"[^A-Za-z0-9_-]+", "-", (name or "").strip()).strip("-")[:64] or "webhook"
    taken = set(taken)
    candidate, number = base, 1
    while candidate in taken:
        number += 1
        suffix = f"-{number}"
        candidate = base[:64 - len(suffix)] + suffix
    return candidate


class WebhookItem(BaseModel):
    """One webhook as the Settings store keeps it (no URL: that is the secret slot `webhook.<id>.url`). `format`:
    json (Pengumon's own payload, docs/webhooks.md), slack ({"text"}) or discord ({"content"}). A header value is
    sent as written, or as the secret it names when it is "secret:<name>". `id` is fixed once saved; None until
    NotificationsConfig (or the store) derives it from the name."""
    model_config = ConfigDict(hide_input_in_errors=True)

    id: Optional[str] = None
    name: Optional[str] = None  # shown in the alerts dialog and the log; "webhook-<n>" when left out
    format: Literal["json", "slack", "discord"] = "json"
    headers: Dict[str, str] = Field(default_factory=dict)
    timeout_seconds: float = Field(default=5.0, gt=0, le=60)

    @field_validator("id")
    @classmethod
    def _id(cls, value: Optional[str]) -> Optional[str]:
        value = (value or "").strip()
        if value and not WEBHOOK_ID.fullmatch(value):
            raise ValueError("id must be 1-64 letters, digits, '_' or '-'")
        return value or None

    @field_validator("name")
    @classmethod
    def _name(cls, value: Optional[str]) -> Optional[str]:
        value = (value or "").strip()
        if len(value) > 60:
            raise ValueError("name is at most 60 characters")
        if any(not c.isprintable() for c in value):  # it reaches log lines: no line breaks or terminal escapes
            raise ValueError("name must not contain control characters")
        return value or None

    @field_validator("headers")
    @classmethod
    def _headers(cls, value: Dict[str, str]) -> Dict[str, str]:
        for name, text in value.items():
            if not _HEADER_NAME.fullmatch(str(name)):
                raise ValueError("a header name has characters a header cannot have")
            if "\r" in text or "\n" in text:
                raise ValueError(f"header {name}: the value must be one line")
            if text.startswith(SECRET_PREFIX) and not _SECRET_NAME.fullmatch(text[len(SECRET_PREFIX):]):
                raise ValueError(f"header {name}: a secret name is letters, digits, '_', '.' and '-'")
        return value


class WebhookConfig(WebhookItem):
    """A webhook as config.yaml writes it: the item plus its URL (http(s), no credentials; a query is allowed)."""

    url: str

    @field_validator("url")
    @classmethod
    def _url(cls, value: str) -> str:
        return http_url(value, "url", allow_query=True)


MAX_WEBHOOKS = 20  # a household has a handful; the cap bounds what one save or import costs


class NotificationsConfig(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)

    public_url: Optional[str] = None  # how people reach this dashboard; the "link" of a json webhook, a line in slack/discord
    webhooks: List[WebhookConfig] = Field(default_factory=list, max_length=MAX_WEBHOOKS)

    @field_validator("public_url")
    @classmethod
    def _public_url(cls, value: Optional[str]) -> Optional[str]:
        if value is None or not value.strip():
            return None
        return http_url(value, "public_url", allow_query=False)

    @model_validator(mode="after")
    def _names(self):
        seen = set()
        for number, hook in enumerate(self.webhooks, start=1):
            hook.name = hook.name or f"webhook-{number}"
            if hook.name in seen:
                raise ValueError(f"webhooks: name {hook.name!r} is used twice")
            seen.add(hook.name)
        ids = [hook.id for hook in self.webhooks if hook.id]
        if len(set(ids)) != len(ids):
            raise ValueError("webhooks: an id is used twice")
        taken = set(ids)
        for hook in self.webhooks:
            if not hook.id:
                hook.id = webhook_id(hook.name, taken)
                taken.add(hook.id)
        return self


class QuietHours(BaseModel):
    """A daily window (in general.timezone) in which alerts wait for its end; it may cross midnight. A critical
    network incident still goes out at once while `critical_still` is on."""
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    start: str = Field("22:00", json_schema_extra={"ui_type": "time"})
    end: str = Field("07:00", json_schema_extra={"ui_type": "time"})
    critical_still: bool = True

    @field_validator("start", "end")
    @classmethod
    def _hhmm(cls, value: str, info) -> str:
        return time_of_day(value, info.field_name)

    @model_validator(mode="after")
    def _not_empty(self):
        if self.start == self.end:
            raise ValueError("quiet hours: start and end must differ")
        return self


class AlertsConfig(BaseModel):
    """When an alert goes out (pengumon.notify). `min_severity`, `delay_seconds` and quiet hours apply to network
    incidents; quiet hours also hold host-app and route-change alerts. `repeat_window_seconds`: an incident with the
    same cause inside this time is not announced again (0: every one is)."""
    model_config = ConfigDict(extra="forbid")

    min_severity: Literal["warning", "critical"] = "warning"
    delay_seconds: float = Field(0.0, ge=0, le=3600)  # an incident shorter than this sends nothing
    repeat_window_seconds: float = Field(300.0, ge=0, le=86400)
    quiet_hours: QuietHours = Field(default_factory=QuietHours)
