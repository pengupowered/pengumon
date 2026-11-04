"""SQL only the reports use, on the per-minute rollups. Each function takes the repository and goes through its public
`flush_samples()`, `read()`, `target_key()` and `shown()` (hops removed from the path are left out), so the repository itself holds no report-specific query."""

from typing import Any, Dict, Iterable, List, NamedTuple, Optional, Sequence, Tuple

from pengumon.incidents.loss import probe_window


async def loss_grid_rows(repository, start_time: float, tz_offset_minutes: int = 0,
                         exclude: Iterable[str] = ()) -> List[Dict[str, Any]]:
    """Probes sent and lost per calendar day and hour (UTC shifted by tz_offset_minutes), all targets together but the
    ones in `exclude` (target keys: the hops that ignore ping), from the per-minute rollups. Only the hours that have
    probes are returned."""
    await repository.flush_samples()
    async with repository.read() as db:
        async with db.execute(
            """
            SELECT strftime('%Y-%m-%d', minute * 60, 'unixepoch', ?) AS day,
                   CAST(strftime('%H', minute * 60, 'unixepoch', ?) AS INTEGER) AS hour,
                   target, SUM(n), SUM(n) - SUM(ok)
            FROM rollup_1m
            WHERE minute >= ?
            GROUP BY day, hour, target
            """,
            (*(f"{int(tz_offset_minutes):+d} minutes",) * 2, int(start_time // 60)),
        ) as cursor:
            rows = await cursor.fetchall()
    skip = set(exclude) | set(getattr(repository, "hidden_targets", ()))  # and the hops removed from the path
    cells: Dict[tuple, List[int]] = {}
    for d, h, target, sent, lost in rows:
        if repository.target_key(target) in skip:
            continue
        cell = cells.setdefault((d, h), [0, 0])
        cell[0] += sent
        cell[1] += lost
    return [{"day": d, "hour": h, "sent": sent, "lost": lost} for (d, h), (sent, lost) in cells.items()]


async def block_stats(repository, first_block: int, last_block: int, block_seconds: int,
                          offset_seconds: int = 0) -> List[Dict[str, Any]]:
    """Probes sent and answered per target and fixed-length block, from the per-minute rollups. Block k covers the
    seconds [k * block_seconds - offset_seconds, (k + 1) * block_seconds - offset_seconds), so a time zone east of
    UTC is a positive offset; first_block and last_block are inclusive. `dead_minutes` counts the minutes in which
    every probe (of at least 10) was lost. Blocks without rollups are not returned."""
    await repository.flush_samples()
    b = int(block_seconds)
    async with repository.read() as db:
        async with db.execute(
            f"""
            SELECT target, (minute * 60 + ?) / {b} AS block, SUM(n), SUM(ok),
                   SUM(CASE WHEN ok = 0 AND n >= 10 THEN 1 ELSE 0 END)
            FROM rollup_1m
            WHERE minute >= ? AND minute < ?
            GROUP BY target, block
            """,
            (int(offset_seconds), (first_block * b - int(offset_seconds)) // 60,
             ((last_block + 1) * b - int(offset_seconds)) // 60),
        ) as cursor:
            rows = await cursor.fetchall()
    return repository.shown([{"target_id": repository.target_key(t), "block": blk, "sent": n, "ok": ok, "dead_minutes": dead}
                             for t, blk, n, ok, dead in rows])


class MinuteRow(NamedTuple):
    """One target's probes in one minute (rollup_1m). avg: mean latency of the answered probes, p90: their 90th
    percentile, jitter: mean of the reported jitters; each None when there was none."""
    minute: int
    target_id: str
    n: int
    ok: int
    avg: Optional[float]
    p90: Optional[float]
    jitter: Optional[float]


async def minute_rows(repository, start_time: float, end_time: float) -> List[MinuteRow]:
    """Every per-minute rollup of the minutes start_time // 60 .. end_time // 60 (inclusive), one SELECT, hops removed
    from the path left out. Order is not defined."""
    await repository.flush_samples()
    async with repository.read() as db:
        async with db.execute(
            "SELECT minute, target, n, ok, lat_sum / NULLIF(ok, 0), lat_p90, jit_sum / NULLIF(jit_n, 0) "
            "FROM rollup_1m WHERE minute >= ? AND minute <= ?",
            (int(start_time // 60), int(end_time // 60)),
        ) as cursor:
            rows = await cursor.fetchall()
    names = {t: repository.target_key(t) for t in {r[1] for r in rows}}
    hidden = getattr(repository, "hidden_targets", ())
    new = tuple.__new__  # a week is 100k+ rows: skip the NamedTuple's Python-level constructor
    return [new(MinuteRow, (m, names[t], n, ok, a, p, j)) for m, t, n, ok, a, p, j in rows
            if names[t] not in hidden]


async def incident_rows(repository, start_time: float, end_time: float) -> List[Dict[str, Any]]:
    """The incidents that started in [start_time, end_time], oldest first, as plain rows with what the pack ranks by
    (loss_start, loss_seconds are None when not known). `affected_targets` stays the stored JSON text."""
    async with repository.read() as db:
        async with db.execute(
            """
            SELECT id, start_time, end_time, duration_seconds, cause_code, title, description, severity,
                   affected_targets, load_pct, expected, kind, loss_start, loss_seconds
            FROM incidents
            WHERE start_time >= ? AND start_time <= ?
            ORDER BY start_time
            """,
            (start_time, end_time),
        ) as cursor:
            names = [d[0] for d in cursor.description]
            return [dict(zip(names, r)) for r in await cursor.fetchall()]


EPISODE_CHUNK = 300  # windows per statement: three parameters each, far under SQLite's limit


async def episode_loss(repository, windows: Sequence[Tuple[Any, float, float]],
                       peaks: Optional[Dict[Any, Dict[str, float]]] = None
                       ) -> Dict[Any, Tuple[str, Dict[str, Tuple[int, int]]]]:
    """Per window (key, start, end) of an incident (start, end: its detection times), per target: (probes sent,
    answered) over the incident's episode, probe_window(start, end). Batched: one statement per EPISODE_CHUNK windows,
    on the raw samples. A window with no raw sample (past retention) is read from the per-minute rollups, which
    count whole minutes. Returns {key: (resolution, {target_id: (sent, ok)})}, resolution "1s" (raw), "minute"
    (rollups) or "none" (nothing stored); hops removed from the path are left out. `peaks`, when given, is filled with
    {key: {target_id: the highest answered probe's latency in ms}} from the same rows (a minute's lat_max for the
    rollups)."""
    await repository.flush_samples()
    spans = [(k, *probe_window(s, e)) for k, s, e in windows]
    out: Dict[Any, Tuple[str, Dict[str, Tuple[int, int]]]] = {k: ("none", {}) for k, _, _ in spans}
    hidden = getattr(repository, "hidden_targets", ())

    async def run(chunk, sql, scale):
        values = ",".join("(?,?,?)" for _ in chunk)
        params = [x for i, (_, lo, hi) in enumerate(chunk) for x in (i, lo / scale, hi / scale)]
        async with repository.read() as db:
            async with db.execute(sql.format(values=values), params) as cursor:
                return await cursor.fetchall()

    raw_sql = ("WITH w(i, lo, hi) AS (VALUES {values}) SELECT w.i, s.target, COUNT(*), SUM(s.ok), "
               "MAX(CASE WHEN s.ok = 1 THEN s.latency_ms END) FROM w "
               "JOIN samples s ON s.ts >= w.lo AND s.ts <= w.hi GROUP BY w.i, s.target")
    # whole minutes: floor(lo / 60) .. floor(hi / 60), as get_rollup_stats
    minute_sql = ("WITH w(i, lo, hi) AS (VALUES {values}) SELECT w.i, r.target, SUM(r.n), SUM(r.ok), MAX(r.lat_max) "
                  "FROM w "
                  "JOIN rollup_1m r ON r.minute >= CAST(w.lo AS INTEGER) AND r.minute <= CAST(w.hi AS INTEGER) "
                  "GROUP BY w.i, r.target")
    for resolution, sql, scale, pending in (("1s", raw_sql, 1.0, spans), ("minute", minute_sql, 60.0, None)):
        if pending is None:
            pending = [sp for sp in spans if out[sp[0]][0] == "none"]
        for at in range(0, len(pending), EPISODE_CHUNK):
            chunk = pending[at:at + EPISODE_CHUNK]
            hops: Dict[int, Dict[str, Tuple[int, int]]] = {}
            tops: Dict[int, Dict[str, float]] = {}
            for i, target, sent, ok, top in await run(chunk, sql, scale):
                tid = repository.target_key(target)
                if tid not in hidden:
                    hops.setdefault(i, {})[tid] = (sent, ok or 0)
                    if top is not None:
                        tops.setdefault(i, {})[tid] = float(top)
            for i, (k, _, _) in enumerate(chunk):
                if i in hops:
                    out[k] = (resolution, hops[i])
                    if peaks is not None:
                        peaks[k] = tops.get(i, {})
    return out


async def path_trace_names(repository, limit: int = 50) -> Dict[str, str]:
    """{address: reverse name} from the path traces stored with the latest `limit` incidents that have one, the newest
    name of an address winning. The provider's routers have such names; the pack shows them as the hops' names."""
    import json
    async with repository.read() as db:
        async with db.execute(
            "SELECT path_trace FROM incidents WHERE path_trace IS NOT NULL ORDER BY start_time DESC LIMIT ?", (limit,),
        ) as cursor:
            rows = await cursor.fetchall()
    names: Dict[str, str] = {}
    for (text,) in rows:
        try:
            trace = json.loads(text) if isinstance(text, str) else (text or [])
        except ValueError:
            continue
        for hop in trace if isinstance(trace, list) else ():
            if isinstance(hop, dict) and hop.get("ip") and hop.get("hostname"):
                names.setdefault(str(hop["ip"]), str(hop["hostname"]))
    return names
