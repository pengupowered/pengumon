"""SQL only the reports use, on the per-minute rollups. Each function takes the repository and goes through its public
`flush_samples()`, `read()` and `target_key()`, so the repository itself holds no report-specific query."""

from typing import Any, Dict, Iterable, List


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
    skip = set(exclude)
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
    return [{"target_id": repository.target_key(t), "block": blk, "sent": n, "ok": ok, "dead_minutes": dead}
            for t, blk, n, ok, dead in rows]
