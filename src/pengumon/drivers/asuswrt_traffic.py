"""The AsusWRT router's own statistics pages: its Traffic Analyzer (bytes per client, per clock hour and per app) and
two status pages (the Ethernet ports' link speeds, the CPU temperature). Used by drivers/asuswrt.py.

The pages are JavaScript served as `application/json`, starting with a byte-order mark:

    var array_statistics = new Array();
    array_statistics = [["02:00:00:00:00:01", 348169292, 1228356885], ...];

* getWanTraffic.asp?client=all|<MAC>&mode=hour&dura=24&date=<epoch>: 24 [up, down] slots, one per clock hour; slot 23 is
  the last complete hour before the hour that contains `date`. The hour in progress is never counted.
* getWanTraffic.asp?...&mode=detail: [app, up, down] over the 24 hours that end at the start of that hour.
* getAppTraffic.asp?client=all&mode=detail&dura=24&date=<epoch>: [MAC, up, down] per client over the same 24 hours.
* `array_statistics = ;` is "no data" (an unsupported span, or the Traffic Analyzer switched off), never an error.

Every page is read only up to PAGE_MAX_BYTES (a longer one is no data, said once) and parsed in time linear in its size
(no pattern that can backtrack over the body; JSON values are decoded where they start, nesting errors caught), and the
rows used are capped (MAX_HOUR_SLOTS, MAX_ROWS), so a round always ends whatever the router answers.

The router refreshes these figures once an hour, a minute or so after the hour: reading more often returns the same
numbers. `TrafficRounds` reads them in rounds (one request first; the rest only when the router has a new hour) and
hands out a few requests per poll, so a poll is never held up by more than a few short requests.
"""

import json
import re
import time
from collections import deque
from typing import Awaitable, Callable, Deque, Dict, List, NamedTuple, Optional, Tuple

import httpx

from pengumon.capabilities import TrafficApps, TrafficHour, TrafficReading, valid_uptime
from pengumon.drivers.common import clean_text, normal_mac

HOUR = 3600
# A figure above this for one hour (10 TB: 22 Gb/s for the whole hour) is not traffic but a bad number; a 24-hour
# figure may be 24 times that. Negative and non-numeric figures are bad numbers too: the row is skipped.
MAX_HOUR_BYTES = 10 * 10**12
MAX_DAY_BYTES = 24 * MAX_HOUR_BYTES
APP_NAME_MAX = 64
# What is read of one page at most: the pages are a few KB (a client per row, an app per row); the rest is never read.
PAGE_MAX_BYTES = 1024 * 1024
MAX_HOUR_SLOTS = 48   # hourly slots used of a mode=hour answer (the newest; the router gives 24)
MAX_ROWS = 1024       # clients of a getAppTraffic answer, apps of a mode=detail answer (the first, in the router's order)

# The pages read (paths relative to the router's base URL). The driver's request allowlist includes them.
WAN_TRAFFIC_PAGE = "getWanTraffic.asp"
APP_TRAFFIC_PAGE = "getAppTraffic.asp"
PORTS_PAGE = "ajax_ethernet_ports.asp"
CORE_TEMP_PAGE = "ajax_coretmp.asp"

# --- reading schedule -----------------------------------------------------------------------------------------------
DEFAULT_INTERVAL_SECONDS = 3600.0
MIN_INTERVAL_SECONDS = 60.0
# With an interval of an hour or more, rounds start this long after the hour: the router publishes the hour that
# just ended a minute or so after it.
AFTER_THE_HOUR_SECONDS = 300.0
RETRY_SECONDS = 300.0           # a round stopped by an error starts again this soon (or after the interval, if sooner)
UNSUPPORTED_RECHECK_SECONDS = 86400.0  # a page the router does not have (404) is asked for again this much later
REQUESTS_PER_POLL = 3           # traffic and status-page requests one poll makes at most ...
POLL_BUDGET_SECONDS = 0.6       # ... and none more once this much time went on them (each takes 40-75 ms)
SPEED_TEST_QUIET_SECONDS = 30.0  # no statistics read this long after a speed test last read the WAN counters
PAGE_TIMEOUT_SECONDS = 2.0      # a page that takes longer is given up (they take 40-75 ms): a poll never waits long

_PORT_MBPS = {"X": 0, "M": 100, "G": 1000, "Q": 2500, "F": 5000, "T": 10000}
# The patterns run on bodies of up to PAGE_MAX_BYTES (the uptime ones on the appGet answer): each is tried from a fixed
# word or a line start and never scans past the line or the run of spaces it is in, and the JSON is decoded by
# `_DECODER` where it starts (never a `.*` that backtracks over the rest of the body).
_RE_ASSIGN = re.compile(r"^[ \t]*(?:var[ \t]+)?array_statistics[ \t]*=([^\n]*)$", re.M)
_RE_PORTS = re.compile(r"get_wan_lan_status\s*=\s*")
_RE_CPU_TEMP = re.compile(r"curr_cpuTemp\s*=\s*[\"']?\s*(-?[0-9]{1,6}(?:\.[0-9]{1,6})?)")
_RE_UPTIME = re.compile(r"\(\s*(\d{1,10})\s*secs?\s+since\s+boot\s*\)", re.I)
_RE_UPTIME_HOOK = re.compile(r"[\"']uptime[\"']\s*:\s*[\"']([^\"']*)[\"']")
_DECODER = json.JSONDecoder()
# What decoding a hostile JSON value can raise besides ValueError: a nesting deeper than the interpreter's recursion limit
_JSON_ERRORS = (ValueError, RecursionError)


def _text(body: str) -> str:
    return body.lstrip("﻿").replace("\r\n", "\n")


def parse_statistics(body: str) -> Optional[list]:
    """The value assigned to `array_statistics` (a list), [] for `array_statistics = ;` or an empty list, None when the
    body has no such assignment or it cannot be read (an HTML page, a login page, a body cut short)."""
    found: Optional[str] = None
    for m in _RE_ASSIGN.finditer(_text(body)):
        value = m.group(1).strip()
        if value.endswith(";"):
            value = value[:-1].rstrip()
        if value.startswith("new Array"):
            continue
        found = value
    if found is None:
        return None
    if found == "":
        return []
    try:
        value = json.loads(found)
    except _JSON_ERRORS:
        return None
    return value if isinstance(value, list) else None


def _bytes(value: object, limit: int) -> Optional[int]:
    """A byte count from the page: a non-negative whole number up to `limit` (numbers and numeric strings); None for
    anything else."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        value = value.strip()
        if not (value.isascii() and value.isdigit()) or len(value) > 20:  # (isdigit alone takes "²", which int() refuses)
            return None
        value = int(value)
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if not isinstance(value, int) or value < 0 or value > limit:
        return None
    return value


def hour_series(rows: list, date: float, client: str) -> List[TrafficHour]:
    """The hourly slots of a mode=hour answer as TrafficHour rows: the last slot is the last complete hour before the
    hour that contains `date`, each earlier one an hour before. Bad slots are left out (the others keep their hour)."""
    newest = int(date // HOUR) * HOUR - HOUR
    rows = rows[-MAX_HOUR_SLOTS:]
    out = []
    for i, row in enumerate(rows):
        if not isinstance(row, list) or len(row) < 2:
            continue
        up, down = _bytes(row[0], MAX_HOUR_BYTES), _bytes(row[1], MAX_HOUR_BYTES)
        if up is None or down is None:
            continue
        out.append(TrafficHour(client, newest - (len(rows) - 1 - i) * HOUR, up, down))
    return out


def app_rows(rows: list) -> Tuple[Tuple[str, int, int], ...]:
    """(app, up, down) of a mode=detail answer, in the router's order; rows with a bad name or figure are left out."""
    out = []
    for row in rows[:MAX_ROWS]:
        if not isinstance(row, list) or len(row) < 3:
            continue
        name = clean_text(row[0], APP_NAME_MAX)
        up, down = _bytes(row[1], MAX_DAY_BYTES), _bytes(row[2], MAX_DAY_BYTES)
        if name and up is not None and down is not None:
            out.append((name, up, down))
    return tuple(out)


def client_totals(rows: list) -> Dict[str, Tuple[str, int, int]]:
    """MAC (normal form) -> (the MAC as the router wrote it, up, down) of a getAppTraffic answer; bad rows left out."""
    out: Dict[str, Tuple[str, int, int]] = {}
    for row in rows[:MAX_ROWS]:
        if not isinstance(row, list) or len(row) < 3 or not isinstance(row[0], str):
            continue
        mac = normal_mac(row[0])
        up, down = _bytes(row[1], MAX_DAY_BYTES), _bytes(row[2], MAX_DAY_BYTES)
        if mac and up is not None and down is not None:
            out[mac] = (row[0].strip(), up, down)
    return out


def wan_link_mbps(body: str) -> Optional[int]:
    """The WAN port's link speed in Mb/s from ajax_ethernet_ports.asp (0: no link); None when it is not there."""
    text = _text(body)
    m = _RE_PORTS.search(text)
    if not m:
        return None
    try:
        data, _ = _DECODER.raw_decode(text, m.end())
    except _JSON_ERRORS:
        return None
    ports = data.get("portSpeed") if isinstance(data, dict) else None
    if not isinstance(ports, dict):
        return None
    wan = [k for k in ports if isinstance(k, str) and k.upper().startswith("WAN")]
    if not wan:
        return None
    letter = ports.get("WAN 0", ports[sorted(wan)[0]])
    return _PORT_MBPS.get(str(letter).strip().upper()) if isinstance(letter, str) else None


def cpu_temp_c(body: str) -> Optional[float]:
    """`curr_cpuTemp` from ajax_coretmp.asp in °C; None when unset or not a plausible temperature."""
    m = _RE_CPU_TEMP.search(_text(body))
    if not m:
        return None
    value = float(m.group(1))
    return round(value, 1) if 0.0 < value < 150.0 else None


def uptime_seconds(text: str) -> Optional[int]:
    """Seconds since boot from an appGet body carrying the uptime() hook ("<date>(6122380 secs since boot)"); None
    without one, or for a figure that is not an uptime (above MAX_UPTIME_SECONDS)."""
    hook = _RE_UPTIME_HOOK.search(text)
    m = _RE_UPTIME.search(hook.group(1)) if hook else None
    return valid_uptime(int(m.group(1))) if m else None


# --- the rounds -----------------------------------------------------------------------------------------------------

class Page(NamedTuple):
    """A page as read: its status, its body as text (read up to PAGE_MAX_BYTES) and whether it was longer."""

    status_code: int
    text: str
    truncated: bool = False


Get = Callable[..., Awaitable[Page]]  # (path, params) -> the page; transport errors propagate


class _Stop(Exception):
    """Ends the round: `retry` (an error: start again soon), `missing` (say why there is no traffic)."""

    def __init__(self, retry: bool, missing: Optional[str] = None, unsupported: bool = False):
        super().__init__(missing or "")
        self.retry, self.missing, self.unsupported = retry, missing, unsupported


NOT_OFFERED = "this router does not offer the Traffic Analyzer pages"
NO_DATA = ("the router's Traffic Analyzer has no data: it may be switched off (Traffic Analyzer > Statistic on the "
           "router's page)")
TOO_BIG = (f"the router's Traffic Analyzer answered with a page larger than {PAGE_MAX_BYTES // 1024 // 1024} MiB: "
           "not read")


class TrafficRounds:
    """When and what to read of the Traffic Analyzer. A round:

    1. the hourly series of every client together (one request). If its newest hour is the one the last finished round
       saw, with the same figures, the round ends there: the router has nothing new.
    2. the 24-hour total of each client (one request) and the app list of every client together (one request);
    3. for each client whose total changed since the last finished round: its hourly series and its app list. A client
       whose total became 0 needs neither (its app window is empty; no hour of it has traffic). A client whose total
       did not change gets the app list last read for it again, as this round's window: an unchanged 24-hour total
       means the hour that left the window moved what the hour that came in moved (almost always nothing), so the
       window holds the same apps. Every round that reads the totals thus gives every client's window ending at this
       round's hour, and the round just after midnight a whole day for each (devices/monitors.py keeps those as days).

    A round starts when due (`interval` after the last one began; with an interval of an hour or more, at
    AFTER_THE_HOUR_SECONDS past the hour), the first one at once (it backfills the last 24 hours). `step` makes at most
    a few requests per poll. A transport error or a refused session (401/403) stops the round; the poll's own request
    logs in again, and the round starts over RETRY_SECONDS later. Nothing a round read counts as seen until the round
    finishes, so a round stopped half way is redone in full."""

    def __init__(self, get: Get, interval: float, *, wall_clock: Callable[[], float] = time.time, log=None):
        self._get = get
        self.interval = max(MIN_INTERVAL_SECONDS, float(interval))
        self._wall_clock = wall_clock
        self._log = log
        self._queue: Deque[tuple] = deque()
        self._date: Optional[int] = None          # the `date` of the round under way
        self._round_started = 0.0
        self._round_newest: Optional[tuple] = None
        self._newest: Optional[tuple] = None      # (hour, up, down) of the newest WAN hour the last finished round saw
        self._totals: Dict[str, Tuple[int, int]] = {}  # MAC -> 24-hour (up, down) as last fetched in full
        self._apps: Dict[str, Tuple[Tuple[str, int, int], ...]] = {}  # MAC -> its app list read with those totals
        self.next_due = 0.0                        # wall clock
        self.unsupported_until = 0.0
        self.requests = 0                          # every request made (tests count them)
        self.last_read_at: Optional[float] = None  # when a round last read something new

    @property
    def busy(self) -> bool:
        return bool(self._queue)

    def _due_after(self, started: float) -> float:
        if self.interval >= HOUR:
            slot = (started - AFTER_THE_HOUR_SECONDS) // self.interval + 1
            return slot * self.interval + AFTER_THE_HOUR_SECONDS
        return started + self.interval

    def _start(self, now: float) -> None:
        self._date = int(now)
        self._round_started = now
        self._round_newest = None
        self._queue = deque([("wan_hour",)])

    def _finish(self, now: float) -> None:
        self._newest = self._round_newest if self._round_newest is not None else self._newest
        self._queue.clear()
        self.next_due = self._due_after(self._round_started)

    def _abort(self, stop: _Stop, now: float) -> None:
        self._queue.clear()
        if stop.unsupported:
            self.unsupported_until = now + UNSUPPORTED_RECHECK_SECONDS
            self.next_due = self.unsupported_until
        elif stop.retry:
            self.next_due = now + min(self.interval, RETRY_SECONDS)
        else:
            self.next_due = self._due_after(self._round_started)

    async def step(self, allowed: int) -> Tuple[Optional[TrafficReading], Optional[str], int]:
        """Makes up to `allowed` requests of the round under way (starting one when due). Returns what they read (None:
        nothing new), why the traffic is unavailable when this step found out (None otherwise), and how many requests
        it made. Never raises for the router's answers or a transport error."""
        now = self._wall_clock()
        if not self._queue:
            if allowed <= 0 or now < self.next_due:
                return None, None, 0
            self._start(now)
        hours: List[TrafficHour] = []
        apps: List[TrafficApps] = []
        made = 0
        missing: Optional[str] = None
        started = time.perf_counter()
        while self._queue and made < allowed and (made == 0 or time.perf_counter() - started < POLL_BUDGET_SECONDS):
            job = self._queue.popleft()
            made += 1
            try:
                await self._run(job, hours, apps)
            except _Stop as stop:
                missing = stop.missing
                self._abort(stop, now)
                break
            except httpx.HTTPError as ex:
                if self._log is not None:
                    self._log.debug(f"Traffic statistics not read: {type(ex).__name__}; trying again later")
                self._abort(_Stop(retry=True), now)
                break
            except Exception as ex:  # a page this code did not foresee must cost the round, never the poll
                if self._log is not None:
                    self._log.warning(f"Traffic statistics not read: {type(ex).__name__} reading the router's answer; "
                                      "trying again later")
                self._abort(_Stop(retry=True), now)
                break
            if not self._queue:
                self._finish(now)
        if hours or apps:
            self.last_read_at = now
        reading = TrafficReading(read_at=now, hours=tuple(hours), apps=tuple(apps)) if hours or apps else None
        return reading, missing, made

    async def _fetch(self, page: str, client: str, mode: str) -> list:
        self.requests += 1
        res = await self._get(page, params={"client": client, "mode": mode, "dura": "24", "date": str(self._date)})
        if res.status_code == 404:
            raise _Stop(retry=False, missing=NOT_OFFERED, unsupported=True)
        if res.status_code != 200:
            if self._log is not None:
                self._log.debug(f"Traffic statistics not read: {page} answered HTTP {res.status_code}")
            raise _Stop(retry=True)
        if res.truncated:  # not the page's size: nothing of it is used; asked again at the next round
            raise _Stop(retry=False, missing=TOO_BIG)
        rows = parse_statistics(res.text)
        if rows is None:  # not the page's shape: a login page, a body cut short
            if self._log is not None:
                self._log.debug(f"Traffic statistics not read: {page} answered without array_statistics")
            raise _Stop(retry=True)
        return rows

    async def _run(self, job: tuple, hours: List[TrafficHour], apps: List[TrafficApps]) -> None:
        kind = job[0]
        window_end = int(self._date // HOUR) * HOUR
        if kind == "wan_hour":
            series = hour_series(await self._fetch(WAN_TRAFFIC_PAGE, "all", "hour"), self._date, "")
            if not series:
                raise _Stop(retry=False, missing=NO_DATA)
            newest = series[-1]
            self._round_newest = (newest.hour, newest.up, newest.down)
            if self._round_newest == self._newest:
                return  # the router has nothing new since the last round: that is the whole round
            hours.extend(series)
            self._queue.extend([("totals",), ("apps", "all", "")])
        elif kind == "totals":
            totals = client_totals(await self._fetch(APP_TRAFFIC_PAGE, "all", "detail"))
            for mac in [m for m in self._totals if m not in totals]:  # gone from the list: no traffic in 24 hours
                totals[mac] = (mac, 0, 0)
            for mac, (raw, up, down) in sorted(totals.items(), key=lambda kv: -kv[1][2]):
                if self._totals.get(mac) == (up, down):
                    if mac in self._apps:  # the same window as last read: given again, as this round's
                        apps.append(TrafficApps(mac, window_end, self._apps[mac]))
                    continue
                if up == 0 and down == 0:
                    apps.append(TrafficApps(mac, window_end, ()))
                    self._totals.pop(mac, None)
                    self._apps.pop(mac, None)
                    continue
                self._queue.extend([("client_hour", raw, mac), ("apps", raw, mac, (up, down))])
        elif kind == "client_hour":
            _, raw, mac = job
            hours.extend(hour_series(await self._fetch(WAN_TRAFFIC_PAGE, raw, "hour"), self._date, mac))
        elif kind == "apps":
            _, raw, client = job[:3]
            rows = app_rows(await self._fetch(WAN_TRAFFIC_PAGE, raw, "detail"))
            apps.append(TrafficApps(client, window_end, rows))
            if len(job) > 3:
                self._totals[client] = job[3]
                self._apps[client] = tuple(rows)
