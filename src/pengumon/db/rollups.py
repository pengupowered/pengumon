"""Reading the quarter-hour rollups (rollup_15m) with the per-minute ones (rollup_1m) at the edges of a window.

A window of whole minutes [first, end) is read as the quarters that lie wholly inside it, from rollup_15m, plus the
minutes of the partial quarters at either end, from rollup_1m. Both give their rows under the same names (MERGED_COLUMNS),
so a query sums them alike, and for a window rollup_1m still covers the sums are those of its minutes alone: n, ok and
dead are equal, the latency sums and the band equal up to the order the floating-point additions are made in.

An edge whose minutes rollup_1m no longer has (older than its first stored minute: past rollup_retention_days) is read
as its whole quarter from rollup_15m instead: such a window is rounded out to whole quarters.

Every time zone's offset from UTC is a whole number of quarter hours, and so is every daylight-saving change, so a
local hour or day is whole quarters; quarter_aligned checks it for the zone and window at hand (a fixed offset of an odd
number of minutes is not), and the callers keep the per-minute query when it fails.
"""

from typing import List, Optional, Sequence, Tuple

from pengumon.timeofday import clock_changes, utc_offset

QUARTER_MINUTES = 15
QUARTER_SECONDS = 900

# Each name as rollup_15m and rollup_1m give it. dead: minutes with every probe (of at least 10) lost (block_stats).
_FROM_15M = {
    "n": "n", "ok": "ok", "lat_sum": "lat_sum", "lat_min": "lat_min", "lat_max": "lat_max", "jit_sum": "jit_sum",
    "jit_n": "jit_n", "p10_sum": "p10_sum", "p10_ok": "p10_ok", "p90_sum": "p90_sum", "p90_ok": "p90_ok",
    "dead": "dead_minutes",
}
_FROM_1M = {
    "n": "n", "ok": "ok", "lat_sum": "lat_sum", "lat_min": "lat_min", "lat_max": "lat_max", "jit_sum": "jit_sum",
    "jit_n": "jit_n", "p10_sum": "lat_p10 * ok", "p10_ok": "CASE WHEN lat_p10 IS NOT NULL THEN ok END",
    "p90_sum": "lat_p90 * ok", "p90_ok": "CASE WHEN lat_p90 IS NOT NULL THEN ok END",
    "dead": "CASE WHEN ok = 0 AND n >= 10 THEN 1 ELSE 0 END",
}
MERGED_COLUMNS = tuple(_FROM_15M)


def split_minutes(first: int, end: Optional[int], floor: Optional[int]
                  ) -> Tuple[int, Optional[int], List[Tuple[int, int]]]:
    """The minutes [first, end) (end None: no upper bound) as (q_lo, q_hi, edges): the quarters [q_lo, q_hi) (q_hi None:
    no upper bound) to read from rollup_15m and the minute ranges [a, b) to read from rollup_1m. `floor` is rollup_1m's
    first stored minute (None: it has none): an edge that starts before it is read as its whole quarter instead."""
    q = QUARTER_MINUTES
    q_lo = -(-first // q)
    q_hi = None if end is None else max(end // q, q_lo)
    head = (first, q_lo * q if end is None else min(q_lo * q, end))
    tail = None if end is None else (max(q_hi * q, first), end)
    if tail is not None and tail[0] <= head[0]:  # the window lies inside one quarter: one edge
        head, tail = (first, end), None
    edges = []
    for edge in (head, tail):
        if edge is None or edge[0] >= edge[1]:
            continue
        if floor is not None and edge[0] >= floor:
            edges.append(edge)
            continue
        q_lo = min(q_lo, edge[0] // q)  # rollup_1m no longer has it: its whole quarter
        if q_hi is not None:
            q_hi = max(q_hi, -(-edge[1] // q))
    return q_lo, q_hi, edges


async def minute_floor(db) -> Optional[int]:
    """rollup_1m's first stored minute (a primary-key lookup), None when it is empty."""
    async with db.execute("SELECT MIN(minute) FROM rollup_1m") as cursor:
        row = await cursor.fetchone()
    return row[0] if row else None


def merged_rows_sql(columns: Sequence[str], first: int, end: Optional[int], floor: Optional[int],
                    target_clause: str = "", target_params: Sequence = ()) -> Tuple[str, list]:
    """(SQL, parameters) of a SELECT of the rows of the minutes [first, end) as `t` (the UTC second the row begins),
    `target` and `columns` (names of MERGED_COLUMNS): the whole quarters from rollup_15m, the edges from rollup_1m
    (split_minutes). target_clause (" AND target IN (?, ...)") and its parameters filter every part."""
    q_lo, q_hi, edges = split_minutes(first, end, floor)
    quarter_cols = ", ".join(f"{_FROM_15M[c]} AS {c}" for c in columns)
    minute_cols = ", ".join(f"{_FROM_1M[c]} AS {c}" for c in columns)
    upper = "" if q_hi is None else " AND quarter < ?"
    parts = [f"SELECT quarter * {QUARTER_SECONDS} AS t, target, {quarter_cols} FROM rollup_15m "
             f"WHERE quarter >= ?{upper}{target_clause}"]
    params: list = [q_lo, *([] if q_hi is None else [q_hi]), *target_params]
    for a, b in edges:
        parts.append(f"SELECT minute * 60 AS t, target, {minute_cols} FROM rollup_1m "
                     f"WHERE minute >= ? AND minute < ?{target_clause}")
        params += [a, b, *target_params]
    return " UNION ALL ".join(parts), params


def quarter_aligned(tz, start_time: float, end_time: float) -> bool:
    """Whether every offset of `tz` from start_time to end_time, and every instant it changes, is a whole number of
    quarter hours (so quarters never straddle a local hour)."""
    if utc_offset(start_time, tz) % QUARTER_SECONDS:
        return False
    return all(int(at) % QUARTER_SECONDS == 0 and int(before) % QUARTER_SECONDS == 0 and int(after) % QUARTER_SECONDS == 0
               for at, before, after in clock_changes(tz, start_time, end_time))


def local_time_sql(tz, start_time: float, end_time: float, column: str = "t") -> Tuple[str, List[int]]:
    """(SQL, parameters) for the wall-clock time in `tz` of `column` (UTC epoch seconds, an integer), as seconds since
    1970 read as if the clock were UTC (the form of reports.queries.local_seconds_sql). Correct from start_time to
    end_time: one CASE arm per daylight-saving change in between."""
    changes = clock_changes(tz, start_time, end_time)
    if not changes:
        return f"({column} + ?)", [utc_offset(start_time, tz)]
    params: List[int] = []
    for at, before, _ in changes:
        params += [int(at), int(before)]
    params.append(int(changes[-1][2]))
    arms = " ".join(f"WHEN {column} < ? THEN ?" for _ in changes)
    return f"({column} + CASE {arms} ELSE ? END)", params
