"""windows.py: the rolling window and its calendar-day cells."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from pengumon.windows import RANGES, day_cells, rolling, week_cells

UTC = timezone.utc
NY = ZoneInfo("America/New_York")


def at(text, tz=UTC):
    return datetime.fromisoformat(text).replace(tzinfo=tz).timestamp()


def test_ranges_and_rolling():
    assert RANGES == {"1h": 3600, "24h": 86400, "7d": 604800, "30d": 2592000}
    assert rolling(RANGES["7d"], 1_000_000.0) == (1_000_000.0 - 604800, 1_000_000.0)


def test_a_window_in_the_middle_of_a_day_has_two_partial_ends():
    end = at("2026-11-14T12:30:00")
    cells = day_cells(end - 7 * 86400, end, UTC)
    assert len(cells) == 8
    assert [c["day"] for c in cells][0] == "2026-11-07" and cells[-1]["day"] == "2026-11-14"
    assert [c["partial"] for c in cells] == [True] + [False] * 6 + [True]
    assert cells[0]["hours"] == 11.5 and cells[-1]["hours"] == 12.5
    assert sum(c["hours"] for c in cells) == 168
    assert cells[0]["start"] == end - 7 * 86400 and cells[-1]["end"] == end
    assert all(a["end"] == b["start"] for a, b in zip(cells, cells[1:]))


def test_a_window_ending_exactly_at_a_midnight_has_no_cell_for_the_new_day():
    end = at("2026-11-14T00:00:00")
    cells = day_cells(end - 7 * 86400, end, UTC)
    assert len(cells) == 7 and cells[-1]["day"] == "2026-11-13"
    assert not any(c["partial"] for c in cells)


def test_short_windows():
    end = at("2026-11-14T12:00:00")
    one = day_cells(end - 3600, end, UTC)
    assert len(one) == 1 and one[0]["partial"] and one[0]["hours"] == 1
    day = day_cells(end - 86400, end, UTC)
    assert [c["hours"] for c in day] == [12, 12] and all(c["partial"] for c in day)
    month = day_cells(end - 30 * 86400, end, UTC)
    assert len(month) == 31 and sum(c["hours"] for c in month) == 720
    assert day_cells(end, end, UTC) == []


def test_a_clock_change_day_is_a_whole_day():
    # New York, spring forward 2026-03-08 (23 h) and fall back 2026-11-01 (25 h)
    end = at("2026-03-10T00:00:00", NY)
    cells = day_cells(end - 3 * 86400 + 3600, end, NY)  # 72 h minus the skipped hour: from midnight of the 7th
    assert [c["day"] for c in cells] == ["2026-03-07", "2026-03-08", "2026-03-09"]
    assert not any(c["partial"] for c in cells)
    spring = [c for c in cells if c["day"] == "2026-03-08"][0]
    assert spring["hours"] == 23 and not spring["partial"]
    end = at("2026-11-02T00:00:00", NY)
    fall = [c for c in day_cells(at("2026-10-31T00:00:00", NY), end, NY) if c["day"] == "2026-11-01"][0]
    assert fall["hours"] == 25 and not fall["partial"]


def test_a_cut_clock_change_day_is_partial():
    end = at("2026-11-01T12:00:00", NY)
    cells = day_cells(at("2026-10-31T12:00:00", NY), end, NY)
    assert [c["partial"] for c in cells] == [True, True]
    assert sum(c["hours"] for c in cells) == 25  # 12 h + 13 h (one is the repeated hour)


# --- local weeks (Monday to Monday) ----------------------------------------------------------------------------------

def test_a_window_starting_and_ending_mid_week_has_two_partial_weeks():
    start, end = at("2026-11-04T12:00:00"), at("2026-11-25T18:30:00")  # a Wednesday to a Wednesday
    cells = week_cells(start, end, UTC)
    assert [c["week"] for c in cells] == ["2026-11-02", "2026-11-09", "2026-11-16", "2026-11-23"]
    assert [c["partial"] for c in cells] == [True, False, False, True]
    assert cells[0]["start"] == start and cells[-1]["end"] == end
    assert cells[0]["hours"] == 5 * 24 - 12 and cells[1]["hours"] == 168 and cells[-1]["hours"] == 2 * 24 + 18.5
    assert sum(c["hours"] for c in cells) == (end - start) / 3600
    assert all(a["end"] == b["start"] for a, b in zip(cells, cells[1:]))
    assert all(datetime.fromisoformat(c["week"]).weekday() == 0 for c in cells)


def test_a_window_on_whole_weeks_has_no_partial_cell_and_none_for_the_week_that_begins_at_its_end():
    start, end = at("2026-11-02T00:00:00"), at("2026-11-23T00:00:00")  # Monday midnights
    cells = week_cells(start, end, UTC)
    assert len(cells) == 3 and cells[-1]["week"] == "2026-11-16" and not any(c["partial"] for c in cells)
    assert week_cells(end, end, UTC) == [] and week_cells(end, start, UTC) == []


def test_a_window_inside_one_week_is_one_partial_cell():
    cells = week_cells(at("2026-11-04T09:00:00"), at("2026-11-04T10:30:00"), UTC)
    assert len(cells) == 1 and cells[0]["partial"] and cells[0]["hours"] == 1.5 and cells[0]["week"] == "2026-11-02"


def test_a_week_with_a_clock_change_is_whole_and_167_or_169_hours_long():
    # New York: the clocks went forward on Sunday 8 March 2026 (week of 2 March: 167 h) and back on 1 November (week of 26 October: 169 h)
    spring = week_cells(at("2026-03-02T00:00:00", NY), at("2026-03-16T00:00:00", NY), NY)
    assert [c["week"] for c in spring] == ["2026-03-02", "2026-03-09"]
    assert [c["hours"] for c in spring] == [167, 168] and not any(c["partial"] for c in spring)
    fall = week_cells(at("2026-10-26T00:00:00", NY), at("2026-11-09T00:00:00", NY), NY)
    assert [c["hours"] for c in fall] == [169, 168] and not any(c["partial"] for c in fall)
    # cut inside the change week: partial, and its hours count the real hours inside the window
    cut = week_cells(at("2026-10-30T12:00:00", NY), at("2026-11-05T12:00:00", NY), NY)
    assert [c["week"] for c in cut] == ["2026-10-26", "2026-11-02"] and all(c["partial"] for c in cut)
    assert sum(c["hours"] for c in cut) == 6 * 24 + 1  # the repeated hour is inside
