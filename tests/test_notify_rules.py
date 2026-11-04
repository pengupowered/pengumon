"""The alert rules (notify_policy.py, notify.py): the severity filter, the delay, quiet hours across midnight and
daylight-saving changes, critical-still, the repeat window, and a reconfigure that keeps what was announced. Time is a
fake wall clock and a fake scheduler: nothing here sleeps."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from pengumon.incidents.model import Incident
from pengumon.notify import Notifier
from pengumon.notify_policy import AlertPolicy
from pengumon.notify_settings import AlertsConfig
from pengumon.timeofday import next_wall_time, window_end, zone


def at(year, month, day, hour=0, minute=0, tz="UTC"):
    return datetime(year, month, day, hour, minute, tzinfo=ZoneInfo(tz)).timestamp()


class _Timer:
    def __init__(self, due, fn):
        self.due, self.fn, self.cancelled = due, fn, False

    def cancel(self):
        self.cancelled = True


class FakeTime:
    """The wall clock, and a scheduler that fires timers as the clock is moved past them."""

    def __init__(self, now):
        self.now = now
        self.timers = []

    def __call__(self):
        return self.now

    def schedule(self, delay, fn):
        timer = _Timer(self.now + delay, fn)
        self.timers.append(timer)
        return timer

    MAX_FIRES = 1000  # a timer that re-arms itself due at or before `when` would otherwise loop for ever

    def to(self, when):
        fires = 0
        while True:
            live = [t for t in self.timers if not t.cancelled and t.due <= when]
            if not live:
                break
            timer = min(live, key=lambda t: t.due)
            self.timers.remove(timer)
            self.now = max(self.now, timer.due)
            fires += 1
            if fires > self.MAX_FIRES:
                raise AssertionError(f"timers keep re-arming: {fires} fired moving the clock to {when} "
                                     f"({len(self.timers)} still waiting)")
            timer.fn()
        self.now = when

    def after(self, seconds):
        self.to(self.now + seconds)

    @property
    def waiting(self):
        return [t for t in self.timers if not t.cancelled]


def make(now, tz="UTC", **alerts):
    """(notifier, fake time, alerts sent); the dispatch is recorded instead of sent."""
    quiet = alerts.pop("quiet_hours", {})
    time_ = FakeTime(now)
    policy = AlertPolicy(AlertsConfig(quiet_hours=quiet, **alerts), tz)
    notifier = Notifier("http://ntfy.test/t", wall=time_, clock=time_, policy=policy, schedule=time_.schedule)
    sent = []
    notifier._dispatch = sent.append
    return notifier, time_, sent


def incident(iid=1, start=0.0, severity="warning", cause="internet_backbone_transit_loss"):
    return Incident(id=iid, start_time=start, cause_code=cause, title="ISP Outage", description="Hub unreachable",
                    severity=severity)


class Service:
    id, name = "ha", "Home Assistant"


class Err:
    message = "connection refused"


QUIET = {"enabled": True, "start": "22:00", "end": "07:00"}
BERLIN = "Europe/Berlin"


# --- the severity filter -----------------------------------------------------------------------------------------------

def test_critical_only_drops_a_warning_incident_and_announces_its_escalation():
    now = at(2026, 5, 4, 12)
    n, t, sent = make(now, min_severity="critical")
    inc = incident(start=now - 30)
    n.on_incident_started(inc)
    assert sent == []
    t.after(600)
    inc.severity = "critical"
    n.on_incident_escalated(inc)  # considered now, as if it started now
    assert [a.event for a in sent] == ["incident_started"] and sent[0].severity == "critical"
    n.on_incident_resolved(inc)
    assert [a.event for a in sent] == ["incident_started", "incident_resolved"]


def test_critical_only_never_announces_a_warning_that_resolves_and_sends_no_resolved():
    n, t, sent = make(at(2026, 5, 4, 12), min_severity="critical")
    inc = incident(start=t.now)
    n.on_incident_started(inc)
    n.on_incident_resolved(inc)
    assert sent == []


def test_a_critical_incident_passes_the_critical_filter_at_once():
    n, t, sent = make(at(2026, 5, 4, 12), min_severity="critical")
    n.on_incident_started(incident(severity="critical", start=t.now))
    assert len(sent) == 1


def test_escalation_of_an_incident_already_announced_sends_nothing_more():
    n, t, sent = make(at(2026, 5, 4, 12))
    inc = incident(start=t.now)
    n.on_incident_started(inc)
    inc.severity = "critical"
    n.on_incident_escalated(inc)
    assert len(sent) == 1


def test_the_filter_does_not_apply_to_host_apps_or_route_changes():
    now = at(2026, 5, 4, 12)
    n, t, sent = make(now, min_severity="critical")
    n.notify_service_down(Service, Err, 60.0)  # a warning-level alert
    n.notify_path_changed(["A new hop on the route: X."])
    assert [a.event for a in sent] == ["service_down", "path_changed"]


# --- the delay -------------------------------------------------------------------------------------------------------

def test_a_delay_drops_a_four_minute_incident_with_no_start_and_no_resolved_message():
    start = at(2026, 5, 4, 12)
    n, t, sent = make(start + 30, delay_seconds=300)
    inc = incident(start=start)  # detected 30 s after the first bad round
    n.on_incident_started(inc)
    t.to(start + 240)
    assert sent == []
    n.on_incident_resolved(inc)
    t.to(start + 900)
    assert sent == [] and not t.waiting


def test_a_delay_sends_a_six_minute_incident_at_minute_five_and_its_resolved_message_after():
    start = at(2026, 5, 4, 12)
    n, t, sent = make(start + 30, delay_seconds=300)
    inc = incident(start=start)
    n.on_incident_started(inc)
    t.to(start + 299)
    assert sent == []
    t.to(start + 301)
    assert [a.event for a in sent] == ["incident_started"]
    t.to(start + 360)
    n.on_incident_resolved(inc)
    assert [a.event for a in sent] == ["incident_started", "incident_resolved"]


def test_an_incident_older_than_the_delay_when_it_is_detected_goes_out_at_once():
    start = at(2026, 5, 4, 12)
    n, t, sent = make(start + 400, delay_seconds=300)
    n.on_incident_started(incident(start=start))
    assert len(sent) == 1


def test_the_delay_does_not_apply_to_host_apps():
    n, t, sent = make(at(2026, 5, 4, 12), delay_seconds=300)
    n.notify_service_down(Service, Err, 60.0)
    assert len(sent) == 1


# --- quiet hours -----------------------------------------------------------------------------------------------------

def test_quiet_hours_across_midnight_hold_an_alert_and_release_it_at_the_end_while_it_is_open():
    start = at(2026, 5, 4, 23, 30, BERLIN)
    n, t, sent = make(start + 20, BERLIN, quiet_hours=QUIET)
    inc = incident(start=start)
    n.on_incident_started(inc)
    t.to(at(2026, 5, 5, 6, 59, BERLIN))
    assert sent == []
    t.to(at(2026, 5, 5, 7, 0, BERLIN))
    assert [a.event for a in sent] == ["incident_started"]
    assert sent[0].title == "Pengumon: Ongoing since 23:30: ISP Outage"
    n.on_incident_resolved(inc)
    assert [a.event for a in sent] == ["incident_started", "incident_resolved"]


def test_quiet_hours_drop_an_incident_that_resolved_before_the_end_and_say_nothing_about_it():
    start = at(2026, 5, 5, 3, 0, BERLIN)
    n, t, sent = make(start + 20, BERLIN, quiet_hours=QUIET)
    inc = incident(start=start)
    n.on_incident_started(inc)
    t.after(1800)
    n.on_incident_resolved(inc)
    t.to(at(2026, 5, 5, 9, 0, BERLIN))
    assert sent == [] and not t.waiting


def test_quiet_hours_are_read_in_the_configured_time_zone_not_utc():
    # 21:30 UTC is 23:30 in Berlin (summer): quiet. In UTC it would not be.
    now = at(2026, 5, 4, 21, 30)
    n, t, sent = make(now, BERLIN, quiet_hours=QUIET)
    n.on_incident_started(incident(start=now))
    assert sent == []
    u, tu, sent_u = make(now, "UTC", quiet_hours=QUIET)
    u.on_incident_started(incident(start=now))
    assert len(sent_u) == 1


def test_outside_the_window_nothing_is_held_and_a_disabled_window_holds_nothing():
    noon = at(2026, 5, 4, 12, 0, BERLIN)
    n, t, sent = make(noon, BERLIN, quiet_hours=QUIET)
    n.on_incident_started(incident(start=noon))
    assert len(sent) == 1
    night = at(2026, 5, 4, 23, 0, BERLIN)
    m, tm, sent_m = make(night, BERLIN, quiet_hours={**QUIET, "enabled": False})
    m.on_incident_started(incident(start=night))
    assert len(sent_m) == 1


def test_a_window_inside_one_day_holds_only_inside_it():
    quiet = {"enabled": True, "start": "13:00", "end": "14:00"}
    n, t, sent = make(at(2026, 5, 4, 13, 30), quiet_hours=quiet)
    n.on_incident_started(incident(start=t.now))
    assert sent == []
    t.to(at(2026, 5, 4, 14, 0))
    assert len(sent) == 1


def test_critical_still_sends_a_critical_incident_at_once_and_holds_a_warning():
    start = at(2026, 5, 4, 23, 30, BERLIN)
    n, t, sent = make(start, BERLIN, quiet_hours=QUIET)
    n.on_incident_started(incident(1, start, "critical"))
    n.on_incident_started(incident(2, start, "warning", cause="target_specific_outage"))
    assert [a.incident_id for a in sent] == [1] and sent[0].title == "Pengumon: ISP Outage"  # no "Ongoing"
    t.to(at(2026, 5, 5, 7, 0, BERLIN))
    assert [a.incident_id for a in sent] == [1, 2]


def test_without_critical_still_a_critical_incident_waits_too():
    start = at(2026, 5, 4, 23, 30, BERLIN)
    n, t, sent = make(start, BERLIN, quiet_hours={**QUIET, "critical_still": False})
    n.on_incident_started(incident(1, start, "critical"))
    assert sent == []
    t.to(at(2026, 5, 5, 7, 0, BERLIN))
    assert len(sent) == 1


def test_a_warning_that_escalates_to_critical_while_held_is_sent_at_once_with_critical_still():
    start = at(2026, 5, 4, 23, 30, BERLIN)
    n, t, sent = make(start, BERLIN, quiet_hours=QUIET)
    inc = incident(start=start)
    n.on_incident_started(inc)
    assert sent == []
    inc.severity = "critical"
    n.on_incident_escalated(inc)
    assert len(sent) == 1 and not t.waiting


def test_the_delay_then_quiet_hours_both_apply_to_the_announce_time():
    start = at(2026, 5, 4, 21, 58, BERLIN)  # 22:03 is inside the window
    n, t, sent = make(start + 10, BERLIN, delay_seconds=300, quiet_hours=QUIET)
    n.on_incident_started(incident(start=start))
    t.to(at(2026, 5, 4, 22, 5, BERLIN))
    assert sent == []
    t.to(at(2026, 5, 5, 7, 0, BERLIN))
    assert len(sent) == 1


def test_a_resolved_message_for_an_announced_warning_waits_for_the_end_of_quiet_hours():
    start = at(2026, 5, 4, 21, 0, BERLIN)
    n, t, sent = make(start, BERLIN, quiet_hours=QUIET)
    inc = incident(start=start)
    n.on_incident_started(inc)  # announced at 21:00, before the window
    t.to(at(2026, 5, 4, 23, 0, BERLIN))
    n.on_incident_resolved(inc)
    assert [a.event for a in sent] == ["incident_started"]
    t.to(at(2026, 5, 5, 7, 0, BERLIN))
    assert [a.event for a in sent] == ["incident_started", "incident_resolved"]


def test_a_resolved_message_for_an_exempt_critical_incident_is_not_held():
    start = at(2026, 5, 4, 23, 0, BERLIN)
    n, t, sent = make(start, BERLIN, quiet_hours=QUIET)
    inc = incident(severity="critical", start=start)
    n.on_incident_started(inc)
    n.on_incident_resolved(inc)
    assert [a.event for a in sent] == ["incident_started", "incident_resolved"]


# --- daylight saving -------------------------------------------------------------------------------------------------

def test_a_night_with_23_hours_still_releases_at_seven_on_the_clock():
    ny = "America/New_York"  # 2026-03-08: 02:00 EST jumps to 03:00 EDT
    start = at(2026, 3, 8, 1, 30, ny)
    n, t, sent = make(start, ny, quiet_hours=QUIET)
    n.on_incident_started(incident(start=start))
    seven = at(2026, 3, 8, 7, 0, ny)
    assert seven - start == 4.5 * 3600  # not 5.5
    t.to(seven - 1)
    assert sent == []
    t.to(seven)
    assert len(sent) == 1


def test_a_night_with_25_hours_still_releases_at_seven_on_the_clock():
    ny = "America/New_York"  # 2026-11-01: 02:00 EDT falls back to 01:00 EST
    start = at(2026, 11, 1, 0, 30, ny)
    n, t, sent = make(start, ny, quiet_hours=QUIET)
    n.on_incident_started(incident(start=start))
    seven = at(2026, 11, 1, 7, 0, ny)
    assert seven - start == 7.5 * 3600
    t.to(seven - 1)
    assert sent == []
    t.to(seven)
    assert len(sent) == 1


def test_a_window_ending_in_the_skipped_hour_ends_within_an_hour_after_it():
    ny = zone("America/New_York")
    start = at(2026, 3, 8, 1, 30, "America/New_York")
    end = window_end(start, "22:00", "02:30", ny)  # 02:30 does not exist that night
    transition = at(2026, 3, 8, 3, 0, "America/New_York")
    assert transition <= end <= transition + 3600


def test_a_window_ending_in_the_repeated_hour_ends_at_its_first_or_second_reading_by_when_it_began():
    ny = zone("America/New_York")
    first = at(2026, 11, 1, 0, 30, "America/New_York")  # EDT
    second = first + 2 * 3600  # 01:30 EST, the second time the clock reads 01:30
    assert datetime.fromtimestamp(window_end(first, "22:00", "01:30", ny), ny).strftime("%H:%M %Z") == "01:30 EDT"
    assert datetime.fromtimestamp(second, ny).strftime("%H:%M %Z") == "01:30 EST"
    assert window_end(second, "22:00", "01:45", ny) - second == 15 * 60


def test_fixed_offsets_and_utc_work_as_zones():
    noon = at(2026, 5, 4, 12)
    assert next_wall_time(noon, "18:00", zone("+05:30")) == at(2026, 5, 4, 12, 30)
    assert next_wall_time(noon, "18:00", zone("UTC")) == at(2026, 5, 4, 18)
    assert next_wall_time(noon, "08:00", zone("-04:00")) == at(2026, 5, 5, 12)  # 08:00 is now there: the next one
    assert zone("Not/AZone") is zone("UTC")  # an unknown zone reads as UTC (the setting is checked on save)


# --- host apps and route changes (quiet hours only) -----------------------------------------------------------------

def test_a_service_down_held_by_quiet_hours_goes_out_at_the_end_if_still_down():
    now = at(2026, 5, 4, 23, 0, BERLIN)
    n, t, sent = make(now, BERLIN, quiet_hours=QUIET)
    n.notify_service_down(Service, Err, 600.0)
    assert sent == []
    t.to(at(2026, 5, 5, 7, 0, BERLIN))
    assert [a.event for a in sent] == ["service_down"]
    assert sent[0].title == "Pengumon: Ongoing since 22:50: Home Assistant not responding"
    assert "Down for" in sent[0].message and sent[0].duration_seconds > 8 * 3600


def test_a_service_that_came_back_while_its_alert_was_held_drops_both_messages():
    now = at(2026, 5, 4, 23, 0, BERLIN)
    n, t, sent = make(now, BERLIN, quiet_hours=QUIET)
    n.notify_service_down(Service, Err, 600.0)
    t.after(3600)
    n.notify_service_up(Service, 4200.0)
    t.to(at(2026, 5, 5, 9, 0, BERLIN))
    assert sent == []


def test_critical_still_does_not_exempt_a_host_app():
    now = at(2026, 5, 4, 23, 0, BERLIN)
    n, t, sent = make(now, BERLIN, quiet_hours=QUIET)
    n.notify_service_down(Service, Err, 60.0)
    assert sent == []


def test_a_route_change_is_held_and_a_newer_one_replaces_it():
    now = at(2026, 5, 4, 23, 0, BERLIN)
    n, t, sent = make(now, BERLIN, quiet_hours=QUIET)
    n.notify_path_changed(["old"])
    n.notify_path_changed(["new"])
    t.to(at(2026, 5, 5, 7, 0, BERLIN))
    assert [a.message.split("\n")[0] for a in sent] == ["new"]


async def test_the_test_alert_is_never_filtered_or_held():
    sent = []
    now = at(2026, 5, 4, 23, 0, BERLIN)
    n, t, _ = make(now, BERLIN, min_severity="critical", delay_seconds=600, quiet_hours={**QUIET, "critical_still": False})

    async def post(url, content, headers):
        sent.append(url)

        class R:
            def raise_for_status(self):
                pass
        return R()
    n._client.post = post
    result = await n.send_test()
    assert sent == ["http://ntfy.test/t"] and result[0]["ok"] is True
    await n.aclose()


# --- the repeat window -----------------------------------------------------------------------------------------------

def test_the_repeat_window_keeps_todays_behaviour_at_300_seconds():
    n, t, sent = make(at(2026, 5, 4, 12))
    n.on_incident_started(incident(1, t.now))
    t.after(100)
    n.on_incident_started(incident(2, t.now))
    assert len(sent) == 1
    t.after(300)
    n.on_incident_started(incident(3, t.now))
    assert [a.incident_id for a in sent] == [1, 3]


def test_a_repeat_window_of_zero_announces_every_flap():
    n, t, sent = make(at(2026, 5, 4, 12), repeat_window_seconds=0)
    for i in (1, 2, 3):
        n.on_incident_started(incident(i, t.now))
        t.after(1)
    assert len(sent) == 3


def test_a_suppressed_repeat_has_no_resolved_message():
    n, t, sent = make(at(2026, 5, 4, 12))
    n.on_incident_started(incident(1, t.now))
    second = incident(2, t.now)
    n.on_incident_started(second)
    n.on_incident_resolved(second)
    assert [a.event for a in sent] == ["incident_started"]


# --- reconfigure -----------------------------------------------------------------------------------------------------

def test_a_reconfigure_keeps_announced_so_resolved_still_goes_out():
    n, t, sent = make(at(2026, 5, 4, 12))
    inc = incident(start=t.now)
    n.on_incident_started(inc)
    n.reconfigure("http://ntfy.test/other", None, [], None, AlertPolicy(AlertsConfig(min_severity="critical"), "UTC"))
    n.on_incident_resolved(inc)  # a warning, which the new filter would not announce, but it was announced
    assert [a.event for a in sent] == ["incident_started", "incident_resolved"]
    assert n.url == "http://ntfy.test/other"


def test_a_reconfigure_keeps_the_repeat_memory():
    n, t, sent = make(at(2026, 5, 4, 12))
    n.on_incident_started(incident(1, t.now))
    n.reconfigure(n.url, None, [], None, AlertPolicy(AlertsConfig(), "UTC"))
    t.after(10)
    n.on_incident_started(incident(2, t.now))
    assert len(sent) == 1


def test_turning_quiet_hours_off_releases_what_they_held_now():
    start = at(2026, 5, 4, 23, 30, BERLIN)
    n, t, sent = make(start, BERLIN, quiet_hours=QUIET)
    n.on_incident_started(incident(start=start))
    assert sent == []
    n.reconfigure(n.url, None, [], None, AlertPolicy(AlertsConfig(), BERLIN))
    assert len(sent) == 1 and not t.waiting


def test_changing_the_time_zone_times_a_held_alert_again():
    start = at(2026, 5, 4, 23, 30, BERLIN)
    n, t, sent = make(start, BERLIN, quiet_hours=QUIET)
    n.on_incident_started(incident(start=start))
    new = AlertPolicy(AlertsConfig(quiet_hours=QUIET), "UTC")  # 21:30 UTC is outside the window in UTC
    n.reconfigure(n.url, None, [], None, new)
    assert len(sent) == 1


def test_a_stricter_filter_drops_a_waiting_warning():
    start = at(2026, 5, 4, 23, 30, BERLIN)
    n, t, sent = make(start, BERLIN, quiet_hours=QUIET)
    n.on_incident_started(incident(start=start))
    n.reconfigure(n.url, None, [], None, AlertPolicy(AlertsConfig(min_severity="critical", quiet_hours=QUIET), BERLIN))
    t.to(at(2026, 5, 5, 9, 0, BERLIN))
    assert sent == []


async def test_aclose_cancels_what_is_waiting():
    start = at(2026, 5, 4, 23, 30, BERLIN)
    n, t, sent = make(start, BERLIN, quiet_hours=QUIET)
    n.on_incident_started(incident(start=start))
    assert n.pending_count() == 1
    await n.aclose()
    assert n.pending_count() == 0 and all(x.cancelled for x in t.timers)


def test_a_timer_that_fires_early_waits_again_instead_of_sending_early():
    start = at(2026, 5, 4, 23, 30, BERLIN)
    n, t, sent = make(start, BERLIN, quiet_hours=QUIET)
    n.on_incident_started(incident(start=start))
    timer = t.waiting[0]
    t.timers.remove(timer)
    t.now = timer.due - 5  # the real timer woke 5 s early
    timer.fn()
    assert sent == [] and len(t.waiting) == 1


def test_the_policy_defaults_are_todays_behaviour():
    p = AlertPolicy()
    assert p.passes_severity("warning") and p.passes_severity("critical")
    assert p.incident_time(1000.0, "warning").at == 1000.0 and p.repeat_window_seconds == 300.0
