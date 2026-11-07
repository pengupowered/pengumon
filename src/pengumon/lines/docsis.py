"""DOCSIS access lines (cable): how a set of channels is summarised and judged, for every DOCSIS modem driver.

Thresholds (a LineSignalConfig, configurable on the Settings page; DEFAULT_LEVELS when none is passed) default to the commonly quoted cable-industry limits: upstream
power above 49 dBmV (51 critical) or below 38 dBmV (35 critical), downstream SNR below 33 dB (30 critical),
downstream power beyond ±8 dBmV. They are one set, used for the snapshot status, the grades the dashboard colours by, the channel table and the dossier's list of
degraded channels (an older dashboard and the dossier used 48 and ±7 dBmV for some of these; the status and the
snapshot averages always used 49 and ±8, and those won). Uncorrectable codewords count from the modem's boot, so
health rests on the errors *between* polls, not on the total.

Storage: a reading is a `line_samples` row of technology "docsis" (pengumon.db.samples.LineSample). The uncorrectable
counts are its generic error columns, the three averages its `summary`, the channels its `detail`; `line_sample` and
`snapshot_from_sample` convert, `DocsisHistory` reads one device's rows back as the DocsisSignalSnapshot the API, the
inspector and the dossier use.
"""

import json
import logging
from typing import Any, Dict, Iterable, List, NamedTuple, Optional, Sequence, Tuple

from pengumon.db.samples import LineSample
from pengumon.grades import CRITICAL, GOOD, WARN, Range, known_grades
from pengumon.lines.docsis_model import DocsisChannel, DocsisSignalSnapshot
from pengumon.lines.levels import LineSignalConfig

logger = logging.getLogger("pengumon.lines")

TECHNOLOGY = "docsis"

# The levels a DOCSIS line is judged by: the Settings page's "Your line" section (pengumon.lines.levels, the
# defaults and their reasoning are there). Every function below takes them as `levels`; the live app passes the live
# config's section (the line monitor reads it at each poll: devices/monitors.py watch_levels), a report its config's.
# DEFAULT_LEVELS, used when none are passed, is never changed.
#
# How new uncorrectable codewords are judged. An uncorrectable codeword is a frame lost after error correction; a few
# are normal on a cable plant, and what matters is how many there are against the traffic and whether they persist:
#  - with the modem's error-free codeword counts, the ratio uncorrectable / all codewords of the interval:
#    warn from uncorrectable_ratio_warning (1e-6: about one lost codeword in a million, a stream starts to stutter
#    when it persists), critical from uncorrectable_ratio_critical (1e-4: lost packets are certain);
#  - without them, the rate per minute (the interval can be 10 s or 30 min): warn from
#    uncorrectable_warning_per_minute (60) or when the previous read had new errors too, critical above
#    uncorrectable_critical_per_minute (500);
# anything below is a short burst: graded good, with a note, and shown to the person as information.
DEFAULT_LEVELS = LineSignalConfig()


def ranges(levels: Optional[LineSignalConfig] = None) -> Dict[str, Range]:
    """The good zone of each metric on the scale a gauge draws, and where it turns critical (see pengumon.grades),
    at `levels` (DEFAULT_LEVELS when None)."""
    lv = levels or DEFAULT_LEVELS
    return {
        "ds_power": Range("dBmV", -15.0, 15.0, good_low=-lv.ds_power_warning_abs_dbmv,
                          good_high=lv.ds_power_warning_abs_dbmv),
        "ds_snr": Range("dB", 20.0, 50.0, good_low=lv.ds_snr_warning_db, critical_low=lv.ds_snr_critical_db),
        "us_power": Range("dBmV", 30.0, 56.0, good_low=lv.us_power_low_warning_dbmv, good_high=lv.us_power_warning_dbmv,
                          critical_low=lv.us_power_low_critical_dbmv, critical_high=lv.us_power_critical_dbmv),
        # new uncorrectable codewords per minute (the grade itself also weighs the ratio and recurrence: judge_errors)
        "uncorrectables": Range("codewords/min", 0.0, 1000.0, good_high=lv.uncorrectable_warning_per_minute,
                                critical_high=float(lv.uncorrectable_critical_per_minute)),
    }


def judge_errors(recent: int, interval_seconds: Optional[float], ratio: Optional[float],
                 recurring: bool, levels: Optional[LineSignalConfig] = None) -> Tuple[str, Optional[str]]:
    """(grade, note) for `recent` new uncorrectable codewords in `interval_seconds`. See the thresholds above."""
    lv = levels or DEFAULT_LEVELS
    if recent <= 0:
        return GOOD, None
    minutes = max(interval_seconds or 60.0, 1.0) / 60.0
    rate = recent / minutes
    if ratio is not None:
        detail = f"{recent:,} uncorrectable codewords in {minutes:.0f} min, {ratio:.1e} of all codewords"
        if ratio >= lv.uncorrectable_ratio_critical:
            return CRITICAL, detail
        if ratio >= lv.uncorrectable_ratio_warning:
            return WARN, detail
        return GOOD, f"{detail}: below the {lv.uncorrectable_ratio_warning:.0e} that matters"
    detail = f"{recent:,} uncorrectable codewords in {minutes:.0f} min ({rate:.0f} a minute)"
    if rate > lv.uncorrectable_critical_per_minute:
        return CRITICAL, detail
    if recurring:
        return WARN, f"{detail}, and new ones in the previous read as well"
    if rate >= lv.uncorrectable_warning_per_minute:
        return WARN, detail
    return GOOD, f"{detail}: a short burst, not repeated"


def grade_snapshot(snapshot: DocsisSignalSnapshot, levels: Optional[LineSignalConfig] = None) -> DocsisSignalSnapshot:
    """Fills in the grades of a reading (in place) at `levels`: the snapshot's four metrics and, per channel, power,
    SNR and new errors. A snapshot without a reading (error, unconfigured) has none."""
    if snapshot.status in NO_READING_STATUSES:
        snapshot.grades = {}
        return snapshot
    graded = ranges(levels)
    snapshot.grades = known_grades(
        ds_power=graded["ds_power"].grade(snapshot.downstream_power_avg),
        ds_snr=graded["ds_snr"].grade(snapshot.downstream_snr_avg),
        us_power=graded["us_power"].grade(snapshot.upstream_power_avg),
        uncorrectables=judge_errors(snapshot.uncorrectables_recent, snapshot.uncorrectables_interval_seconds,
                                    snapshot.uncorrectables_ratio, snapshot.errors_recurring, levels)[0],
    )
    for channel in snapshot.channels:
        power = graded["ds_power" if channel.direction == "downstream" else "us_power"]
        channel.grades = known_grades(
            power=power.grade(channel.power_dbmv),
            snr=graded["ds_snr"].grade(channel.snr_db),
            uncorrectables=snapshot.grades["uncorrectables"] if channel.uncorrectables_recent > 0 else GOOD,
        )
    return snapshot


def summarize(channels: Sequence[DocsisChannel], ts: float,
              levels: Optional[LineSignalConfig] = None) -> DocsisSignalSnapshot:
    """Averages and RF status of one reading, at `levels`. The error deltas are added by DocsisErrorTracker."""
    ds_channels = [c for c in channels if c.direction == "downstream"]
    us_channels = [c for c in channels if c.direction == "upstream"]

    ds_power_avg = round(sum(c.power_dbmv for c in ds_channels) / len(ds_channels), 2) if ds_channels else None
    # Average over the channels that report an SNR; dividing by every channel would bias it low.
    snrs = [c.snr_db for c in ds_channels if c.snr_db is not None]
    ds_snr_avg = round(sum(snrs) / len(snrs), 2) if snrs else None
    us_power_avg = round(sum(c.power_dbmv for c in us_channels) / len(us_channels), 2) if us_channels else None
    uncorrectables = sum(c.uncorrectables or 0 for c in ds_channels)

    lv = levels or DEFAULT_LEVELS
    status = "healthy"
    if (us_power_avg and us_power_avg > lv.us_power_critical_dbmv) or (ds_snr_avg and ds_snr_avg < lv.ds_snr_critical_db):
        status = "critical"
    elif ((us_power_avg and us_power_avg > lv.us_power_warning_dbmv) or (ds_snr_avg and ds_snr_avg < lv.ds_snr_warning_db)
          or (ds_power_avg and abs(ds_power_avg) > lv.ds_power_warning_abs_dbmv)):
        status = "warning"

    return grade_snapshot(DocsisSignalSnapshot(
        timestamp=ts,
        downstream_power_avg=ds_power_avg,
        downstream_snr_avg=ds_snr_avg,
        upstream_power_avg=us_power_avg,
        uncorrectables_total=uncorrectables,
        channels=[c.model_copy() for c in channels],  # the error tracker fills in fields; readings stay as read
        status=status,
    ), levels)


class DocsisErrorTracker:
    """New uncorrectable codewords since the previous good reading (in total and per channel), how serious they are
    (judge_errors) and the status that implies. The first reading after a start only sets the baseline."""

    def __init__(self):
        self._prev_total: Optional[int] = None
        self._prev_ts: Optional[float] = None
        self._prev_channels: Dict[int, int] = {}
        self._prev_counts: Dict[int, tuple] = {}  # channel -> (error-free, correctable, uncorrectable) codewords
        self._prev_had_errors = False

    def seed(self, snapshot: DocsisSignalSnapshot) -> None:
        """Takes a good reading from before a restart as the previous one, so the first poll after it has a delta.
        The error-free counts are not stored, so the first ratio after a restart is unknown."""
        self._prev_total = snapshot.uncorrectables_total
        self._prev_ts = snapshot.timestamp
        self._prev_channels = {ch.channel_id: (ch.uncorrectables or 0) for ch in snapshot.channels}
        self._prev_counts = {}
        self._prev_had_errors = snapshot.uncorrectables_recent > 0

    def _ratio(self, snapshot: DocsisSignalSnapshot) -> Optional[float]:
        """Uncorrectable / all codewords since the previous reading, over the downstream channels that report
        error-free codewords now and did in the previous reading (a modem may leave the row out for one channel, e.g.
        the OFDM one; requiring every channel made the ratio unknown on such a modem, so grading never used it).
        None when no channel is covered, when a covered counter went backwards (a modem restart), or when a channel
        outside the covered ones had new uncorrectable codewords: the ratio would then leave out part of the errors
        and understate it. Errors on uncovered channels are judged by rate and recurrence instead."""
        downstream = [c for c in snapshot.channels if c.direction == "downstream"]
        covered = [c for c in downstream if c.unerrored is not None and c.channel_id in self._prev_counts]
        if not covered or any(c.uncorrectables_recent > 0 for c in downstream if c not in covered):
            return None
        total = bad = 0
        for c in covered:
            now = (c.unerrored, c.correcteds or 0, c.uncorrectables or 0)
            deltas = [n - p for n, p in zip(now, self._prev_counts[c.channel_id])]
            if min(deltas) < 0:
                return None
            total += sum(deltas)
            bad += deltas[2]
        return bad / total if total else None

    def apply(self, snapshot: DocsisSignalSnapshot, levels: Optional[LineSignalConfig] = None) -> DocsisSignalSnapshot:
        """Fills in the recent-error fields of a good snapshot (in place), graded at `levels`, and returns it."""
        if self._prev_total is not None and self._prev_ts is not None:
            dt = max(1.0, snapshot.timestamp - self._prev_ts)
            delta_total = max(0, snapshot.uncorrectables_total - self._prev_total)
            snapshot.uncorrectables_recent = delta_total
            snapshot.uncorrectables_rate_per_sec = round(delta_total / dt, 2)
            for ch in snapshot.channels:
                prev = self._prev_channels.get(ch.channel_id)
                ch.uncorrectables_recent = max(0, (ch.uncorrectables or 0) - prev) if prev is not None else 0
            if delta_total > 0:
                hit = [c for c in snapshot.channels if c.uncorrectables_recent > 0]
                snapshot.uncorrectables_interval_seconds = round(dt, 1)
                snapshot.uncorrectables_ratio = self._ratio(snapshot)
                snapshot.errors_affected_channels = len(hit)
                freqs = [c.frequency_mhz for c in hit if c.frequency_mhz]
                snapshot.errors_span_mhz = [min(freqs), max(freqs)] if freqs else None
                snapshot.errors_recurring = self._prev_had_errors
                grade, snapshot.errors_note = judge_errors(
                    delta_total, dt, snapshot.uncorrectables_ratio, snapshot.errors_recurring, levels)
                # Live health rests on how serious the errors since the last poll are, not on the totals since the
                # modem booted and not on a single small burst.
                if grade == CRITICAL:
                    snapshot.status = "critical"
                elif grade == WARN and snapshot.status == "healthy":
                    snapshot.status = "warning"
        else:
            snapshot.uncorrectables_recent = 0
            snapshot.uncorrectables_rate_per_sec = 0.0
            for ch in snapshot.channels:
                ch.uncorrectables_recent = 0
        self._prev_had_errors = snapshot.uncorrectables_recent > 0
        self._prev_total = snapshot.uncorrectables_total
        self._prev_ts = snapshot.timestamp
        self._prev_channels = {ch.channel_id: (ch.uncorrectables or 0) for ch in snapshot.channels}
        self._prev_counts = {c.channel_id: (c.unerrored, c.correcteds or 0, c.uncorrectables or 0)
                             for c in snapshot.channels if c.direction == "downstream" and c.unerrored is not None}
        return grade_snapshot(snapshot, levels)  # the recent-error figures are known only now

# --- the dossier and the AI pack: which channels are outside typical levels, and why ----------------------

# A channel's uncorrectable codewords since the modem started (not the new ones between polls, which the grades judge):
# above UNCORRECTABLES_LISTED the channel is listed as degraded, above UNCORRECTABLES_HIGH it is called a high count.
UNCORRECTABLES_LISTED = 100
UNCORRECTABLES_HIGH = 500


class Finding(NamedTuple):
    """One way a channel is outside the levels: `rank` (lower is worse: 0 past a critical level, 1 past a warning
    level, 2 and 3 the counts since the modem started), `label` for the dossier, `words` for the AI pack."""
    rank: int
    label: str
    words: str


def channel_findings(c, levels: Optional[LineSignalConfig] = None) -> List[Finding]:
    """Every way channel `c` is outside `levels`, in the dossier's order (its first is the reason the dossier gives;
    the AI pack sorts them by rank). Empty for a channel within them."""
    lv = levels or DEFAULT_LEVELS
    unc = c.uncorrectables or 0
    found: List[Finding] = []
    if unc > UNCORRECTABLES_HIGH:
        found.append(Finding(2, "High uncorrectable count", "high uncorrectable count since the modem started"))
    if c.snr_db is not None and c.snr_db < lv.ds_snr_critical_db:
        found.append(Finding(0, "Low SNR", f"SNR below {lv.ds_snr_critical_db:g} dB (errors likely)"))
    elif c.snr_db is not None and c.snr_db < lv.ds_snr_warning_db:
        found.append(Finding(1, "Low SNR", "low SNR"))
    if c.direction == "upstream":
        if c.power_dbmv > lv.us_power_critical_dbmv:
            found.append(Finding(0, "High upstream power",
                                 f"power above {lv.us_power_critical_dbmv:g} dBmV (may drop off)"))
        elif c.power_dbmv > lv.us_power_warning_dbmv:
            found.append(Finding(1, "High upstream power", "high power"))
        elif c.power_dbmv < lv.us_power_low_warning_dbmv:
            found.append(Finding(1, "Low upstream power", "low power"))
    if UNCORRECTABLES_LISTED < unc <= UNCORRECTABLES_HIGH:
        found.append(Finding(3, "Uncorrectables present", "uncorrectables since the modem started"))
    if c.direction == "downstream" and abs(c.power_dbmv) > lv.ds_power_warning_abs_dbmv:
        found.append(Finding(1, "Power level out of range", "high power" if c.power_dbmv > 0 else "low power"))
    return found


def is_degraded(c, levels: Optional[LineSignalConfig] = None) -> bool:
    """A channel outside the warning levels of `levels` (listed in the dossier and the AI pack)."""
    return bool(channel_findings(c, levels))


def uncorrectable_increase(snapshots) -> int:
    """New uncorrectable codewords seen across consecutive good snapshots (a modem reboot resets the counter)."""
    good = [s for s in snapshots if s.status not in ("error", "unconfigured")]
    total = 0
    for prev, cur in zip(good, good[1:]):
        if cur.uncorrectables_total >= prev.uncorrectables_total:
            total += cur.uncorrectables_total - prev.uncorrectables_total
    return total


def channel_finding(c, levels: Optional[LineSignalConfig] = None) -> str:
    """Why a degraded channel is listed at `levels`, for the dossier: the first of channel_findings."""
    found = channel_findings(c, levels)
    return found[0].label if found else "Power level out of range"


# --- storage ---------------------------------------------------------------------------------------------------------
#
# `detail` holds the channels as positional rows under a format version, {"v":1,"ch":[[...], ...]}: keyed JSON
# (DocsisChannel.model_dump) repeats nine key names in every channel, about three quarters of an 8 KB row for a
# modem with 36 channels. Append-only: a new channel field is added at the end of the version's field list, and a
# reader ignores values beyond the fields it knows (so an older build still reads a newer build's rows). Removing,
# reordering or changing the meaning of a field needs a new version; every version stays readable.

DETAIL_VERSION = 1
DETAIL_FIELDS = {
    1: ("channel_id", "direction", "frequency_mhz", "power_dbmv", "snr_db", "modulation", "correcteds",
        "uncorrectables", "uncorrectables_recent"),
}
_DIRECTION_CODES = {"downstream": "d", "upstream": "u"}
_DIRECTIONS = {code: name for name, code in _DIRECTION_CODES.items()}
_SUMMARY_KEYS = (("ds_power", "downstream_power_avg"), ("ds_snr", "downstream_snr_avg"),
                 ("us_power", "upstream_power_avg"))
_COMPACT = (",", ":")
# Statuses without a reading: their error counts are NULL, not 0 (an error row read nothing).
NO_READING_STATUSES = frozenset({"error", "unconfigured"})
_warned: set = set()


def encode_channels(channels: Iterable[DocsisChannel]) -> Optional[str]:
    """The `detail` of a reading: its channels as positional rows (None for no channels), in their order."""
    fields = DETAIL_FIELDS[DETAIL_VERSION]
    rows = []
    for channel in channels:
        values = channel.model_dump()
        values["direction"] = _DIRECTION_CODES[values["direction"]]
        rows.append([values[f] for f in fields])
    if not rows:
        return None
    return json.dumps({"v": DETAIL_VERSION, "ch": rows}, separators=_COMPACT)


def decode_channels(detail: Optional[str]) -> List[DocsisChannel]:
    """The channels of a `detail`. Values after the version's fields are ignored (fields are only appended). An
    unreadable one (an unknown version, a channel with fewer values than its fields) gives no channels, as an
    unreadable channels_json did before schema v9; it is logged once per process and reason."""
    if not detail:
        return []
    try:
        data = json.loads(detail)
        fields = DETAIL_FIELDS[data["v"]]
        channels = []
        for row in data["ch"]:
            if len(row) < len(fields):
                raise ValueError(f"a channel with {len(row)} values, format {data['v']} has {len(fields)}")
            values = dict(zip(fields, row))  # values beyond the known fields: appended by a newer build
            values["direction"] = _DIRECTIONS[values["direction"]]
            channels.append(DocsisChannel(**values))
        return channels
    except Exception as e:  # whatever is wrong with one stored row, the rest of the history stays readable
        reason = f"{type(e).__name__}: {e}"[:200]
        if reason not in _warned:
            _warned.add(reason)
            logger.warning(f"Unreadable DOCSIS channel detail in line_samples, read as no channels ({reason})")
        return []


def encode_summary(downstream_power_avg: Optional[float], downstream_snr_avg: Optional[float],
                   upstream_power_avg: Optional[float]) -> Optional[str]:
    """The `summary` of a reading: the averages that are known, under short keys (None when none is)."""
    values = dict(zip((short for short, _ in _SUMMARY_KEYS),
                      (downstream_power_avg, downstream_snr_avg, upstream_power_avg)))
    known = {k: v for k, v in values.items() if v is not None}
    return json.dumps(known, separators=_COMPACT) if known else None


def decode_summary(summary: Optional[str]) -> Dict[str, Any]:
    """DocsisSignalSnapshot field -> value for the averages in a `summary`."""
    data = json.loads(summary) if summary else {}
    return {name: data.get(short) for short, name in _SUMMARY_KEYS}


def line_sample(device_id: str, snapshot: DocsisSignalSnapshot) -> LineSample:
    """The stored form of a snapshot. Not stored, as before schema v9: the error rate (derived from the counts) and
    the duplicate fields; stored but not read back into snapshots (step 5 of the device-capabilities plan decides how
    the history shows it): the error message."""
    reading = snapshot.status not in NO_READING_STATUSES
    return LineSample(
        device_id=device_id,
        ts=snapshot.timestamp,
        technology=TECHNOLOGY,
        status=snapshot.status,
        error_kind=snapshot.error_kind,
        error_message=snapshot.error_message,
        errors_total=snapshot.uncorrectables_total if reading else None,
        errors_recent=snapshot.uncorrectables_recent if reading else None,
        summary=encode_summary(snapshot.downstream_power_avg, snapshot.downstream_snr_avg,
                               snapshot.upstream_power_avg),
        detail=encode_channels(snapshot.channels),
    )


def snapshot_from_sample(sample: LineSample) -> DocsisSignalSnapshot:
    """A stored DOCSIS reading as the snapshot the API has always answered with for stored rows: no error message,
    error counts 0 when there was no reading, a missing status read as "unconfigured"."""
    return DocsisSignalSnapshot(
        timestamp=sample.ts,
        **decode_summary(sample.summary),
        uncorrectables_total=sample.errors_total or 0,
        uncorrectables_recent=sample.errors_recent or 0,
        status=sample.status or "unconfigured",
        channels=decode_channels(sample.detail),
        error_kind=sample.error_kind,
    )


class DocsisHistory:
    """One device's stored DOCSIS readings as DocsisSignalSnapshot (the inspector and the dossier read these). Rows of
    another technology are skipped: they are not DOCSIS readings."""

    def __init__(self, repository, device_id: str):
        self.repository = repository
        self.device_id = device_id

    async def get_latest_docsis_snapshot(self) -> Optional[DocsisSignalSnapshot]:
        sample = await self.repository.get_latest_line_sample(self.device_id)
        return snapshot_from_sample(sample) if sample is not None and sample.technology == TECHNOLOGY else None

    async def get_docsis_snapshots_in_range(self, start_time: float, end_time: float,
                                            padding_seconds: float = 120.0) -> List[DocsisSignalSnapshot]:
        """Snapshots with start_time - padding_seconds <= ts <= end_time + padding_seconds, oldest first."""
        samples = await self.repository.get_line_samples(self.device_id, start_time, end_time, padding_seconds)
        return [snapshot_from_sample(s) for s in samples if s.technology == TECHNOLOGY]

    async def get_channel_history(self, start_time: float, end_time: float, channel_id: Optional[int] = None,
                                  direction: Optional[str] = None) -> List[list]:
        """channel_history_rows of the readings in the range, no padding."""
        samples = await self.repository.get_line_samples(self.device_id, start_time, end_time)
        return channel_history_rows(samples, channel_id, direction)


# --- per-channel history ----------------------------------------------------------------------------------------------

CHANNEL_HISTORY_FIELDS = ("ts", "direction", "channel_id", "power_dbmv", "snr_db", "uncorrectables_recent")


def channel_history_rows(samples: Iterable[LineSample], channel_id: Optional[int] = None,
                         direction: Optional[str] = None) -> List[list]:
    """Compact per-channel rows [ts, direction ("d"/"u"), channel_id, power_dbmv, snr_db, uncorrectables_recent] from
    stored readings, oldest first, optionally of one channel and/or one direction ("downstream"/"upstream"). Reads the
    positional `detail` directly (no DocsisChannel objects: a week is about 25,000 channel values). Readings of
    another technology or without channels give no rows; an unreadable detail is skipped and logged like
    decode_channels does. ts is whole seconds; a missing SNR is null."""
    fields = {name: i for i, name in enumerate(DETAIL_FIELDS[DETAIL_VERSION])}
    i_id, i_dir, i_pw, i_snr, i_rec = (fields[k] for k in ("channel_id", "direction", "power_dbmv", "snr_db",
                                                           "uncorrectables_recent"))
    want_dir = _DIRECTION_CODES.get(direction) if direction else None
    rows: List[list] = []
    for sample in samples:
        if sample.technology != TECHNOLOGY or not sample.detail:
            continue
        ts = int(sample.ts)
        try:
            data = json.loads(sample.detail)
            if data["v"] != DETAIL_VERSION:
                raise ValueError(f"unknown channel format {data['v']}")
            for ch in data["ch"]:
                if channel_id is not None and ch[i_id] != channel_id:
                    continue
                if want_dir is not None and ch[i_dir] != want_dir:
                    continue
                rows.append([ts, ch[i_dir], ch[i_id], ch[i_pw], ch[i_snr], ch[i_rec] or 0])
        except Exception as e:
            reason = f"{type(e).__name__}: {e}"[:200]
            if reason not in _warned:
                _warned.add(reason)
                logger.warning(f"Unreadable DOCSIS channel detail in line_samples, skipped in channel history ({reason})")
    return rows
