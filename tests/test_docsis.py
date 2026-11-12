"""DOCSIS modem device: signal parsing, status thresholds, no event-log request, snapshot storage."""

import sqlite3
import time
from types import SimpleNamespace

import httpx
from httpx import ASGITransport, AsyncClient

from pengumon.db import migrations
from pengumon.db.repository import DatabaseRepository
from pengumon.db.schema import CREATE_DOCSIS_LOGS_INDEX, CREATE_DOCSIS_LOGS_TABLE, CREATE_DOCSIS_SIGNALS_TABLE
from pengumon.devices.registry import DeviceRegistry
from pengumon.lines.docsis import DocsisHistory, line_sample
from pengumon.main import app
from pengumon.incidents.model import Incident
from pengumon.lines.docsis_model import DocsisSignalSnapshot
from pengumon.reports.dossier import build_dossier

from device_helpers import build_modem


def _status_page(snrs=("38.0", "37.5")):
    """The gateway's horizontal channel tables: two downstream channels, one upstream."""
    ids = "".join(f"<td>{i + 1}</td>" for i in range(len(snrs)))
    pwr = "".join("<td>1.0 dBmV</td>" for _ in snrs)
    snr = "".join(f"<td>{s} dB</td>" for s in snrs if s)
    freq = "".join("<td>600000000 Hz</td>" for _ in snrs)
    return f"""<html><table>
    <tr><th class="row-label">Downstream</th></tr>
    <tr><th class="row-label">Channel ID</th>{ids}</tr>
    <tr><th class="row-label">Frequency</th>{freq}</tr>
    <tr><th class="row-label">Power Level</th>{pwr}</tr>
    <tr><th class="row-label">SNR</th>{snr}</tr>
    <tr><th class="row-label">Upstream</th></tr>
    <tr><th class="row-label">Channel ID</th><td>9</td></tr>
    <tr><th class="row-label">Power Level</th><td>42.0 dBmV</td></tr>
    </table></html>"""


def _collector(page=None, **config):
    """A collector whose gateway is a mock transport; `collector.requests` lists the paths it was asked for.
    The event-log endpoint answers with T3/T4 events, which must never be asked for."""
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.path.endswith("network_setup.jst"):
            return httpx.Response(200, text=page or _status_page())
        return httpx.Response(200, json=[{"time": "10/15/2026 21:30:00", "Level": "Critical",
                                          "Des": "Started Unicast Maintenance Ranging - No Response received - T3 time-out"},
                                         {"time": "10/15/2026 21:31:00", "Level": "Critical",
                                          "Des": "Received Response to Broadcast Maintenance Request, But no Unicast "
                                                 "Maintenance opportunities received - T4 time-out"}])

    collector = build_modem(password="pw", **config, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    collector.requests = requests
    return collector


# --- no event-log read ---------------------------------------------------------------------------

async def test_collector_never_requests_the_event_log():
    collector = _collector()
    snap = await collector.poll(force=True)
    assert collector.requests == ["/check.jst", "/network_setup.jst"]  # a login, the status page, nothing else
    assert snap.status == "healthy" and len(snap.channels) == 3


async def test_status_rests_on_signal_levels_not_on_events():
    # The gateway would report T3/T4 events; status is decided by power and SNR alone.
    assert (await _collector().poll(force=True)).status == "healthy"
    assert (await _collector(page=_status_page(snrs=("32.0", "32.0"))).poll(force=True)).status == "warning"
    assert (await _collector(page=_status_page(snrs=("29.0", "29.0"))).poll(force=True)).status == "critical"


async def test_snapshot_has_no_event_log_fields():
    snap = await _collector().poll(force=True)
    assert not {"t3_count_today", "t4_count_today", "event_log_ok", "recent_events"} & set(snap.model_dump())


async def test_signal_snapshot_round_trips_through_the_database(tmp_path):
    repo = DatabaseRepository(db_path=str(tmp_path / "t.db"))
    await repo.initialize()
    await repo.insert_line_sample(line_sample("modem", DocsisSignalSnapshot(
        timestamp=1000.0, downstream_power_avg=1.0, downstream_snr_avg=38.0, upstream_power_avg=42.0,
        uncorrectables_total=7, status="healthy")))
    history = DocsisHistory(repo, "modem")
    latest = await history.get_latest_docsis_snapshot()
    assert (latest.downstream_snr_avg, latest.uncorrectables_total, latest.status) == (38.0, 7, "healthy")
    assert [s.timestamp for s in await history.get_docsis_snapshots_in_range(0, 4000, padding_seconds=0)] == [1000.0]
    # schema v9 dropped the event-log columns with their table
    with sqlite3.connect(str(tmp_path / "t.db")) as con:
        columns = {r[1] for r in con.execute("PRAGMA table_info(line_samples)")}
        assert columns and not {"t3_count_today", "t4_count_today", "event_log_ok"} & columns
        assert not con.execute("SELECT 1 FROM sqlite_master WHERE name = 'docsis_signals'").fetchall()


# --- dossier and incident inspector --------------------------------------------------------------

def _cfg():
    return SimpleNamespace(
        targets=[SimpleNamespace(id="router", name="Router", address="192.168.1.1", category="local")],
        dns_probes=[], classifier=SimpleNamespace(busy_load_pct=60.0),
    )


async def _repo_with_snapshot(tmp_path):
    repo = DatabaseRepository(db_path=str(tmp_path / "t.db"))
    await repo.initialize()
    await repo.insert_line_sample(line_sample("modem", DocsisSignalSnapshot(
        timestamp=time.time(), downstream_power_avg=1.0, downstream_snr_avg=38.0, upstream_power_avg=42.0,
        status="healthy")))
    return repo


async def test_dossier_reports_signal_levels_without_event_log_lines(tmp_path):
    repo = await _repo_with_snapshot(tmp_path)
    md = (await build_dossier(repo, _cfg(), 24, line=DocsisHistory(repo, "modem")))["formatted_markdown"]
    assert "Downstream average SNR: 38.0 dB" in md
    assert "The modem shows no RF impairment in the window" in md
    assert "T3" not in md and "T4" not in md and "event log" not in md and "could not be read" not in md


async def test_incident_inspector_carries_no_event_log_fields(tmp_path, app_state):
    repo = await _repo_with_snapshot(tmp_path)
    now = time.time()
    iid = await repo.create_incident(Incident(
        start_time=now - 30, cause_code="unknown_outage", title="t", description="d", severity="warning"))
    app_state.repository = repo
    app_state.devices = DeviceRegistry([build_modem(password="pw")])
    app_state.config = SimpleNamespace(targets=[], dns_probes=[],
                                       docsis=SimpleNamespace(poll_interval_seconds=900.0))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        res = await ac.get(f"/api/incidents/{iid}/inspect")
    assert res.status_code == 200
    corr = res.json()["docsis_correlation"]
    assert not {"t3_count", "t4_count", "event_log_ok", "events_in_window"} & set(corr)
    assert not {"t3_count_today", "t4_count_today", "event_log_ok"} & set(corr["nearest_snapshot"])


# --- SNR average ---------------------------------------------------------------------------------

async def test_snr_average_ignores_channels_without_snr():
    collector = _collector(page=_status_page(snrs=("36.0", "")))
    snap = await collector.poll(force=True)
    ds = [c for c in snap.channels if c.direction == "downstream"]
    assert [c.snr_db for c in ds] == [36.0, None]
    assert snap.downstream_snr_avg == 36.0  # not 18.0, which would also flip the status to critical
    assert snap.status == "healthy"


async def test_snr_average_is_none_when_no_channel_reports_one():
    collector = _collector(page=_status_page(snrs=("", "")))
    snap = await collector.poll(force=True)
    assert snap.downstream_snr_avg is None


# --- migrations (v5 cleaned the event-log table up; v9 dropped it, with the event-log columns) -----------------

async def test_migration_removes_existing_duplicate_log_rows(tmp_path, monkeypatch):
    path = str(tmp_path / "dups.db")
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")
    for stmt in (CREATE_DOCSIS_SIGNALS_TABLE, CREATE_DOCSIS_LOGS_TABLE, CREATE_DOCSIS_LOGS_INDEX):
        con.execute(stmt)
    for _ in range(3):  # the same two events inserted by three polls
        con.executemany("INSERT INTO docsis_logs (timestamp, level, event_code, event_text) VALUES (?, ?, ?, ?)",
                        [(1000.0, "Critical", "T3", "a"), (1001.0, "Notice", None, "b")])
    con.commit()
    con.close()

    with monkeypatch.context() as patch:
        patch.setattr(migrations, "MIGRATIONS", migrations.MIGRATIONS[:8])  # the last schema with the table
        assert await migrations.migrate(path) == 8

    con = sqlite3.connect(path)
    try:
        assert con.execute("SELECT timestamp, event_text, id FROM docsis_logs ORDER BY id").fetchall() == [
            (1000.0, "a", 1), (1001.0, "b", 2)]  # the lowest id of each survives
        assert "event_log_ok" in {r[1] for r in con.execute("PRAGMA table_info(docsis_signals)")}
        assert con.execute("SELECT sql FROM sqlite_master WHERE name = 'idx_docsis_logs_unique'").fetchone()
    finally:
        con.close()

    assert await migrations.migrate(path) == migrations.LATEST_VERSION
    con = sqlite3.connect(path)
    try:
        names = {r[0] for r in con.execute("SELECT name FROM sqlite_master")}
        assert not {"docsis_logs", "idx_docsis_logs_unique", "idx_docsis_logs_time", "docsis_signals"} & names
    finally:
        con.close()


# --- error tracker -------------------------------------------------------------------------------------------

def _tracked(tracker, ts, uncorrectables):
    from pengumon.lines.docsis import summarize
    from pengumon.lines.docsis_model import DocsisChannel
    channels = [DocsisChannel(channel_id=1, direction="downstream", frequency_mhz=600.0, power_dbmv=1.0,
                              snr_db=38.0, uncorrectables=uncorrectables)]
    return tracker.apply(summarize(channels, ts))


def test_counters_that_go_backwards_after_a_modem_reboot_give_no_recent_errors_not_a_negative_number():
    from pengumon.lines.docsis import DocsisErrorTracker
    tracker = DocsisErrorTracker()
    _tracked(tracker, 1000.0, 5000)
    rebooted = _tracked(tracker, 1010.0, 12)
    assert rebooted.uncorrectables_recent == 0 and rebooted.uncorrectables_rate_per_sec == 0.0
    assert rebooted.channels[0].uncorrectables_recent == 0 and rebooted.status == "healthy"
    after = _tracked(tracker, 1020.0, 20)  # the new counters are the baseline from here on
    assert after.uncorrectables_recent == 8 and after.channels[0].uncorrectables_recent == 8


def test_upstream_channels_with_the_ids_of_downstream_ones_do_not_reset_their_error_baselines():
    """Downstream and upstream channel ids are separate number spaces: a modem has downstream 1-32 and upstream 1-5.
    The upstream rows (no codewords) must not become the baseline of the downstream channels with the same id."""
    from pengumon.lines.docsis import DocsisErrorTracker, summarize
    from pengumon.lines.docsis_model import DocsisChannel

    def read(ts, ds1, ds2):
        channels = [DocsisChannel(channel_id=1, direction="downstream", frequency_mhz=600.0, power_dbmv=1.0,
                                  snr_db=38.0, uncorrectables=ds1, unerrored=10**6),
                    DocsisChannel(channel_id=2, direction="downstream", frequency_mhz=606.0, power_dbmv=1.0,
                                  snr_db=38.0, uncorrectables=ds2, unerrored=10**6),
                    DocsisChannel(channel_id=1, direction="upstream", frequency_mhz=17.0, power_dbmv=44.0)]
        return summarize(channels, ts)

    for seeded in (False, True):
        tracker = DocsisErrorTracker()
        first = read(1000.0, 1000, 0)
        if seeded:  # (the baseline from before a restart)
            tracker.seed(tracker.apply(first))
        else:
            tracker.apply(first)
        snap = tracker.apply(read(1010.0, 1000, 5))  # five new on channel 2, none on channel 1
        assert [c.uncorrectables_recent for c in snap.channels] == [0, 5, 0]
        assert snap.uncorrectables_recent == 5 and snap.errors_affected_channels == 1
        assert snap.errors_span_mhz == [606.0, 606.0]


def _codeword_page(with_unerrored):
    extra = '<tr><th class="row-label">Unerrored Codewords</th><td>1000</td><td>2000</td></tr>' if with_unerrored else ""
    return f"""<html><table>
    <tr><th class="row-label">Downstream</th></tr>
    <tr><th class="row-label">Channel ID</th><td>1</td><td>2</td></tr>
    <tr><th class="row-label">Frequency</th><td>600000000 Hz</td><td>606000000 Hz</td></tr>
    <tr><th class="row-label">Power Level</th><td>1.0 dBmV</td><td>1.0 dBmV</td></tr>
    <tr><th class="row-label">SNR</th><td>38 dB</td><td>38 dB</td></tr>
    <tr><th class="row-label">CM Error Codewords</th></tr>
    <tr><th class="row-label">Channel ID</th><td>1</td><td>2</td></tr>
    <tr><th class="row-label">Correctable Codewords</th><td>5</td><td>6</td></tr>
    <tr><th class="row-label">Uncorrectable Codewords</th><td>3</td><td>4</td></tr>
    {extra}
    </table></html>"""


async def test_an_unerrored_codewords_row_is_read_when_the_page_has_one():
    """A fixture, not the live page: the row's presence on the real gateway is unverified, so it is optional."""
    snap = await _collector(page=_codeword_page(True)).poll(force=True)
    assert [(c.unerrored, c.correcteds, c.uncorrectables) for c in snap.channels] == [(1000, 5, 3), (2000, 6, 4)]
    snap = await _collector(page=_codeword_page(False)).poll(force=True)
    assert [(c.unerrored, c.uncorrectables) for c in snap.channels] == [(None, 3), (None, 4)]
