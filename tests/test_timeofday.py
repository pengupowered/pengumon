"""next_wall_time on the two daylight-saving days: a missing time fires at the end of the gap, a repeated one at both
occurrences, and the answer is always after the instant asked about."""

from datetime import datetime, timedelta, timezone

import pytest

from pengumon.timeofday import next_wall_time, zone


def _utc(*args):
    return datetime(*args, tzinfo=timezone.utc).timestamp()


def _local(epoch, tz):
    return datetime.fromtimestamp(epoch, tz).strftime("%Y-%m-%d %H:%M")


# zone, spring-forward gap start (UTC instant of the jump), gap length, a wall time inside the gap
SPRING = [
    ("America/New_York", _utc(2026, 3, 8, 7), 3600, "02:30"),
    ("Europe/Berlin", _utc(2026, 3, 29, 1), 3600, "02:15"),
    ("Europe/London", _utc(2026, 3, 29, 1), 3600, "01:30"),
    ("Australia/Lord_Howe", _utc(2026, 10, 3, 15, 30), 1800, "02:10"),  # a 30 minute gap
]


@pytest.mark.parametrize("name,jump,length,hhmm", SPRING)
def test_a_time_in_the_spring_gap_fires_when_the_clock_passes_it_not_an_hour_early(name, jump, length, hhmm):
    tz = zone(name)
    before = jump - 6 * 3600
    assert next_wall_time(before, hhmm, tz) == jump  # the instant the clock first reads that time or later
    assert datetime.fromtimestamp(jump, tz).strftime("%H:%M") >= hhmm
    assert datetime.fromtimestamp(jump - 1, tz).strftime("%H:%M") < hhmm
    assert next_wall_time(jump - 1, hhmm, tz) == jump
    # after the jump the next one is the following day's real time
    assert _local(next_wall_time(jump, hhmm, tz), tz).endswith(hhmm)
    assert next_wall_time(jump, hhmm, tz) > jump + 20 * 3600


# zone, the wall time that happens twice, the UTC instants of its two occurrences
AUTUMN = [
    ("America/New_York", "01:30", _utc(2026, 11, 1, 5, 30), _utc(2026, 11, 1, 6, 30)),
    ("Europe/Berlin", "02:30", _utc(2026, 10, 25, 0, 30), _utc(2026, 10, 25, 1, 30)),
]


@pytest.mark.parametrize("name,hhmm,first,second", AUTUMN)
def test_a_repeated_wall_time_fires_at_both_occurrences(name, hhmm, first, second):
    tz = zone(name)
    assert next_wall_time(first - 3600, hhmm, tz) == first
    assert next_wall_time(first, hhmm, tz) == second
    assert next_wall_time(second, hhmm, tz) > second + 20 * 3600


@pytest.mark.parametrize("name", ["America/New_York", "Europe/London", "Australia/Lord_Howe", "UTC", "+05:30"])
def test_the_answer_is_always_after_the_instant_asked_about(name):
    tz = zone(name)
    start = _utc(2026, 3, 1)
    for step in range(0, 70 * 24 * 4):  # every 15 minutes for 70 days: both changes of the northern spring and autumn
        after = start + step * 900
        for hhmm in ("00:00", "01:30", "02:00", "02:30", "03:00"):
            assert next_wall_time(after, hhmm, tz) > after


def test_a_zone_that_cannot_load_is_utc_with_one_warning(caplog):
    with caplog.at_level("WARNING", logger="pengumon.timeofday"):
        assert zone("Example/Nowhere") is timezone.utc and zone("Example/Nowhere") is timezone.utc
    assert [r.getMessage().count("'Example/Nowhere'") for r in caplog.records] == [1]
