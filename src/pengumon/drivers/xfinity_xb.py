"""Xfinity / Comcast XB gateways (XB6, XB7, XB8): DOCSIS channel tables from the gateway's web UI.

Read-only: one status page (network_setup.jst), with a form login when the session has expired. The page is
parsed into DOCSIS channels; judging them (averages, thresholds, codeword error deltas) is DOCSIS logic shared by
every DOCSIS modem and lives in pengumon.lines.docsis.
"""

import re
import time
from typing import TYPE_CHECKING, Callable, Dict, List, Optional

import httpx
from pydantic import BaseModel, ConfigDict

from pengumon.capabilities import Capability, DriverError, LineReading, PollResult
from pengumon.drivers.http import DeviceHttp, Verify, device_verify
from pengumon.lines.docsis_model import DocsisChannel
from pengumon.observability import get_logger

if TYPE_CHECKING:
    from pengumon.devices.settings import DeviceConfig
    from pengumon.secrets import Credentials

_log = get_logger("pengumon.drivers.xfinity_xb")

STATUS_PAGE = "network_setup.jst"
# Any of these on the status page means the session is not logged in (the gateway answers 200 with a login page).
# The gateway forgets a login sooner than the poll interval (measured: not after 15 minutes). When the last good
# read is older than this, the driver logs in first instead of spending a request on a page it knows will be a login
# page. A guess on the safe side: too short only costs a login that was not needed.
SESSION_LIFETIME_SECONDS = 300.0
_LOGGED_OUT_MARKERS = ("Please Login First", "home_loggedout", "Gateway > Login", "check.jst")


class XfinityXbOptions(BaseModel):
    """The driver takes no options (a devices: entry's `options:` must be empty)."""
    model_config = ConfigDict(extra="forbid")


class XfinityXbDriver:
    name = "xfinity_xb"
    label = "Xfinity gateway"  # what the device is, in the dashboard's words (api.devices.device_label)
    offers = frozenset({Capability.ACCESS_LINE})
    line_technology = "docsis"
    options_model = XfinityXbOptions
    # The gateway's web UI is slow and some units fail or reboot when it is read often; the DOCSIS counters move
    # slowly. Each poll costs two or three requests, one of them a login.
    default_poll_seconds = 1800.0
    min_poll_seconds = 600.0

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

    def _new_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=12.0,
            verify=self._verify,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
        )

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
        res = await self.http.post("check.jst", data={"username": self._username, "password": self._password})
        if "Incorrect password" in res.text or "Access denied" in res.text:
            raise DriverError("auth", f"Authentication failed on gateway ({self.host}). Please verify password.")

    async def poll(self) -> PollResult:
        age = None if self._last_read is None else self._monotonic() - self._last_read
        _log.debug("polling the gateway", host=self.host,
                   session_age_s=None if age is None else round(age), session_lifetime_s=SESSION_LIFETIME_SECONDS)
        res = None
        if age is not None and age < SESSION_LIFETIME_SECONDS:
            # A recent read: the session is probably still good, so try the page first.
            res = await self.http.get(STATUS_PAGE)
        # The session is checked by asking for the page: a login page (or anything without the channel table)
        # means logging in and asking again. With no recent read the login comes first (two requests, not three).
        if (res is None or res.status_code != 200 or "Downstream" not in res.text
                or any(m in res.text for m in _LOGGED_OUT_MARKERS)):
            await self._login()
            res = await self.http.get(STATUS_PAGE)
        if res.status_code != 200:
            raise DriverError("protocol", f"the gateway's status page answered HTTP {res.status_code}")
        channels = self._parse_channels(res.text)
        if not channels:
            if any(m in res.text for m in _LOGGED_OUT_MARKERS[:3]):
                # Still the login page after logging in: the gateway refused the login without saying so in a way
                # _login recognises (a firmware with other wording).
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
        """Extracts Downstream and Upstream channels from gateway HTML."""
        channels: List[DocsisChannel] = []

        # 1. Primary parser: Comcast XB6/XB7/XB8 horizontal row-label format
        try:
            tr_blocks = re.findall(r'<tr[^>]*>(.*?)</tr>', html, re.I | re.DOTALL)
            current_section = None  # "downstream", "upstream", "codewords"
            ds_rows: Dict[str, List[str]] = {}
            us_rows: Dict[str, List[str]] = {}
            cw_rows: Dict[str, List[str]] = {}

            for tr in tr_blocks:
                m = re.search(r'<(?:th|td)[^>]*class=[\'"][^\'"]*row-label[^\'"]*[\'"][^>]*>(.*?)</(?:th|td)>', tr, re.I | re.DOTALL)
                if not m:
                    continue
                label = re.sub(r'<[^>]+>', '', m.group(1)).strip().lower()
                td_vals = [re.sub(r'<[^>]+>', '', td).strip() for td in re.findall(r'<td>(.*?)</td>', tr, re.I | re.DOTALL)]

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

            # Process Downstream
            ds_ids = ds_rows.get("channel id", [])
            ds_pwrs = ds_rows.get("power level", [])
            ds_snrs = ds_rows.get("snr", [])
            ds_freqs = ds_rows.get("frequency", [])
            ds_mods = ds_rows.get("modulation", [])

            for i in range(min(len(ds_ids), len(ds_pwrs))):
                try:
                    cid = cls._clean_int(ds_ids[i])
                    if cid is None:
                        continue
                    freq_raw = cls._clean_float(ds_freqs[i]) if i < len(ds_freqs) else None
                    freq = (freq_raw / 1e6) if freq_raw and freq_raw > 10000 else (freq_raw or 0.0)
                    pwr = cls._clean_float(ds_pwrs[i])
                    if pwr is None:
                        continue
                    snr = cls._clean_float(ds_snrs[i]) if i < len(ds_snrs) else None
                    mod = ds_mods[i] if i < len(ds_mods) else "QAM256"

                    channels.append(DocsisChannel(
                        channel_id=cid,
                        direction="downstream",
                        frequency_mhz=round(freq, 2),
                        power_dbmv=round(pwr, 2),
                        snr_db=round(snr, 2) if snr is not None else None,
                        modulation=mod,
                        correcteds=0,
                        uncorrectables=0
                    ))
                except Exception:
                    continue

            # Process Upstream
            us_ids = us_rows.get("channel id", [])
            us_pwrs = us_rows.get("power level", [])
            us_freqs = us_rows.get("frequency", [])
            us_mods = us_rows.get("modulation", [])

            for i in range(min(len(us_ids), len(us_pwrs))):
                try:
                    cid = cls._clean_int(us_ids[i])
                    if cid is None:
                        continue
                    freq_raw = cls._clean_float(us_freqs[i]) if i < len(us_freqs) else None
                    freq = (freq_raw / 1e6) if freq_raw and freq_raw > 10000 else (freq_raw or 0.0)
                    pwr = cls._clean_float(us_pwrs[i])
                    if pwr is None:
                        continue
                    mod = us_mods[i] if i < len(us_mods) else "ATDMA"

                    channels.append(DocsisChannel(
                        channel_id=cid,
                        direction="upstream",
                        frequency_mhz=round(freq, 2),
                        power_dbmv=round(pwr, 2),
                        snr_db=None,
                        modulation=mod,
                        correcteds=0,
                        uncorrectables=0
                    ))
                except Exception:
                    continue

            # Process Codewords
            cw_ids = cw_rows.get("channel id", [])
            corr_vals = cw_rows.get("correctable codewords", [])
            uncorr_vals = cw_rows.get("uncorrectable codewords", [])
            unerr_vals = cw_rows.get("unerrored codewords", [])  # optional: not every page has the row

            for i, cid_str in enumerate(cw_ids):
                try:
                    cid = cls._clean_int(cid_str)
                    if cid is None:
                        continue
                    c_ch = next((c for c in channels if c.direction == "downstream" and c.channel_id == cid), None)
                    if c_ch:
                        if i < len(corr_vals):
                            c_ch.correcteds = cls._clean_int(corr_vals[i]) or 0
                        if i < len(uncorr_vals):
                            c_ch.uncorrectables = cls._clean_int(uncorr_vals[i]) or 0
                        if i < len(unerr_vals):
                            c_ch.unerrored = cls._clean_int(unerr_vals[i])
                except Exception:
                    continue

            if channels:
                return channels
        except Exception as ex:
            _log.debug("horizontal channel table could not be parsed; trying the vertical layout",
                       error=f"{type(ex).__name__}: {ex}")

        # 2. Fallback parser: standard vertical rows
        ds_matches = re.findall(
            r"<tr[^>]*>.*?<td>(\d+)</td>.*?<td>(?:Locked|active)</td>.*?<td>([^<]+)</td>.*?<td>([\d.]+)\s*(?:Hz|MHz)?</td>.*?<td>(-?[\d.]+)\s*dBmV</td>.*?<td>([\d.]+)\s*dB</td>.*?<td>(\d+)</td>.*?<td>(\d+)</td>.*?</tr>",
            html,
            re.IGNORECASE | re.DOTALL
        )

        for m in ds_matches:
            try:
                ch_id = int(m[0])
                modulation = m[1].strip()
                freq_val = float(m[2])
                freq_mhz = round(freq_val / 1e6, 2) if freq_val > 10000 else freq_val
                pwr = float(m[3])
                snr = float(m[4])
                corr = int(m[5])
                uncorr = int(m[6])

                channels.append(DocsisChannel(
                    channel_id=ch_id,
                    direction="downstream",
                    frequency_mhz=freq_mhz,
                    power_dbmv=pwr,
                    snr_db=snr,
                    modulation=modulation,
                    correcteds=corr,
                    uncorrectables=uncorr
                ))
            except Exception:
                continue

        us_matches = re.findall(
            r"<tr[^>]*>.*?<td>(\d+)</td>.*?<td>(?:Locked|active)</td>.*?<td>([^<]+)</td>.*?<td>([\d.]+)\s*(?:Hz|MHz)?</td>.*?<td>(?:[^<]+)</td>.*?<td>([\d.]+)\s*dBmV</td>.*?</tr>",
            html,
            re.IGNORECASE | re.DOTALL
        )

        for m in us_matches:
            try:
                ch_id = int(m[0])
                mod_type = m[1].strip()
                freq_val = float(m[2])
                freq_mhz = round(freq_val / 1e6, 2) if freq_val > 10000 else freq_val
                pwr = float(m[3])

                channels.append(DocsisChannel(
                    channel_id=ch_id,
                    direction="upstream",
                    frequency_mhz=freq_mhz,
                    power_dbmv=pwr,
                    snr_db=None,
                    modulation=mod_type,
                    correcteds=0,
                    uncorrectables=0
                ))
            except Exception:
                continue

        return channels
