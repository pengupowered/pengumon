"""The router's traffic per client and hour, its app windows (schema v19) and the device events (restarts, WAN link
changes). Rows name clients by MAC: they are read back only by the routes and reports that apply the client privacy
setting (devices/traffic.py)."""

import json
from typing import Callable, Iterable, List, Optional, Tuple

from pengumon.capabilities import TrafficApps, TrafficHour


class TrafficQueries:
    async def upsert_traffic_hours(self, device_id: str, rows: Iterable[TrafficHour], read_at: float) -> None:
        """Stores hourly byte counts; an hour already stored for the client is replaced (a later read corrects it)."""
        values = [(device_id, int(r.hour), r.client, int(r.up), int(r.down), read_at) for r in rows]
        if not values:
            return
        async with self._db.write() as db:
            await db.executemany(
                "INSERT OR REPLACE INTO traffic_hourly (device_id, hour, client, up, down, read_at) "
                "VALUES (?, ?, ?, ?, ?, ?)", values)

    async def store_traffic_apps(self, device_id: str, windows: Iterable[TrafficApps], read_at: float,
                                 day_of: Callable[[int], Optional[str]]) -> None:
        """Each window replaces the client's latest one (day ''). A window that is a whole local day (`day_of` names it:
        it ends at a midnight) also becomes that day's row, unless the day already has one."""
        latest, days = [], []
        for w in windows:
            apps = json.dumps([[name, int(up), int(down)] for name, up, down in w.apps], ensure_ascii=False,
                              separators=(",", ":"))
            latest.append((device_id, w.client, "", int(w.window_end), read_at, apps))
            day = day_of(int(w.window_end))
            if day:
                days.append((device_id, w.client, day, int(w.window_end), read_at, apps))
        if not latest:
            return
        async with self._db.write() as db:
            await db.executemany(
                "INSERT OR REPLACE INTO traffic_apps (device_id, client, day, window_end, read_at, apps) "
                "VALUES (?, ?, ?, ?, ?, ?)", latest)
            if days:
                await db.executemany(
                    "INSERT OR IGNORE INTO traffic_apps (device_id, client, day, window_end, read_at, apps) "
                    "VALUES (?, ?, ?, ?, ?, ?)", days)

    async def get_traffic_hours(self, device_id: str, start_hour: float, end_hour: float,
                                client: Optional[str] = None) -> List[Tuple[int, str, int, int]]:
        """(hour, client, up, down) with start_hour <= hour < end_hour, oldest first; one client's (and the WAN's, '')
        when `client` is given."""
        sql = "SELECT hour, client, up, down FROM traffic_hourly WHERE device_id = ? AND hour >= ? AND hour < ?"
        params: list = [device_id, int(start_hour), int(end_hour)]
        if client is not None:
            sql += " AND client = ?"
            params.append(client)
        async with self._db.read() as db:
            async with db.execute(sql + " ORDER BY hour, client", params) as cursor:
                return [tuple(r) for r in await cursor.fetchall()]

    async def get_traffic_totals(self, device_id: str, start_hour: float,
                                 end_hour: float) -> List[Tuple[str, int, int, int]]:
        """(client, up, down, hours stored) summed over start_hour <= hour < end_hour, per client ('' = the WAN)."""
        async with self._db.read() as db:
            async with db.execute(
                "SELECT client, SUM(up), SUM(down), COUNT(*) FROM traffic_hourly "
                "WHERE device_id = ? AND hour >= ? AND hour < ? GROUP BY client",
                (device_id, int(start_hour), int(end_hour)),
            ) as cursor:
                return [tuple(r) for r in await cursor.fetchall()]

    async def get_traffic_latest(self, device_id: str) -> Tuple[Optional[int], Optional[float]]:
        """(the newest hour stored for the whole WAN, when the traffic was last stored); (None, None) without rows."""
        async with self._db.read() as db:
            async with db.execute(
                "SELECT MAX(hour) FROM traffic_hourly WHERE device_id = ? AND client = ''", (device_id,)
            ) as cursor:
                hour = (await cursor.fetchone())[0]
            async with db.execute(
                "SELECT MAX(read_at) FROM traffic_hourly WHERE device_id = ? AND hour >= ?",
                (device_id, (hour or 0) - 86400),
            ) as cursor:
                read_at = (await cursor.fetchone())[0]
        return hour, read_at

    async def get_traffic_apps(self, device_id: str, client: Optional[str] = None,
                               day: str = "") -> List[Tuple[str, str, int, float, list]]:
        """(client, day, window_end, read_at, [[app, up, down], ...]) of the latest windows (day '') or of one day's,
        for every client or one."""
        sql = "SELECT client, day, window_end, read_at, apps FROM traffic_apps WHERE device_id = ? AND day = ?"
        params: list = [device_id, day]
        if client is not None:
            sql += " AND client = ?"
            params.append(client)
        async with self._db.read() as db:
            async with db.execute(sql, params) as cursor:
                rows = await cursor.fetchall()
        return [(c, d, w, r, _apps(a)) for c, d, w, r, a in rows]

    async def get_traffic_app_days(self, device_id: str, client: str,
                                   since_window_end: float) -> List[Tuple[str, int, list]]:
        """(day, window_end, apps) of one client's day windows ending at or after `since_window_end`, oldest first."""
        async with self._db.read() as db:
            async with db.execute(
                "SELECT day, window_end, apps FROM traffic_apps WHERE device_id = ? AND client = ? AND day != '' "
                "AND window_end >= ? ORDER BY day", (device_id, client, int(since_window_end)),
            ) as cursor:
                return [(d, w, _apps(a)) for d, w, a in await cursor.fetchall()]

    async def get_traffic_app_day_range(self, device_id: str, first_day: str,
                                        last_day: str) -> List[Tuple[str, str, int, list]]:
        """(client, day, window_end, apps) of every client's day rows with first_day <= day <= last_day ("YYYY-MM-DD"),
        oldest first."""
        async with self._db.read() as db:
            async with db.execute(
                "SELECT client, day, window_end, apps FROM traffic_apps WHERE device_id = ? AND day >= ? AND day <= ? "
                "AND day != '' ORDER BY day, client", (device_id, first_day, last_day),
            ) as cursor:
                return [(c, d, w, _apps(a)) for c, d, w, a in await cursor.fetchall()]

    async def insert_device_event(self, device_id: str, ts: float, kind: str, detail: dict) -> None:
        async with self._db.write() as db:
            await db.execute("INSERT INTO device_events (device_id, ts, kind, detail) VALUES (?, ?, ?, ?)",
                             (device_id, ts, kind, json.dumps(detail, separators=(",", ":"))))

    async def get_device_events(self, device_id: Optional[str], start_time: float,
                                end_time: float) -> List[Tuple[str, float, str, dict]]:
        """(device_id, ts, kind, detail) with start_time <= ts <= end_time, oldest first; every device's when
        `device_id` is None."""
        sql = "SELECT device_id, ts, kind, detail FROM device_events WHERE ts >= ? AND ts <= ?"
        params: list = [start_time, end_time]
        if device_id is not None:
            sql += " AND device_id = ?"
            params.append(device_id)
        async with self._db.read() as db:
            async with db.execute(sql + " ORDER BY ts", params) as cursor:
                rows = await cursor.fetchall()
        out = []
        for device, ts, kind, detail in rows:
            try:  # a row that is not a JSON object (or holds NaN or Infinity, which no JSON answer may carry) is {}
                parsed = json.loads(detail, parse_constant=_no_constant)
            except (ValueError, TypeError, RecursionError):
                parsed = {}
            out.append((device, ts, kind, parsed if isinstance(parsed, dict) else {}))
        return out

    async def get_latest_router_extras(self, device_id: str) -> Tuple[Optional[Tuple[float, int]],
                                                                       Optional[Tuple[float, int]]]:
        """((ts, uptime_s), (ts, wan_link_mbps)) of the device's newest poll that read each, or None for one never read:
        what the device events compare the first reading after a start with."""
        out = []
        async with self._db.read() as db:
            for column in ("uptime_s", "wan_link_mbps"):
                async with db.execute(
                    f"SELECT ts, {column} FROM device_samples WHERE device_id = ? AND {column} IS NOT NULL "
                    "ORDER BY ts DESC LIMIT 1", (device_id,),
                ) as cursor:
                    row = await cursor.fetchone()
                out.append((row[0], row[1]) if row else None)
        return out[0], out[1]


def _apps(text: str) -> list:
    try:
        value = json.loads(text)
    except ValueError:
        return []
    return [a for a in value if isinstance(a, list) and len(a) == 3] if isinstance(value, list) else []


def _no_constant(name: str) -> float:
    raise ValueError(f"{name} in a stored event")
