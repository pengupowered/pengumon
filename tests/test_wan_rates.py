"""WAN rates from cumulative counters (pengumon.devices.wan)."""

from pengumon.capabilities import WanCounters
from pengumon.devices.wan import RATE_HISTORY_SECONDS, WanRates

MB = 1_000_000


def test_first_reading_only_sets_the_baseline():
    wan = WanRates(clock=lambda: 1010.0)
    assert wan.update(1000.0, WanCounters(100 * MB, 50 * MB)) == (None, None)
    assert wan.update(1010.0, WanCounters(112_500_000, 56_250_000)) == (10.0, 5.0)
    assert wan.recent_rates() == [(1010.0, 10.0, 5.0)]


def test_readings_closer_than_a_second_give_no_rate_and_keep_the_baseline():
    """Stretching 0.2 s to 1 s would report a fifth of the real rate; the next reading covers the whole span."""
    wan = WanRates()
    wan.update(1000.0, WanCounters(0, 0))
    assert wan.update(1000.2, WanCounters(25 * MB, 2_500_000)) == (None, None)
    assert wan.update(1002.0, WanCounters(250 * MB, 25 * MB)) == (1000.0, 100.0)


def test_the_counters_read_time_is_used_when_the_driver_gives_one():
    wan = WanRates()
    wan.update(1010.0, WanCounters(0, 0, read_at=1000.0))
    assert wan.update(1020.0, WanCounters(250 * MB, 25 * MB, read_at=1002.0)) == (1000.0, 100.0)  # over 2 s, not 10


def test_a_clock_set_back_starts_a_new_baseline_instead_of_stalling():
    wan = WanRates()
    wan.update(5000.0, WanCounters(0, 0))
    assert wan.update(1000.0, WanCounters(10 * MB, 1 * MB)) == (None, None)  # new baseline at 1000
    assert wan.update(1010.0, WanCounters(22_500_000, 2_250_000)) == (10.0, 1.0)


def test_a_poll_without_counters_keeps_the_baseline():
    """The next rate covers the whole interval since the last reading, instead of a too-short one."""
    wan = WanRates()
    wan.update(1000.0, WanCounters(0, 0))
    assert wan.update(1010.0, None) == (None, None)
    assert wan.update(1020.0, WanCounters(25 * MB, 2_500_000)) == (10.0, 1.0)  # 25 MB over 20 s, not 10 s


def test_a_counter_reset_gives_no_rate_and_a_new_baseline():
    """A router restart starts its counters from zero: that is not an idle line."""
    wan = WanRates(clock=lambda: 1020.0)
    wan.update(1000.0, WanCounters(900 * MB, 90 * MB))
    assert wan.update(1010.0, WanCounters(1 * MB, 1 * MB)) == (None, None)
    assert wan.update(1020.0, WanCounters(13_500_000, 2 * MB)) == (10.0, 0.8)
    assert [r[1] for r in wan.recent_rates()] == [10.0]


def test_one_direction_reset_drops_the_whole_rate():
    wan = WanRates()
    wan.update(1000.0, WanCounters(10 * MB, 10 * MB))
    assert wan.update(1010.0, WanCounters(20 * MB, 1 * MB)) == (None, None)


def test_known_width_counters_wrap():
    wan = WanRates()
    top = 1 << 32
    wan.update(1000.0, WanCounters(top - 5 * MB, 100, counter_bits=32))
    assert wan.update(1010.0, WanCounters(7_500_000, 100, counter_bits=32)) == (10.0, 0.0)


def test_history_keeps_ten_minutes():
    now = {"t": 2000.0}
    wan = WanRates(clock=lambda: now["t"])
    for i, ts in enumerate((now["t"] - 700, now["t"] - 690, now["t"] - 500, now["t"] - 5)):
        wan.update(ts, WanCounters(i * 10 * MB, i * MB))
    # rates at 1310 (outside the window), 1500 and 1995
    assert [round(r[0]) for r in wan.recent_rates()] == [1500, 1995]
    now["t"] += 101  # 1500 is now more than RATE_HISTORY_SECONDS old
    assert now["t"] - RATE_HISTORY_SECONDS > 1500
    assert wan.recent_rates() == [(1995.0, 0.16, 0.02)]  # 10 MB / 1 MB over 495 s
