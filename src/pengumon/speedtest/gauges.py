"""The speed test's gauges, appended to GAUGES by observability/gauges.py: the speeds and the loaded latency of the last
test that worked (status ok). Read from the runner's `last_ok` in memory, synchronously, as the export-thread rule in
gauges.py requires (import `pengumon.observability.gauges` first, never this module on its own: it needs `Gauge`
from there); no sample until a test has worked (a series is absent, not zero). Labels are fixed enums only."""

from typing import Any, List, Optional

from pengumon.observability.gauges import Gauge, Sample
from pengumon.speedtest.quality import headline

PHASES = (("idle", "idle_ms"), ("download", "loaded_down_ms"), ("upload", "loaded_up_ms"))


def _last(state: Any) -> Optional[dict]:
    runner = getattr(state, "speedtest", None)
    return getattr(runner, "last_ok", None) if runner is not None else None


def _speed(key: str):
    """The headline speed (speedtest/quality.headline: the line's rate the router measured, else the test's own)."""
    def collect(state: Any) -> List[Sample]:
        row = _last(state)
        value = headline(row)[key] if row is not None else None
        return [({}, value)] if value is not None else []
    return collect


def _loaded_latency(state: Any) -> List[Sample]:
    row = _last(state)
    if row is None:
        return []
    return [({"phase": phase}, row[column]) for phase, column in PHASES if row.get(column) is not None]


SPEED_GAUGES: List[Gauge] = [
    Gauge("pengumon_speedtest_download_mbps", "pengumon.speedtest.download", "Mbit/s",
          "Download speed of the last speed test that worked (the line's rate the router measured during it, else the "
          "test's own).", _speed("down_mbps")),
    Gauge("pengumon_speedtest_upload_mbps", "pengumon.speedtest.upload", "Mbit/s",
          "Upload speed of the last speed test that worked (the line's rate the router measured during it, else the "
          "test's own).", _speed("up_mbps")),
    Gauge("pengumon_speedtest_loaded_latency_ms", "pengumon.speedtest.loaded_latency", "{ms}",
          "Median latency of the public probe targets in the last speed test that worked, by phase (idle before the "
          "test, download, upload).", _loaded_latency),
]
