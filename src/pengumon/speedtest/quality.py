"""How far a measured speed can be trusted, and which speed answers the question (`headline`).

A direction runs until data has flowed for about 8 seconds or its data cap (Data per test) is used, whichever comes
first, so on a fast line with a small cap it lasts very little: 25 MB at 600 Mbps is a third of a second, about as long
as the server takes to start some of its answers. Such a short test scatters: measured on a line that Cloudflare served
at about 600 Mbps (2026-11), seven 25 MB tests read 500 to 840 Mbps, five 100 MB tests 575 to 665. The time a direction
was measured over is not stored, but it follows from what is: bytes * 8 / Mbps (a phase that ran its time is about 8 s
long by this count; one the cap cut short, less). Under MIN_SECONDS it is `low` confidence, with a note that says how
much data a test of that line needs: MIN_SECONDS of it at the speed measured (`needed_mb`), as the next of the
setting's choices. Pure; computed when read, like the bufferbloat grade.
"""

import math
import typing
from typing import Any, Dict, Optional, Sequence, Tuple

from pengumon.speedtest.settings import SpeedtestConfig

MIN_SECONDS = 1.0
HOUSEHOLD_SHARE = 0.7  # the test's own rate under this share of the line's: the rest was other traffic (headline)
# the choices of Data per test (speedtest.download_mb / upload_mb)
DOWNLOAD_CHOICES = typing.get_args(SpeedtestConfig.model_fields["download_mb"].annotation)
UPLOAD_CHOICES = typing.get_args(SpeedtestConfig.model_fields["upload_mb"].annotation)


def measured_seconds(nbytes: Optional[int], mbps: Optional[float]) -> Optional[float]:
    """About how long the direction was measured over; None without a speed."""
    if not nbytes or not mbps or mbps <= 0:
        return None
    return nbytes * 8 / (mbps * 1e6)


def needed_mb(mbps: float, seconds: float = MIN_SECONDS) -> int:
    """Megabytes a direction of `mbps` needs to last `seconds` (rounded up to whole MB)."""
    return max(1, math.ceil(mbps * seconds / 8))


def _choice(needed: int, choices: Sequence[int]) -> Optional[int]:
    return next((c for c in choices if c >= needed), None)


def _direction(name: str, nbytes: Optional[int], mbps: Optional[float],
               choices: Sequence[int]) -> Tuple[Optional[str], Optional[str]]:
    """(confidence, note) of one direction: (None, None) without a speed."""
    seconds = measured_seconds(nbytes, mbps)
    if seconds is None:
        return None, None
    if seconds >= MIN_SECONDS:
        return "ok", None
    needed = needed_mb(mbps)
    choice = _choice(needed, choices)
    advice = (f"about {needed} MB would ({choice} MB in Data per test)" if choice is not None
              else f"about {needed} MB would, more than the largest choice ({choices[-1]} MB)")
    return "low", (f"{name}: {nbytes / 1e6:.0f} MB lasted {seconds:.1f} s, too little to measure a line this fast; "
                   f"{advice}")


def confidence(row: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    """(confidence, note) of a stored test: "low" when a direction lasted under MIN_SECONDS (the note says which and how
    much data would do), "ok" when the measured directions lasted long enough, None when it measured no speed."""
    results = [_direction("download", row.get("bytes_down"), row.get("down_mbps"), DOWNLOAD_CHOICES),
               _direction("upload", row.get("bytes_up"), row.get("up_mbps"), UPLOAD_CHOICES)]
    levels = [level for level, _ in results if level is not None]
    if not levels:
        return None, None
    notes = [note for _, note in results if note]
    return ("low" if "low" in levels else "ok"), ("; ".join(notes) or None)


def _pick(test: Optional[float], line: Optional[float], level: Optional[str]) -> Tuple[Optional[float], Optional[str]]:
    """(headline Mbps, "router" | "test" | None) of one direction."""
    if test is None:
        return None, None
    if line is not None and line >= test and level != "low":
        return line, "router"
    return test, "test"


def headline(row: Dict[str, Any]) -> Dict[str, Any]:
    """The speeds a stored test answers "is my line as fast as I pay for?" with, per direction: the line's rate the
    router measured during the test (`wan_down_mbps` / `wan_up_mbps`: the test and the household's traffic, what the
    line carried while it was full; speedtest/router.py) when it gave one, else the test's own rate (`down_mbps` /
    `up_mbps`). Both are lower bounds of what the line can carry, so the router's is taken when it is at least the
    test's own: one under it was not the line's full view (a router that refreshes its counters slowly, a phase too
    short for its window). A direction too short to measure the line (confidence "low") keeps the test's own figure:
    the router's window then saw mostly the time around it.

    `{down_mbps, up_mbps, down_by, up_by, measured_by, note}`: `measured_by` is "router" when every direction with a
    speed was measured by the router, "test" when any was not, None without a speed. `note` says when the test's own
    rate was far below the line's (under HOUSEHOLD_SHARE of it): the rest was the household's traffic sharing the
    line, not a slow line."""
    down_level = _direction("download", row.get("bytes_down"), row.get("down_mbps"), DOWNLOAD_CHOICES)[0]
    up_level = _direction("upload", row.get("bytes_up"), row.get("up_mbps"), UPLOAD_CHOICES)[0]
    down, down_by = _pick(row.get("down_mbps"), row.get("wan_down_mbps"), down_level)
    up, up_by = _pick(row.get("up_mbps"), row.get("wan_up_mbps"), up_level)
    sources = [by for by in (down_by, up_by) if by is not None]
    shared = [f"{name} {own:.0f} of the {line:.0f} Mbps the line carried"
              for name, by, own, line in (("download", down_by, row.get("down_mbps"), down),
                                          ("upload", up_by, row.get("up_mbps"), up))
              if by == "router" and own is not None and line and own < line * HOUSEHOLD_SHARE]
    note = (f"The test itself got {' and '.join(shared)}: the rest was other traffic on your network at the same time, "
            "not a slow line." if shared else None)
    return {"down_mbps": down, "up_mbps": up, "down_by": down_by, "up_by": up_by,
            "measured_by": None if not sources else ("router" if all(s == "router" for s in sources) else "test"),
            "note": note}


def trusted(row: Dict[str, Any]) -> Tuple[Optional[float], Optional[float]]:
    """(down_mbps, up_mbps) of a stored test as the headline gives them (the line's rate the router measured, else the
    test's own), each None when that direction measured nothing or provably lasted too short to measure the line
    (confidence "low"): what the reports' figures and the slow-line alert may judge. A speed without its byte count
    cannot be judged and is kept."""
    down = _direction("download", row.get("bytes_down"), row.get("down_mbps"), DOWNLOAD_CHOICES)[0]
    up = _direction("upload", row.get("bytes_up"), row.get("up_mbps"), UPLOAD_CHOICES)[0]
    best = headline(row)
    return (None if down == "low" else best["down_mbps"], None if up == "low" else best["up_mbps"])
