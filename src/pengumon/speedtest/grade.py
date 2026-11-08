"""The bufferbloat grade: how much a loaded line adds to the delay. Pure; computed when read, never stored."""

from typing import Optional

# (upper bound in ms of the added latency, exclusive, grade)
GRADES = ((5.0, "A+"), (30.0, "A"), (60.0, "B"), (200.0, "C"), (400.0, "D"))


def added_latency_ms(idle_ms: Optional[float], loaded_down_ms: Optional[float],
                     loaded_up_ms: Optional[float]) -> Optional[float]:
    """The larger of the two loaded medians minus the idle one (never below 0); None without the idle median or any
    loaded one."""
    loaded = [v for v in (loaded_down_ms, loaded_up_ms) if v is not None]
    if idle_ms is None or not loaded:
        return None
    return max(0.0, max(loaded) - idle_ms)


def grade_for_added(added_ms: Optional[float]) -> Optional[str]:
    """The grade of an added latency in ms; None for None."""
    if added_ms is None:
        return None
    return next((grade for limit, grade in GRADES if added_ms < limit), "F")


def bufferbloat_grade(idle_ms: Optional[float], loaded_down_ms: Optional[float],
                      loaded_up_ms: Optional[float]) -> Optional[str]:
    """A+ under 5 ms added, A under 30, B under 60, C under 200, D under 400, else F. The added latency is the larger of
    the two loaded medians minus the idle one (never below 0). None without the idle median or any loaded one."""
    return grade_for_added(added_latency_ms(idle_ms, loaded_down_ms, loaded_up_ms))
