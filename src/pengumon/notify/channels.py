"""The channels the settings describe: the ntfy topic and token, and one sender per usable webhook, with the secrets
they name looked up (environment, secrets directory, the settings manager's store)."""

import logging
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Mapping, Optional

from pengumon.notify.settings import SECRET_PREFIX, NotificationsConfig, WebhookConfig
from pengumon.notify.webhook import WebhookSender
from pengumon.secrets import SECRETS_DIR, SecretStore, resolve_webhook_secret
from pengumon.settings.env import secret_override

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger("pengumon.notify")


@dataclass
class Channels:
    """What the settings say the Notifier talks to now: the ntfy topic and token, and one sender per usable webhook."""
    url: Optional[str]
    token: Optional[str]
    senders: List[WebhookSender]


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
        headers = _sender_headers(label, hook.headers, env, secrets_dir, store, secret_value)
        if headers is not None:
            senders.append(WebhookSender(hook, headers, settings.public_url))
    return Channels(url, token, senders)


def _text(value: Any) -> Optional[str]:
    return value.strip() or None if isinstance(value, str) else None


def resolve_headers(headers: Mapping[str, str], lookup: Callable[[str], Optional[str]],
                    on_missing: Optional[Callable[[str, str], None]] = None) -> Optional[Dict[str, Optional[str]]]:
    """The headers with each "secret:<name>" value replaced by `lookup(name)`, the other values as they are. A secret
    `lookup` cannot find stays None in the result; with `on_missing(header, name)` given, it is called for the first
    such header and the answer is None instead (the later secrets are not looked up)."""
    resolved: Dict[str, Optional[str]] = {}
    for header, value in headers.items():
        if isinstance(value, str) and value.startswith(SECRET_PREFIX):
            secret_name = value[len(SECRET_PREFIX):]
            value = lookup(secret_name)
            if value is None and on_missing is not None:
                on_missing(header, secret_name)
                return None
        resolved[header] = value
    return resolved


def _sender_headers(name: str, headers: Mapping[str, str], env: Mapping[str, str],
                    secrets_dir: "Optional[Path]", store: Optional[SecretStore],
                    secret_value: Optional[Callable[[str], Optional[str]]] = None) -> Optional[Dict[str, str]]:
    """The headers to send, with each "secret:<name>" value replaced by the secret; None (and a log line naming the
    webhook and the secret, never a value) when one cannot be found."""
    def lookup(secret_name: str) -> Optional[str]:
        return (secret_value(f"webhook_header.{secret_name}") if secret_value is not None else
                resolve_webhook_secret(secret_name, env=env, secrets_dir=secrets_dir, store=store))

    def missing(header: str, secret_name: str) -> None:
        logger.error(f"Webhook {name} is not used: header {header} names the secret {secret_name!r}, which "
                     "is not set (environment, secrets directory or data/secrets.json)")

    return resolve_headers(headers, lookup, missing)
