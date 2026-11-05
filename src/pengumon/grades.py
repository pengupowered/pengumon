"""One place that decides good / warn / critical for a number the dashboard colours.

A `Range` is the good zone of one metric plus the limits beyond which it is critical, and the scale a gauge draws it
on. Every metric that is coloured (DOCSIS power and SNR, uncorrectables, router CPU and memory, WAN load) has its
Range defined next to the code that knows its thresholds (pengumon.lines.docsis, pengumon.devices.monitors); the Wi-Fi
signal levels, which are not settings, are here (`SIGNAL_LEVELS`). The snapshots and answers carry the resulting
grades, so the browser only maps a grade to a colour and never compares numbers.

Boundaries belong to the better grade: a value exactly on the edge of the good zone is good, one exactly on a
critical limit is warn.
"""

from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional

GOOD = "good"
WARN = "warn"
CRITICAL = "critical"
_SEVERITY = {GOOD: 0, WARN: 1, CRITICAL: 2}


@dataclass(frozen=True)
class Range:
    """The scale of a gauge (`min` to `max`), its good zone (`good_low` to `good_high`, None: as far as the scale goes)
    and the limits of the critical zones (None: there is no critical zone on that side). Between the good zone and a
    critical limit a value is warn."""

    unit: str
    min: float
    max: float
    good_low: Optional[float] = None
    good_high: Optional[float] = None
    critical_low: Optional[float] = None
    critical_high: Optional[float] = None

    def grade(self, value: Optional[float]) -> Optional[str]:
        """good / warn / critical, or None for a reading that is missing."""
        if value is None:
            return None
        if (self.critical_low is not None and value < self.critical_low) or (
                self.critical_high is not None and value > self.critical_high):
            return CRITICAL
        if (self.good_low is not None and value < self.good_low) or (
                self.good_high is not None and value > self.good_high):
            return WARN
        return GOOD

    def as_dict(self) -> Dict[str, Any]:
        """What the API sends: the gauge scale and the zones, with open ends filled in from the scale."""
        return {
            "unit": self.unit,
            "min": self.min,
            "good_low": self.min if self.good_low is None else self.good_low,
            "good_high": self.max if self.good_high is None else self.good_high,
            "max": self.max,
            "critical_low": self.critical_low,
            "critical_high": self.critical_high,
        }


def worst(grades: Iterable[Optional[str]]) -> Optional[str]:
    """The most severe of the grades (None entries are readings that are missing); None when there is none."""
    known = [g for g in grades if g is not None]
    return max(known, key=_SEVERITY.__getitem__) if known else None


def known_grades(**grades: Optional[str]) -> Dict[str, str]:
    """The grades that exist, by metric name (a missing reading has no entry rather than a null one)."""
    return {name: grade for name, grade in grades.items() if grade is not None}


# A Wi-Fi signal in dBm as a word, the number of bars (of 4) and a grade: the thresholds UniFi uses.
SIGNAL_LEVELS = ((-60, "Excellent", 4, "good"), (-67, "Good", 3, "good"), (-75, "Fair", 2, "warn"))
SIGNAL_WEAK = ("Weak", 1, "critical")


def signal_grade(dbm: Optional[int]) -> Optional[Dict[str, Any]]:
    """The word, bars and grade (good / warn / critical) of a signal; None without one."""
    if dbm is None:
        return None
    for above, word, bars, grade in SIGNAL_LEVELS:
        if dbm > above:
            return {"word": word, "bars": bars, "grade": grade}
    word, bars, grade = SIGNAL_WEAK
    return {"word": word, "bars": bars, "grade": grade}
