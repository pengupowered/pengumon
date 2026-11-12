"""Xfinity / Comcast XB gateways (XB6, XB7, XB8): DOCSIS channel tables from the gateway's web UI.

Read-only: one status page (network_setup.jst), with a form login when the session has expired. The page is
parsed into DOCSIS channels; judging them (averages, thresholds, codeword error deltas) is DOCSIS logic shared by
every DOCSIS modem and lives in pengumon.lines.docsis.
"""

import re
import time
from urllib.parse import urljoin, urlsplit
from typing import TYPE_CHECKING, Callable, ClassVar, Dict, List, NamedTuple, Optional, Tuple

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

from pengumon.capabilities import Capability, DriverError, LineReading, PollResult
from pengumon.capabilities.contrib import DriverNotes
from pengumon.drivers.common import clean_text
from pengumon.drivers.http import DeviceHttp, Verify, device_verify, new_client
from pengumon.lines.docsis_model import DocsisChannel
from pengumon.observability import get_logger

if TYPE_CHECKING:
    from pengumon.devices.settings import DeviceConfig
    from pengumon.secrets import Credentials

_log = get_logger("pengumon.drivers.xfinity_xb")

STATUS_PAGE = "network_setup.jst"
PAGE_MAX = 512 * 1024  # the status page is read up to this (a real one is tens of KB); a longer one fails the poll
# Any of these on the status page means the session is not logged in (the gateway answers 200 with a login page).
# The gateway forgets a login sooner than the poll interval (measured: not after 15 minutes). When the last good
# read is older than this, the driver logs in first instead of spending a request on a page it knows will be a login
# page. A guess on the safe side: too short only costs a login that was not needed.
SESSION_LIFETIME_SECONDS = 300.0
_LOGGED_OUT_MARKERS = ("Please Login First", "home_loggedout", "Gateway > Login", "check.jst")
MAX_REDIRECTS = 5  # redirects followed for one request (the gateway sends one, http to https, or a login page)
_REDIRECTS = (301, 302, 303, 307, 308)


def _log_dropped(dropped: int) -> None:
    if dropped:
        _log.debug("rows of the gateway's channel table could not be read; they are left out", dropped=dropped)


class XfinityXbOptions(BaseModel):
    """The driver takes no options (a devices: entry's `options:` must be empty)."""
    model_config = ConfigDict(extra="forbid")


class XfinityXbDriver:
    name = "xfinity_xb"
    label = "Xfinity gateway"  # what the device is, in the dashboard's words (devices.summary.device_label)
    offers = frozenset({Capability.ACCESS_LINE})
    line_technology = "docsis"
    options_model = XfinityXbOptions
    # The gateway's web UI is slow and some units fail or reboot when it is read often; the DOCSIS counters move
    # slowly. Each poll costs two or three requests, one of them a login.
    default_poll_seconds = 1800.0
    min_poll_seconds = 600.0
    # The AI pack's note on this driver (capabilities/contrib.py). Each statement is established by this file:
    #  - "Xfinity gateway (XB6, XB7 or XB8)": the module docstring, :1 and `label` :48;
    #  - read never more often than every {min_poll} (`min_poll_seconds` :55); how often it was read is the DOCSIS
    #    section's, from the stored readings, so the pack states the interval once;
    #  - slow web page, some units fail or reboot when read often: the comment at :52-53.
    # No claim about who supplies the gateway or whether it routes (nothing here says). The anonymized text leaves
    # out the vendor and model words, which would name the provider.
    pack_notes: ClassVar[DriverNotes] = DriverNotes(
        full=("Device notes: the modem is an Xfinity gateway (XB6, XB7 or XB8). Pengumon reads its status page never "
              "more often than every {min_poll}, because the gateway's web page is slow and some units fail or reboot "
              "when it is read often."),
        anonymized="Device notes: the modem is a cable gateway, read never more often than every {min_poll}.",
    )

    def __init__(self, url: str, username: str, password: Optional[str], *,
                 client: Optional[httpx.AsyncClient] = None, verify: Verify = True,
                 monotonic: Callable[[], float] = time.monotonic):
        self._monotonic = monotonic
        self._last_read: Optional[float] = None  # when the status page was last read successfully
        self._username = username
        self._password = password
        self._verify = verify
        self.http = DeviceHttp(url, self._new_client, driver=self.name, client=client)

    @classmethod
    def from_config(cls, cfg: "DeviceConfig", credentials: "Credentials", *,
                    client: Optional[httpx.AsyncClient] = None) -> "XfinityXbDriver":
        cls.options_model.model_validate(cfg.options)
        return cls(cfg.url, credentials.username, credentials.password, client=client, verify=device_verify(cfg))

    async def _only_this_gateway(self, request: httpx.Request) -> None:
        """Runs before every request, the ones a redirect leads to included: the gateway may send the client from http
        to https (or to another path), but never to another host, and never from https down to http. The login form
        (user name and password) is re-sent by a 307/308, so it must not follow a redirect anywhere else."""
        base = urlsplit(self.http.base_url)
        if (request.url.host or "").lower() != (self.host or "").lower() or (
                base.scheme == "https" and request.url.scheme != "https"):
            raise DriverError("protocol", f"the gateway ({self.host}) redirected to another host or down to plain HTTP, "
                                          "which is not followed")

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        """One request and the redirects it leads to, up to MAX_REDIRECTS, every hop through the bounded reader
        (`DeviceHttp.request`: its body is read decoded up to the limit and no further) and past `_only_this_gateway`.
        A 307/308 re-sends the method and body (the login form); the other redirects turn into a GET without one."""
        url = path
        for _ in range(MAX_REDIRECTS + 1):
            res = await self.http.request(method, url, **kwargs)
            location = res.headers.get("location")
            if res.status_code not in _REDIRECTS or not location:
                return res
            url = urljoin(str(res.request.url), location)
            if res.status_code not in (307, 308):
                method = "GET"
                kwargs = {k: v for k, v in kwargs.items() if k not in ("data", "content", "json", "files")}
        raise DriverError("protocol", f"the gateway ({self.host}) redirected more than {MAX_REDIRECTS} times")

    def _new_client(self) -> httpx.AsyncClient:
        # (no redirects followed by httpx: `_request` does it by hand, httpx would read a redirect's body without a limit)
        return new_client(self._verify, timeout=12.0, event_hooks={"request": [self._only_this_gateway]},
                          headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})

    @property
    def host(self) -> str:
        return self.http.host

    def missing_configuration(self) -> Optional[str]:
        return None if self._password else "no password"

    def set_credentials(self, username: str, password: str) -> None:
        self._username, self._password = username, password
        self._last_read = None
        self.http.clear_session()  # the next poll logs in with the new credentials instead of reusing a session

    async def aclose(self) -> None:
        await self.http.aclose()

    async def _login(self) -> None:
        """Posts the login form; the session cookie stays in the client. Raises DriverError when refused."""
        res = await self._request("POST", "check.jst", data={"username": self._username, "password": self._password})
        if "Incorrect password" in res.text or "Access denied" in res.text:
            raise DriverError("auth", f"Authentication failed on gateway ({self.host}). Please verify password.")

    async def poll(self) -> PollResult:
        age = None if self._last_read is None else self._monotonic() - self._last_read
        _log.debug("polling the gateway", session_age_s=None if age is None else round(age),
                   session_lifetime_s=SESSION_LIFETIME_SECONDS)
        res = None
        if age is not None and age < SESSION_LIFETIME_SECONDS:
            # A recent read: the session is probably still good, so try the page first.
            res = await self._request("GET", STATUS_PAGE, limit=PAGE_MAX)
        # The session is checked by asking for the page: a login page (or anything without the channel table)
        # means logging in and asking again. With no recent read the login comes first (two requests, not three).
        if (res is None or res.status_code != 200 or "Downstream" not in res.text
                or any(m in res.text for m in _LOGGED_OUT_MARKERS)):
            await self._login()
            res = await self._request("GET", STATUS_PAGE, limit=PAGE_MAX)
        if res.status_code != 200:
            raise DriverError("protocol", f"the gateway's status page answered HTTP {res.status_code}")
        channels = self._parse_channels(res.text)
        if not channels:
            if any(m in res.text for m in _LOGGED_OUT_MARKERS[:3]):
                # Still the login page after logging in: the gateway refused the login without saying so in a way
                # _login recognizes (a firmware with other wording).
                raise DriverError("auth", f"The gateway ({self.host}) still shows its login page after logging in. "
                                          "Please verify password.")
            raise DriverError("protocol", "the gateway's status page had no DOCSIS channel table")
        self._last_read = self._monotonic()
        return PollResult(readings={Capability.ACCESS_LINE: LineReading(technology="docsis",
                                                                        channels=tuple(channels))})

    @staticmethod
    def _clean_float(val: str) -> Optional[float]:
        m = re.search(r'[-+]?\d*\.?\d+', val)
        return float(m.group(0)) if m else None

    @staticmethod
    def _clean_int(val: str) -> Optional[int]:
        m = re.search(r'\d+', val)
        return int(m.group(0)) if m else None

    @classmethod
    def _parse_channels(cls, html: str) -> List[DocsisChannel]:
        """Extracts Downstream and Upstream channels from gateway HTML. A row that does not make a valid channel (a
        firmware that words a value differently) is left out, and one debug line says how many were. Linear in the
        size of the page whatever it holds (the page is read only up to PAGE_MAX): the rows are split in one pass
        over the tags, and every pattern runs on one cell."""
        rows = _rows(html)
        channels, dropped = cls._parse_horizontal(rows)
        if channels:
            _log_dropped(dropped)
            return channels
        channels, more = cls._parse_vertical(rows)
        _log_dropped(dropped + more)
        return channels

    @classmethod
    def _parse_horizontal(cls, rows: "List[List[_Cell]]") -> Tuple[List[DocsisChannel], int]:
        """Comcast XB6/XB7/XB8: one row per value, a row-label cell first and a column per channel."""
        ds_rows, us_rows, cw_rows = cls._horizontal_tables(rows)
        channels, dropped = cls._horizontal_channels(ds_rows, us_rows)
        return channels, dropped + cls._horizontal_codewords(channels, cw_rows)

    @staticmethod
    def _horizontal_tables(rows: "List[List[_Cell]]"
                           ) -> Tuple[Dict[str, List[str]], Dict[str, List[str]], Dict[str, List[str]]]:
        """The downstream, upstream and codeword tables (row label -> the row's plain cells) of the rows: a row-label
        cell named Downstream, Upstream or "CM error codewords" starts a section, and the rows after it belong to it."""
        current_section = None  # "downstream", "upstream", "codewords"
        ds_rows: Dict[str, List[str]] = {}
        us_rows: Dict[str, List[str]] = {}
        cw_rows: Dict[str, List[str]] = {}

        for row in rows:
            label_cell = next((c for c in row if _ROW_LABEL.search(c.attrs)), None)
            if label_cell is None:
                continue
            label = label_cell.text.lower()
            td_vals = [c.text for c in row if c.tag == "td" and not c.attrs]

            if label == "downstream":
                current_section = "downstream"
                continue
            elif label == "upstream":
                current_section = "upstream"
                continue
            elif "cm error codewords" in label:
                current_section = "codewords"
                continue

            if not td_vals:
                continue

            if current_section == "downstream":
                ds_rows[label] = td_vals
            elif current_section == "upstream":
                us_rows[label] = td_vals
            elif current_section == "codewords":
                cw_rows[label] = td_vals
        return ds_rows, us_rows, cw_rows

    @classmethod
    def _horizontal_channels(cls, ds_rows: Dict[str, List[str]], us_rows: Dict[str, List[str]]
                             ) -> Tuple[List[DocsisChannel], int]:
        """The channels of the downstream and upstream tables (no codeword counts yet) and how many columns were left
        out because they do not make a valid channel."""
        channels: List[DocsisChannel] = []
        dropped = 0
        for direction, table in (("downstream", ds_rows), ("upstream", us_rows)):
            ids = table.get("channel id", [])
            pwrs = table.get("power level", [])
            snrs = table.get("snr", []) if direction == "downstream" else []
            freqs = table.get("frequency", [])
            mods = table.get("modulation", [])
            for i in range(min(len(ids), len(pwrs), MAX_CHANNELS)):
                try:
                    cid = cls._clean_int(ids[i])
                    if cid is None:
                        continue
                    freq_raw = cls._clean_float(freqs[i]) if i < len(freqs) else None
                    freq = (freq_raw / 1e6) if freq_raw and freq_raw > 10000 else (freq_raw or 0.0)
                    pwr = cls._clean_float(pwrs[i])
                    if pwr is None:
                        continue
                    snr = cls._clean_float(snrs[i]) if i < len(snrs) else None
                    default = "QAM256" if direction == "downstream" else "ATDMA"
                    channels.append(DocsisChannel(
                        channel_id=cid,
                        direction=direction,
                        frequency_mhz=round(freq, 2),
                        power_dbmv=round(pwr, 2),
                        snr_db=round(snr, 2) if snr is not None else None,
                        modulation=_modulation(mods[i]) if i < len(mods) else default,
                        correcteds=0,
                        uncorrectables=0
                    ))
                except (ValueError, ValidationError):
                    dropped += 1
                    continue
        return channels, dropped

    @classmethod
    def _horizontal_codewords(cls, channels: List[DocsisChannel], cw_rows: Dict[str, List[str]]) -> int:
        """Sets the codeword counts of the downstream channels from the codeword table, by channel id; returns how many
        columns were left out because they could not be read."""
        dropped = 0
        # Codewords, by downstream channel id (the first channel of an id, as before)
        downstream: Dict[int, DocsisChannel] = {}
        for c in channels:
            if c.direction == "downstream":
                downstream.setdefault(c.channel_id, c)
        cw_ids = cw_rows.get("channel id", [])
        corr_vals = cw_rows.get("correctable codewords", [])
        uncorr_vals = cw_rows.get("uncorrectable codewords", [])
        unerr_vals = cw_rows.get("unerrored codewords", [])  # optional: not every page has the row

        for i, cid_str in enumerate(cw_ids):
            try:
                cid = cls._clean_int(cid_str)
                if cid is None:
                    continue
                c_ch = downstream.get(cid)
                if c_ch:
                    if i < len(corr_vals):
                        c_ch.correcteds = cls._clean_int(corr_vals[i]) or 0
                    if i < len(uncorr_vals):
                        c_ch.uncorrectables = cls._clean_int(uncorr_vals[i]) or 0
                    if i < len(unerr_vals):
                        c_ch.unerrored = cls._clean_int(unerr_vals[i])
            except (ValueError, ValidationError):
                dropped += 1
                continue
        return dropped

    @classmethod
    def _parse_vertical(cls, rows: "List[List[_Cell]]") -> Tuple[List[DocsisChannel], int]:
        """The fallback: one row per channel. A row is a channel when its plain cells hold, in this order with any
        cells between, what _DS_ROW (downstream) or _US_ROW (upstream) asks for."""
        downstream: List[DocsisChannel] = []
        upstream: List[DocsisChannel] = []
        dropped = 0
        for row in rows:
            cells = [c.text for c in row if c.tag == "td" and not c.attrs]
            ds = _in_order(cells, _DS_ROW) if len(downstream) < MAX_CHANNELS else None
            us = _in_order(cells, _US_ROW) if ds is None and len(upstream) < MAX_CHANNELS else None
            try:
                if ds is not None:
                    ch_id, _, modulation, freq, pwr, snr, corr, uncorr = ds
                    freq_val = float(_number(freq))
                    downstream.append(DocsisChannel(
                        channel_id=int(ch_id),
                        direction="downstream",
                        frequency_mhz=round(freq_val / 1e6, 2) if freq_val > 10000 else freq_val,
                        power_dbmv=float(_number(pwr)),
                        snr_db=float(_number(snr)),
                        modulation=_modulation(modulation),
                        correcteds=int(corr),
                        uncorrectables=int(uncorr)
                    ))
                elif us is not None:
                    ch_id, _, mod_type, freq, _, pwr = us
                    freq_val = float(_number(freq))
                    upstream.append(DocsisChannel(
                        channel_id=int(ch_id),
                        direction="upstream",
                        frequency_mhz=round(freq_val / 1e6, 2) if freq_val > 10000 else freq_val,
                        power_dbmv=float(_number(pwr)),
                        snr_db=None,
                        modulation=_modulation(mod_type),
                        correcteds=0,
                        uncorrectables=0
                    ))
            except (ValueError, ValidationError):
                dropped += 1
                continue
        return downstream + upstream, dropped


MAX_CHANNELS = 128  # per direction: more is no DOCSIS modem (3.1 has at most 32 + 2 OFDM downstream)
MODULATION_MAX = 16
# One pass over the page's table tags. `[^<>]*` stops at the next tag's start: a "<tr" without its ">" costs the
# distance to the next "<" once, not the rest of the page for every one of them.
_TABLE_TAG = re.compile(r"<(/?)(tr|td|th)\b([^<>]*)>", re.I)
_TAG = re.compile(r"<[^<>]*>")
_ROW_LABEL = re.compile(r"""class=['"][^'"]*row-label""", re.I)
_NUMBER = re.compile(r"-?[\d.]+")
# The fallback layout's rows: each pattern must match a whole cell, in this order, with any cells between.
_DS_ROW = tuple(re.compile(p, re.I) for p in (
    r"\d+", r"Locked|active", r"[^<]+", r"[\d.]+\s*(?:Hz|MHz)?", r"-?[\d.]+\s*dBmV", r"[\d.]+\s*dB", r"\d+", r"\d+"))
_US_ROW = tuple(re.compile(p, re.I) for p in (
    r"\d+", r"Locked|active", r"[^<]+", r"[\d.]+\s*(?:Hz|MHz)?", r"[^<]+", r"[\d.]+\s*dBmV"))


class _Cell(NamedTuple):
    tag: str  # "td" or "th"
    attrs: str  # what the opening tag holds after its name ("" for a plain <td>)
    text: str  # the content without its tags, trimmed


def _rows(html: str) -> List[List[_Cell]]:
    """The table rows of a page, each the list of its cells. A cell ends at its closing tag or where the next cell or
    row starts; a row at its closing tag or where the next one starts."""
    rows: List[List[_Cell]] = []
    row: Optional[List[_Cell]] = None
    cell: Optional[Tuple[str, str, int]] = None  # (tag, attrs, where its content starts)
    for m in _TABLE_TAG.finditer(html):
        if cell is not None and row is not None:
            tag, attrs, start = cell
            row.append(_Cell(tag, attrs.strip(), _TAG.sub("", html[start:m.start()]).strip()))
        cell = None
        closing, name = m.group(1) == "/", m.group(2).lower()
        if name == "tr":
            if row is not None:
                rows.append(row)
            row = None if closing else []
        elif not closing and row is not None:
            cell = (name, m.group(3), m.end())
    if row is not None:  # (a cell left open at the end of the page is not one)
        rows.append(row)
    return rows


def _in_order(cells: List[str], patterns: Tuple["re.Pattern[str]", ...]) -> Optional[List[str]]:
    """The first cell matching each pattern in turn, each after the one before (None when one has none): what the
    old page-wide pattern with `.*?` between the cells found, in one pass over the row."""
    found: List[str] = []
    i = 0
    for pattern in patterns:
        while i < len(cells) and not pattern.fullmatch(cells[i]):
            i += 1
        if i == len(cells):
            return None
        found.append(cells[i])
        i += 1
    return found


def _number(text: str) -> str:
    m = _NUMBER.match(text)
    return m.group(0) if m else text


def _modulation(text: str) -> Optional[str]:
    return clean_text(text, MODULATION_MAX)
