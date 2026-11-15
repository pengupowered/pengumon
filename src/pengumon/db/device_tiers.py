"""Reading the router's and the mesh's history across the storage tiers (schema v21).

Raw polls are kept `retention_days`, the minute tier (device_rollup_1m) `rollup_retention_days` and the quarter-hour
tiers (device_rollup_15m, mesh_rollup_15m) `long_rollup_retention_days`. A range that raw still covers is read from raw
alone, by the query each history had before the tiers (so its answer is unchanged). A range that starts before raw's
first whole minute (quarter, for the mesh) is read in up to three parts, each as partial aggregates (sums, counts,
maximums) grouped into buckets, and the parts are merged by summing:

  - raw polls from that boundary on,
  - minutes from device_rollup_1m, for the minutes before the boundary that the minute tier still has,
  - whole quarters from device_rollup_15m for the rest (db/rollups.py split_minutes: a range edge older than the minute
    tier is rounded out to its whole quarter, and so is the start of a range inside a minute or a quarter: the
    buckets say what the data holds, not what the range asked for).

Buckets are epoch-aligned as ever. A bucket narrower than a tier's own width is widened to it for the part read from
that tier (60 s for minutes, 900 s for quarters); a wider one is kept, so the parts line up in the same buckets.
"""

import math
from typing import List, Optional, Tuple

from pengumon.db.rollups import QUARTER_SECONDS, split_minutes
from pengumon.db.schema import DEVICE_ROLLUP_CLIENT_COLUMNS, DEVICE_ROLLUP_FIGURES

DAY = 86400
# partial aggregates, in the order every part of a merged query selects them
PARTIALS = ([f"{c}_{s}" for c in DEVICE_ROLLUP_FIGURES for s in ("sum", "n", "max")] + ["wan_link_min"]
            + [f"{c}_{s}" for c in DEVICE_ROLLUP_CLIENT_COLUMNS for s in ("sum", "n")])


def raw_boundary(now: float, retention_days: float, quantum: int) -> int:
    """The first whole `quantum` (60 or 900 s) inside the raw rows' retention: raw is complete from here on."""
    return math.ceil((now - retention_days * DAY) / quantum) * quantum


def _raw_partials() -> str:
    parts = [f"SUM({c}) AS {c}_sum, COUNT({c}) AS {c}_n, MAX({c}) AS {c}_max" for c in DEVICE_ROLLUP_FIGURES]
    parts.append("MIN(wan_link_mbps) AS wan_link_min")
    # only the polls that carry counts, as the rollup writer (db/schema.py) and the raw client query do
    parts += [f"SUM(CASE WHEN clients_online IS NOT NULL THEN {c} END) AS {c}_sum, "
              f"COUNT(CASE WHEN clients_online IS NOT NULL THEN {c} END) AS {c}_n"
              for c in DEVICE_ROLLUP_CLIENT_COLUMNS]
    return ", ".join(parts)


def _tier_partials() -> str:
    parts = [f"SUM({c}_sum) AS {c}_sum, SUM({c}_n) AS {c}_n, MAX({c}_max) AS {c}_max" for c in DEVICE_ROLLUP_FIGURES]
    parts.append("MIN(wan_link_min) AS wan_link_min")
    parts += [f"SUM({c}_sum) AS {c}_sum, SUM({c}_n) AS {c}_n" for c in DEVICE_ROLLUP_CLIENT_COLUMNS]
    return ", ".join(parts)


def device_parts(device_id: str, start: float, end: float, bucket: int, now: float, retention_days: float,
                 minute_floor: Optional[int]) -> Optional[Tuple[str, list]]:
    """(SQL, parameters) of a SELECT of (bucket, then the PARTIALS) rows for the range, one row per bucket and part
    (the caller sums them), or None when raw covers the whole range (read raw alone); an empty SQL when the range holds no tier part."""
    boundary = raw_boundary(now, retention_days, 60)
    if start >= boundary:
        return None
    b = max(1, int(bucket))
    parts: List[str] = []
    params: list = []
    if end >= boundary:
        parts.append(f"SELECT CAST(ts / ? AS INTEGER) * ? AS bucket, {_raw_partials()} FROM device_samples "
                     "WHERE device_id = ? AND ts >= ? AND ts <= ? GROUP BY bucket")
        params += [b, b, device_id, boundary, end]
    first, last = int(start // 60), min(int(end // 60) + 1, boundary // 60)
    if first < last:
        q_lo, q_hi, edges = split_minutes(first, last, minute_floor)
        q_hi = min(q_hi, boundary // QUARTER_SECONDS)  # never past raw: a rounded-out tail would count its polls twice
        b1, b15 = max(b, 60), max(b, QUARTER_SECONDS)
        for a, c in edges:
            parts.append(f"SELECT (minute * 60 / ?) * ? AS bucket, {_tier_partials()} FROM device_rollup_1m "
                         "WHERE device_id = ? AND minute >= ? AND minute < ? GROUP BY bucket")
            params += [b1, b1, device_id, a, c]
        if q_hi > q_lo:
            parts.append(f"SELECT (quarter * {QUARTER_SECONDS} / ?) * ? AS bucket, {_tier_partials()} "
                         "FROM device_rollup_15m WHERE device_id = ? AND quarter >= ? AND quarter < ? GROUP BY bucket")
            params += [b15, b15, device_id, q_lo, q_hi]
    if not parts:
        return "", []
    return " UNION ALL ".join(parts), params


def mesh_parts(device_id: str, start: float, end: float, bucket: int, now: float,
               retention_days: float) -> Optional[Tuple[str, list]]:
    """(SQL, parameters) of a SELECT of (bucket, node_id, sum, n) rows for the mesh counts of the range, or None when
    raw covers it (read raw alone). The tier is mesh_rollup_15m: whole quarters, the start rounded out to its quarter."""
    boundary = raw_boundary(now, retention_days, QUARTER_SECONDS)
    if start >= boundary:
        return None
    b = max(1, int(bucket))
    parts: List[str] = []
    params: list = []
    if end >= boundary:
        parts.append("SELECT CAST(ts / ? AS INTEGER) * ? AS bucket, node_id, SUM(clients) AS s, COUNT(clients) AS n "
                     "FROM mesh_samples WHERE device_id = ? AND ts >= ? AND ts <= ? GROUP BY bucket, node_id")
        params += [b, b, device_id, boundary, end]
    first, last = int(start // QUARTER_SECONDS), min(int(end // QUARTER_SECONDS) + 1, boundary // QUARTER_SECONDS)
    if first < last:
        b15 = max(b, QUARTER_SECONDS)
        parts.append(f"SELECT (quarter * {QUARTER_SECONDS} / ?) * ? AS bucket, node_id, SUM(clients_sum) AS s, "
                     "SUM(clients_n) AS n FROM mesh_rollup_15m WHERE device_id = ? AND quarter >= ? AND quarter < ? "
                     "GROUP BY bucket, node_id")
        params += [b15, b15, device_id, first, last]
    if not parts:
        return "", []
    return " UNION ALL ".join(parts), params
