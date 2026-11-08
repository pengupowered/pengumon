"""Alerts by webhook: the config section, the three payload formats, delivery (retry, redirects, no secrets in logs),
the Notifier's quiet rules applying to webhooks as to ntfy, secrets for header values, and the test endpoints."""

import asyncio
import json
import logging

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from pengumon.state import STATE_ATTR, AppState
from pengumon.config import AppConfig
from pengumon.incidents.model import Incident
from pengumon.main import app, security_config
from pengumon.notify.notifier import Notifier
from pengumon.notify.settings import NotificationsConfig, WebhookConfig
from pengumon.secrets import SecretStore, resolve_webhook_secret
from pengumon.notify.webhook import DISCORD_LIMIT, Alert, WebhookSender

HOOK_URL = "https://hooks.example.com/api/webhook/abc123?token=SEKRET-QUERY"
LINK = "http://pengu.example.com:8000"


def _alert(**kw):
    base = dict(event="incident_started", severity="critical", title="Pengumon: ISP Outage",
                message="Cause: x\nHub unreachable", incident_id=7, cause="x", started_at=1_700_000_000.0)
    base.update(kw)
    return Alert(**base)


def _sender(fmt="json", headers=None, url=HOOK_URL, public_url=LINK, retry_delay=0.0):
    cfg = WebhookConfig(url=url, name="hook", format=fmt, headers=headers or {})
    return WebhookSender(cfg, headers or {}, public_url, retry_delay=retry_delay)


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _incident(iid=1, cause="internet_backbone_transit_loss", **kw):
    return Incident(id=iid, start_time=1000.0, cause_code=cause, title="ISP Outage", description="Hub unreachable",
                    severity="critical", **kw)


async def _drain(n):
    await asyncio.gather(*list(n._tasks))


# --- config ---------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("url", ["ftp://example.com/x", "file:///etc/passwd", "javascript:alert(1)", "example.com/x",
                                 "http://", "https://user:pw@example.com/x"])
def test_webhook_url_must_be_http_without_credentials(url):
    with pytest.raises(ValidationError) as e:
        WebhookConfig(url=url)
    assert "pw@" not in str(e.value) and url not in str(e.value)  # the error never repeats the URL


def test_webhook_defaults_and_names():
    cfg = NotificationsConfig(webhooks=[{"url": "http://a.example.com/x"}, {"url": "http://b.example.com/x", "name": "b"}])
    assert [(w.name, w.format, w.timeout_seconds) for w in cfg.webhooks] == [("webhook-1", "json", 5.0), ("b", "json", 5.0)]
    with pytest.raises(ValidationError):
        NotificationsConfig(webhooks=[{"url": "http://a.example.com/", "name": "a"}, {"url": "http://b.example.com/", "name": "a"}])
    with pytest.raises(ValidationError):
        WebhookConfig(url="http://a.example.com/", format="teams")


def test_webhook_header_checks_and_public_url():
    with pytest.raises(ValidationError):
        WebhookConfig(url="http://a.example.com/", headers={"X": "a\r\nInjected: 1"})
    with pytest.raises(ValidationError):
        WebhookConfig(url="http://a.example.com/", headers={"X": "secret:bad name!"})
    with pytest.raises(ValidationError):
        NotificationsConfig(public_url="http://pengu.example.com/?t=1")
    assert NotificationsConfig(public_url="  ").public_url is None
    assert AppConfig().notifications.webhooks == []


# --- payloads -------------------------------------------------------------------------------------------------------

def test_json_payload_is_stable():
    body = _sender("json").payload(_alert(load_pct=72.0))
    assert body == {
        "version": 1, "source": "pengumon", "event": "incident_started", "severity": "critical",
        "title": "Pengumon: ISP Outage", "message": "Cause: x\nHub unreachable", "incident_id": 7, "outage_id": None, "drops": None, "cause": "x",
        "service_id": None, "started_at": "2023-11-14T22:13:20Z", "ended_at": None, "duration_seconds": None,
        "load_pct": 72.0, "link": LINK,
    }
    assert _sender("json", public_url=None).payload(_alert())["link"] is None


def test_slack_payload_escapes_and_links():
    body = _sender("slack").payload(_alert(title="A & B <b>", message="x > y"))
    assert body == {"text": f"*A &amp; B &lt;b&gt;*\nx &gt; y\n<{LINK}|Open the dashboard>"}


def test_discord_payload_respects_the_limit_and_cannot_ping():
    body = _sender("discord").payload(_alert(message="m" * 5000))
    assert len(body["content"]) == DISCORD_LIMIT and body["content"].endswith(f"\n{LINK}")
    assert "…" in body["content"] and body["allowed_mentions"] == {"parse": []}
    short = _sender("discord", public_url=None).payload(_alert())
    assert short["content"] == "**Pengumon: ISP Outage**\nCause: x\nHub unreachable"


# --- delivery -------------------------------------------------------------------------------------------------------

async def test_send_posts_json_with_headers():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(204)

    sender = _sender(headers={"Authorization": "Bearer abc"})
    assert await sender.send(_client(handler), _alert()) is None
    request, = seen
    assert request.headers["Authorization"] == "Bearer abc" and request.headers["Content-Type"] == "application/json"
    assert json.loads(request.content)["event"] == "incident_started"
    assert str(request.url) == HOOK_URL


async def test_one_retry_on_a_server_error_then_success():
    codes = iter([503, 200])
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(next(codes))

    assert await _sender().send(_client(handler), _alert()) is None
    assert len(calls) == 2


async def test_retry_is_only_one_and_not_for_client_errors(caplog):
    calls = []

    def always(code):
        def handler(request):
            calls.append(code)
            return httpx.Response(code)
        return handler

    caplog.set_level(logging.WARNING, logger="pengumon.notify")
    assert await _sender().send(_client(always(500)), _alert()) == "the server answered 500"
    assert calls == [500, 500]
    calls.clear()
    assert await _sender().send(_client(always(404)), _alert()) == "the server answered 404"
    assert calls == [404]
    assert "Webhook hook not delivered: the server answered 404" in caplog.text


async def test_a_transport_error_is_contained_and_the_log_has_no_url_or_header(caplog):
    def handler(request):
        raise httpx.ConnectError(f"cannot reach {request.url}")

    caplog.set_level(logging.DEBUG)
    sender = _sender(headers={"Authorization": "Bearer TOPSECRET"})
    assert await sender.send(_client(handler), _alert()) == "could not connect"
    assert "could not connect" in caplog.text
    for secret in ("TOPSECRET", "SEKRET-QUERY", "abc123", "hooks.example.com"):
        assert secret not in caplog.text


async def test_timeout_is_reported():
    def handler(request):
        raise httpx.ReadTimeout("slow")

    assert await _sender().send(_client(handler), _alert()) == "timed out"


async def test_redirect_within_the_host_is_followed_and_to_another_host_refused():
    seen = []

    def handler(request):
        seen.append(str(request.url))
        if request.url.path == "/start":
            return httpx.Response(307, headers={"Location": "/moved"})
        if request.url.path == "/away":
            return httpx.Response(302, headers={"Location": "https://evil.example.net/steal"})
        return httpx.Response(200)

    sender = _sender(url="https://hooks.example.com/start", headers={"Authorization": "x"})
    assert await sender.send(_client(handler), _alert()) is None
    assert seen == ["https://hooks.example.com/start", "https://hooks.example.com/moved"]
    seen.clear()
    away = _sender(url="https://hooks.example.com/away")
    assert (await away.send(_client(handler), _alert())).startswith("refused")
    assert seen == ["https://hooks.example.com/away"]  # the other host was never contacted


async def test_redirect_to_a_non_http_scheme_or_a_loop_is_refused():
    def handler(request):
        return httpx.Response(302, headers={"Location": "file:///etc/passwd" if request.url.path == "/a" else "/a"})

    assert (await _sender(url="http://h.example.com/a").send(_client(handler), _alert())).startswith("refused")
    assert (await _sender(url="http://h.example.com/b").send(_client(handler), _alert())).startswith("refused")


# --- the Notifier ---------------------------------------------------------------------------------------------------

def _notifier(requests, webhooks, ntfy="http://ntfy.test/topic", **kw):
    def handler(request):
        requests.append(request)
        return httpx.Response(200)
    return Notifier(ntfy, client=_client(handler), webhooks=webhooks, **kw)


async def test_webhook_only_notifier_sends_start_and_resolve_with_the_facts():
    requests = []
    n = _notifier(requests, [_sender()], ntfy=None, wall=lambda: 5000.0)
    inc = _incident(load_pct=72.0)
    n.on_incident_started(inc)
    await _drain(n)
    inc.end_time, inc.duration_seconds = 1300.0, 300.0
    n.on_incident_resolved(inc)
    await _drain(n)
    start, end = [json.loads(r.content) for r in requests]
    assert (start["event"], start["severity"], start["incident_id"], start["load_pct"]) == ("incident_started", "critical", 1, 72.0)
    assert start["cause"] == "internet_backbone_transit_loss"  # the field stays the documented code
    assert "Cause: ISP Outage" in start["message"] and "internet_backbone_transit_loss" not in start["message"] and "72%" in start["message"]
    assert (end["event"], end["severity"], end["duration_seconds"], end["ended_at"]) == ("incident_resolved", "info", 300.0, "1970-01-01T00:21:40Z")
    assert "5.0 min" in end["message"]
    await n.aclose()


async def test_ntfy_and_several_webhooks_all_hear_the_same_alert():
    requests = []
    hooks = [_sender("json", url="http://a.example.com/x"), _sender("slack", url="http://b.example.com/x"),
             _sender("discord", url="http://c.example.com/x")]
    n = _notifier(requests, hooks)
    n.on_incident_started(_incident())
    await _drain(n)
    assert sorted(r.url.host for r in requests) == ["a.example.com", "b.example.com", "c.example.com", "ntfy.test"]
    bodies = {r.url.host: json.loads(r.content) for r in requests if r.url.host != "ntfy.test"}
    assert "event" in bodies["a.example.com"] and "text" in bodies["b.example.com"] and "content" in bodies["c.example.com"]
    await n.aclose()


async def test_webhooks_follow_the_quiet_rules():
    requests = []
    now = [0.0]
    n = _notifier(requests, [_sender()], ntfy=None, clock=lambda: now[0])
    n.on_incident_started(_incident(1, expected=True))                   # expected: quiet
    n.on_incident_started(_incident(2))
    n.on_incident_started(_incident(3))                                  # same cause inside 5 minutes: suppressed
    n.on_incident_started(_incident(4, cause="dns_resolver_failure"))
    await _drain(n)
    assert len(requests) == 2
    n.on_incident_resolved(_incident(3))                                 # never announced: no resolve
    n.on_incident_resolved(_incident(2))
    await _drain(n)
    assert len(requests) == 3
    await n.aclose()


async def test_service_alerts_reach_webhooks(monkeypatch):
    class Svc:
        id, name = "my_app", "My app"

    class Err:
        message = "connection refused"

    requests = []
    n = _notifier(requests, [_sender()], ntfy=None, wall=lambda: 10_000.0)
    n.notify_service_down(Svc, Err, 310.0)
    n.notify_service_up(Svc, 400.0)
    await _drain(n)
    down, up = [json.loads(r.content) for r in requests]
    assert (down["event"], down["severity"], down["service_id"], down["duration_seconds"]) == ("service_down", "warning", "my_app", 310.0)
    assert (up["event"], up["severity"], up["ended_at"]) == ("service_up", "info", "1970-01-01T02:46:40Z")
    await n.aclose()


async def test_a_failing_webhook_neither_raises_nor_blocks_ntfy():
    seen = []

    def handler(request):
        seen.append(request.url.host)
        return httpx.Response(500 if request.url.host == "hooks.example.com" else 200)

    n = Notifier("http://ntfy.test/t", client=_client(handler), webhooks=[_sender()])
    n.on_incident_started(_incident())
    await _drain(n)
    assert seen.count("ntfy.test") == 1 and seen.count("hooks.example.com") == 2  # one retry
    await n.aclose()


# --- building it from the config, secrets ---------------------------------------------------------------------------

def _settings(**hook):
    return NotificationsConfig(public_url=LINK, webhooks=[{"url": HOOK_URL, "name": "ha", **hook}])


def test_from_env_builds_channels_and_nothing_without_any():
    assert Notifier.from_env(NotificationsConfig(), store=SecretStore(data={}), env={}) is None
    n = Notifier.from_env(_settings(), store=SecretStore(data={}), env={})
    assert n.url is None and [w.name for w in n.webhooks] == ["ha"]
    n = Notifier.from_env(_settings(), store=SecretStore(data={}), env={"PENGUMON_NOTIFY_URL": "https://ntfy.sh/t"})
    assert n.url == "https://ntfy.sh/t"
    assert n.channels() == [{"name": "ntfy", "kind": "ntfy", "format": None, "host": "ntfy.sh"},
                            {"name": "ha", "kind": "webhook", "format": "json", "host": "hooks.example.com"}]
    assert "abc123" not in json.dumps(n.channels()) and "SEKRET" not in json.dumps(n.channels())


def test_header_secrets_come_from_env_file_or_store(tmp_path):
    (tmp_path / "pengumon_webhook_from_file").write_text("file-value\n")
    store = SecretStore(tmp_path / "s.json", data={"webhooks": {"from_store": "store-value"}})
    common = dict(secrets_dir=tmp_path, store=store)
    assert resolve_webhook_secret("from_env", env={"PENGUMON_WEBHOOK_SECRET_FROM_ENV": "env-value"}, **common) == "env-value"
    assert resolve_webhook_secret("from_file", env={}, **common) == "file-value"
    assert resolve_webhook_secret("from_store", env={}, **common) == "store-value"
    assert resolve_webhook_secret("nobody", env={}, **common) is None
    assert resolve_webhook_secret("from_env-x.y", env={"PENGUMON_WEBHOOK_SECRET_FROM_ENV_X_Y": "v"}, **common) == "v"


async def test_a_named_secret_is_sent_and_a_missing_one_drops_the_webhook(tmp_path, caplog):
    settings = _settings(headers={"Authorization": "secret:ha_token", "X-Plain": "plain"})
    n = Notifier.from_env(settings, env={"PENGUMON_WEBHOOK_SECRET_HA_TOKEN": "Bearer s3cr3t"}, secrets_dir=None,
                          store=SecretStore(tmp_path / "s.json", data={}))
    seen = []
    n._client = _client(lambda r: seen.append(r) or httpx.Response(200))
    n.on_incident_started(_incident())
    await _drain(n)
    assert seen[0].headers["Authorization"] == "Bearer s3cr3t" and seen[0].headers["X-Plain"] == "plain"
    await n.aclose()

    caplog.set_level(logging.WARNING)
    assert Notifier.from_env(settings, env={}, secrets_dir=None, store=SecretStore(tmp_path / "s.json", data={})) is None
    assert "ha_token" in caplog.text and "Webhook ha is not used" in caplog.text


async def test_send_test_reports_each_channel():
    def handler(request):
        return httpx.Response(404 if request.url.host == "bad.example.com" else 200)

    hooks = [_sender(url="http://good.example.com/x"), _sender(url="http://bad.example.com/x")]
    hooks[0].name, hooks[1].name = "good", "bad"
    n = Notifier("http://ntfy.test/t", client=_client(handler), webhooks=hooks)
    results = await n.send_test()
    assert results == [{"name": "ntfy", "ok": True, "detail": None}, {"name": "good", "ok": True, "detail": None},
                       {"name": "bad", "ok": False, "detail": "the server answered 404"}]
    await n.aclose()


# --- the endpoints --------------------------------------------------------------------------------------------------

@pytest.fixture
def api_state(monkeypatch):
    monkeypatch.setattr(security_config, "access_token", "")
    monkeypatch.setattr(security_config, "allowed_hosts", [])
    monkeypatch.setattr(security_config, "allowed_origins", [])
    previous = getattr(app.state, STATE_ATTR, None)
    state = AppState()
    setattr(app.state, STATE_ATTR, state)
    yield state
    setattr(app.state, STATE_ATTR, previous)


def _api():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_endpoints_without_a_channel(api_state):
    async with _api() as ac:
        assert (await ac.get("/api/notifications")).json() == {"channels": []}
        assert (await ac.post("/api/notifications/test")).status_code == 409


async def test_endpoints_list_channels_and_send_a_test(api_state):
    seen = []
    api_state.notifier = _notifier(seen, [_sender()], ntfy=None)
    async with _api() as ac:
        listed = (await ac.get("/api/notifications")).json()
        assert listed == {"channels": [{"name": "hook", "kind": "webhook", "format": "json", "host": "hooks.example.com"}]}
        assert "abc123" not in json.dumps(listed)
        tested = await ac.post("/api/notifications/test")
    assert tested.json() == {"channels": [{"name": "hook", "ok": True, "detail": None}]}
    assert json.loads(seen[0].content)["event"] == "test"
