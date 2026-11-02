"""The notifications config section (`notifications:`): webhooks next to the ntfy topic (PENGUMON_NOTIFY_URL)."""

import re
from typing import Dict, List, Literal, Optional
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SECRET_PREFIX = "secret:"  # a header value "secret:ha_token" names a secret instead of holding it (pengumon.secrets)
_SECRET_NAME = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")


def http_url(value: str, what: str, *, allow_query: bool) -> str:
    """`value` stripped, when it is an http(s) URL with a host and no credentials. The message never repeats it: a
    webhook URL can hold a token."""
    value = value.strip()
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"{what} must be an http or https URL with a host")
    if parts.username is not None or parts.password is not None:
        raise ValueError(f"{what} must not contain credentials (use headers for a token)")
    if not allow_query and ("?" in value or "#" in value):
        raise ValueError(f"{what} must not have a query string")
    return value


class WebhookConfig(BaseModel):
    """One webhook. `format`: json (Pengumon's own payload, docs/webhooks.md), slack ({"text"}) or discord
    ({"content"}). A header value is sent as written, or as the secret it names when it is "secret:<name>"."""
    model_config = ConfigDict(hide_input_in_errors=True)

    url: str
    name: Optional[str] = None  # shown in the alerts dialog and the log; "webhook-<n>" when left out
    format: Literal["json", "slack", "discord"] = "json"
    headers: Dict[str, str] = Field(default_factory=dict)
    timeout_seconds: float = Field(default=5.0, gt=0, le=60)

    @field_validator("url")
    @classmethod
    def _url(cls, value: str) -> str:
        return http_url(value, "url", allow_query=True)

    @field_validator("name")
    @classmethod
    def _name(cls, value: Optional[str]) -> Optional[str]:
        value = (value or "").strip()
        if len(value) > 60:
            raise ValueError("name is at most 60 characters")
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


class NotificationsConfig(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)

    public_url: Optional[str] = None  # how people reach this dashboard; the "link" of a json webhook, a line in slack/discord
    webhooks: List[WebhookConfig] = Field(default_factory=list)

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
        return self
