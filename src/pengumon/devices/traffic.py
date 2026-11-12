"""The router's traffic statistics as the API, the MCP tool, the incident inspector, the dossier and the AI pack read
them: hourly totals, clients ranked by download with their top apps, apps ranked, the hours an incident or a busy spell
covers, and a client's main app as a hint.

The router counts apps only over 24 hours. Over a day or less the apps are its latest 24-hour window; over a longer
range they are the sum of the whole local days in it: the windows that end exactly at a local midnight, one per day and
client, kept as the day's row (devices/monitors.py, `day_of_window`). A day without such a row is not counted, and the
answer says how many days were.

Everything is computed on read from `traffic_hourly` and `traffic_apps` (db/traffic.py). The rows name clients by MAC;
what a reader is shown of a client follows `Namer`: "full" (its name and MAC), "masked" (its maker and the last four hex
digits of its MAC, as /clients shows it without the token) or "generic" (what kind of device it is, never which one: the
dossier and the AI pack, whose texts may leave the household).
"""

import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from pengumon.capabilities import Capability, Client, ClientsReading
from pengumon.drivers.common import BRACES

HOUR = 3600
DEFAULT_HOURS = 24
MAX_HOURS = 24 * 400
TOP_APPS = 3                 # apps listed per client
HINT_SHARE = 0.8             # a client whose traffic is at least this share one recognized app gets it as a hint ...
HINT_MIN_BYTES = 50 * 10**6  # ... when it moved at least this much in the window ...
HINT_MAX_AGE_SECONDS = 48 * HOUR  # ... and the window ended this recently
# The router's names for protocols and catch-alls: they say how, not what, so they are never a client's hint.
GENERIC_APPS = (
    "General", "SSL/TLS", "HTTP Protocol over TLS SSL", "HTTP", "HTTPS", "DNS", "QUIC", "NTP", "UDP", "TCP", "ICMP",
    "Dynamic Host Configuration Protocol (DHCP)", "Network Service", "Others", "Unknown", "IPv6", "mDNS",
    "Multicast DNS", "SSDP", "NetBIOS", "Web Services Dynamic Discovery", "Simple Service Discovery Protocol",
    "Generic Routing Encapsulation", "Internet Message Access Protocol", "Secure Shell", "File Transfer Protocol",
)
KIND_WORDS = {"phone": "phone", "computer": "computer", "game": "game console", "network": "network device",
              "printer": "printer"}
NOT_YET = "not yet available"   # an hour the router has not published (or Pengumon not read) yet
NO_DATA = "no data"             # an older hour that was never read


def is_generic_app(name: str) -> bool:
    """Whether an app name is one of the router's protocol or catch-all labels (also cut off, as the router cuts long
    names at about 39 characters)."""
    low = name.strip().lower()
    return any(low == g.lower() or (len(low) >= 20 and g.lower().startswith(low)) for g in GENERIC_APPS)


def traffic_device(devices):
    """The device whose traffic the views read: the first enabled one offering TRAFFIC_USAGE (DeviceRegistry.primary,
    which falls back to a disabled one so its history still shows). None without one."""
    return devices.primary(Capability.TRAFFIC_USAGE) if devices is not None else None


def hour_floor(ts: float) -> int:
    return int(ts // HOUR) * HOUR


def gb(n: Optional[float]) -> str:
    """Bytes in words: "75 GB", "1.2 GB", "350 MB"."""
    n = float(n or 0)
    if n >= 10e9:
        return f"{n / 1e9:.0f} GB"
    if n >= 1e9:
        return f"{n / 1e9:.1f} GB"
    return f"{n / 1e6:.0f} MB"


class Namer:
    """What a reader is shown of a client (by MAC): `mode` "full", "masked" or "generic"; `clients` the router's
    latest client list (labels, makers, kinds); `key` turns a MAC into the opaque row key /clients uses (None: no
    key); `names` the names given to clients in Pengumon (clients/names.py), used in "full" mode only."""

    def __init__(self, mode: str, clients: Optional[ClientsReading] = None,
                 key: Optional[Callable[[str], str]] = None, names: Optional[Dict[str, str]] = None):
        self.mode = mode
        self.key = key
        self.names = names if mode == "full" and names else {}
        self.by_mac: Dict[str, Client] = {}
        for c in (clients.clients if clients is not None else ()):
            if c.mac:
                self.by_mac[c.mac] = c

    def describe(self, mac: str, hint: Optional[str] = None) -> str:
        """The client in a few words for a sentence."""
        if self.mode == "generic":
            c = self.by_mac.get(mac)
            word = KIND_WORDS.get(c.kind or "") if c is not None else None
            text = f"a {word}" if word else "one device"
            return f"{text} ({hint})" if hint else text
        row = self.row(mac)
        return f"{row['label']} ({mac})" if self.mode == "full" and row["label"] != mac else row["label"]

    def row(self, mac: str) -> Dict[str, Any]:
        """{key, label, kind, vendor} and, in "full" mode, {mac}: the label logic of /clients."""
        c = self.by_mac.get(mac)
        digits = "".join(ch for ch in mac if ch in "0123456789abcdefABCDEF")[-4:].upper()
        vendor = c.vendor if c is not None else None
        masked = f"{vendor or 'Device'} {digits}".strip()
        router_label = None
        if self.mode == "full":
            label = (c.label_parts[0] if c is not None and c.label_parts else None) or mac
            if self.names.get(mac):
                label, router_label = self.names[mac], label
        elif self.mode == "generic":
            word = KIND_WORDS.get(c.kind or "") if c is not None else None
            label = word or "device"
        else:
            label = masked
        out: Dict[str, Any] = {"key": self.key(mac) if self.key is not None else None, "label": label,
                               "kind": c.kind if c is not None else None,
                               "vendor": vendor}
        if self.mode == "full":
            out["mac"] = mac
            out["router_label"] = router_label  # the router's own name when Pengumon gives it another, else null
        return out


def _apps(rows: Sequence[Sequence], limit: Optional[int] = None) -> List[Dict[str, Any]]:
    ranked = sorted(((str(a), int(u), int(d)) for a, u, d in rows), key=lambda r: (-r[2], -r[1], r[0]))
    ranked = [r for r in ranked if r[1] or r[2]]
    return [{"app": a, "up": u, "down": d} for a, u, d in (ranked[:limit] if limit else ranked)]


def hint_of(apps: Sequence[Sequence], window_end: float, now: float) -> Optional[str]:
    """The app a client's traffic is at least HINT_SHARE of (by bytes, both directions), when it is a recognized app
    (not a protocol label), the client moved at least HINT_MIN_BYTES and the window is recent; else None."""
    if now - window_end > HINT_MAX_AGE_SECONDS:
        return None
    total = sum(int(u) + int(d) for _, u, d in apps)
    if total < HINT_MIN_BYTES:
        return None
    name, up, down = max(apps, key=lambda a: int(a[1]) + int(a[2]))
    if (int(up) + int(down)) / total < HINT_SHARE or is_generic_app(str(name)):
        return None
    return str(name).translate(BRACES)  # it goes into sentences, which must not carry a time token it made


async def traffic_hints(repository, device_id: str, now: Optional[float] = None) -> Dict[str, str]:
    """MAC -> its main app (hint_of) from the latest app windows."""
    now = time.time() if now is None else now
    out = {}
    for client, _, window_end, _, apps in await repository.get_traffic_apps(device_id):
        if client and apps:
            hint = hint_of(apps, window_end, now)
            if hint:
                out[client] = hint
    return out


@dataclass(frozen=True)
class Status:
    """Whether the device's traffic can be shown, and why not."""
    available: bool
    reason: Optional[str]
    read_at: Optional[float]
    interval_seconds: Optional[float]


def status_of(device) -> Status:
    """From the device's driver options and its monitor: switched off in the options, the reason the driver gave, or
    available."""
    driver, monitor = device.driver, device.monitor
    interval = getattr(driver, "traffic_interval_seconds", None)
    read_at = getattr(monitor, "traffic_read_at", None)
    if Capability.TRAFFIC_USAGE not in driver.offers:
        return Status(False, "switched off in the device's options (traffic_usage: false)", read_at, interval)
    if not device.enabled:
        return Status(False, "the device is disabled in configuration", read_at, interval)
    reason = getattr(monitor, "traffic_reason", None)
    return Status(reason is None, reason, read_at, interval)


def _range(hours: int, now: float) -> Tuple[int, int]:
    end = hour_floor(now)  # the hour in progress is never counted
    return end - hours * HOUR, end


# --- whole local days ---------------------------------------------------------------------------------------------------

MAX_DAYS = MAX_HOURS // 24


def midnight(day: date, tz: tzinfo) -> int:
    """The epoch second `day` starts at in `tz`."""
    return int(datetime(day.year, day.month, day.day, tzinfo=tz).timestamp())


def day_of_window(window_end: int, tz: tzinfo) -> Optional[str]:
    """The local day ("YYYY-MM-DD") a 24-hour app window is when it ends exactly at a midnight in `tz`; else None."""
    end = datetime.fromtimestamp(window_end, tz)
    if (end.hour, end.minute, end.second) != (0, 0, 0):
        return None
    day = end.date() - timedelta(days=1)
    return day.isoformat() if midnight(day + timedelta(days=1), tz) == int(window_end) else None


def whole_days(count: int, now: float, tz: tzinfo) -> List[date]:
    """The `count` whole local days that end at the last midnight at or before `now`, oldest first."""
    today = datetime.fromtimestamp(now, tz).date()
    return [today - timedelta(days=k) for k in range(count, 0, -1)]


async def day_apps(repository, device_id: str, days: Sequence[date],
                   tz: tzinfo) -> Tuple[Dict[str, List[Tuple[str, int, int]]], List[str]]:
    """The apps of each client ('' = every client together) summed over the day rows of `days` that are whole days in
    `tz` (a row cut on another zone, or one stored before rows had to be, is left out), and the days counted (those
    with the row of every client together), oldest first."""
    if not days:
        return {}, []
    ends = {d.isoformat(): midnight(d + timedelta(days=1), tz) for d in days}
    sums: Dict[str, Dict[str, List[int]]] = {}
    counted = set()
    for client, day, window_end, apps in await repository.get_traffic_app_day_range(
            device_id, days[0].isoformat(), days[-1].isoformat()):
        if ends.get(day) != int(window_end):
            continue
        if client == "":
            counted.add(day)
        acc = sums.setdefault(client, {})
        for name, up, down in apps:
            a = acc.setdefault(str(name), [0, 0])
            a[0] += int(up)
            a[1] += int(down)
    return ({c: [(n, u, d) for n, (u, d) in acc.items()] for c, acc in sums.items()},
            sorted(counted))


async def summary(repository, device_id: str, hours: int, namer: Namer, now: Optional[float] = None,
                  top: Optional[int] = None, *, days: Optional[int] = None,
                  tz: Optional[tzinfo] = None) -> Dict[str, Any]:
    """The last `hours` complete clock hours, or with `days` the last `days` whole local days in `tz` (up to the last
    midnight, with the WAN's totals per day in `daily` and `wan` empty): hourly WAN totals (null for an hour not
    stored), the totals,
    clients ranked by download with their top apps and hint, and the apps of every client together, ranked by
    download.

    The apps: over 24 hours or less, the router's latest 24-hour window (`apps_window_end`); over more, the sum of the
    whole days in the range (`hours // 24` of them up to the last midnight, or the `days`), described by `apps_days`
    (first and last day, how many were wanted and how many had a row)."""
    now = time.time() if now is None else now
    tz = tz or timezone.utc
    start, end, hours, wanted = _window(hours, days, now, tz)
    rows = await repository.get_traffic_hours(device_id, start, end, client="")
    by_hour = {h: (u, d) for h, _, u, d in rows}
    wan = [{"hour": h, "up": by_hour[h][0] if h in by_hour else None, "down": by_hour[h][1] if h in by_hour else None}
           for h in range(start, end, HOUR)]
    totals = {c: (u, d) for c, u, d, _ in await repository.get_traffic_totals(device_id, start, end)}
    if wanted:
        summed, counted = await day_apps(repository, device_id, wanted, tz)
        apps_end = midnight(date.fromisoformat(counted[-1]) + timedelta(days=1), tz) if counted else None
        windows = {c: (apps_end, apps) for c, apps in summed.items()}
        apps_days = _apps_days(wanted, counted)
    else:
        windows = {c: (w, apps) for c, _, w, _, apps in await repository.get_traffic_apps(device_id)}
        apps_days = None
    clients = []
    for mac, (up, down) in sorted(((c, t) for c, t in totals.items() if c), key=lambda kv: (-kv[1][1], -kv[1][0])):
        if not (up or down):
            continue
        window_end, apps = windows.get(mac, (None, []))
        hint = hint_of(apps, window_end, now) if window_end is not None else None
        clients.append({**namer.row(mac), "up": up, "down": down, "apps": _apps(apps, TOP_APPS),
                        "traffic_hint": hint})
    if top:
        clients = clients[:top]
    all_end, all_apps = windows.get("", (None, []))
    newest, read_at = await repository.get_traffic_latest(device_id)
    wan_up, wan_down = totals.get("", (0, 0))
    return {
        "hours": hours, "days": days or None, "start": start, "end": end,
        "window_end": newest + HOUR if newest is not None else None,
        "stored_at": read_at,
        "wan": [] if days else wan,  # with days the chart is per day: the hours are left out (85 % of a 30 day answer)
        "daily": _daily(wan, wanted, tz) if days else None,
        "totals": {"up": wan_up, "down": wan_down},
        "clients": clients,
        "apps": _apps(all_apps, top),
        "apps_window_end": all_end,
        "apps_days": apps_days,
    }


def _window(hours: int, days: Optional[int], now: float, tz: tzinfo) -> Tuple[int, int, int, List[date]]:
    """(start, end, hours, the whole days whose apps are summed) of a range: the last `hours` complete hours (the days:
    the last hours // 24 up to the last midnight, none under 48 hours), or with `days` the last `days` whole days."""
    if days:
        wanted = whole_days(days, now, tz)
        start = hour_floor(midnight(wanted[0], tz))
        end = hour_floor(midnight(wanted[-1] + timedelta(days=1), tz))
        return start, end, (end - start) // HOUR, wanted
    start, end = _range(hours, now)
    return start, end, hours, whole_days(hours // 24, now, tz) if hours >= 48 else []


def _apps_days(wanted: Sequence[date], counted: List[str]) -> Dict[str, Any]:
    return {"first": wanted[0].isoformat(), "last": wanted[-1].isoformat(), "wanted": len(wanted),
            "stored": len(counted), "days": counted}


def _daily(wan: List[Dict[str, Any]], days: Sequence[date], tz: tzinfo) -> List[Dict[str, Any]]:
    """The WAN's totals per local day: {day, start, end, up, down (null without a stored hour), hours, stored}."""
    out = []
    for d in days:
        s, e = hour_floor(midnight(d, tz)), hour_floor(midnight(d + timedelta(days=1), tz))
        mine = [r for r in wan if s <= r["hour"] < e and r["down"] is not None]
        out.append({"day": d.isoformat(), "start": s, "end": e,
                    "up": sum(r["up"] for r in mine) if mine else None,
                    "down": sum(r["down"] for r in mine) if mine else None,
                    "hours": (e - s) // HOUR, "stored": len(mine)})
    return out


async def client_detail(repository, device_id: str, mac: str, hours: int, namer: Namer,
                        now: Optional[float] = None, tz: Optional[tzinfo] = None, *,
                        days: Optional[int] = None) -> Dict[str, Any]:
    """One client over the last `hours` complete hours, or with `days` the last `days` whole local days in `tz` (with
    its totals per day in `daily`, and `range_days`): its hourly series (0 for an hour the router has, with no row for
    this client; null for an hour the router's WAN figure is missing too), its apps and hint over the range as
    `summary` counts them (`apps_window_end`, `apps_days`), and its apps by whole local day (`days`, day_of_window)."""
    now = time.time() if now is None else now
    tz = tz or timezone.utc
    start, end, hours, wanted = _window(hours, days, now, tz)
    rows = await repository.get_traffic_hours(device_id, start, end, client=mac)
    wan_hours = {h for h, _, _, _ in await repository.get_traffic_hours(device_id, start, end, client="")}
    mine = {h: (u, d) for h, _, u, d in rows}
    series = [{"hour": h, "up": mine.get(h, (0, 0))[0] if h in wan_hours or h in mine else None,
               "down": mine.get(h, (0, 0))[1] if h in wan_hours or h in mine else None}
              for h in range(start, end, HOUR)]
    if wanted:
        summed, counted = await day_apps(repository, device_id, wanted, tz)
        apps = summed.get(mac, [])
        window_end = midnight(date.fromisoformat(counted[-1]) + timedelta(days=1), tz) if counted and apps else None
        apps_days = _apps_days(wanted, counted)
    else:
        latest = await repository.get_traffic_apps(device_id, client=mac)
        window_end, apps = (latest[0][2], latest[0][4]) if latest else (None, [])
        apps_days = None
    by_day = [(day, w, a) for day, w, a in await repository.get_traffic_app_days(device_id, mac, start)
              if day_of_window(w, tz) == day]
    return {
        "client": {**namer.row(mac), "traffic_hint": hint_of(apps, window_end, now) if window_end else None},
        "hours": hours, "range_days": days or None, "start": start, "end": end,
        "series": [] if days else series,  # (with days: per day only, as summary)
        "daily": _daily(series, wanted, tz) if days else None,
        "totals": {"up": sum(u for u, _ in mine.values()), "down": sum(d for _, d in mine.values())},
        "apps": _apps(apps),
        "apps_window_end": window_end,
        "apps_days": apps_days,
        "days": [{"day": day, "window_end": w, "apps": _apps(a, 10)} for day, w, a in by_day],
    }


async def stored_clients(repository, device_id: str, hours: int = MAX_HOURS,
                         now: Optional[float] = None) -> List[str]:
    """The MACs with traffic stored in the last `hours` (to resolve an opaque key back to a MAC)."""
    now = time.time() if now is None else now
    start, end = _range(hours, now)
    macs = {c for c, *_ in await repository.get_traffic_totals(device_id, start, end + HOUR) if c}
    macs |= {c for c, *_ in await repository.get_traffic_apps(device_id) if c}
    return sorted(macs)


# --- explaining a span: an incident, a busy spell ---------------------------------------------------------------------

async def hours_explained(repository, device_id: str, start: float, end: float, namer: Namer, *, top: int = 3,
                          max_hours: int = 6, tz: Optional[tzinfo] = None) -> List[Dict[str, Any]]:
    """The clock hours [start, end] touches (at most `max_hours`, the busiest when there are more): for each, the
    WAN's download and upload and the top clients by download, from the stored hours. An hour without a stored WAN row
    is "not yet available" when it is newer than the newest hour stored (the router publishes an hour after it ends),
    else "no data". Each hour has a `text`: "game console (02:00:00:00:00:03) downloaded 75 GB in 08:00-09:00"."""
    first, last = hour_floor(start), hour_floor(end)
    hours = list(range(first, last + HOUR, HOUR))
    newest, _ = await repository.get_traffic_latest(device_id)
    rows = await repository.get_traffic_hours(device_id, first, last + HOUR)
    by_hour: Dict[int, Dict[str, Tuple[int, int]]] = {}
    for h, client, up, down in rows:
        by_hour.setdefault(h, {})[client] = (up, down)
    hints = await traffic_hints(repository, device_id, now=end) if namer.mode == "generic" else {}
    out = []
    for h in hours:
        clients = by_hour.get(h, {})
        span = f"{_clock(h, tz)}-{_clock(h + HOUR, tz)}"
        if "" not in clients:
            if newest is None:  # nothing stored yet: an hour that ended in the last two hours may still come
                state = NOT_YET if h + 2 * HOUR > time.time() else NO_DATA
            else:
                state = NOT_YET if h > newest else NO_DATA
            out.append({"hour": h, "available": False, "state": state, "up": None, "down": None, "clients": [],
                        "text": f"{span}: traffic {state}"})
            continue
        up, down = clients[""]
        ranked = sorted(((m, t) for m, t in clients.items() if m and t[1] > 0), key=lambda kv: -kv[1][1])[:top]
        listed = [{**namer.row(m), "who": namer.describe(m, hints.get(m)), "up": u, "down": d,
                   "share": round(d / down, 2) if down else None} for m, (u, d) in ranked]
        if ranked:
            m, (_, d) = ranked[0]
            text = f"{namer.describe(m, hints.get(m))} downloaded {gb(d)} in {span} (all clients: {gb(down)})"
        else:
            text = f"all clients downloaded {gb(down)} in {span}"
        out.append({"hour": h, "available": True, "state": "ok", "up": up, "down": down, "clients": listed,
                    "text": text})
    if newest is None and all(o["state"] == NO_DATA for o in out):
        return []  # no traffic was ever stored (not read yet, or the router gives none): nothing to say
    if len(out) > max_hours:
        keep = {o["hour"] for o in sorted(out, key=lambda o: -(o["down"] or 0))[:max_hours]}
        out = [o for o in out if o["hour"] in keep]
    return out


def _clock(ts: float, tz: Optional[tzinfo]) -> str:
    return datetime.fromtimestamp(ts, tz).strftime("%H:%M")


def traffic_source(devices, mode: str, key: Optional[Callable[[str], str]] = None,
                   names: Optional[Dict[str, str]] = None) -> Optional[Tuple[str, Namer]]:
    """(device id, Namer) for the device whose traffic explains incidents and busy spells (traffic_device), naming
    clients in `mode` (with the names given in Pengumon, in "full"); None without one."""
    device = traffic_device(devices)
    if device is None:
        return None
    return device.id, Namer(mode, getattr(device.monitor, "latest_clients", None), key=key, names=names)

