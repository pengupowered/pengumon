"""The slow-line alert (speedtest.slow_alert_pct): the runner decides after a test that worked, once until a test is above
the line again; the notifier turns it into the `speed_slow` event (quiet hours apply, no incident). A fake provider and a
real repository on a temporary database; nothing reaches the network."""

import pytest

from pengumon.notify.webhook import json_payload
from pengumon.speedtest.providers.base import ProviderError, Transfer
from pengumon.speedtest.runner import SLOW_KEY, SpeedTestRunner
from test_notify_rules import QUIET, BERLIN, at, make
from test_speedtest_runner import Clock, FakeProvider, make_config, settle


def good(mbps=100.0):
    return Transfer(25_000_000, 4.0, mbps, "TST")


class Scripted:
    """A provider factory: each test takes the next (down, up) script; the notifications are recorded."""

    def __init__(self):
        self.scripts = []
        self.sent = []

    def runner(self, repo, clock, config):
        return SpeedTestRunner(config, repo, clock=clock, offset_source=lambda: 0, notify_slow=lambda *a, **k: self.sent.append(a),
                               provider_factory=lambda cfg: FakeProvider(clock, **self.scripts.pop(0)))


def planned(pct=50, down=100.0, up=20.0, **speedtest):
    config = make_config(slow_alert_pct=pct, **speedtest)
    config.line.down_mbps, config.line.up_mbps = down, up
    return config


async def run(runner):
    assert await runner.run_now() is None
    await settle(runner)


async def test_a_slow_test_alerts_once_and_again_only_after_a_good_one(repo):
    s, clock = Scripted(), Clock()
    runner = s.runner(repo, clock, planned())
    s.scripts = [{"down": good(30.0), "up": good(19.0)}, {"down": good(10.0), "up": good(19.0)},
                 {"down": good(90.0), "up": good(19.0)}, {"down": good(40.0), "up": good(19.0)}]
    await run(runner)
    assert s.sent == [(30.0, 19.0, 100.0, 20.0, 50)]          # (download, upload, plan down, plan up, alert level)
    await run(runner)
    assert len(s.sent) == 1                                      # still slow: not again
    await run(runner)
    assert len(s.sent) == 1                                      # a test above the line re-arms it ...
    await run(runner)
    assert len(s.sent) == 2 and s.sent[1][0] == 40.0             # ... and the next slow one alerts


async def test_the_line_is_a_share_of_the_plan_for_either_direction(repo):
    s, clock = Scripted(), Clock()
    runner = s.runner(repo, clock, planned(pct=50))
    s.scripts = [{"down": good(50.0), "up": good(10.0)}, {"down": good(100.0), "up": good(9.9)}]
    await run(runner)
    assert s.sent == []                                          # exactly at 50 %: not under
    await run(runner)
    assert len(s.sent) == 1 and s.sent[0][1] == 9.9              # a slow upload alone is slow


async def test_off_at_zero_and_without_a_plan_speed(repo):
    s, clock = Scripted(), Clock()
    s.scripts = [{"down": good(1.0), "up": good(1.0)}] * 3
    await run(s.runner(repo, clock, planned(pct=0)))
    await run(s.runner(repo, clock, planned(pct=50, down=None, up=None)))
    assert s.sent == []
    await run(s.runner(repo, clock, planned(pct=50, down=None, up=20.0)))   # one direction's plan is enough
    assert len(s.sent) == 1


async def test_a_partial_or_failed_test_neither_alerts_nor_re_arms(repo):
    s, clock = Scripted(), Clock()
    runner = s.runner(repo, clock, planned())
    s.scripts = [{"down": good(10.0), "up": ProviderError("refused")}, {"down": good(10.0), "up": good(19.0)},
                 {"down": ProviderError("x"), "up": ProviderError("y")}, {"down": good(10.0), "up": good(19.0)}]
    await run(runner)
    assert s.sent == []                                          # partial: not judged
    await run(runner)
    assert len(s.sent) == 1
    await run(runner)                                            # a failed test does not count as "above the line"
    await run(runner)
    assert len(s.sent) == 1


async def test_what_was_announced_survives_a_restart(repo):
    s, clock = Scripted(), Clock()
    s.scripts = [{"down": good(10.0), "up": good(19.0)}] * 2
    await run(s.runner(repo, clock, planned()))
    assert await repo.get_meta(SLOW_KEY) == "1"
    await run(s.runner(repo, clock, planned()))                  # a new runner (a restart), still slow
    assert len(s.sent) == 1


# --- the notification ---------------------------------------------------------------------------------------------

def test_the_notification_is_an_info_speed_slow_event_with_no_incident():
    n, t, sent = make(at(2026, 5, 4, 12))
    n.notify_speed_slow(30.0, 19.0, 100.0, 20.0, 50)
    (alert,) = sent
    assert (alert.event, alert.severity, alert.priority, alert.incident_id) == ("speed_slow", "info", "default", None)
    assert alert.message.splitlines() == ["Download: 30 of 100 Mbps (30 %) - under the alert level",
                                          "Upload: 19 of 20 Mbps (95 %)", "The alert level is 50 % of your plan speed."]
    payload = json_payload(alert, None)
    assert (payload["event"], payload["severity"], payload["incident_id"], payload["cause"]) == (
        "speed_slow", "info", None, None)


def test_quiet_hours_hold_it_and_a_newer_one_replaces_the_waiting_one():
    now = at(2026, 5, 4, 23, 0, BERLIN)
    n, t, sent = make(now, BERLIN, quiet_hours=QUIET)
    n.notify_speed_slow(30.0, 19.0, 100.0, 20.0, 50)
    n.notify_speed_slow(20.0, 19.0, 100.0, 20.0, 50)
    assert sent == []
    t.to(at(2026, 5, 5, 7, 0, BERLIN))
    assert [a.message.split("\n")[0] for a in sent] == ["Download: 20 of 100 Mbps (20 %) - under the alert level"]


def test_a_critical_only_filter_does_not_hold_it_back():
    n, t, sent = make(at(2026, 5, 4, 12), min_severity="critical")
    n.notify_speed_slow(30.0, 19.0, 100.0, 20.0, 50)
    assert [a.event for a in sent] == ["speed_slow"]             # like the route change: the filter is for incidents


def test_the_setting_is_zero_to_a_hundred_and_off_by_default():
    from pengumon.speedtest.settings import SpeedtestConfig
    from pydantic import ValidationError
    assert SpeedtestConfig().slow_alert_pct == 0
    assert SpeedtestConfig(slow_alert_pct=100).slow_alert_pct == 100
    for bad in (-1, 101):
        with pytest.raises(ValidationError):
            SpeedtestConfig(slow_alert_pct=bad)


async def test_a_direction_too_short_to_measure_the_line_neither_alerts_nor_re_arms(repo):
    # A gigabit plan: 25 MB read at 300 Mbps lasted 0.7 s (speedtest/quality.py: too short), so it is not a slow line.
    s, clock = Scripted(), Clock()
    runner = s.runner(repo, clock, planned(pct=50, down=1100.0, up=40.0))
    short_down, short_up = Transfer(25_000_000, 0.7, 300.0, "TST"), Transfer(1_000_000, 0.2, 40.0, "TST")
    slow_down = Transfer(100_000_000, 4.0, 200.0, "TST")          # 4 s: trusted, and under half of 1100
    s.scripts = [{"down": short_down, "up": good(38.0)}, {"down": slow_down, "up": good(38.0)},
                 {"down": Transfer(25_000_000, 0.2, 990.0, "TST"), "up": short_up}, {"down": slow_down, "up": good(38.0)}]
    await run(runner)
    assert s.sent == []                                          # the short download judges nothing; the upload is fine
    await run(runner)
    assert len(s.sent) == 1                                      # a trusted slow download alerts
    await run(runner)                                            # both directions too short: no re-arm ...
    await run(runner)
    assert len(s.sent) == 1                                      # ... so the next slow one does not alert again


async def test_the_alert_judges_the_lines_rate_the_router_measured_not_the_tests_own(repo):
    from test_speedtest_runner import Router, RouterPaced
    sent, clock, router = [], Clock(), Router()                  # the router: 800 Mbps down, 40 up during the test
    RouterPaced.router = router

    def build(config):
        runner = SpeedTestRunner(config, repo, clock=clock, offset_source=lambda: 0, wan_reader=router,
                                 notify_slow=lambda *a, **k: sent.append((a, k)),
                                 provider_factory=lambda cfg: RouterPaced(
                                     clock, down=Transfer(300_000_000, 8.0, 300.0, "TST"),   # the test's own: 300
                                     up=Transfer(38_000_000, 8.0, 38.0, "TST")))
        runner.wan_sample_s = 0.001
        return runner

    await run(build(planned(pct=50, down=1000.0, up=40.0)))
    assert sent == []                                            # 300 alone would be slow; the line carried 800
    await run(build(planned(pct=50, down=2000.0, up=40.0)))
    ((args, kw),) = sent
    assert args == (800.0, 40.0, 2000.0, 40.0, 50) and kw["measured_by"] == "router"
    assert kw["note"] is not None and "other traffic" in kw["note"]   # 300 of 800: the household's traffic


def test_the_notification_says_how_the_speed_was_measured():
    n, t, sent = make(at(2026, 5, 4, 12))
    n.notify_speed_slow(300.0, 19.0, 1000.0, 20.0, 50, measured_by="router", note="The test itself got ...")
    n.notify_speed_slow(30.0, 19.0, 100.0, 20.0, 50, measured_by="test")
    lines = [a.message.splitlines() for a in sent]
    assert "Measured by your router: what the line carried during the test." in lines[0]
    assert "The test itself got ..." in lines[0]
    assert "Measured by the test alone (one server; your router gave no reading of the line)." in lines[1]
