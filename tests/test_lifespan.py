"""Shutdown order: background tasks of the classifier (path traces) end before the repository closes."""

import asyncio

import pytest

from pengumon import main
from platform_helpers import posix_signals
from pengumon.db import retention
from pengumon.db.repository import DatabaseRepository
from pengumon.engine import probing
from pengumon.engine.classifier import component as classifier_component
from pengumon.services import component as services_component


async def _idle(*args, **kwargs):
    await asyncio.sleep(3600)


async def test_shutdown_cancels_the_classifier_background_tasks_before_closing_the_repository(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text(
        "database:\n  db_path: " + repr(str(tmp_path / "pengumon.db")) + "\n"
        "targets:\n  - {id: gw, name: Gateway, address: 192.168.100.1, category: gateway}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PENGUMON_CONFIG", str(config))
    _workers_idle(monkeypatch)

    events = []
    made = []
    real_classifier = classifier_component.OutageClassifier

    def spy_classifier(*args, **kwargs):
        made.append(real_classifier(*args, **kwargs))
        return made[-1]

    monkeypatch.setattr(classifier_component, "OutageClassifier", spy_classifier)
    real_close = DatabaseRepository.close

    async def spy_close(self):
        events.append(("repo closed", trace.cancelled() or trace.done()))
        await real_close(self)

    monkeypatch.setattr(DatabaseRepository, "close", spy_close)

    async with main.lifespan(main.app):
        (classifier,) = made
        trace = asyncio.create_task(_idle())            # stands in for a running path-trace task
        classifier._background.add(trace)
        trace.add_done_callback(classifier._background.discard)
        await asyncio.sleep(0)
        assert not trace.done()

    assert trace.cancelled()
    assert events == [("repo closed", True)]            # already cancelled when the repository closed


def _config_file(tmp_path, devices: str) -> str:
    path = tmp_path / "config.yaml"
    path.write_text(
        "database:\n  db_path: " + repr(str(tmp_path / "pengumon.db")) + "\n"
        "targets:\n  - {id: gw, name: Gateway, address: 192.168.100.1, category: gateway}\n" + devices,
        encoding="utf-8",
    )
    return str(path)


async def test_the_lifespan_installs_the_app_state_and_removes_it_at_shutdown(tmp_path, monkeypatch):
    from httpx import ASGITransport, AsyncClient
    from pengumon.api.state import STATE_ATTR

    monkeypatch.setenv("PENGUMON_CONFIG", _config_file(
        tmp_path, 'devices:\n  - {id: wifi, driver: asuswrt, url: "http://192.0.2.1", enabled: false}\n'))
    _workers_idle(monkeypatch)

    async with main.lifespan(main.app):
        state = getattr(main.app.state, STATE_ATTR)
        assert [d.id for d in state.devices] == ["wifi"]
        assert state.config.devices[0].id == "wifi"
        assert None not in (state.repository, state.prober, state.classifier, state.secret_store, state.credentials)
        assert state.service_checker is not None and state.service_checker.services == []  # always built: services can be added live
        assert state.notifier is not None and state.notifier.channels() == []  # likewise
        # an incident that rises to critical reaches the notifier (the critical-only rule announces it then)
        assert state.classifier.on_incident_escalated == state.notifier.on_incident_escalated
        assert state.credentials.store is state.secret_store
        async with AsyncClient(transport=ASGITransport(app=main.app), base_url="http://test") as ac:
            (device,) = (await ac.get("/api/devices")).json()
            assert (device["id"], device["enabled"]) == ("wifi", False)
            assert (await ac.get("/api/devices/modem")).status_code == 404  # no such device
    assert getattr(main.app.state, STATE_ATTR, None) is None


async def test_a_device_that_cannot_run_is_reported_and_the_start_goes_on(tmp_path, monkeypatch, caplog):
    """The devices are built from the settings store after the database opened: one that cannot run (its CA file is
    missing) is logged and left out, and the app starts without it."""
    import logging
    from pengumon.api.state import STATE_ATTR

    monkeypatch.setenv("PENGUMON_CONFIG", _config_file(
        tmp_path, 'devices:\n  - {id: wifi, driver: asuswrt, url: "https://192.0.2.1", tls: {ca_file: /nope.pem}}\n'
                  '  - {id: modem, driver: xfinity_xb, url: "http://192.0.2.2", enabled: false}\n'))
    _workers_idle(monkeypatch)
    with caplog.at_level(logging.ERROR, logger="pengumon"):
        async with main.lifespan(main.app):
            state = getattr(main.app.state, STATE_ATTR)
            assert [d.id for d in state.devices] == ["modem"]
            assert [d.id for d in state.config.devices] == ["wifi", "modem"]  # still the stored list
            assert "tls.ca_file '/nope.pem' cannot be used" in state.devices.errors["wifi"]
    assert any("device wifi: tls.ca_file '/nope.pem' cannot be used" in r.getMessage() for r in caplog.records)
    assert (tmp_path / "pengumon.db").exists()


# --- stored history and the configured devices ----------------------------------------------------------------------

def _workers_idle(monkeypatch):
    monkeypatch.setattr(probing, "probe_background_worker", _idle)
    monkeypatch.setattr(retention, "cleanup_background_worker", _idle)
    monkeypatch.setattr(services_component, "collector_background_worker", _idle)


async def _seed(db_path: str, seed) -> None:
    from pengumon.db.repository import DatabaseRepository

    repo = DatabaseRepository(db_path=db_path)
    await repo.initialize()
    try:
        await seed(repo)
        await repo.flush_samples()
    finally:
        await repo.close()


def _rate_row(device_id: str, ts: float, rx: float, tx: float):
    from pengumon.db.samples import DeviceSample
    return DeviceSample(device_id=device_id, ts=ts, status="healthy", wan_rx_mbps=rx, wan_tx_mbps=tx)


async def test_a_disabled_routers_stored_rates_still_serve_the_load_backfill(tmp_path, monkeypatch):
    """Review note: capacity configured, the router switched off in config: its stored rows still annotate the
    incidents recorded before load awareness (LineLoad reads DeviceRegistry.primary's history)."""
    import time
    from pengumon.incidents.model import Incident

    start = time.time() - 3600
    ids = {}

    async def seed(repo):
        await repo.insert_device_sample(_rate_row("router", start - 5, 950.0, 3.0))
        ids["busy"] = await repo.create_incident(Incident(
            start_time=start, end_time=start + 30, duration_seconds=30, cause_code="access_line_drop",
            title="ISP Latency Spikes / Jitter", description="d", severity="warning", resolved=True,
            kind="upstream_latency"))

    db = str(tmp_path / "pengumon.db")
    await _seed(db, seed)
    monkeypatch.setenv("PENGUMON_CONFIG", _config_file(
        tmp_path, "line: {down_mbps: 1000, up_mbps: 40}\n"
                  'devices:\n  - {id: router, driver: asuswrt, url: "http://192.0.2.1", enabled: false}\n'))
    _workers_idle(monkeypatch)
    async with main.lifespan(main.app):
        from pengumon.api.state import STATE_ATTR
        state = getattr(main.app.state, STATE_ATTR)
        assert state.classifier.load.device_id == "router"
        incident = await state.repository.get_incident_by_id(ids["busy"])
    assert (incident.load_pct, incident.expected) == (95.0, True)


async def test_a_disabled_routers_stored_rates_still_teach_the_line_capacity(tmp_path, monkeypatch):
    import time
    from pengumon.engine.load import CAPACITY_MIN_SAMPLES

    now = time.time()

    async def seed(repo):
        for i in range(CAPACITY_MIN_SAMPLES):
            await repo.insert_device_sample(_rate_row("router", now - 10 * (i + 1), 500.0, 30.0))

    await _seed(str(tmp_path / "pengumon.db"), seed)
    monkeypatch.setenv("PENGUMON_CONFIG", _config_file(
        tmp_path, 'devices:\n  - {id: router, driver: asuswrt, url: "http://192.0.2.1", enabled: false}\n'))
    _workers_idle(monkeypatch)
    async with main.lifespan(main.app):
        from pengumon.api.state import STATE_ATTR
        load = getattr(main.app.state, STATE_ATTR).classifier.load
        assert (load.down_mbps, load.up_mbps) == (500.0, 30.0)
        assert load.current() is None  # no live rates: the router is not polled


async def test_stored_history_of_devices_not_in_the_config_is_named_once(tmp_path, monkeypatch, caplog):
    import logging
    from pengumon.db.samples import LineSample

    async def seed(repo):
        await repo.insert_device_sample(_rate_row("router", 1.0, 1.0, 1.0))
        await repo.insert_device_sample(_rate_row("old-router", 2.0, 1.0, 1.0))
        await repo.insert_line_sample(LineSample(device_id="modem", ts=1.0, technology="docsis", status="healthy"))
        await repo.insert_line_sample(LineSample(device_id="attic-modem", ts=1.0, technology="docsis",
                                                 status="healthy"))

    await _seed(str(tmp_path / "pengumon.db"), seed)
    monkeypatch.setenv("PENGUMON_CONFIG", _config_file(
        tmp_path, 'devices:\n  - {id: router, driver: asuswrt, url: "http://192.0.2.1", enabled: false}\n'
                  '  - {id: modem, driver: xfinity_xb, url: "http://192.0.2.2", enabled: false}\n'))
    _workers_idle(monkeypatch)
    with caplog.at_level(logging.WARNING, logger="pengumon"):
        async with main.lifespan(main.app):
            pass
    warnings = [r.getMessage() for r in caplog.records if "not in the config" in r.getMessage()]
    assert warnings == ["Stored readings of devices not in the config: attic-modem, old-router; their history is not "
                        "shown (retention removes it)"]


async def test_no_warning_when_every_stored_device_is_configured(tmp_path, monkeypatch, caplog):
    import logging

    async def seed(repo):
        await repo.insert_device_sample(_rate_row("router", 1.0, 1.0, 1.0))

    await _seed(str(tmp_path / "pengumon.db"), seed)
    monkeypatch.setenv("PENGUMON_CONFIG", _config_file(
        tmp_path, 'devices:\n  - {id: router, driver: asuswrt, url: "http://192.0.2.1", enabled: false}\n'))
    _workers_idle(monkeypatch)
    with caplog.at_level(logging.WARNING, logger="pengumon"):
        async with main.lifespan(main.app):
            pass
    assert not [r for r in caplog.records if "not in the config" in r.getMessage()]


# --- podman stop: SIGTERM ends the real server cleanly ---------------------------------------------------------------

@posix_signals
def test_sigterm_shuts_the_server_down_cleanly_within_podman_stops_grace_period(tmp_path):
    """`podman stop` sends SIGTERM and kills the container 10 s later. The real entry point (pengumon.main.main, as the
    image runs it) must run the whole lifespan shutdown in that time, without an error or a traceback, and end on the
    signal: uvicorn re-raises SIGTERM after a clean shutdown, so podman sees the usual exit status 143."""
    import os
    import signal
    import socket
    import subprocess
    import sys
    import threading
    import time
    from pathlib import Path

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    config = tmp_path / "config.yaml"
    config.write_text(
        "database:\n  db_path: " + repr(str(tmp_path / "pengumon.db")) + "\n"
        "targets:\n  - {id: lo, name: Loopback, address: 127.0.0.1, category: local}\n"
        "dns_probes:\n  - {id: dns, name: DNS, domain: localhost, nameserver: 127.0.0.1}\n"
        "service_checks:\n  services:\n"
        f"    - {{id: self, name: Self, url: 'http://127.0.0.1:{port}/api/health'}}\n"
        'devices:\n  - {id: modem, driver: xfinity_xb, url: "http://192.0.2.2"}\n',
        encoding="utf-8")
    src = Path(__file__).resolve().parents[1] / "src"
    env = {**os.environ, "PYTHONPATH": str(src), "PENGUMON_CONFIG": str(config), "PENGUMON_HOST": "127.0.0.1",
           "PENGUMON_PORT": str(port), "PENGUMON_NOTIFY_URL": "http://127.0.0.1:9/topic"}
    for name in ("PENGUMON_ACCESS_TOKEN", "PENGUMON_LOG_FORMAT", "PENGUMON_LOG_LEVEL", "OTEL_EXPORTER_OTLP_ENDPOINT"):
        env.pop(name, None)
    server = subprocess.Popen([sys.executable, "-c", "from pengumon.main import main; main()"], cwd=tmp_path, env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    lines = []
    reader = threading.Thread(target=lambda: lines.extend(server.stdout), daemon=True)
    reader.start()
    try:
        deadline = time.monotonic() + 30
        while not any("Application startup complete" in line for line in lines):
            assert server.poll() is None and time.monotonic() < deadline, "".join(lines)
            time.sleep(0.1)
        time.sleep(1.5)  # a few probe rounds and the first service check
        server.send_signal(signal.SIGTERM)
        assert server.wait(timeout=10) == -signal.SIGTERM, "".join(lines)
    finally:
        if server.poll() is None:
            server.kill()
    reader.join(timeout=5)
    log = "".join(lines)
    assert "Pengumon shut down successfully." in log and "Application shutdown complete" in log
    assert "Traceback" not in log and "[ERROR]" not in log and "[CRITICAL]" not in log, log
    assert log.index("Pengumon shut down successfully.") < log.index("Application shutdown complete")


# --- devices through the settings manager (the lifespan sets up logging, so it lives with the lifespan tests) ------------------------------------------------------------------------------------

async def test_the_manager_refuses_a_device_that_cannot_be_built_and_applies_a_good_list(tmp_path, monkeypatch):
    from pengumon import main
    from pengumon.api.state import STATE_ATTR

    monkeypatch.setenv("PENGUMON_CONFIG", _config_file(tmp_path, ""))
    _workers_idle(monkeypatch)
    async with main.lifespan(main.app):
        state = getattr(main.app.state, STATE_ATTR)
        manager = state.settings
        bad = {"id": "wifi", "driver": "asuswrt", "url": "https://192.0.2.1", "tls": {"verify": True, "ca_file": "/nope.pem"}}
        checked = await manager.validate({}, {"devices": [bad]})
        assert [(e["where"]) for e in checked.errors] == ["lists.devices[0]"]
        assert "tls.ca_file '/nope.pem' cannot be used" in checked.errors[0]["message"]
        good = {"id": "wifi", "driver": "asuswrt", "url": "http://192.0.2.1", "enabled": False}
        await manager.apply(manager.revision, {}, {"devices": [good]})
        assert [d.id for d in state.devices] == ["wifi"]
        task = state.device_supervisor._tasks["wifi"]
        await manager.apply(manager.revision, {}, {"devices": [good, {**good, "id": "wifi2", "url": "http://192.0.2.2"}]})
        assert state.device_supervisor._tasks["wifi"] is task  # unchanged: same task
        assert [d.id for d in state.devices] == ["wifi", "wifi2"]
