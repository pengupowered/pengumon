"""Settings applied live (W4): the notifier's channels from the settings and secrets, the hub reaching the notifier,
the service checks and the route-change checker, and the daily route check at a configured time in the configured zone.
The settings manager is not here: these tests change the live config in place and publish the change, as it will."""

import asyncio
import time

import httpx
import pytest

from pengumon.state import AppState
from pengumon.components import Context
from pengumon.config import AppConfig
from pengumon.db.repository import DatabaseRepository
from pengumon.engine.discovery import component as discovery_component
from pengumon.engine.discovery.drift import DAY_SECONDS, DriftChecker
from pengumon.main import app
from pengumon.notify.notifier import Notifier, build_channels
from pengumon.notify.component import register as register_notify
from pengumon.notify.settings import NotificationsConfig, WebhookConfig, WebhookItem
from pengumon.secrets import SecretStore
from pengumon.services import component as services_component
from pengumon.services.checker import ServiceChecker
from pengumon.services.settings import ServiceCheckConfig
from pengumon.settings.hub import Change, SettingsHub

from test_discovery_drift import MemoryRepo, Network, ROUTE_A, ROUTER, ISP_1, ISP_2, SERVERS, checker as drift_checker
from test_notify_rules import at

NO_ENV = {}


def store(**data):
    return SecretStore(data=data)


# --- the channels the settings describe ----------------------------------------------------------------------------------

def test_ntfy_comes_from_the_secret_slots_and_the_environment_wins():
    s = store(ntfy={"url": "https://ntfy.example.com/topic", "token": "tk_stored"})
    found = build_channels(NotificationsConfig(), s, NO_ENV)
    assert (found.url, found.token) == ("https://ntfy.example.com/topic", "tk_stored")
    env = {"PENGUMON_NOTIFY_URL": " https://env.example.com/t ", "PENGUMON_NOTIFY_TOKEN": ""}
    found = build_channels(NotificationsConfig(), s, env)
    assert found.url == "https://env.example.com/t" and found.token == "tk_stored"  # an empty variable does not win


def test_nothing_set_means_no_channel_and_no_error():
    found = build_channels(NotificationsConfig(), store(), NO_ENV)
    assert (found.url, found.token, found.senders) == (None, None, [])


def test_a_webhook_item_takes_its_url_from_the_secret_slot():
    hook = WebhookItem(name="Home", format="slack")
    cfg = NotificationsConfig.model_construct(public_url=None, webhooks=[hook])
    hook.id = "home"
    s = store(webhook_urls={"home": "https://hooks.example.com/services/T0/B0/XYZ"})
    [sender] = build_channels(cfg, s, NO_ENV).senders
    assert sender.name == "Home" and sender.format == "slack" and sender.host == "hooks.example.com"


def test_a_webhook_without_a_url_or_with_a_bad_one_is_left_out_and_the_log_never_shows_the_url(caplog):
    good, none_set, bad = (WebhookItem(id=i, name=i) for i in ("good", "none", "bad"))
    cfg = NotificationsConfig.model_construct(public_url=None, webhooks=[good, none_set, bad])
    s = store(webhook_urls={"good": "https://h.example.com/a", "bad": "ftp://secret-host.example.com/TOKEN123"})
    with caplog.at_level("ERROR", logger="pengumon.notify"):
        senders = build_channels(cfg, s, NO_ENV).senders
    assert [x.name for x in senders] == ["good"]
    assert "TOKEN123" not in caplog.text and "secret-host" not in caplog.text
    assert "none" in caplog.text and "bad" in caplog.text


def test_a_header_secret_comes_from_the_header_slot_and_a_missing_one_leaves_the_webhook_out():
    hook = WebhookItem(id="ha", name="ha", headers={"Authorization": "secret:ha_token"})
    cfg = NotificationsConfig.model_construct(public_url=None, webhooks=[hook])
    with_secret = store(webhook_urls={"ha": "https://h.example.com/a"}, webhooks={"ha_token": "Bearer abc"})
    [sender] = build_channels(cfg, with_secret, NO_ENV, secrets_dir=None).senders
    assert sender._headers == {"Authorization": "Bearer abc"}
    without = store(webhook_urls={"ha": "https://h.example.com/a"})
    assert build_channels(cfg, without, NO_ENV, secrets_dir=None).senders == []


def test_a_config_yaml_webhook_still_works_with_its_own_url():
    cfg = NotificationsConfig(webhooks=[WebhookConfig(url="https://h.example.com/a", name="x")])
    [sender] = build_channels(cfg, store(), NO_ENV).senders
    assert sender.host == "h.example.com"


def test_from_env_is_still_none_without_a_channel_and_build_is_never_none():
    assert Notifier.from_env(NotificationsConfig(), store(), NO_ENV) is None
    assert Notifier.build(NotificationsConfig(), store(), NO_ENV).channels() == []
    assert Notifier.from_env(NotificationsConfig(), store(ntfy={"url": "https://n.example.com/t"}), NO_ENV).url


# --- the hub reaches the notifier -------------------------------------------------------------------------------------

def context(config=None, secrets=None):
    config = config or AppConfig()
    ctx = Context(app=app, config=config, state=AppState(config=config, secret_store=secrets or store()))
    ctx.settings = SettingsHub()
    return ctx


async def test_the_notifier_is_always_built_and_a_saved_channel_arrives_without_a_restart():
    ctx = context()
    comp = await register_notify(ctx)
    notifier = ctx.state.notifier
    assert notifier is ctx.notifier and notifier.channels() == []
    ctx.state.secret_store.write({"ntfy": {"url": "https://ntfy.example.com/alerts"}})
    failed = await ctx.settings.publish(Change.of(slots=["ntfy.url"]))
    assert failed == [] and ctx.state.notifier is notifier  # the same object
    assert notifier.channels() == [{"name": "ntfy", "kind": "ntfy", "format": None, "host": "ntfy.example.com"}]
    await comp.stop()


async def test_a_new_webhook_its_url_slot_and_a_rule_change_apply_live():
    ctx = context()
    await register_notify(ctx)
    notifier = ctx.state.notifier
    ctx.config.notifications.webhooks[:] = [WebhookItem(id="chat", name="Chat", format="discord")]
    ctx.state.secret_store.write({"webhook_urls": {"chat": "https://discord.example.com/api/webhooks/1/TOKEN"}})
    ctx.config.alerts.min_severity = "critical"
    ctx.config.alerts.quiet_hours.enabled = True
    ctx.config.general.timezone = "Europe/Berlin"
    assert await ctx.settings.publish(Change.of(lists=["webhooks"], slots=["webhook.chat.url"],
                                                keys=["alerts.min_severity", "general.timezone"])) == []
    assert [c["name"] for c in notifier.channels()] == ["Chat"]
    assert notifier.policy.min_severity == "critical" and notifier.policy.timezone == "Europe/Berlin"
    assert notifier.policy.quiet.enabled is True
    await notifier.aclose()


async def test_the_public_url_change_reaches_the_senders_links():
    ctx = context(AppConfig(notifications={"webhooks": [{"url": "https://h.example.com/a", "name": "x"}]}))
    await register_notify(ctx)
    ctx.config.notifications.public_url = "https://pengu.example.com"
    await ctx.settings.publish(Change.of(keys=["notifications.public_url"]))
    assert ctx.state.notifier.public_url == "https://pengu.example.com"
    assert ctx.state.notifier.webhooks[0]._public_url == "https://pengu.example.com"
    await ctx.state.notifier.aclose()


async def test_the_notifier_subscribes_to_exactly_the_alert_keys_lists_and_slots():
    ctx = context()
    await register_notify(ctx)
    hub = ctx.settings
    for change in (Change.of(keys=["alerts.delay_seconds"]), Change.of(keys=["alerts.quiet_hours.end"]),
                   Change.of(keys=["general.timezone"]), Change.of(keys=["notifications.public_url"]),
                   Change.of(lists=["webhooks"]), Change.of(slots=["ntfy.token"]),
                   Change.of(slots=["webhook.x.url"]), Change.of(slots=["webhook_header.t"])):
        assert hub.subscribers(change) == ["notify.reconfigure"], change
    for change in (Change.of(keys=["prober.timeout_seconds"]), Change.of(lists=["devices"])):
        assert hub.subscribers(change) == []
    await ctx.state.notifier.aclose()


# --- the real delivery path, once -----------------------------------------------------------------------------------

async def test_a_reconfigured_notifier_delivers_to_the_new_webhook_only():
    seen = []

    def handler(request):
        seen.append((str(request.url), request.content))
        return httpx.Response(200)

    ctx = context()
    await register_notify(ctx)
    notifier = ctx.state.notifier
    notifier._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    ctx.config.notifications.webhooks[:] = [WebhookItem(id="chat", name="Chat", format="slack")]
    ctx.state.secret_store.write({"webhook_urls": {"chat": "https://h.example.com/new"}})
    await ctx.settings.publish(Change.of(lists=["webhooks"]))
    [result] = await notifier.send_test()
    assert result == {"name": "Chat", "ok": True, "detail": None}
    assert [u for u, _ in seen] == ["https://h.example.com/new"] and b'"text"' in seen[0][1]
    await notifier.aclose()


# --- the service checks -----------------------------------------------------------------------------------------------

A = ServiceCheckConfig(id="a", name="App A", url="http://127.0.0.1:9000/health")
B = ServiceCheckConfig(id="b", name="App B", url="http://127.0.0.1:9001/health")


class Hits:
    def __init__(self, status=200):
        self.status, self.urls = status, []

    def __call__(self, request):
        self.urls.append(str(request.url))
        return httpx.Response(self.status)


async def service_context(tmp_path, *services, hits=None):
    repo = DatabaseRepository(db_path=str(tmp_path / "t.db"))
    await repo.initialize()
    config = AppConfig(service_checks={"services": [s.model_dump() for s in services], "failures_for_incident": 1})
    ctx = Context(app=app, config=config, state=AppState(config=config, repository=repo))
    ctx.settings = SettingsHub()
    comp = await services_component.register(ctx)
    checker = ctx.state.service_checker
    hits = hits or Hits()
    checker._client_factory = lambda svc: httpx.AsyncClient(transport=httpx.MockTransport(hits))
    return ctx, comp, checker, repo, hits


async def test_the_checker_exists_with_no_service_and_checks_one_added_live(tmp_path):
    ctx, comp, checker, repo, hits = await service_context(tmp_path)
    assert comp is not None and ctx.state.service_checker is checker and checker.services == []
    await checker.poll()
    assert hits.urls == []
    ctx.config.service_checks.services[:] = [A]
    assert await ctx.settings.publish(Change.of(lists=["services"])) == []
    await checker.poll()
    assert hits.urls == [A.url] and checker.status["a"].ok is True
    await comp.stop()
    await repo.close()


async def test_a_removed_service_loses_its_status_open_incident_and_waiting_alert(tmp_path):
    hits = Hits(503)
    ctx, comp, checker, repo, _ = await service_context(tmp_path, A, B, hits=hits)
    forgotten = []
    ctx.notifier = type("N", (), {"forget_service": staticmethod(forgotten.append)})
    await checker.poll()
    assert len(await repo.get_recent_incidents()) == 2 and set(checker.status) == {"a", "b"}
    ctx.config.service_checks.services[:] = [A]
    reset = await checker.set_services()
    assert reset == ["b"] and set(checker.status) == {"a"}
    incidents = {i.affected_targets[0]: i for i in await repo.get_recent_incidents()}
    assert incidents["b"].resolved and not incidents["a"].resolved
    await comp.stop()
    await repo.close()


async def test_a_changed_url_gets_a_fresh_client_and_status_but_a_renamed_service_keeps_its_history(tmp_path):
    ctx, comp, checker, repo, hits = await service_context(tmp_path, A)
    await checker.poll()
    assert checker.status["a"].ok is True
    ctx.config.service_checks.services[0].name = "Renamed"
    assert await checker.set_services() == [] and "a" in checker.status
    ctx.config.service_checks.services[0].url = "http://127.0.0.1:9100/health"
    assert await checker.set_services() == ["a"] and "a" not in checker.status
    await checker.poll()
    assert hits.urls[-1] == "http://127.0.0.1:9100/health"
    await comp.stop()
    await repo.close()


async def test_a_disabled_service_is_dropped_and_comes_back_when_enabled(tmp_path):
    ctx, comp, checker, repo, hits = await service_context(tmp_path, A)
    await checker.poll()
    ctx.config.service_checks.services[0].enabled = False
    assert await checker.set_services() == ["a"] and checker.services == []
    ctx.config.service_checks.services[0].enabled = True
    assert await checker.set_services() == []
    await checker.poll()
    assert checker.status["a"].ok is True
    await comp.stop()
    await repo.close()


async def test_the_interval_is_read_every_loop(monkeypatch):
    """A 30 s interval shortened to 4 s while the worker waits: the next poll comes after about 4 s, not 30."""
    clock, interval, polled_at, sleeps = [0.0], [30.0], [], []

    class Polls:
        async def poll(self, force=False):
            polled_at.append(clock[0])
            if len(polled_at) == 3:
                raise asyncio.CancelledError

    async def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds
        if clock[0] >= 6:
            interval[0] = 4.0  # the owner saved a shorter interval

    monkeypatch.setattr(services_component, "asyncio",
                        type("A", (), {"sleep": staticmethod(sleep), "CancelledError": asyncio.CancelledError}))
    monkeypatch.setattr(services_component.time, "monotonic", lambda: clock[0])
    await services_component.collector_background_worker("t", Polls(), lambda: interval[0])
    assert max(sleeps) <= services_component.MAX_SLEEP_SECONDS  # never one long sleep
    assert polled_at[1] < 30.0 and polled_at[2] - polled_at[1] <= 4.1  # not two old-length waits


# --- route-change checks: the live flags and the schedule ---------------------------------------------------------------

class Settings:
    def __init__(self, background=True, asn_lookup=False, daily_check_time=None):
        self.background, self.asn_lookup, self.daily_check_time = background, asn_lookup, daily_check_time


def live_checker(settings, tz="UTC", **kw):
    net = Network(ROUTE_A, [ROUTER, ISP_1, ISP_2, *SERVERS])
    drift, sent = drift_checker(net, settings=settings, timezone=lambda: tz, **kw)
    return drift, net, sent


def test_the_asn_lookup_and_background_flags_are_read_live():
    settings = Settings()
    drift, _, _ = live_checker(settings)
    assert drift.options.asn_lookup is False and drift.enabled
    settings.asn_lookup, settings.background = True, False
    assert drift.options.asn_lookup is True and not drift.enabled


async def test_a_disabled_checker_shows_nothing_and_schedules_no_incident_check():
    settings = Settings(background=False)
    drift, _, _ = live_checker(settings)
    assert (await drift.public())["state"] == "never"
    drift.incident_ended()
    assert drift._incident_task is None


def test_the_daily_check_runs_at_the_configured_time_in_the_configured_zone():
    settings = Settings(daily_check_time="03:00")
    drift, _, _ = live_checker(settings, "Europe/Berlin")
    now = at(2026, 5, 4, 12, 0, "UTC")
    assert drift.next_daily(now) == at(2026, 5, 5, 3, 0, "Europe/Berlin")
    settings.daily_check_time = None
    assert drift.next_daily(now) != at(2026, 5, 5, 3, 0, "Europe/Berlin")  # the random minute (UTC) again
    assert drift.next_daily(now) - now <= DAY_SECONDS


def test_the_daily_time_follows_a_daylight_saving_change():
    settings = Settings(daily_check_time="03:00")
    drift, _, _ = live_checker(settings, "America/New_York")
    before = at(2026, 3, 7, 12, 0, "America/New_York")
    first = drift.next_daily(before)
    assert first == at(2026, 3, 8, 3, 0, "America/New_York")  # 03:00 exists (EDT) on the 23-hour day
    second = drift.next_daily(first)
    assert second - first == 24 * 3600 and second == at(2026, 3, 9, 3, 0, "America/New_York")


async def eventually(condition, seconds=2.0):
    """Lets the loop run until `condition()` holds (or `seconds` pass). Not a fixed sleep: on Windows asyncio treats a
    sleep shorter than its clock resolution (about 15.6 ms) as due at once, which is one turn of the loop."""
    deadline = time.monotonic() + seconds
    while not condition() and time.monotonic() < deadline:
        await asyncio.sleep(0.005)
    return condition()


async def test_wake_makes_the_daily_loop_work_out_its_next_check_again():
    settings = Settings()
    sleeps = []
    release = asyncio.Event()

    async def sleep(seconds):
        sleeps.append(seconds)
        await release.wait()

    drift, net, sent = live_checker(settings, sleep=sleep, clock=lambda: 1_000_000.0)
    task = asyncio.ensure_future(drift.run_daily())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert len(sleeps) == 1
    settings.daily_check_time = "12:00"
    drift.wake()
    assert await eventually(lambda: len(sleeps) == 2)
    assert sleeps[1] != sleeps[0]
    assert net.runs == []  # waking did not run a check
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_a_switched_off_daily_loop_waits_and_starts_again_when_switched_on():
    settings = Settings(background=False)
    sleeps = []

    async def sleep(seconds):
        sleeps.append(seconds)
        await asyncio.sleep(3600)

    drift, net, sent = live_checker(settings, sleep=sleep)
    task = asyncio.ensure_future(drift.run_daily())
    await asyncio.sleep(0.01)
    assert sleeps == []  # off: no timer at all
    settings.background = True
    drift.wake()
    assert await eventually(lambda: len(sleeps) == 1)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_the_component_always_builds_the_checker_and_the_hub_wakes_it():
    config = AppConfig(discovery={"background": False})
    state = AppState(config=config, repository=MemoryRepo())
    ctx = Context(app=app, config=config, state=state)
    ctx.settings = SettingsHub()
    comp = await discovery_component.register(ctx)
    drift = state.drift
    assert isinstance(drift, DriftChecker) and not drift.enabled
    config.discovery.background = True
    config.discovery.asn_lookup = True
    assert ctx.settings.subscribers(Change.of(keys=["discovery.background"])) == ["discovery.drift"]
    assert ctx.settings.subscribers(Change.of(keys=["general.timezone"])) == ["discovery.drift"]
    await ctx.settings.publish(Change.of(keys=["discovery.background", "discovery.asn_lookup"]))
    assert drift._wake.is_set() and drift.enabled and drift.options.asn_lookup is True
    await comp.stop()


# --- the settings manager's secret_value is used when there is one --------------------------------------------------

def test_secret_value_of_the_manager_feeds_ntfy_webhook_urls_and_header_secrets():
    values = {"ntfy.url": "https://ntfy.example.com/m", "ntfy.token": "tk_m", "webhook.ha.url": "https://h.example.com/m",
              "webhook_header.tok": "Bearer m"}
    hook = WebhookItem(id="ha", name="ha", headers={"Authorization": "secret:tok"})
    cfg = NotificationsConfig.model_construct(public_url=None, webhooks=[hook])
    found = build_channels(cfg, store(), NO_ENV, secret_value=values.get)  # the store is empty: only the manager knows
    assert (found.url, found.token) == ("https://ntfy.example.com/m", "tk_m")
    assert found.senders[0]._headers == {"Authorization": "Bearer m"} and found.senders[0].host == "h.example.com"
    assert build_channels(cfg, store(), NO_ENV, secret_value={}.get).senders == []
