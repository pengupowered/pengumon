"""The speed tests (schema v17, table speed_tests): one row per test, scheduled or manual, whatever came of it. The
runner (pengumon.speedtest.runner) writes them; GET /api/speedtests, the reports and the monthly data cap read them."""

from typing import Any, Dict, List, Optional

import aiosqlite

SPEED_TEST_COLUMNS = ("ts", "duration_s", "trigger", "provider", "server", "status", "reason", "down_mbps", "up_mbps",
                      "idle_ms", "loaded_down_ms", "loaded_up_ms", "bytes_down", "bytes_up", "wan_down_mbps",
                      "wan_up_mbps")
_SELECT = "SELECT id, " + ", ".join(SPEED_TEST_COLUMNS) + " FROM speed_tests"


class SpeedTestQueries:
    async def insert_speed_test(self, row: Dict[str, Any]) -> int:
        """Stores one test (the SPEED_TEST_COLUMNS; a missing one is NULL) and returns its id."""
        unknown = set(row) - set(SPEED_TEST_COLUMNS)
        if unknown:
            raise ValueError(f"unknown speed test column(s): {', '.join(sorted(unknown))}")
        async with self._db.write() as db:
            cursor = await db.execute(
                f"INSERT INTO speed_tests ({', '.join(SPEED_TEST_COLUMNS)}) "
                f"VALUES ({', '.join('?' * len(SPEED_TEST_COLUMNS))})",
                [row.get(c) for c in SPEED_TEST_COLUMNS])
            return cursor.lastrowid

    async def get_speed_tests(self, start_time: float, end_time: float, limit: int = 100) -> List[Dict[str, Any]]:
        """The tests that started in [start_time, end_time], newest first, at most `limit`."""
        async with self._db.read() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(f"{_SELECT} WHERE ts >= ? AND ts <= ? ORDER BY ts DESC, id DESC LIMIT ?",
                                  (start_time, end_time, limit)) as cursor:
                return [dict(r) for r in await cursor.fetchall()]

    async def latest_speed_test(self, statuses: Optional[List[str]] = None) -> Optional[Dict[str, Any]]:
        """The newest test (of one of `statuses`, when given), or None."""
        where, params = "", []
        if statuses:
            where = f" WHERE status IN ({', '.join('?' * len(statuses))})"
            params = list(statuses)
        async with self._db.read() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(f"{_SELECT}{where} ORDER BY ts DESC, id DESC LIMIT 1", params) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    async def latest_speed_test_moving_schedule(self, busy_reason: str) -> Optional[Dict[str, Any]]:
        """The newest test that moves the schedule: any but a skip whose reason starts with `busy_reason` (a busy
        line), however many of those came after it. None when there is none."""
        async with self._db.read() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                f"{_SELECT} WHERE NOT (status = 'skipped' AND substr(COALESCE(reason, ''), 1, ?) = ?) "
                "ORDER BY ts DESC, id DESC LIMIT 1", (len(busy_reason), busy_reason)) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    async def speed_test_bytes(self, start_time: float, end_time: float) -> int:
        """Bytes the tests that started in [start_time, end_time) moved, both directions (the monthly data cap)."""
        async with self._db.read() as db:
            async with db.execute(
                "SELECT COALESCE(SUM(COALESCE(bytes_down, 0) + COALESCE(bytes_up, 0)), 0) FROM speed_tests "
                "WHERE ts >= ? AND ts < ?", (start_time, end_time)) as cursor:
                return int((await cursor.fetchone())[0])
