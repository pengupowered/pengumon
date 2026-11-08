"""The speed tests in the AI pack (reports.ai_pack: SpeedFact, the "Speed tests" section): the summary of every test in
the window, the newest rows as a table, the caps per detail level, the trimming order, and what stays out at each
redaction level (a LibreSpeed server's host never; a colo or M-Lab machine name may be named)."""

import dataclasses
import re

import pytest

from ai_pack_helpers import canary_facts, ts
from pengumon.reports import ai_pack
from pengumon.reports.ai_pack import render_pack, speed_fact

HOST = "canary-librespeed.lan"


def row(k, provider="cloudflare", server="AMS", status="ok", down=212.0, up=19.0, reason=None, hour=None):
    when = ts(11, 3, 0, 5) + 1800 * k if hour is None else hour
    moved = status in ("ok", "partial")
    return {"ts": when, "status": status, "provider": provider, "server": server, "reason": reason,
            "down_mbps": down if moved else None, "up_mbps": up if status == "ok" else None,
            "idle_ms": 10.0 if moved else None, "loaded_down_ms": 50.0 if moved else None,
            "loaded_up_ms": 30.0 if moved else None, "trigger": "scheduled"}


def with_speed(rows, level_facts=None, plan=(300.0, 20.0)):
    return dataclasses.replace(level_facts or canary_facts(), speed=speed_fact(rows, *plan))


def section(md):
    return md.split("## Speed tests\n")[1].split("\n## ")[0]


def test_a_pack_without_speed_tests_has_no_section():
    assert "Speed tests" not in render_pack(canary_facts(), "full").markdown
    assert speed_fact([], 300.0, 20.0) is None


def test_the_summary_covers_every_test_and_the_table_lists_the_ones_that_ran():
    rows = [row(0), row(1, down=90.0), row(2, status="failed", reason="Cloudflare answered HTTP 500"),
            row(3, status="skipped", reason="line busy"), row(4, status="partial", up=None, reason="upload: x")]
    md = render_pack(with_speed(rows), "full").markdown
    sec = section(md)
    assert "- 5 tests (2 ok, 1 partial, 1 failed, 1 skipped)" in sec
    assert "- 1 of 3 tests under 50 % of the plan speed" in sec                      # 90 of 300; the failed one has no speed
    assert "Latency under load: median 40 ms added, bufferbloat grade B" in sec
    table = [l for l in sec.splitlines() if l.startswith("| ") and "Start" not in l and "---" not in l]
    assert len(table) == 4                                                             # the skipped one is only counted
    assert any("failed" in l and "Cloudflare answered HTTP 500" in l for l in table)
    assert any("partial" in l and "upload: x" in l for l in table)
    assert all("line busy" not in l for l in table)
    assert md.index("## This week") < md.index("## Speed tests") < md.index("## Your devices")


def test_the_exact_summary_lines():
    rows = [row(0, down=212.0), row(1, down=90.0), row(2, down=212.0)]
    sec = section(render_pack(with_speed(rows), "full").markdown)
    assert "- 3 tests (3 ok)" in sec
    assert "- Download: median 212 of 300 Mbps (71 %), lowest 90 Mbps" in sec
    assert "- Upload: median 19 of 20 Mbps (95 %), lowest 19 Mbps" in sec
    assert "- 1 of 3 tests under 50 % of the plan speed" in sec
    plain = section(render_pack(with_speed(rows, plan=(None, None)), "full").markdown)
    assert "No plan speed is set in Pengumon" in plain and "of the plan speed" not in plain


@pytest.mark.parametrize("detail,shown", [("compact", ai_pack.CAPS["compact"]["speed"]),
                                          ("detailed", ai_pack.CAPS["detailed"]["speed"])])
def test_long_periods_show_the_newest_rows_and_summarize_the_rest(detail, shown):
    rows = [row(k, down=100.0 + k) for k in range(40)]
    sec = section(render_pack(with_speed(rows), "full", detail).markdown)
    table = [l for l in sec.splitlines() if l.startswith("| ") and "Start" not in l and "---" not in l]
    assert len(table) == shown and f"(the {shown} newest of 40 that ran)" in sec
    assert "- 40 tests (40 ok)" in sec and "lowest 100 Mbps" in sec                   # the summary sees all 40
    assert "| 139 |" in table[-1] and "| 100 |" not in " ".join(table)                   # the newest rows, oldest first


def test_the_hard_cap_cuts_the_speed_rows_before_the_module_tables(monkeypatch):
    facts = with_speed([row(k) for k in range(30)])
    full = render_pack(facts, "full", "detailed")
    assert full.trimmed == [] or all("speed" not in t for t in full.trimmed)
    monkeypatch.setattr(ai_pack, "HARD_CAP_CHARS", full.chars - 200)
    cut = render_pack(facts, "full", "detailed")
    assert "speed test rows cut to 5" in cut.trimmed and "module tables dropped" not in cut.trimmed
    table = [l for l in section(cut.markdown).splitlines() if l.startswith("| ") and "Start" not in l and "---" not in l]
    assert len(table) == 5 and "- 30 tests (30 ok)" in section(cut.markdown)


@pytest.mark.parametrize("level", ["full", "vague", "anonymized"])
def test_a_librespeed_servers_host_is_never_shown(level):
    rows = [row(0, provider="librespeed", server=HOST),
            row(1, provider="librespeed", server=HOST, status="failed", reason=f"could not connect to {HOST}: ConnectError")]
    md = render_pack(with_speed(rows), level).markdown
    assert HOST not in md and "canary-librespeed" not in md.lower()
    sec = section(md)
    assert "own LibreSpeed server" in sec and "could not connect to the server: ConnectError" in sec


@pytest.mark.parametrize("level", ["full", "vague", "anonymized"])
def test_a_librespeed_servers_host_is_left_out_whatever_its_case_and_wherever_the_note_is_cut(level):
    """The server label is lowercased, the 404 hint repeats the address as entered; and the note is cut at 80
    characters after the host is replaced, never before (a cut host could not be found)."""
    padding = "x" * (80 - len("download: could not connect to ") - 8)      # the cut falls 8 characters into the host
    rows = [row(0, provider="librespeed", server=HOST, status="failed",
                reason=f"download: {HOST.upper()} answered HTTP 404"),
            row(1, provider="librespeed", server=HOST, status="failed",
                reason=f"{padding}download: could not connect to {HOST}: ConnectError")]
    md = render_pack(with_speed(rows), level).markdown
    assert "canary-l" not in md.lower()                    # (not even the first 8 characters; the pack's other canaries
    assert "download: the server answered HTTP 404" in section(md)    # are named canary-router-name and the like)

def test_a_colo_or_machine_name_may_be_named_and_the_times_follow_the_level():
    rows = [row(0, server="AMS"), row(1, provider="mlab", server="mlab1-ams03.mlab-oti.measurement-lab.org")]
    full = section(render_pack(with_speed(rows), "full").markdown)
    assert "| Cloudflare | AMS |" in full and "M-Lab | mlab1-ams03.mlab-oti.measurement-lab.org |" in full
    assert "| Tue 00:05:00 |" in full                                                  # a week's rows: weekday and clock
    anon = render_pack(with_speed(rows), "anonymized")
    assert re.search(r"\| Day \d 00:05:00 \|", section(anon.markdown)) and "Tue" not in section(anon.markdown)
    assert anon.hits == {}


def test_the_speeds_are_the_lines_the_router_measured_and_the_table_says_by_what():
    rows = [row(0, down=300.0, up=19.0), row(1, down=280.0, up=18.0)]
    rows[0].update(bytes_down=300_000_000, bytes_up=19_000_000, wan_down_mbps=905.0, wan_up_mbps=19.5)
    sec = section(render_pack(with_speed(rows, plan=(1000.0, 20.0)), "full").markdown)
    head = next(l for l in sec.splitlines() if l.startswith("| Start"))
    assert "| By |" in head
    table = [l for l in sec.splitlines() if l.startswith("| ") and "Start" not in l and "---" not in l]
    assert "| 905 | 19.5 | router |" in table[0] and "| 280 | 18 | test |" in table[1]
    assert ("Measured by the router (what the line carried during the test, all traffic): 1 of 2 tests; the others by "
            "the test alone (one server); in 1 the test itself got under 70 % of what the line carried") in sec
    assert "*By* router" in sec
