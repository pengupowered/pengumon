"""Unit tests for traceroute parsing."""

from pengumon.engine.mtr import (
    parse_traceroute_output,
    parse_tracert_output,
)

TRACERT_OUTPUT = """
Tracing route to 1.1.1.1 over a maximum of 15 hops

  1    <1 ms    <1 ms    <1 ms  192.168.1.1
  2    10 ms    12 ms    10 ms  192.0.2.131
  3     *        *        *     Request timed out.
  4    11 ms     *       11 ms  198.51.100.1
  5     9 ms     8 ms     9 ms  one.one.one.one [1.1.1.1]

Trace complete.
"""

TRACEROUTE_OUTPUT = """traceroute to 1.1.1.1 (1.1.1.1), 15 hops max, 60 byte packets
 1  192.168.1.1  1.874 ms  1.674 ms  1.854 ms
 2  192.0.2.131  16.656 ms  15.087 ms 192.0.2.130  16.639 ms
 3  * * *
"""


def test_parse_tracert_keeps_sub_ms_and_timed_out_hops():
    assert parse_tracert_output(TRACERT_OUTPUT) == [
        (1, "192.168.1.1", 0.5),
        (2, "192.0.2.131", 10.67),
        (3, None, None),
        (4, "198.51.100.1", 11.0),
        (5, "1.1.1.1", 8.67),
    ]


def test_parse_traceroute_linux():
    assert parse_traceroute_output(TRACEROUTE_OUTPUT) == [
        (1, "192.168.1.1", 1.8),
        (2, "192.0.2.131", 16.13),
        (3, None, None),
    ]
