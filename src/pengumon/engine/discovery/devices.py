"""Credential-free device checks: what a hop's web page and (when asked) its SNMP agent say it is.

Web page: one GET of `/` over HTTP (HTTPS when nothing listens on port 80), short timeouts, no credentials, no
cookies kept, the body read up to PAGE_MAX bytes. A redirect is followed only to the same host (a device's login
page, or its HTTPS port) and at most MAX_REDIRECTS times; a redirect to another host is never followed and only
noted. TLS certificates are not checked: home devices have self-signed ones, and nothing is sent that a certificate
would protect. Only the page title, the Server header and the fingerprint below are kept.

The fingerprints are what this project's drivers know of each device's web interface (their code, docs and tests),
nothing beyond that:
  xfinity_xb  the gateway's login form posts to check.jst; its logged-out pages carry "home_loggedout" or the
              "Gateway > Login" heading (drivers/xfinity_xb.py)
  asuswrt     the firmware's login page, Main_Login.asp (the front page sends the browser there with a script, or
              a redirect), or the login form's `login_authorization` field, which the asuswrt driver posts to
              login.cgi. Main_Login.asp was seen on a real AsusWRT router's front page; it is the stock and Merlin
              firmware's login page name, not anything of one router.
  openwrt     LuCI, OpenWrt's web interface (served under /cgi-bin/luci by uhttpd; docs/devices.md)
A page that matches none is still evidence: a private hop that serves a web page is a device in the home.

SNMP (only when the owner ticks it): one GET of sysDescr with the community "public", through the snmp driver's own
client (drivers/snmp_ber.py).
"""

import html
import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urljoin, urlsplit

import httpx

from pengumon.drivers.common import clean_text
from pengumon.drivers.http import read_capped
from pengumon.engine.discovery.addresses import parse

PAGE_MAX = 64 * 1024
MAX_REDIRECTS = 2
HTTP_TIMEOUT_S = 2.0
SNMP_TIMEOUT_S = 1.0
CABLE_MODEM_ADDRESS = "192.168.100.1"  # where DOCSIS cable modems answer with their status page
USER_AGENT = "pengumon-discovery"

SCAN_MAX = 16 * 1024  # how much of a page is searched for the title
TITLE_MAX = 600  # how far past <title> its closing tag is looked for


@dataclass(frozen=True)
class Fingerprint:
    driver: str
    words: str  # what was recognized, in plain words


FINGERPRINTS = (
    (Fingerprint("xfinity_xb", "an Xfinity XB gateway's login page"),
     lambda body, title, location: any(m in body for m in ("check.jst", "home_loggedout", "Gateway > Login"))),
    (Fingerprint("asuswrt", "an Asus router's login page"),
     lambda body, title, location: "login_authorization" in body or "Main_Login.asp" in body
     or "Main_Login.asp" in location),
    (Fingerprint("openwrt", "OpenWrt's web interface (LuCI)"),
     lambda body, title, location: "cgi-bin/luci" in body or "cgi-bin/luci" in location
     or "luci" in (title or "").lower()),
)


@dataclass
class PageInfo:
    address: str
    scheme: str
    status: int
    title: Optional[str] = None
    server: Optional[str] = None
    fingerprint: Optional[Fingerprint] = None
    off_host_redirect: bool = False  # it redirected to another host (not followed)

    def describe(self) -> str:
        """The page in plain words, for the evidence."""
        parts = []
        if self.fingerprint:
            parts.append(f"looks like {self.fingerprint.words}")
        if self.title:
            parts.append(f"its page is titled “{self.title}”")
        if self.server:
            parts.append(f"server {self.server}")
        if not parts:
            parts.append(f"it answers HTTP {self.status}")
        if self.off_host_redirect:
            parts.append("it redirects to another host (not followed)")
        return "answers on its web port: " + "; ".join(parts)


def new_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(follow_redirects=False, verify=False, trust_env=False,
                             timeout=httpx.Timeout(HTTP_TIMEOUT_S), headers={"User-Agent": USER_AGENT})


def _url(scheme: str, address: str, path: str = "/") -> str:
    ip = parse(address)
    host = f"[{address}]" if ip is not None and ip.version == 6 else address
    return f"{scheme}://{host}{path}"


def _same_host(url: str, address: str) -> bool:
    host = urlsplit(url).hostname
    ip_a, ip_b = parse(host), parse(address)
    if ip_a is not None and ip_b is not None:
        return ip_a == ip_b
    return (host or "").lower() == address.lower()


def fingerprint(body: str, title: Optional[str], location: str = "") -> Optional[Fingerprint]:
    for fp, matches in FINGERPRINTS:
        if matches(body, title, location):
            return fp
    return None


def _text(headers: httpx.Headers, body: bytes) -> str:
    match = re.search(r"charset=([A-Za-z0-9_.:-]+)", headers.get("content-type", ""))
    try:
        return body.decode(match.group(1) if match else "utf-8", "replace")
    except (LookupError, UnicodeError, ValueError, TypeError):  # an odd charset (idna, undefined, punycode...)
        return body.decode("utf-8", "replace")


def _find_title(body: str) -> Optional[str]:
    """The text of the first <title>, found without backtracking (a regex with a lazy `.*?` was quadratic on a page
    of many `<title>` starts and no end). Only the first SCAN_MAX characters are searched."""
    text = body[:SCAN_MAX]
    low = text.lower()
    if len(low) != len(text):  # a few letters change length when lowered: keep the offsets true
        low = "".join(c.lower() if c.isascii() else c for c in text)
    start = low.find("<title")
    if start < 0:
        return None
    open_end = low.find(">", start)
    if open_end < 0:
        return None
    close = low.find("</title", open_end + 1, open_end + 1 + TITLE_MAX)
    if close < 0:
        return None
    return text[open_end + 1:close]


async def _get(client: httpx.AsyncClient, url: str):
    """(status, headers, body text up to PAGE_MAX) of one GET."""
    status, headers, body, _ = await read_capped(client, "GET", url)
    return status, headers, _text(headers, body)


async def fetch_page(address: str, client: httpx.AsyncClient, *, budget: int = 1 + MAX_REDIRECTS + 1,
                     fallback_https: bool = True) -> Optional[PageInfo]:
    """What the device at `address` serves at `/`, or None when nothing answers. `budget` caps the requests sent
    to it (the HTTPS fallback and redirects included)."""
    sent = 0
    for scheme in (("http", "https") if fallback_https else ("http",)):
        if sent >= budget:
            return None
        url = _url(scheme, address)
        locations = []
        try:
            sent += 1
            status, headers, body = await _get(client, url)
            redirects = 0
            off_host = False
            while 300 <= status < 400 and headers.get("location"):
                target = urljoin(url, headers["location"])
                locations.append(target)
                if not _same_host(target, address) or urlsplit(target).scheme not in ("http", "https"):
                    off_host = True
                    break
                if redirects >= MAX_REDIRECTS or sent >= budget:
                    break
                redirects += 1
                sent += 1
                url = target
                status, headers, body = await _get(client, url)
        except httpx.HTTPError:
            continue  # nothing on this port, a reset, a timeout, a bad Location: try the next scheme
        raw_title = _find_title(body)
        title = clean_text(html.unescape(raw_title), limit=80) if raw_title is not None else None
        return PageInfo(address=address, scheme=urlsplit(url).scheme, status=status, title=title,
                        server=clean_text(headers.get("server"), limit=60),
                        fingerprint=fingerprint(body, title, " ".join(locations)), off_host_redirect=off_host)
    return None


async def snmp_sysdescr(address: str, community: str = "public", timeout: float = SNMP_TIMEOUT_S) -> Optional[str]:
    """The device's sysDescr (first line, short) from one SNMPv2c GET, or None when no agent answers."""
    from pengumon.drivers.snmp import SYS_DESCR, model_of
    from pengumon.drivers.snmp_ber import SnmpClient
    from pengumon.capabilities import DriverError

    client = SnmpClient(address, 161, community, timeout=timeout, retries=0)
    try:
        values = await client.get([SYS_DESCR])
        return model_of(values[0][1].text()) if values else None
    except (DriverError, OSError):
        return None
    finally:
        await client.aclose()
