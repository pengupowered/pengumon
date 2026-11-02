"""v9: device readings keyed by device id (device_samples, line_samples)."""

import json
import logging
import time
from dataclasses import astuple

import aiosqlite

from pengumon.db.migrations.common import scalar, table_exists
from pengumon.db.schema import (
    CREATE_DEVICE_SAMPLES_TABLE,
    CREATE_LINE_SAMPLES_TABLE,
    CREATE_LINE_SAMPLES_INDEX,
)
from pengumon.db.samples import LINE_SAMPLE_COLUMNS
from pengumon.lines import docsis
from pengumon.lines.docsis_model import DocsisChannel, DocsisSignalSnapshot
from pengumon.observability.telemetry import tracer

logger = logging.getLogger("pengumon.db")

VERSION = 9
LINE_COPY_CHUNK_ROWS = 1000  # DOCSIS rows are about 8 KB each before v9: a chunk is a few MB in memory
# The device ids of the modem and the router of the builds before `devices:`: v9 stores the rows of the tables it
# replaces under them (so config.yaml keeps these ids), and pengumon.secrets moves their flat secrets.json keys to them.
LEGACY_MODEM_ID = "modem"
LEGACY_ROUTER_ID = "router"


def _legacy_docsis_snapshot(row) -> DocsisSignalSnapshot:
    """A docsis_signals row as the v8 build read it (a frozen copy of its reader): channels that do not validate give
    none, a missing count reads 0, a missing status "unconfigured". Built without validation, so a row the v8 reader
    would have refused (an unknown status) is copied as it is instead of stopping the migration."""
    ts, ds_power, ds_snr, us_power, total, recent, status, channels_json, error_kind = row
    channels = []
    if channels_json:
        try:
            channels = [DocsisChannel(**c) for c in json.loads(channels_json)]
        except Exception:
            pass
    return DocsisSignalSnapshot.model_construct(
        timestamp=ts, downstream_power_avg=ds_power, downstream_snr_avg=ds_snr, upstream_power_avg=us_power,
        uncorrectables_total=total or 0, uncorrectables_recent=recent or 0, status=status or "unconfigured",
        channels=channels, error_message=None, error_kind=error_kind)


async def migrate(db: aiosqlite.Connection):
    """Device readings keyed by device id: router_metrics -> device_samples (device "router"), docsis_signals ->
    line_samples (device "modem", technology "docsis", channels re-encoded compactly by pengumon.lines.docsis, the
    same conversion the modem's monitor stores with). docsis_logs (never written since the event log went) and the
    T3/T4 and event_log_ok columns go with their tables.

    The old tables are dropped, so an older build cannot run against v9 (its retention fails on the missing tables at
    startup) and rows it wrote would have nowhere to go: going back needs the pre-deploy backup."""
    with tracer.start_as_current_span("db.migrate.v9") as span:
        started = time.monotonic()
        for stmt in (CREATE_DEVICE_SAMPLES_TABLE, CREATE_LINE_SAMPLES_TABLE, CREATE_LINE_SAMPLES_INDEX):
            await db.execute(stmt)

        router_rows = router_copied = 0
        if await table_exists(db, "router_metrics"):
            router_rows = await scalar(db, "SELECT COUNT(*) FROM router_metrics")
            # In key order, so the clustered table is appended to; a second row at the same time keeps the first.
            cursor = await db.execute(
                """
                INSERT OR IGNORE INTO device_samples
                    (device_id, ts, status, error_kind, wan_rx_bytes, wan_tx_bytes, wan_rx_mbps, wan_tx_mbps,
                     cpu_pct, mem_pct, client_count, model)
                SELECT ?, timestamp, status, error_kind, wan_rx_bytes, wan_tx_bytes, wan_rx_rate_mbps,
                       wan_tx_rate_mbps, cpu_usage_pct, mem_usage_pct, client_count, model_name
                FROM router_metrics
                ORDER BY timestamp, id
                """,
                (LEGACY_ROUTER_ID,),
            )
            router_copied = max(cursor.rowcount, 0)
            await cursor.close()

        modem_rows = modem_copied = 0
        if await table_exists(db, "docsis_signals"):
            insert = (f"INSERT OR IGNORE INTO line_samples ({', '.join(LINE_SAMPLE_COLUMNS)}) "
                      f"VALUES ({', '.join('?' for _ in LINE_SAMPLE_COLUMNS)})")
            last_id = -1
            while True:
                async with db.execute(
                    """
                    SELECT id, timestamp, downstream_power_avg, downstream_snr_avg, upstream_power_avg,
                           uncorrectables_total, uncorrectables_recent, status, channels_json, error_kind
                    FROM docsis_signals WHERE id > ? ORDER BY id LIMIT ?
                    """,
                    (last_id, LINE_COPY_CHUNK_ROWS),
                ) as cursor:
                    rows = await cursor.fetchall()
                if not rows:
                    break
                last_id = rows[-1][0]
                modem_rows += len(rows)
                samples = [docsis.line_sample(LEGACY_MODEM_ID, _legacy_docsis_snapshot(r[1:])) for r in rows]
                cursor = await db.executemany(insert, [astuple(s) for s in samples])
                modem_copied += max(cursor.rowcount, 0)
                await cursor.close()

        for table in ("router_metrics", "docsis_signals", "docsis_logs"):
            await db.execute(f"DROP TABLE IF EXISTS {table}")  # their indexes go with them

        seconds = time.monotonic() - started
        span.set_attributes({"db.copied.router_rows": router_copied, "db.copied.modem_rows": modem_copied,
                             "db.skipped_rows": router_rows - router_copied + modem_rows - modem_copied})
        logger.info(f"Schema v9: copied {router_copied:,} router rows and {modem_copied:,} modem rows into the device "
                    f"tables ({seconds:.1f} s)")
        if router_copied < router_rows or modem_copied < modem_rows:
            logger.warning(f"Schema v9: {router_rows - router_copied:,} router and {modem_rows - modem_copied:,} modem "
                           "rows had the time of an earlier row and were not copied")
