"""The router's traffic statistics as the API, the MCP tool, the incident inspector, the dossier and the AI pack read
them: hourly totals, clients ranked by download with their top apps, apps ranked, the hours an incident or a busy spell
covers, and a client's main app as a hint.

The router counts apps only over 24 hours. A range's apps are the stored 24-hour windows that lie inside the range
(`cover_apps`), added up; what they do not account for is the range's total minus them, shown as "not broken down by
app", so a list always adds up to its total. Ranges are rolling windows ending at the newest hour read (windows.py).

Everything is computed on read from `traffic_hourly` and `traffic_apps` (db/traffic.py). The rows name clients by MAC;
what a reader is shown of a client follows `Namer`: "full" (its name and MAC), "masked" (its maker and the last four hex
digits of its MAC, as /clients shows it without the token) or "generic" (what kind of device it is, never which one: the
dossier and the AI pack, whose texts may leave the household).
"""

import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from pengumon.capabilities import Capability, Client, ClientsReading
from pengumon.drivers.common import BRACES
from pengumon.windows import day_cells

HOUR = 3600
DEFAULT_HOURS = 24
MAX_HOURS = 24 * 400
MAX_DAYS = MAX_HOURS // 24
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


# --- the apps of a rolling window -------------------------------------------------------------------------------------

APP_WINDOW = 24 * HOUR  # the router counts apps over 24 hours only


def cover_apps(windows: Iterable[Tuple[int, Sequence[Sequence]]], start: float,
               end: float) -> List[Tuple[int, Sequence[Sequence]]]:
    """The stored 24-hour app windows `(window_end, apps)` that tile [start, end) best: only a window whose whole 24
    hours lie inside, chosen newest first, each ending at or before the start of the one chosen before it (so none
    overlap). Windows ending at every hour tile it exactly: H, H - 24 h, H - 48 h, ... Newest first."""
    chosen: List[Tuple[int, Sequence[Sequence]]] = []
    limit = end
    for window_end, apps in sorted(windows, key=lambda w: -w[0]):
        if window_end <= limit and window_end - APP_WINDOW >= start:
            chosen.append((window_end, apps))
            limit = window_end - APP_WINDOW
    return chosen


def sum_apps(chosen: Sequence[Tuple[int, Sequence[Sequence]]]) -> List[Tuple[str, int, int]]:
    """The apps of the chosen windows added up: (app, up, down)."""
    acc: Dict[str, List[int]] = {}
    for _, apps in chosen:
        for name, up, down in apps:
            a = acc.setdefault(str(name), [0, 0])
            a[0] += int(up)
            a[1] += int(down)
    return [(n, u, d) for n, (u, d) in acc.items()]


def unattributed(total: Tuple[int, int], apps: Sequence[Sequence]) -> Dict[str, int]:
    """What of a client's (or the network's) total (up, down) over the window no app list accounts for: the total minus
    the apps' sum, each direction on its own and never below 0."""
    return {"up": max(0, int(total[0]) - sum(int(a[1]) for a in apps)),
            "down": max(0, int(total[1]) - sum(int(a[2]) for a in apps))}


def _by_client(rows: Iterable[Tuple[str, int, Sequence[Sequence]]]) -> Dict[str, List[Tuple[int, Sequence[Sequence]]]]:
    out: Dict[str, List[Tuple[int, Sequence[Sequence]]]] = {}
    for client, window_end, apps in rows:
        out.setdefault(client, []).append((window_end, apps))
    return out


def _covered(chosen: Sequence[Tuple[int, Sequence[Sequence]]]) -> Dict[str, Any]:
    return {"hours": len(chosen) * 24, "windows": [w for w, _ in chosen]}


async def _window(repository, device_id: str, hours: int, days: Optional[int], now: float) -> Tuple[int, int, int]:
    """(start, end, hours) of a range: the last `hours` hours (`days` * 24 with `days`) that end at the newest hour
    the router published when that is within an hour of the current hour, else at the current hour (the hour in
    progress is never counted)."""
    hours = days * 24 if days else hours
    floor = hour_floor(now)
    newest, _ = await repository.get_traffic_latest(device_id)
    end = newest + HOUR if newest is not None and abs(floor - (newest + HOUR)) <= HOUR else floor
    return end - hours * HOUR, end, hours


def _daily(rows: Sequence[Dict[str, Any]], start: float, end: float, tz: tzinfo) -> List[Dict[str, Any]]:
    """The totals per local calendar day inside the window ([start, end), windows.day_cells): {day, start, end, up, down
    (null without a stored hour), hours, partial, stored}. An hour belongs to the day it begins in."""
    out = []
    for cell in day_cells(start, end, tz):
        mine = [r for r in rows if cell["start"] <= r["hour"] < cell["end"] and r["down"] is not None]
        out.append({**cell, "up": sum(r["up"] for r in mine) if mine else None,
                    "down": sum(r["down"] for r in mine) if mine else None, "stored": len(mine)})
    return out


async def summary(repository, device_id: str, hours: int, namer: Namer, now: Optional[float] = None,
                  top: Optional[int] = None, *, days: Optional[int] = None, tz: Optional[tzinfo] = None,
                  with_hours: Optional[bool] = None) -> Dict[str, Any]:
    """The last `hours` hours (`days` * 24 with `days`), a rolling window that ends at the newest hour the router
    published (see `_window`): hourly WAN totals (null for an hour not stored; left out of a range over 7 days unless
    `with_hours`), the totals, `daily` (per local calendar day inside the window, the first and last `partial`, for
    ranges of two days or more), clients ranked by download with their top apps and hint, and the apps of every
    client together, ranked by download.

    The apps cover the same window and add up: the stored 24-hour windows that lie inside it (`cover_apps`, listed in
    `apps_covered`), summed; `apps_unattributed` is the part of the total they do not account for ("not broken down
    by app"), so apps plus it equal `totals`."""
    now = time.time() if now is None else now
    tz = tz or timezone.utc
    start, end, hours = await _window(repository, device_id, hours, days, now)
    rows = await repository.get_traffic_hours(device_id, start, end, client="")
    by_hour = {h: (u, d) for h, _, u, d in rows}
    wan = [{"hour": h, "up": by_hour[h][0] if h in by_hour else None, "down": by_hour[h][1] if h in by_hour else None}
           for h in range(start, end, HOUR)]
    totals = {c: (u, d) for c, u, d, _ in await repository.get_traffic_totals(device_id, start, end)}
    windows = _by_client(await repository.get_traffic_app_windows(device_id, start + APP_WINDOW, end))
    covers = {c: cover_apps(w, start, end) for c, w in windows.items()}
    clients = []
    for mac, (up, down) in sorted(((c, t) for c, t in totals.items() if c), key=lambda kv: (-kv[1][1], -kv[1][0])):
        if not (up or down):
            continue
        chosen = covers.get(mac, [])
        apps = sum_apps(chosen)
        hint = hint_of(apps, chosen[0][0], now) if chosen else None
        clients.append({**namer.row(mac), "up": up, "down": down, "apps": _apps(apps, TOP_APPS),
                        "apps_unattributed": unattributed((up, down), apps), "traffic_hint": hint})
    if top:
        clients = clients[:top]
    all_chosen = covers.get("", [])
    all_apps = sum_apps(all_chosen)
    newest, read_at = await repository.get_traffic_latest(device_id)
    wan_up, wan_down = totals.get("", (0, 0))
    with_hours = hours <= 7 * 24 if with_hours is None else with_hours
    return {
        "hours": hours, "days": days or None, "start": start, "end": end,
        "window_end": newest + HOUR if newest is not None else None,
        "stored_at": read_at,
        "wan": wan if with_hours else [],  # a 30 day answer leaves the hours out (85 % of its size): `daily` has them
        "daily": _daily(wan, start, end, tz) if hours >= 48 else None,
        "totals": {"up": wan_up, "down": wan_down},
        "clients": clients,
        "apps": _apps(all_apps, top),
        "apps_unattributed": unattributed((wan_up, wan_down), all_apps),
        "apps_covered": _covered(all_chosen),
        "apps_window_end": all_chosen[0][0] if all_chosen else None,
    }


async def client_detail(repository, device_id: str, mac: str, hours: int, namer: Namer,
                        now: Optional[float] = None, tz: Optional[tzinfo] = None, *,
                        days: Optional[int] = None) -> Dict[str, Any]:
    """One client over the last `hours` hours (`days` * 24 with `days`; the window of `summary`): its hourly series (0
    for an hour the router has, with no row for this client; null for an hour the router's WAN figure is missing
    too; left out of a range over 7 days), `daily` as `summary` has it, its apps and hint over the window as
    `summary` counts them (`apps_covered`, `apps_unattributed`, `apps_window_end`), and its apps by whole local day
    (`days`, day_of_window)."""
    now = time.time() if now is None else now
    tz = tz or timezone.utc
    start, end, hours = await _window(repository, device_id, hours, days, now)
    rows = await repository.get_traffic_hours(device_id, start, end, client=mac)
    wan_hours = {h for h, _, _, _ in await repository.get_traffic_hours(device_id, start, end, client="")}
    mine = {h: (u, d) for h, _, u, d in rows}
    series = [{"hour": h, "up": mine.get(h, (0, 0))[0] if h in wan_hours or h in mine else None,
               "down": mine.get(h, (0, 0))[1] if h in wan_hours or h in mine else None}
              for h in range(start, end, HOUR)]
    chosen = cover_apps([(w, a) for _, w, a in await repository.get_traffic_app_windows(
        device_id, start + APP_WINDOW, end, client=mac)], start, end)
    apps = sum_apps(chosen)
    total = (sum(u for u, _ in mine.values()), sum(d for _, d in mine.values()))
    by_day = [(day, w, a) for day, w, a in await repository.get_traffic_app_days(device_id, mac, start)
              if day_of_window(w, tz) == day]
    return {
        "client": {**namer.row(mac), "traffic_hint": hint_of(apps, chosen[0][0], now) if chosen else None},
        "hours": hours, "range_days": days or None, "start": start, "end": end,
        "series": series if hours <= 7 * 24 else [],
        "daily": _daily(series, start, end, tz) if hours >= 48 else None,
        "totals": {"up": total[0], "down": total[1]},
        "apps": _apps(apps),
        "apps_unattributed": unattributed(total, apps),
        "apps_covered": _covered(chosen),
        "apps_window_end": chosen[0][0] if chosen else None,
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

