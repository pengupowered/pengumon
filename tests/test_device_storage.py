"""Device readings per device id (schema v9): the migration from the vendor tables, the compact DOCSIS encoding,
per-device isolation, retention, the WAN-rate queries and the size of a row."""

import json
import logging
import math
import random
import sqlite3
import time
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from pengumon.capabilities import Capability, DriverError, LineReading, PollResult, SystemReading, WanCounters
from pengumon.devices.settings import DeviceConfig
from pengumon.db import migrations
from pengumon.db.migrations import v009_device_tables
from pengumon.db.repository import DatabaseRepository
from pengumon.db.samples import DeviceSample, LineSample
from pengumon.devices.registry import build_devices
from pengumon.devices.samples import RouterHistory, device_sample, line_history, router_history, router_snapshot
from pengumon.lines import docsis
from pengumon.lines.docsis import DocsisHistory, decode_channels, encode_channels, line_sample, snapshot_from_sample
from pengumon.main import app
from pengumon.devices.snapshots import RouterMetricsSnapshot
from pengumon.lines.docsis_model import DocsisChannel, DocsisSignalSnapshot

DAY = 86400
# Measured on the live database before v9 (read only): router_metrics with its index, docsis_signals with its index.
BASELINE_ROUTER_BYTES_PER_ROW = 123
BASELINE_MODEM_BYTES_PER_ROW = 8200


def _query(path, sql, params=()):
    con = sqlite3.connect(path)
    try:
        return con.execute(sql, params).fetchall()
    finally:
        con.close()


def realistic_channels(rng: random.Random, recent: bool = True):
    """A modem's channels as the XB driver reads them: 32 SC-QAM downstream, 4 ATDMA upstream (no SNR)."""
    out = [DocsisChannel(channel_id=i + 1, direction="downstream", frequency_mhz=477.0 + 6 * i,
                         power_dbmv=round(rng.uniform(-2, 5), 1), snr_db=round(rng.uniform(36, 41), 1),
                         modulation="256QAM", correcteds=rng.randint(0, 9_000_000),
                         uncorrectables=rng.randint(0, 20_000),
                         uncorrectables_recent=rng.choice((0, 0, 0, 3)) if recent else 0)
           for i in range(32)]
    out += [DocsisChannel(channel_id=i + 1, direction="upstream", frequency_mhz=f,
                          power_dbmv=round(rng.uniform(40, 46), 1), modulation="ATDMA")
            for i, f in enumerate((16.4, 22.8, 29.2, 35.6))]
    return out


class Spans:
    """Stands in for the OpenTelemetry tracer: the attributes each span was given, by name."""

    def __init__(self):
        self.attributes = {}

    @contextmanager
    def start_as_current_span(self, name):
        attributes = self.attributes.setdefault(name, {})
        yield SimpleNamespace(set_attributes=attributes.update, set_attribute=attributes.__setitem__)


# --- a v8 database, and how the v8 build read it ----------------------------------------------------------------------

V8_ROUTER_COLUMNS = ("timestamp, wan_rx_bytes, wan_tx_bytes, wan_rx_rate_mbps, wan_tx_rate_mbps, cpu_usage_pct, "
                     "mem_usage_pct, client_count, model_name, status, error_kind")
V8_DOCSIS_COLUMNS = ("timestamp, downstream_power_avg, downstream_snr_avg, upstream_power_avg, uncorrectables_total, "
                     "uncorrectables_recent, status, channels_json, error_kind")


async def v8_database(path, monkeypatch, router_rows=(), docsis_rows=()):
    """A database at schema v8 holding the given raw rows (tuples in V8_*_COLUMNS order, plus t3/t4 for DOCSIS)."""
    with monkeypatch.context() as patch:  # (undo() would also drop conftest's patches)
        patch.setattr(migrations, "MIGRATIONS", migrations.MIGRATIONS[:8])
        assert await migrations.migrate(path) == 8
    con = sqlite3.connect(path)
    con.executemany(f"INSERT INTO router_metrics ({V8_ROUTER_COLUMNS}) VALUES ({', '.join('?' * 11)})", router_rows)
    con.executemany(f"INSERT INTO docsis_signals ({V8_DOCSIS_COLUMNS}, t3_count_today, t4_count_today) "
                    f"VALUES ({', '.join('?' * 11)})", docsis_rows)
    con.execute("INSERT INTO docsis_logs (timestamp, level, event_text) VALUES (1.0, 'Notice', 'never read')")
    con.commit()
    con.close()


def v8_router_snapshots(path, start=-math.inf, end=math.inf):
    """Frozen copy of the v8 build's get_router_snapshots_in_range (the shape the router history answered with)."""
    rows = _query(path, f"SELECT {V8_ROUTER_COLUMNS} FROM router_metrics WHERE timestamp >= ? AND timestamp <= ? "
                        "ORDER BY timestamp ASC", (start, end))
    return [RouterMetricsSnapshot(timestamp=r[0], wan_rx_bytes=r[1], wan_tx_bytes=r[2], wan_rx_rate_mbps=r[3],
                                  wan_tx_rate_mbps=r[4], cpu_usage_pct=r[5], mem_usage_pct=r[6], client_count=r[7],
                                  model_name=r[8], status=r[9] or "unconfigured", error_kind=r[10]) for r in rows]


def v8_docsis_snapshot(r):
    channels = []
    if r[7]:
        try:
            channels = [DocsisChannel(**c) for c in json.loads(r[7])]
        except Exception:
            pass
    return DocsisSignalSnapshot(timestamp=r[0], downstream_power_avg=r[1], downstream_snr_avg=r[2],
                                upstream_power_avg=r[3], uncorrectables_total=r[4] or 0,
                                uncorrectables_recent=r[5] or 0, status=r[6] or "unconfigured", channels=channels,
                                error_kind=r[8])


def v8_docsis_snapshots(path, start=-math.inf, end=math.inf):
    """Frozen copy of the v8 build's get_docsis_snapshots_in_range."""
    rows = _query(path, f"SELECT {V8_DOCSIS_COLUMNS} FROM docsis_signals WHERE timestamp >= ? AND timestamp <= ? "
                        "ORDER BY timestamp ASC", (start, end))
    return [v8_docsis_snapshot(r) for r in rows]


def v8_router_rates(path, start, end):
    return _query(path, "SELECT timestamp, wan_rx_rate_mbps, wan_tx_rate_mbps FROM router_metrics "
                        "WHERE timestamp >= ? AND timestamp <= ? AND wan_rx_rate_mbps IS NOT NULL "
                        "AND wan_tx_rate_mbps IS NOT NULL ORDER BY timestamp", (start, end))


def shapes(snapshots):
    """What a reader sees of a snapshot: its JSON (as the API sends it) and the stored-only error kind."""
    return [(s.model_dump_json(), s.error_kind) for s in snapshots]


def realistic_v8_rows(n_router=2000, n_modem=60, seed=11):
    """Rows as the builds before v9 wrote them: 10 s router polls with cumulative counters, unmeasured (NULL) rates,
    pre-step-1 fake 0.0 rates, error rows with and without an error kind, rows without a model or client count;
    15 min DOCSIS polls with 36 channels, error rows, rows from before per-channel recent counts, an unreadable
    channel list and an empty one."""
    rng = random.Random(seed)
    now = time.time()
    router, rx, tx = [], 3_512_345_678_901, 412_345_678_901
    for i in range(n_router):
        ts = now - (n_router - i) * 10 + rng.random() * 0.3
        if i % 97 == 0:
            router.append((ts, None, None, None, None, None, None, None, "Example_Router", "error",
                           "timeout" if i % 2 else None))
            continue
        rx += int(rng.expovariate(1 / 30e6))
        tx += int(rng.expovariate(1 / 3e6))
        if i < 50:  # an older build: 0.0 for a rate it could not measure, no client count, no model
            router.append((ts, rx, tx, 0.0, 0.0, 7.3, 41.0, None, None, "healthy", None))
            continue
        rates = (None, None) if i % 360 == 51 else (round(rng.uniform(0, 900), 2), round(rng.uniform(0, 40), 2))
        router.append((ts, rx, tx, *rates, round(rng.uniform(1, 95), 1), round(rng.uniform(38, 46), 1), 35,
                       "Example_Router", "warning" if i % 500 == 7 else "healthy", None))
    modem = []
    for k in range(n_modem):
        ts = now - (n_modem - k) * 900 + rng.random()
        channels = realistic_channels(rng)
        dumped = [c.model_dump() for c in channels]
        status, kind, total, recent = "healthy", None, sum(c.uncorrectables for c in channels), k % 3
        if k % 13 == 5:
            modem.append((ts, None, None, None, 0, 0, "error", "[]", "timeout" if k % 2 else None, 0, 0))
            continue
        if k == 1:
            for c in dumped:
                del c["uncorrectables_recent"]  # written before the per-channel recent count existed
        if k == 2:
            dumped = [{"channel_id": 1, "direction": "sideways"}]  # did not validate: read as no channels
        channels_json = "[]" if k == 3 else json.dumps(dumped)
        modem.append((ts, 1.23 + k / 100, 38.45, 43.1, total, None if k == 4 else recent, status, channels_json, kind,
                      k % 4, k % 2))
    return router, modem


# --- the migration ----------------------------------------------------------------------------------------------------

async def test_v9_migration_keeps_every_reading_as_the_api_showed_it(tmp_path, monkeypatch, caplog):
    path = str(tmp_path / "v8.db")
    router_rows, docsis_rows = realistic_v8_rows()
    await v8_database(path, monkeypatch, router_rows, docsis_rows)
    span_ts = sorted(r[0] for r in router_rows)
    window = (span_ts[300], span_ts[900])
    before = {
        "router": shapes(v8_router_snapshots(path)),
        "router_window": shapes(v8_router_snapshots(path, *window)),
        "docsis": shapes(v8_docsis_snapshots(path)),
        "rates": v8_router_rates(path, *window),
    }
    assert any(s.channels == [] for s in v8_docsis_snapshots(path))  # the scenario holds what it claims
    spans = Spans()
    monkeypatch.setattr(v009_device_tables, "tracer", spans)

    with caplog.at_level(logging.INFO, logger="pengumon.db"):
        assert await migrations.migrate(path) == migrations.LATEST_VERSION == 15

    repo = DatabaseRepository(db_path=path)
    await repo.initialize()
    router, modem = RouterHistory(repo, "router"), DocsisHistory(repo, "modem")
    assert shapes(await router.get_router_snapshots_in_range(-1e12, 1e12, padding_seconds=0)) == before["router"]
    assert shapes(await router.get_router_snapshots_in_range(*window, padding_seconds=0)) == before["router_window"]
    assert shapes(await modem.get_docsis_snapshots_in_range(-1e12, 1e12, padding_seconds=0)) == before["docsis"]
    assert shapes([await modem.get_latest_docsis_snapshot()]) == before["docsis"][-1:]
    assert await repo.get_wan_rates("router", *window) == before["rates"]

    names = {r[0] for r in _query(path, "SELECT name FROM sqlite_master")}
    assert {"device_samples", "line_samples", "idx_line_samples_device_ts"} <= names
    assert not {n for n in names if "router_metrics" in n or "docsis" in n}  # tables and their indexes
    assert _query(path, "PRAGMA freelist_count")[0][0] == 0  # the dropped tables' pages went back right away
    error_row = next(r for r in router_rows if r[9] == "error" and r[10])
    assert _query(path, "SELECT error_kind, model FROM device_samples WHERE ts = ?", (error_row[0],)) == [
        ("timeout", "Example_Router")]

    n_router, n_modem = len(router_rows), len(docsis_rows)
    assert spans.attributes["db.migrate.v9"] == {
        "db.copied.router_rows": n_router, "db.copied.modem_rows": n_modem, "db.skipped_rows": 0}
    (line,) = [r.getMessage() for r in caplog.records if "into the device tables" in r.getMessage()]
    assert line.startswith(f"Schema v9: copied {n_router:,} router rows and {n_modem:,} modem rows into the device "
                           "tables (")


async def test_v9_migration_keeps_the_first_of_two_rows_at_one_time_and_says_so(tmp_path, monkeypatch, caplog):
    path = str(tmp_path / "dup.db")
    await v8_database(path, monkeypatch, router_rows=[
        (1000.0, 1, 1, 5.0, 1.0, None, None, None, None, "healthy", None),
        (1000.0, 2, 2, 9.0, 9.0, None, None, None, None, "healthy", None),
        (1010.0, 3, 3, 6.0, 1.0, None, None, None, None, "healthy", None)])
    with caplog.at_level(logging.INFO, logger="pengumon.db"):
        await migrations.migrate(path)
    assert _query(path, "SELECT ts, wan_rx_mbps FROM device_samples ORDER BY ts") == [(1000.0, 5.0), (1010.0, 6.0)]
    assert "Schema v9: 1 router and 0 modem rows had the time of an earlier row and were not copied" in caplog.text


async def test_v9_migration_runs_once_and_a_fresh_database_starts_at_v9(tmp_path, monkeypatch):
    path = str(tmp_path / "v8.db")
    await v8_database(path, monkeypatch, *realistic_v8_rows(200, 5))
    assert await migrations.migrate(path) == migrations.LATEST_VERSION
    count = _query(path, "SELECT COUNT(*) FROM device_samples")[0][0]
    assert await migrations.migrate(path) == migrations.LATEST_VERSION and _query(path, "SELECT COUNT(*) FROM device_samples")[0][0] == count

    fresh = str(tmp_path / "fresh.db")
    assert await migrations.migrate(fresh) == migrations.LATEST_VERSION
    assert {r[0] for r in _query(fresh, "SELECT name FROM sqlite_master WHERE type = 'table'")} >= {
        "device_samples", "line_samples"}
    assert _query(fresh, "SELECT sql FROM sqlite_master WHERE name = 'device_samples'")[0][0].rstrip().endswith(
        "WITHOUT ROWID")


async def test_an_older_build_does_not_migrate_a_v9_database_back(tmp_path, monkeypatch, caplog):
    """What an older build's migrate() does (it cannot know v9): it warns and leaves the database alone. Its first
    retention run then fails on the missing tables (see the plan's v9 note): going back needs the backup."""
    path = str(tmp_path / "v10.db")
    latest = migrations.LATEST_VERSION
    assert await migrations.migrate(path) == latest
    monkeypatch.setattr(migrations, "MIGRATIONS", migrations.MIGRATIONS[:9])
    monkeypatch.setattr(migrations, "LATEST_VERSION", 9)
    with caplog.at_level(logging.WARNING, logger="pengumon.db"):
        assert await migrations.migrate(path) == latest
    assert f"Database schema v{latest} is newer than this build (v9); not migrating" in caplog.text
    assert not _query(path, "SELECT 1 FROM sqlite_master WHERE name = 'router_metrics'")


# --- the compact DOCSIS encoding --------------------------------------------------------------------------------------

def test_channels_round_trip_through_the_compact_encoding():
    channels = realistic_channels(random.Random(3))
    channels += [DocsisChannel(channel_id=99, direction="downstream", frequency_mhz=690.0, power_dbmv=-0.0, snr_db=None,
                               modulation=None, correcteds=None, uncorrectables=None, uncorrectables_recent=7)]
    detail = encode_channels(channels)
    assert decode_channels(detail) == channels
    assert [c.model_dump_json() for c in decode_channels(detail)] == [c.model_dump_json() for c in channels]  # -0.0
    assert encode_channels([]) is None and decode_channels(None) == []

    keyed = len(json.dumps([c.model_dump() for c in channels]))
    print(f"\nDOCSIS detail for {len(channels)} channels: {len(detail):,} B compact, {keyed:,} B keyed JSON "
          f"({len(detail) / keyed:.0%})")
    assert len(detail) < keyed * 0.3
    data = json.loads(detail)
    assert data["v"] == docsis.DETAIL_VERSION and len(data["ch"]) == len(channels) and " " not in detail


def test_a_snapshot_round_trips_through_its_line_sample():
    channels = realistic_channels(random.Random(4))
    snapshot = DocsisSignalSnapshot(timestamp=1000.5, downstream_power_avg=1.2, downstream_snr_avg=None,
                                    upstream_power_avg=43.0, uncorrectables_total=12345, uncorrectables_recent=6,
                                    channels=channels, status="warning")
    sample = line_sample("cable", snapshot)
    assert (sample.device_id, sample.technology, sample.errors_total, sample.errors_recent) == ("cable", "docsis",
                                                                                                12345, 6)
    assert json.loads(sample.summary) == {"ds_power": 1.2, "us_power": 43.0}
    assert snapshot_from_sample(sample) == snapshot

    error = DocsisSignalSnapshot(timestamp=5.0, status="error", error_message="timed out", error_kind="timeout")
    stored = line_sample("cable", error)
    assert (stored.errors_total, stored.errors_recent, stored.summary, stored.detail) == (None, None, None, None)
    assert stored.error_message == "timed out"
    # read back as stored rows always were: counts 0, no message (the history's shape is step 5's business)
    assert snapshot_from_sample(stored) == error.model_copy(update={"error_message": None})


def test_an_unreadable_detail_reads_as_no_channels_and_is_logged_once(caplog):
    with caplog.at_level(logging.WARNING, logger="pengumon.lines"):
        assert decode_channels('{"v":99,"ch":[]}') == []
        assert decode_channels('{"v":99,"ch":[]}') == []
        assert decode_channels('{"v":1,"ch":[[1,"d"]]}') == []
    messages = [r.getMessage() for r in caplog.records]
    assert len(messages) == 2 and "KeyError: 99" in messages[0] and "a channel with 2 values" in messages[1]


def test_values_after_the_known_fields_are_ignored(caplog):
    """Append-only: a newer build may add fields at the end of a v1 row; this build reads the fields it knows."""
    channels = realistic_channels(random.Random(5))[:3]
    data = json.loads(encode_channels(channels))
    data["ch"] = [row + ["a later field", {"x": 1}] for row in data["ch"]]
    with caplog.at_level(logging.WARNING, logger="pengumon.lines"):
        assert decode_channels(json.dumps(data)) == channels
    assert caplog.records == []


def test_a_router_snapshot_round_trips_through_its_device_sample():
    snapshot = RouterMetricsSnapshot(timestamp=10.0, wan_rx_bytes=5, wan_tx_bytes=6, wan_rx_rate_mbps=1.5,
                                     wan_tx_rate_mbps=None, cpu_usage_pct=3.0, mem_usage_pct=40.0, client_count=35,
                                     model_name="Example-Mesh", status="healthy")
    sample = device_sample("wifi", snapshot)
    assert (sample.device_id, sample.wan_rx_mbps, sample.wan_tx_mbps, sample.model) == ("wifi", 1.5, None, "Example-Mesh")
    assert router_snapshot(sample) == snapshot
    # the error message is not stored: the history leaves it null
    live = snapshot.model_copy(update={"error_message": "x"})
    assert router_snapshot(device_sample("wifi", live)) == snapshot


# --- per device -------------------------------------------------------------------------------------------------------

async def test_two_routers_store_and_read_independently(repo):
    registry = build_devices([DeviceConfig(id="upstairs", driver="asuswrt", url="http://192.168.1.1"),
                              DeviceConfig(id="downstairs", driver="asuswrt", url="http://192.168.2.1")], repo)
    up, down = registry.get("upstairs").monitor, registry.get("downstairs").monitor
    for i, ts in enumerate((1000.0, 1010.0, 1020.0)):
        await up.on_result(ts, PollResult({Capability.WAN_COUNTERS: WanCounters(i * 12_500_000, i * 1_250_000),
                                           Capability.ROUTER_SYSTEM: SystemReading(10.0, 50.0, 20)}))
        await down.on_result(ts + 0.5, PollResult({Capability.WAN_COUNTERS: WanCounters(i * 2_500_000, 0),
                                                   Capability.ROUTER_SYSTEM: SystemReading(70.0, 30.0, 3)}))
    await down.on_error(1030.0, DriverError("timeout", "timed out", transport=True))

    assert await repo.get_wan_rates("upstairs", 0, 2000) == [(1010.0, 10.0, 1.0), (1020.0, 10.0, 1.0)]
    assert await repo.get_wan_rates("downstairs", 0, 2000) == [(1010.5, 2.0, 0.0), (1020.5, 2.0, 0.0)]
    assert await repo.get_wan_rate_percentile("upstairs", 0, 1.0) == (2, 10.0, 1.0)
    assert await repo.get_wan_rate_percentile("downstairs", 0, 1.0) == (2, 2.0, 0.0)
    ups = await RouterHistory(repo, "upstairs").get_router_snapshots_in_range(0, 2000)
    downs = await RouterHistory(repo, "downstairs").get_router_snapshots_in_range(0, 2000)
    assert [s.client_count for s in ups] == [20, 20, 20]
    assert [(s.client_count, s.status) for s in downs] == [(3, "healthy")] * 3 + [(None, "error")]


def _ungraded(snapshot):
    """A live snapshot as it is stored: the grades are decided when a reading is taken, not stored."""
    return snapshot.model_copy(update={"grades": {}, "uncorrectables_interval_seconds": None,
                                        "uncorrectables_ratio": None, "errors_affected_channels": 0,
                                        "errors_span_mhz": None, "errors_recurring": False, "errors_note": None,
                                        "channels": [c.model_copy(update={"grades": {}})
                                                                  for c in snapshot.channels]})


async def test_two_modems_store_and_read_independently(repo):
    registry = build_devices([DeviceConfig(id="cable", driver="xfinity_xb", url="http://192.168.100.1"),
                              DeviceConfig(id="backup", driver="xfinity_xb", url="http://10.0.1.1")], repo)
    rng = random.Random(5)
    for ts in (1000.0, 1900.0):
        for device_id in ("cable", "backup"):
            channels = tuple(realistic_channels(rng, recent=False))
            await registry.get(device_id).monitor.on_result(
                ts, PollResult({Capability.ACCESS_LINE: LineReading("docsis", channels)}))
    for device_id in ("cable", "backup"):
        stored = await DocsisHistory(repo, device_id).get_docsis_snapshots_in_range(0, 3000)
        live = registry.get(device_id).monitor.latest_snapshot
        assert len(stored) == 2 and stored[-1] == _ungraded(live).model_copy(update={"uncorrectables_rate_per_sec": 0.0})
    cable = await DocsisHistory(repo, "cable").get_latest_docsis_snapshot()
    backup = await DocsisHistory(repo, "backup").get_latest_docsis_snapshot()
    assert cable.channels != backup.channels


async def test_a_line_history_skips_rows_of_another_technology(repo):
    await repo.insert_line_sample(LineSample(device_id="m", ts=1.0, technology="docsis", status="healthy"))
    await repo.insert_line_sample(LineSample(device_id="m", ts=2.0, technology="vdsl", status="healthy",
                                             summary='{"snr_margin":6}'))
    history = DocsisHistory(repo, "m")
    assert [s.timestamp for s in await history.get_docsis_snapshots_in_range(0, 3)] == [1.0]
    assert await history.get_latest_docsis_snapshot() is None  # the latest row is not a DOCSIS reading
    assert (await repo.get_latest_line_sample("m")).summary == '{"snr_margin":6}'  # stored as given


async def test_one_poll_per_device_and_time(repo):
    sample = DeviceSample(device_id="r", ts=1.0, status="healthy")
    await repo.insert_device_sample(sample)
    await repo.insert_device_sample(DeviceSample(device_id="other", ts=1.0, status="healthy"))
    with pytest.raises(sqlite3.IntegrityError):
        await repo.insert_device_sample(sample)


async def test_ranges_are_inclusive_and_padded(repo):
    for ts in (100.0, 200.0, 300.0):
        await repo.insert_device_sample(DeviceSample(device_id="r", ts=ts, status="healthy"))
        await repo.insert_line_sample(LineSample(device_id="m", ts=ts, technology="docsis", status="healthy"))
    assert [s.ts for s in await repo.get_device_samples("r", 100.0, 200.0)] == [100.0, 200.0]
    assert [s.ts for s in await repo.get_device_samples("r", 150.0, 250.0, padding_seconds=50.0)] == [100.0, 200.0,
                                                                                                        300.0]
    assert [s.ts for s in await repo.get_line_samples("m", 200.0, 300.0)] == [200.0, 300.0]
    assert await repo.get_line_samples("r", 0, 400) == [] and await repo.get_device_samples("m", 0, 400) == []


# --- retention and the WAN-rate queries -------------------------------------------------------------------------------

async def test_retention_prunes_both_tables_for_every_device(repo):
    now = time.time()
    for age_days in (40, 8, 1, 0):
        ts = now - age_days * DAY
        for device_id in ("a", "b"):
            await repo.insert_device_sample(DeviceSample(device_id=device_id, ts=ts, status="healthy"))
            await repo.insert_line_sample(LineSample(device_id=device_id, ts=ts, technology="docsis",
                                                     status="healthy", detail='{"v":1,"ch":[]}'))
    removed = await repo.cleanup_old_samples(retention_days=7)
    assert (removed["device_samples"], removed["line_samples"]) == (4, 4)
    assert len(await repo.get_device_samples("a", 0, now + 1)) == 2
    assert len(await repo.get_line_samples("b", 0, now + 1)) == 2


def _nearest_rank(values, fraction):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, math.ceil(fraction * len(ordered)) - 1))]


async def test_the_rate_percentile_is_the_nearest_rank_from_either_end(repo):
    rng = random.Random(9)
    rows = []
    for i in range(1500):
        measured = i % 50 != 0
        rx = round(rng.choice((rng.uniform(0, 900), 100.0)), 2) if measured else None  # ties included
        tx = round(rng.uniform(0, 40), 2) if measured else None
        rows.append((1000.0 + i * 10, rx, tx))
        await repo.insert_device_sample(DeviceSample(device_id="r", ts=1000.0 + i * 10, status="healthy",
                                                     wan_rx_mbps=rx, wan_tx_mbps=tx))
    await repo.insert_device_sample(DeviceSample(device_id="noise", ts=5000.0, status="healthy", wan_rx_mbps=1e6,
                                                 wan_tx_mbps=1e6))
    measured_rows = [r for r in rows if r[1] is not None]
    for since in (0.0, 9000.0):
        subset = [r for r in measured_rows if r[0] >= since]
        for fraction in (0.0, 0.001, 0.25, 0.5, 0.501, 0.75, 0.999, 1.0):
            assert await repo.get_wan_rate_percentile("r", since, fraction) == (
                len(subset), _nearest_rank([r[1] for r in subset], fraction),
                _nearest_rank([r[2] for r in subset], fraction)), (since, fraction)
    assert await repo.get_wan_rates("r", 1000.0, 1100.0) == [r for r in measured_rows if r[0] <= 1100.0]


# --- size -------------------------------------------------------------------------------------------------------------

def _bytes(path, *names):
    con = sqlite3.connect(path)
    try:
        try:
            con.execute("SELECT 1 FROM dbstat LIMIT 1")
        except sqlite3.OperationalError:
            pytest.skip("this SQLite build has no dbstat table (SQLITE_ENABLE_DBSTAT_VTAB; e.g. Python on Windows)")
        return sum(con.execute("SELECT COALESCE(SUM(pgsize), 0) FROM dbstat WHERE name = ?", (n,)).fetchone()[0]
                   for n in names)
    finally:
        con.close()


async def test_a_row_takes_far_less_room_than_before(repo):
    """A soft check against the bytes per row measured before v9 (the numbers are printed with -s)."""
    router_rows, docsis_rows = realistic_v8_rows(n_router=8640, n_modem=96, seed=21)  # one day
    for r in router_rows:
        await repo.insert_device_sample(device_sample("router", RouterMetricsSnapshot(
            timestamp=r[0], wan_rx_bytes=r[1], wan_tx_bytes=r[2], wan_rx_rate_mbps=r[3], wan_tx_rate_mbps=r[4],
            cpu_usage_pct=r[5], mem_usage_pct=r[6], client_count=r[7], model_name=r[8], status=r[9], error_kind=r[10])))
    for r in docsis_rows:
        await repo.insert_line_sample(line_sample("modem", v8_docsis_snapshot(r)))
    await repo.close()
    device = _bytes(repo.db_path, "device_samples") / len(router_rows)
    line = _bytes(repo.db_path, "line_samples", "idx_line_samples_device_ts") / len(docsis_rows)
    print(f"\ndevice_samples: {device:.0f} B per row (was {BASELINE_ROUTER_BYTES_PER_ROW}); "
          f"line_samples: {line:,.0f} B per row (was {BASELINE_MODEM_BYTES_PER_ROW:,})")
    assert device < BASELINE_ROUTER_BYTES_PER_ROW * 0.95
    assert line < BASELINE_MODEM_BYTES_PER_ROW * 0.4


# --- which device the views read ------------------------------------------------------------------------------------

async def test_the_views_read_the_primary_devices_history(repo, app_state):
    registry = build_devices([DeviceConfig(id="old", driver="xfinity_xb", url="http://10.0.9.1", enabled=False),
                              DeviceConfig(id="cable", driver="xfinity_xb", url="http://192.168.100.1"),
                              DeviceConfig(id="wifi", driver="asuswrt", url="http://192.168.1.1")], repo)
    app_state.repository, app_state.devices = repo, registry
    app_state.config = SimpleNamespace(targets=[], dns_probes=[])
    assert (line_history(registry, repo).device_id, router_history(registry, repo).device_id) == ("cable", "wifi")
    now = time.time()
    await repo.insert_line_sample(line_sample("old", DocsisSignalSnapshot(timestamp=now - 60, downstream_snr_avg=20.0,
                                                                          status="critical")))
    await repo.insert_line_sample(line_sample("cable", DocsisSignalSnapshot(timestamp=now - 60, downstream_snr_avg=38.5,
                                                                            status="healthy")))
    await repo.insert_device_sample(DeviceSample(device_id="wifi", ts=now - 30, status="healthy", wan_rx_mbps=5.0))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        dossier = (await ac.get("/api/incidents/dossier")).json()
        history = (await ac.get(f"/api/devices/wifi/history?start_time={now - 100}&end_time={now}")).json()
    assert dossier["docsis"]["downstream_snr_avg"] == 38.5  # the enabled modem's, not the disabled one's
    assert [h["wan_rx_rate_mbps"] for h in history] == [5.0]

    assert line_history(None, repo) is None and router_history(None, repo) is None
    assert router_history(build_devices([], repo), repo) is None


async def test_the_converted_config_yaml_shows_the_history_stored_under_modem_and_router(repo, app_state):
    """End to end: rows under the ids the v9 migration gave a user's history, plus config.example.yaml as read
    from the file, give the inspector, the dossier and /api/devices/router/history those rows."""
    from pathlib import Path

    from pengumon.config import load_config
    from pengumon.incidents.model import Incident

    from pengumon.devices.settings import DeviceConfig

    config = load_config(str(Path(__file__).resolve().parents[1] / "config.example.yaml"))
    config.devices = [  # what the example's (commented-out) modem and router describe
        DeviceConfig(id="modem", driver="xfinity_xb", url="http://192.168.100.1", poll_interval_seconds=900),
        DeviceConfig(id="router", driver="asuswrt", url="http://192.168.1.1", poll_interval_seconds=10)]
    registry = build_devices(config.devices, repo)  # no credentials and no polling: only the stored rows
    app_state.repository, app_state.devices, app_state.config = repo, registry, config
    now = time.time()
    channels = realistic_channels(random.Random(6))
    await repo.insert_line_sample(line_sample("modem", DocsisSignalSnapshot(
        timestamp=now - 600, downstream_power_avg=2.5, downstream_snr_avg=38.7, upstream_power_avg=44.0,
        uncorrectables_total=777, channels=channels, status="healthy")))
    for i in range(3):
        await repo.insert_device_sample(DeviceSample(
            device_id="router", ts=now - 300 + 10 * i, status="healthy", wan_rx_mbps=100.0 + i, wan_tx_mbps=5.0,
            cpu_pct=7.5, mem_pct=40.0, client_count=31, model="Example_Router"))
    incident_id = await repo.create_incident(Incident(
        start_time=now - 290, end_time=now - 280, duration_seconds=10.0, cause_code="access_line_drop",
        title="ISP Line Packet Loss", description="d", severity="warning", resolved=True, kind="isp_loss"))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        history = (await ac.get(f"/api/devices/router/history?start_time={now - 400}&end_time={now}")).json()
        inspection = (await ac.get(f"/api/incidents/{incident_id}/inspect")).json()
        dossier = (await ac.get("/api/incidents/dossier")).json()

    assert [(h["wan_rx_rate_mbps"], h["model_name"], h["client_count"]) for h in history] == [
        (100.0, "Example_Router", 31), (101.0, "Example_Router", 31), (102.0, "Example_Router", 31)]
    assert [s["wan_rx_rate_mbps"] for s in inspection["router_snapshots"]] == [100.0, 101.0, 102.0]
    # The modem row is outside the listed window but within one poll interval (900 s, from config.yaml) of it
    assert inspection["docsis_snapshots"] == []
    nearest = inspection["docsis_correlation"]["nearest_snapshot"]
    assert (nearest["downstream_snr_avg"], nearest["uncorrectables_total"], len(nearest["channels"])) == (
        38.7, 777, len(channels))
    assert dossier["docsis"]["downstream_snr_avg"] == 38.7
    assert dossier["docsis"]["uncorrectables_total"] == 777

    await repo.insert_line_sample(line_sample("modem", DocsisSignalSnapshot(
        timestamp=now - 285, downstream_snr_avg=31.0, uncorrectables_total=900, channels=channels, status="warning")))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        inspection = (await ac.get(f"/api/incidents/{incident_id}/inspect")).json()
    assert [(r["timestamp"], r["downstream_snr_avg"]) for r in inspection["docsis_snapshots"]] == [(now - 285, 31.0)]
