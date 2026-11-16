"""The oldest stored time of each kind of data: the answer of GET /api/oldest (api/oldest.py).

A chart at "All" starts at the oldest stored row (js/ranges.js loadFromOldest). Asking the history route for the whole
window and reading the answer's first row costs a request that is thrown away when the data is younger than the
retention; this is the cheap read that says where to start. Each table is read at the start of its primary key, so it
costs nothing however large the table is (/api/storage knows the same figures but reads the size of every table).

The per-table queries live here and nowhere else: when a table changes shape (the probe samples' time column) this is the
one place to change (TABLES).
"""

from typing import Dict, List, Optional, Tuple

# kind (the retention kinds of js/ranges.js RETENTION_KEYS) -> [(table, time column, seconds per unit, device_id first in
# the key)]. The oldest of a kind is the oldest of its tiers (raw readings, minute and quarter-hour rollups).
TABLES: Dict[str, List[Tuple[str, str, int, bool]]] = {
    "probes": [("samples", "ts", 1, False), ("rollup_10s", "slot", 10, False), ("rollup_1m", "minute", 60, False),
               ("rollup_15m", "quarter", 900, False)],
    "router": [("device_samples", "ts", 1, True), ("device_rollup_1m", "minute", 60, True),
               ("device_rollup_15m", "quarter", 900, True)],
    "mesh": [("mesh_samples", "ts", 1, True), ("mesh_rollup_15m", "quarter", 900, True)],
    "line": [("line_samples", "ts", 1, True)],
    "traffic": [("traffic_hourly", "hour", 1, True)],
    "speedtest": [("speed_tests", "ts", 1, False)],
}


def _first(table: str, column: str, by_device: bool) -> str:
    """SQL for the smallest `column` of a table. A key that starts with the device has no cheap global MIN: a loose index
    scan hops from device to device (one seek each), then takes each device's first row."""
    if not by_device:
        return f'SELECT MIN("{column}") FROM "{table}"'
    return (f'WITH RECURSIVE d(id) AS (SELECT MIN(device_id) FROM "{table}" UNION ALL '
            f'SELECT (SELECT MIN(device_id) FROM "{table}" WHERE device_id > d.id) FROM d WHERE d.id IS NOT NULL) '
            f'SELECT MIN((SELECT MIN("{column}") FROM "{table}" WHERE device_id = d.id)) FROM d WHERE d.id IS NOT NULL')


async def oldest_stored(repository) -> Dict[str, Optional[float]]:
    """{kind: epoch seconds of the oldest stored row of any of its tables, or None when it holds nothing}."""
    out: Dict[str, Optional[float]] = {}
    async with repository.read() as db:
        for kind, tables in TABLES.items():
            found: List[float] = []
            for table, column, unit, by_device in tables:
                async with db.execute(_first(table, column, by_device)) as cursor:
                    first = (await cursor.fetchone())[0]
                if first is not None:
                    found.append(float(first) * unit)
            out[kind] = min(found) if found else None
    return out
