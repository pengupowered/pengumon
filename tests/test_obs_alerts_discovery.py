"""Telemetry of discovery, route-change checks, traceroute and alerts: runs by trigger and outcome with their spans,
changes found and confirmed, traces, and what becomes of every alert (never its address, topic or text)."""

import asyncio
import logging

import httpx
import pytest

import obs_helpers
from icmp_fakes import FakeBackend, FakePath
from obs_helpers import FakeTracer
from discovery_fakes import discover, three_paths
from pengumon.notify import notifier as notify_module
from pengumon.engine.discovery import engine as engine_module, traces as traces_module
from pengumon.engine.mtr import MtrEngine
from pengumon.observability import export
from pengumon.observability.gauges import GAUGES

# Fixtures of the neighbouring suites: a notifier with a fake clock, a route that changes, a webhook with a mock transport.
from test_discovery_drift import APPLIED, DOWN, ROUTE_A, ROUTE_B, Network, checker
from test_notify_rules import QUIET, at, incident, make
from test_webhooks import _alert, _client, _sender

obs_helpers.install()
SHARED = [("192.168.1.1", 0.6), ("203.0.113.1", 9.0)]


def _runs(trigger, outcome):
    return obs_helpers.total("pengumon.discovery.runs", trigger=trigger, outcome=outcome)


@pytest.fixture
def tracers(monkeypatch):
    tracer = FakeTracer()
    for module in (engine_module, traces_module, notify_module):
        monkeypatch.setattr(module, "tracer", tracer)
    from pengumon.engine.discovery import drift
    monkeypatch.setattr(drift, "tracer", tracer)
    return tracer


# --- discovery runs -------------------------------------------------------------------------------------------------

async def test_a_manual_run_is_counted_timed_and_has_a_span_with_a_child_per_step(tracers):
    runs, took = _runs("manual", "done"), obs_helpers.total("pengumon.discovery.duration", trigger="manual")
    await discover(three_paths(SHARED))
    assert _runs("manual", "done") == runs + 1
    assert obs_helpers.total("pengumon.discovery.duration", trigger="manual") == took + 1
    run = tracers.named("discovery.run")[0]
    assert run.attributes["discovery.trigger"] == "manual" and run.attributes["discovery.outcome"] == "done"
    assert len(tracers.named("discovery.traces")) == 1 and len(tracers.named("discovery.checks")) == 1
    traced = tracers.named("discovery.trace")
    assert [s.attributes["discovery.trace.n"] for s in traced] == [1, 2, 3]
    assert all(s.attributes["discovery.trace.hops"] > 0 for s in traced)


async def test_a_run_that_reached_no_server_is_no_answer_and_one_that_raised_is_failed(tracers):
    quiet = _runs("manual", "no_answer")
    await discover({})
    assert _runs("manual", "no_answer") == quiet + 1
    failed = _runs("manual", "failed")

    async def broken(host):
        raise RuntimeError("no ICMP")

    from pengumon.engine.discovery.engine import Deps, DiscoveryOptions, run_discovery
    with pytest.raises(RuntimeError):
        await run_discovery(DiscoveryOptions(), lambda: [], Deps(trace=broken))
    assert _runs("manual", "failed") == failed + 1
    assert tracers.named("discovery.run")[-1].attributes["discovery.outcome"] == "failed"


# --- route-change checks --------------------------------------------------------------------------------------------

async def test_a_check_that_finds_a_change_runs_twice_and_counts_found_and_confirmed(tracers):
    daily, confirm = _runs("daily", "done"), _runs("confirm", "done")
    found = {k: obs_helpers.total("pengumon.drift.route_changes", kind=k, stage="found") for k in ("new", "gone")}
    confirmed = {k: obs_helpers.total("pengumon.drift.route_changes", kind=k, stage="confirmed") for k in ("new", "gone")}
    checks = obs_helpers.total("pengumon.drift.checks", reason="daily", outcome="changed")
    drift, _ = checker(Network(ROUTE_B, APPLIED))
    await drift.check()
    assert _runs("daily", "done") == daily + 1 and _runs("confirm", "done") == confirm + 1
    for kind in ("new", "gone"):
        assert obs_helpers.total("pengumon.drift.route_changes", kind=kind, stage="found") == found[kind] + 1
        assert obs_helpers.total("pengumon.drift.route_changes", kind=kind, stage="confirmed") == confirmed[kind] + 1
    assert obs_helpers.total("pengumon.drift.checks", reason="daily", outcome="changed") == checks + 1
    check = tracers.named("discovery.drift_check")[0]
    assert check.attributes == {"drift.reason": "daily", "drift.outcome": "changed"}


async def test_an_unchanged_route_runs_once_and_counts_no_change():
    incident_runs = _runs("incident", "done")
    confirm = _runs("confirm", "done")
    unchanged = obs_helpers.total("pengumon.drift.checks", reason="incident", outcome="unchanged")
    found = obs_helpers.total("pengumon.drift.route_changes", stage="found")
    drift, _ = checker(Network(ROUTE_A, APPLIED))
    await drift.check("incident")
    assert _runs("incident", "done") == incident_runs + 1 and _runs("confirm", "done") == confirm
    assert obs_helpers.total("pengumon.drift.checks", reason="incident", outcome="unchanged") == unchanged + 1
    assert obs_helpers.total("pengumon.drift.route_changes", stage="found") == found


async def test_a_check_in_an_outage_is_failed_and_its_run_no_answer():
    failed = obs_helpers.total("pengumon.drift.checks", reason="daily", outcome="failed")
    quiet = _runs("daily", "no_answer")
    drift, _ = checker(Network(DOWN, APPLIED))
    await drift.check()
    assert obs_helpers.total("pengumon.drift.checks", reason="daily", outcome="failed") == failed + 1
    assert _runs("daily", "no_answer") == quiet + 1


async def test_a_manual_run_after_a_check_is_still_manual():
    drift, _ = checker(Network(ROUTE_A, APPLIED))
    await drift.check()
    manual = _runs("manual", "done")
    await discover(three_paths(SHARED))
    assert _runs("manual", "done") == manual + 1


# --- traceroute -----------------------------------------------------------------------------------------------------

async def test_a_traceroute_records_its_time_and_hops_by_family_without_the_wait_for_its_turn():
    count = obs_helpers.total("pengumon.traceroute.hops", family="4")
    hops = obs_helpers.histogram_sum("pengumon.traceroute.hops", family="4")
    backend = FakeBackend(path=FakePath(["192.168.1.1", "198.51.100.1", "198.51.100.2"]))
    trace = await MtrEngine("1.1.1.1", max_hops=15, timeout=0.2, backend=backend).run_trace()
    assert len(trace) == 4
    assert obs_helpers.total("pengumon.traceroute.hops", family="4") == count + 1
    assert obs_helpers.histogram_sum("pengumon.traceroute.hops", family="4") == hops + 4
    assert obs_helpers.total("pengumon.traceroute.duration", family="4") >= 1
    assert obs_helpers.histogram_sum("pengumon.traceroute.duration", family="4") > 0


# --- alerts: decisions ----------------------------------------------------------------------------------------------

def _alerts(event, outcome, channel="all"):
    return obs_helpers.total("pengumon.alerts", event=event, outcome=outcome, channel=channel)


def test_an_alert_the_severity_filter_refuses_is_counted_as_dropped():
    dropped = _alerts("incident_started", "dropped")
    n, t, sent = make(at(2026, 5, 4, 12), min_severity="critical")
    n.on_incident_started(incident(start=t.now))
    assert sent == [] and _alerts("incident_started", "dropped") == dropped + 1


def test_a_delay_counts_one_delayed_alert_and_its_timer_firing_counts_nothing_more():
    delayed, sent_now = _alerts("incident_started", "delayed"), _alerts("incident_started", "dropped")
    n, t, sent = make(at(2026, 5, 4, 12), delay_seconds=300)
    n.on_incident_started(incident(start=t.now))
    assert _alerts("incident_started", "delayed") == delayed + 1
    t.after(301)
    assert len(sent) == 1
    assert _alerts("incident_started", "delayed") == delayed + 1  # (the timer asking again is not a new decision)
    assert _alerts("incident_started", "dropped") == sent_now


def test_quiet_hours_count_a_held_alert_once_and_the_queue_gauge_sees_it():
    held = _alerts("incident_started", "held")
    n, t, sent = make(at(2026, 5, 4, 23), quiet_hours=QUIET)
    n.on_incident_started(incident(start=t.now))
    assert _alerts("incident_started", "held") == held + 1 and sent == []
    from types import SimpleNamespace
    gauge = next(g for g in GAUGES if g.otel == "pengumon.alerts.held")
    assert gauge.collect(SimpleNamespace(notifier=n)) == [({}, 1)]
    n.policy.quiet.enabled = False
    n.reconfigure(None, None, [], None, n.policy)  # the rules changed: timed again, not a second decision
    assert _alerts("incident_started", "held") == held + 1 and len(sent) == 1
    assert gauge.collect(SimpleNamespace(notifier=n)) == [({}, 0)]
    assert gauge.collect(SimpleNamespace(notifier=None)) == []


def test_a_repeat_inside_the_window_is_counted_as_suppressed():
    suppressed = _alerts("incident_started", "suppressed")
    n, t, sent = make(at(2026, 5, 4, 12), repeat_window_seconds=300)
    n.on_incident_started(incident(1, start=t.now))
    n.on_incident_started(incident(2, start=t.now))
    assert len(sent) == 1 and _alerts("incident_started", "suppressed") == suppressed + 1


def test_a_service_alert_held_by_quiet_hours_is_counted_under_its_own_event():
    from test_notify_rules import Err, Service
    held = _alerts("service_down", "held")
    n, t, sent = make(at(2026, 5, 4, 23), quiet_hours=QUIET)
    n.notify_service_down(Service(), Err(), 120)
    assert _alerts("service_down", "held") == held + 1


# --- alerts: deliveries ---------------------------------------------------------------------------------------------

async def test_each_channel_counts_sent_and_failed_with_a_span_and_nothing_of_the_address(tracers):
    def handler(request):
        return httpx.Response(500 if request.url.host == "bad.example.org" else 200)

    from pengumon.notify.notifier import Notifier
    from pengumon.notify.settings import WebhookConfig
    from pengumon.notify.webhook import WebhookSender
    good = WebhookSender(WebhookConfig(url="https://hooks.example.com/api/SEKRET-PATH?token=SEKRET-QUERY", id="homechat",
                                       name="Home chat"), {})
    bad = WebhookSender(WebhookConfig(url="https://bad.example.org/hook/SEKRET2", id="pager", name="Pager"), {},
                        retry_delay=0.0)
    notifier = Notifier("https://ntfy.example.net/SEKRET-TOPIC", client=_client(handler), webhooks=[good, bad])
    before = {c: (_alerts("incident_started", o, c)) for c, o in (("ntfy", "sent"), ("homechat", "sent"),
                                                                   ("pager", "failed"))}
    timed = obs_helpers.total("pengumon.alert.delivery.duration", channel="pager")
    notifier._dispatch(_alert())
    await asyncio.gather(*list(notifier._tasks))
    assert _alerts("incident_started", "sent", "ntfy") == before["ntfy"] + 1
    assert _alerts("incident_started", "sent", "homechat") == before["homechat"] + 1
    assert _alerts("incident_started", "failed", "pager") == before["pager"] + 1
    assert obs_helpers.total("pengumon.alert.delivery.duration", channel="pager") == timed + 1
    spans = {s.attributes["alert.channel"]: s.attributes for s in tracers.named("alert.deliver")}
    assert spans["pager"]["alert.outcome"] == "failed" and spans["ntfy"]["alert.outcome"] == "sent"
    everything = [str(v) for name in ("pengumon.alerts", "pengumon.alert.delivery.duration")
                  for attrs, _ in obs_helpers.points(name) for v in attrs.values()]
    shown = " ".join(everything) + str([s.attributes for s in tracers.spans])
    assert not any(secret in shown for secret in ("SEKRET", "example.net", "example.com", "example.org"))
    await notifier.aclose()


async def test_the_test_alert_is_counted_as_a_test_per_channel():
    from pengumon.notify.notifier import Notifier
    sent = _alerts("test", "sent", "ntfy")
    notifier = Notifier("https://ntfy.example.net/t", client=_client(lambda request: httpx.Response(200)))
    results = await notifier.send_test()
    assert results == [{"name": "ntfy", "ok": True, "detail": None}]
    assert _alerts("test", "sent", "ntfy") == sent + 1
    await notifier.aclose()


async def test_a_failed_ntfy_post_logs_the_error_type_and_status_but_not_the_topic(caplog):
    from pengumon.notify.notifier import Notifier
    notifier = Notifier("https://ntfy.example.net/SEKRET-TOPIC", client=_client(lambda request: httpx.Response(403)))
    with caplog.at_level(logging.WARNING, logger="pengumon.notify"):
        assert await notifier._send_ntfy(_alert()) == "the server answered 403"
    assert "HTTPStatusError (HTTP 403)" in caplog.text and "SEKRET" not in caplog.text and "example.net" not in caplog.text
    await notifier.aclose()
