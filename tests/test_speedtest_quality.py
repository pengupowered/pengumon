"""How far a speed test's result can be trusted (speedtest/quality.py): a direction that lasted too short to measure the
line is `low` confidence, with a note that says how much data per test would measure it."""

import pytest

from pengumon.speedtest.quality import MIN_SECONDS, confidence, measured_seconds, needed_mb


def row(**kw):
    return {"bytes_down": None, "down_mbps": None, "bytes_up": None, "up_mbps": None, **kw}


def test_the_time_measured_follows_from_the_bytes_and_the_speed():
    assert measured_seconds(25_000_000, 100.0) == pytest.approx(2.0)
    assert measured_seconds(25_000_000, None) is None and measured_seconds(0, 100.0) is None
    assert needed_mb(1000.0) == 125 and needed_mb(40.0) == 5 and needed_mb(1.0) == 1 and needed_mb(300.0, 2.0) == 75


def test_a_fast_line_with_little_data_is_low_confidence_and_says_how_much_would_do():
    # 25 MB at 600 Mbps: a third of a second
    level, note = confidence(row(bytes_down=25_000_000, down_mbps=600.0, bytes_up=10_000_000, up_mbps=35.0))
    assert level == "low"
    assert note == ("download: 25 MB lasted 0.3 s, too little to measure a line this fast; about 75 MB would "
                    "(100 MB in Data per test)")


def test_a_line_faster_than_the_largest_choice_can_measure_says_so():
    level, note = confidence(row(bytes_down=1_000_000_000, down_mbps=9000.0))
    assert level == "low" and "more than the largest choice (1000 MB)" in note


def test_both_directions_can_be_low():
    level, note = confidence(row(bytes_down=10_000_000, down_mbps=300.0, bytes_up=5_000_000, up_mbps=80.0))
    assert level == "low" and note.startswith("download: 10 MB") and "; upload: 5 MB lasted 0.5 s" in note
    assert note.endswith("about 10 MB would (10 MB in Data per test)")


def test_enough_data_is_ok_with_no_note():
    assert confidence(row(bytes_down=250_000_000, down_mbps=900.0, bytes_up=10_000_000, up_mbps=35.0)) == ("ok", None)
    seconds = MIN_SECONDS
    assert confidence(row(bytes_down=int(100e6 * seconds / 8), down_mbps=100.0)) == ("ok", None)   # exactly the minimum


def test_no_speed_is_no_confidence():
    assert confidence(row()) == (None, None)                                                 # skipped or failed
    assert confidence(row(bytes_down=1000, down_mbps=None, bytes_up=500)) == (None, None)


def test_the_reports_leave_a_direction_too_short_to_measure_the_line_out_of_the_speeds():
    from pengumon.reports.speed import counts_text, summarize
    from pengumon.speedtest.quality import trusted
    short = {"status": "ok", "down_mbps": 300.0, "bytes_down": 25_000_000, "up_mbps": 19.0, "bytes_up": 10_000_000}
    long = {"status": "ok", "down_mbps": 900.0, "bytes_down": 250_000_000, "up_mbps": 18.0, "bytes_up": 10_000_000}
    unknown = {"status": "ok", "down_mbps": 500.0, "up_mbps": 19.0}         # no byte count: cannot be judged, kept
    assert trusted(short) == (None, 19.0) and trusted(long) == (900.0, 18.0) and trusted(unknown) == (500.0, 19.0)
    s = summarize([short, long, unknown], 1100.0, 20.0)
    assert (s.down_median, s.down_low, s.too_short, s.measured) == (700.0, 500.0, 1, 3)
    assert s.under == 1                                                       # 500 of 1100 is under half; 300 is not judged
    assert "1 too short to measure the line, left out of the speeds" in counts_text(s)


# --- the headline: the line's rate the router measured, else the test's own ------------------------------------------

def measured(down=670.0, up=41.0, wan_down=None, wan_up=None, **kw):
    """A test that ran its 8 s each way (long enough to measure the line), with the router's peaks when given."""
    return row(bytes_down=int(down * 1e6 / 8 * 8), down_mbps=down, bytes_up=int(up * 1e6 / 8 * 8), up_mbps=up,
               wan_down_mbps=wan_down, wan_up_mbps=wan_up, **kw)


def test_the_router_measures_the_headline_when_it_gave_a_rate_at_least_the_tests_own():
    from pengumon.speedtest.quality import headline, trusted
    h = headline(measured(wan_down=905.0, wan_up=41.5))
    assert (h["down_mbps"], h["up_mbps"], h["down_by"], h["up_by"], h["measured_by"]) == (905.0, 41.5, "router",
                                                                                         "router", "router")
    assert h["note"] is None                                       # 670 of 905 is 74 %: not far below
    assert trusted(measured(wan_down=905.0, wan_up=41.5)) == (905.0, 41.5)


def test_without_the_router_or_with_a_router_rate_under_the_tests_the_test_measures_it():
    from pengumon.speedtest.quality import headline
    assert headline(measured()) == {"down_mbps": 670.0, "up_mbps": 41.0, "down_by": "test", "up_by": "test",
                                    "measured_by": "test", "note": None}
    # a router rate under the test's own was not the line's full view (a smeared or stale reading): the test's own
    h = headline(measured(wan_down=558.0, wan_up=44.0))
    assert (h["down_mbps"], h["down_by"], h["up_mbps"], h["up_by"], h["measured_by"]) == (670.0, "test", 44.0, "router",
                                                                                         "test")
    assert headline(measured(wan_down=670.0))["down_by"] == "router"   # equal: the router's (it saw all traffic)


def test_a_direction_too_short_to_measure_the_line_keeps_the_tests_own_figure():
    from pengumon.speedtest.quality import headline, trusted
    short = row(bytes_down=25_000_000, down_mbps=600.0, wan_down_mbps=300.0 + 600.0)    # 0.3 s: the router saw around it
    assert (headline(short)["down_mbps"], headline(short)["down_by"]) == (600.0, "test")
    assert trusted(short) == (None, None)


def test_a_test_far_below_the_lines_rate_says_the_rest_was_household_traffic():
    from pengumon.speedtest.quality import HOUSEHOLD_SHARE, headline
    h = headline(measured(down=300.0, wan_down=900.0, wan_up=41.0))
    assert h["down_mbps"] == 900.0 and h["measured_by"] == "router"
    assert h["note"] == ("The test itself got download 300 of the 900 Mbps the line carried: the rest was other "
                         "traffic on your network at the same time, not a slow line.")
    assert HOUSEHOLD_SHARE == 0.7
    assert headline(measured(down=640.0, wan_down=900.0))["note"] is None     # 71 %: not far enough below


def test_no_speed_no_headline():
    from pengumon.speedtest.quality import headline
    assert headline(row(wan_down_mbps=900.0)) == {"down_mbps": None, "up_mbps": None, "down_by": None, "up_by": None,
                                                  "measured_by": None, "note": None}


def test_the_reports_judge_the_headline():
    from pengumon.reports.speed import method_text, summarize
    rows = [{"status": "ok", **measured(down=300.0, wan_down=900.0, up=19.0, wan_up=19.5)},   # household traffic
            {"status": "ok", **measured(down=640.0, up=18.0)}]                                   # no router reading
    s = summarize(rows, 1000.0, 40.0)
    assert (s.down_median, s.down_low, s.by_router, s.shared) == (770.0, 640.0, 1, 1)
    assert s.under == 2                                   # the uploads (19.5 and 18 of 40) are under half; 300 is not judged
    assert method_text(s) == ("Measured by the router (what the line carried during the test, all traffic): 1 of 2 "
                              "tests; the others by the test alone (one server); in 1 the test itself got under 70 % "
                              "of what the line carried: other traffic on the network, not a slow line")
    s2 = summarize([{"status": "ok", **measured(down=300.0, wan_down=900.0, up=30.0, wan_up=35.0)}], 1000.0, 40.0)
    assert s2.under == 0                                  # the line's 900, not the test's 300, against the plan
