"""Alerts per outage (notify/notifier.py, incidents/outages.py): a flapping line is one start alert and one resolved alert.
Same fake clock and scheduler as test_notify_rules.py; nothing here sleeps."""

from pengumon.incidents.model import Incident
from pengumon.incidents.settings import IncidentsConfig
from pengumon.notify.notifier import Notifier
from pengumon.notify.policy import AlertPolicy
from pengumon.notify.settings import AlertsConfig
from pengumon.notify.webhook import json_payload

from test_notify_rules import BERLIN, QUIET, FakeTime, at

LINE = "access_line_drop"
ROUTER = "local_wifi_mesh_drop"
MIN = 60.0


def make(now, gap_minutes=10, tz="UTC", **alerts):
    """(notifier, fake time, alerts sent), grouping on with a gap of `gap_minutes` (0: off)."""
    quiet = alerts.pop("quiet_hours", {})
    time_ = FakeTime(now)
    policy = AlertPolicy(AlertsConfig(quiet_hours=quiet, **alerts), tz, IncidentsConfig(group_gap_minutes=gap_minutes))
    notifier = Notifier("http://ntfy.test/t", wall=time_, clock=time_, policy=policy, schedule=time_.schedule)
    sent = []
    notifier._dispatch = sent.append
    return notifier, time_, sent


def drop(iid, t, cause=LINE, severity="critical", kind=None):
    return Incident(id=iid, start_time=t.now, cause_code=cause, title="Line outage" if cause == LINE else "Router drop",
                    description="Packets are lost", severity=severity, kind=kind)


def end(n, t, inc):
    inc.end_time, inc.resolved, inc.duration_seconds = t.now, True, t.now - inc.start_time
    n.on_incident_resolved(inc)


def events(sent):
    return [a.event for a in sent]


def test_a_flapping_line_is_one_start_and_one_resolved():
    n, t, sent = make(at(2026, 5, 4, 12))
    first = drop(1, t)
    n.on_incident_started(first)
    assert events(sent) == ["incident_started"]
    t.after(60)
    end(n, t, first)
    t.after(120)                                  # the line is back for 2 minutes: nothing yet
    assert events(sent) == ["incident_started"]
    second = drop(2, t)
    n.on_incident_started(second)
    t.after(30)
    end(n, t, second)
    t.after(200)
    third = drop(3, t)
    n.on_incident_started(third)
    t.after(45)
    end(n, t, third)
    t.after(10 * MIN - 1)
    assert events(sent) == ["incident_started"]   # not over until the gap has passed with no new drop
    t.after(1)
    assert events(sent) == ["incident_started", "incident_resolved"]
    started, resolved = sent
    assert (started.outage_id, started.drops, started.incident_id) == (1, 1, 1)
    assert resolved.title == "Pengumon: resolved, Line outage" and (resolved.outage_id, resolved.drops) == (1, 3)
    assert "Drops: 3 (3 critical)" in resolved.message and "Down: " in resolved.message
    assert resolved.ended_at == third.end_time and resolved.started_at == first.start_time
    t.after(24 * 3600)
    assert events(sent) == ["incident_started", "incident_resolved"]   # and it is only sent once
    assert n.pending_count() == 0


def test_a_drop_that_joins_sends_nothing_even_when_the_first_was_announced_earlier():
    n, t, sent = make(at(2026, 5, 4, 12))
    first = drop(1, t)
    n.on_incident_started(first)
    end(n, t, first)
    for k in range(2, 6):
        t.after(90)
        d = drop(k, t)
        n.on_incident_started(d)
        t.after(20)
        end(n, t, d)
    assert events(sent) == ["incident_started"]


def test_a_drop_after_the_gap_starts_a_new_outage():
    n, t, sent = make(at(2026, 5, 4, 12))
    first = drop(1, t)
    n.on_incident_started(first)
    t.after(30)
    end(n, t, first)
    t.after(10 * MIN)
    assert events(sent) == ["incident_started", "incident_resolved"]
    t.after(10 * MIN)                              # past the repeat window as well
    second = drop(2, t)
    n.on_incident_started(second)
    t.after(30)
    end(n, t, second)
    t.after(10 * MIN)
    assert events(sent) == ["incident_started", "incident_resolved", "incident_started", "incident_resolved"]
    assert [a.outage_id for a in sent] == [1, 1, 2, 2]


def test_a_drop_in_the_last_moment_of_the_gap_cancels_the_resolved_alert():
    n, t, sent = make(at(2026, 5, 4, 12))
    first = drop(1, t)
    n.on_incident_started(first)
    t.after(30)
    end(n, t, first)
    t.after(10 * MIN - 5)
    second = drop(2, t)
    n.on_incident_started(second)
    t.after(10 * MIN)                              # the first resolved timer would have fired by now
    assert events(sent) == ["incident_started"]
    end(n, t, second)
    t.after(10 * MIN)
    assert events(sent) == ["incident_started", "incident_resolved"] and sent[-1].drops == 2


def test_the_gap_off_is_exactly_the_old_behavior():
    n, t, sent = make(at(2026, 5, 4, 12), gap_minutes=0)
    first = drop(1, t)
    n.on_incident_started(first)
    t.after(30)
    end(n, t, first)
    assert events(sent) == ["incident_started", "incident_resolved"]   # the resolved goes out at once
    assert sent[1].title == "Pengumon: resolved, Line outage" and "Drops" not in sent[1].message
    t.after(400)                                                       # past the repeat window; right after the end
    second = drop(2, t)
    n.on_incident_started(second)
    assert events(sent)[2:] == ["incident_started"]                    # a second drop is announced on its own
    t.after(20)
    end(n, t, second)
    assert events(sent)[3:] == ["incident_resolved"]
    assert [(a.outage_id, a.drops) for a in sent] == [(1, 1)] * 2 + [(2, 1)] * 2   # each is an outage of one
    assert n.pending_count() == 0


def test_an_incident_that_is_never_grouped_keeps_its_own_resolved_alert():
    n, t, sent = make(at(2026, 5, 4, 12))
    odd = drop(1, t, cause="unknown_outage")
    n.on_incident_started(odd)
    t.after(10)
    end(n, t, odd)
    assert events(sent) == ["incident_started", "incident_resolved"]
    assert sent[0].outage_id is None and sent[0].drops is None


def test_a_drop_at_a_place_not_next_to_the_outages_is_its_own_outage():
    n, t, sent = make(at(2026, 5, 4, 12))
    line = drop(1, t)
    n.on_incident_started(line)
    t.after(20)
    router = drop(2, t, cause=ROUTER)               # the router is two steps from the line: not the same event
    n.on_incident_started(router)
    assert events(sent) == ["incident_started", "incident_started"] and [a.outage_id for a in sent] == [1, 2]
    end(n, t, router)
    end(n, t, line)
    t.after(10 * MIN)
    assert events(sent).count("incident_resolved") == 2


def test_a_warning_that_was_filtered_is_announced_when_a_critical_drop_joins():
    n, t, sent = make(at(2026, 5, 4, 12), min_severity="critical")
    first = drop(1, t, severity="warning")
    n.on_incident_started(first)
    t.after(20)
    end(n, t, first)
    assert sent == []
    t.after(60)
    second = drop(2, t)
    n.on_incident_started(second)                  # joins; the outage is now critical, so it is announced
    assert events(sent) == ["incident_started"] and sent[0].severity == "critical" and sent[0].drops == 2
    t.after(30)
    end(n, t, second)
    t.after(10 * MIN)
    assert events(sent) == ["incident_started", "incident_resolved"]


def test_an_outage_never_announced_sends_no_resolved_alert():
    n, t, sent = make(at(2026, 5, 4, 12), min_severity="critical")
    for k in (1, 2):
        d = drop(k, t, severity="warning")
        n.on_incident_started(d)
        t.after(20)
        end(n, t, d)
        t.after(60)
    t.after(24 * 3600)
    assert sent == [] and n.pending_count() == 0


def test_an_outage_over_before_the_delay_sends_nothing():
    n, t, sent = make(at(2026, 5, 4, 12), delay_seconds=120)
    d = drop(1, t)
    n.on_incident_started(d)
    t.after(30)
    end(n, t, d)
    t.after(24 * 3600)
    assert sent == [] and n.pending_count() == 0


def test_the_delay_runs_from_the_outages_start_and_a_later_drop_asks_again():
    n, t, sent = make(at(2026, 5, 4, 12), delay_seconds=120)
    first = drop(1, t)
    n.on_incident_started(first)
    t.after(30)
    end(n, t, first)
    t.after(100)                                    # the 120 s are up: the outage is over for now, so nothing went out
    assert sent == []
    second = drop(2, t)
    n.on_incident_started(second)                   # joins; considered as an escalation is, a delay from now
    assert sent == []
    t.after(119)
    assert sent == []
    t.after(2)
    assert events(sent) == ["incident_started"]


def test_a_resolved_alert_waits_out_quiet_hours():
    n, t, sent = make(at(2026, 5, 4, 21, 0, BERLIN), tz=BERLIN, quiet_hours=QUIET)
    d = drop(1, t, severity="warning")
    n.on_incident_started(d)
    assert events(sent) == ["incident_started"]     # 21:00 is before the quiet hours
    t.after(50 * MIN)
    end(n, t, d)                                    # over at 21:50; the gap passes at 22:00, inside quiet hours
    t.to(at(2026, 5, 4, 22, 0, BERLIN) + 1)
    assert events(sent) == ["incident_started"]
    t.to(at(2026, 5, 5, 7, 0, BERLIN) - 1)
    assert events(sent) == ["incident_started"]
    t.to(at(2026, 5, 5, 7, 0, BERLIN))
    assert events(sent) == ["incident_started", "incident_resolved"]


def test_a_critical_outage_is_not_held_by_quiet_hours_and_neither_is_its_resolved_alert():
    n, t, sent = make(at(2026, 5, 4, 23, 0, BERLIN), tz=BERLIN, quiet_hours=dict(QUIET, critical_still=True))
    d = drop(1, t)
    n.on_incident_started(d)
    t.after(30)
    end(n, t, d)
    t.after(10 * MIN)
    assert events(sent) == ["incident_started", "incident_resolved"]


def test_the_alerts_carry_the_outage_for_webhooks():
    n, t, sent = make(at(2026, 5, 4, 12))
    first = drop(1, t)
    n.on_incident_started(first)
    t.after(10)
    end(n, t, first)
    t.after(20)
    second = drop(2, t)
    n.on_incident_started(second)
    t.after(10)
    end(n, t, second)
    t.after(10 * MIN)
    start, done = (json_payload(a, None) for a in sent)
    assert (start["event"], start["outage_id"], start["drops"]) == ("incident_started", 1, 1)
    assert (done["event"], done["outage_id"], done["drops"], done["incident_id"]) == ("incident_resolved", 1, 2, 1)


def test_a_restart_forgets_the_live_outage():
    n, t, sent = make(at(2026, 5, 4, 12))
    first = drop(1, t)
    n.on_incident_started(first)
    end(n, t, first)
    t.after(120)
    fresh, _, fresh_sent = make(t.now)             # a restarted Notifier
    fresh.on_incident_started(drop(2, t))
    assert events(fresh_sent) == ["incident_started"] and fresh_sent[0].outage_id == 2


def test_a_drop_that_escalates_is_judged_by_the_outage():
    n, t, sent = make(at(2026, 5, 4, 12), min_severity="critical")
    d = drop(1, t, severity="warning")
    n.on_incident_started(d)
    assert sent == []
    t.after(20)
    d.severity = "critical"
    n.on_incident_escalated(d)
    assert events(sent) == ["incident_started"] and sent[0].outage_id == 1
    t.after(20)
    n.on_incident_escalated(d)                     # again: already announced, nothing more
    assert events(sent) == ["incident_started"]


def test_changing_the_gap_applies_to_the_next_drop():
    n, t, sent = make(at(2026, 5, 4, 12))
    first = drop(1, t)
    n.on_incident_started(first)
    end(n, t, first)
    n.reconfigure(n.url, None, [], None, AlertPolicy(AlertsConfig(), "UTC", IncidentsConfig(group_gap_minutes=0)))
    t.after(100)
    second = drop(2, t)
    n.on_incident_started(second)                  # off now: announced on its own (not suppressed: new cause window)
    assert "incident_started" in events(sent)
