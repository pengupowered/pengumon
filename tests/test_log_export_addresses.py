"""What leaves the machine in log records and spans (observability/export.py): a device is named by its id and driver,
never by its LAN address or its login name. The snapshot (the API, the page) keeps the full error text, so the owner
still sees which address failed."""

import logging

import httpx
import pytest
from opentelemetry.trace import StatusCode

import obs_helpers
from pengumon.capabilities import Capability, DriverError, PollResult
from pengumon.devices import device as device_module
from pengumon.devices.device import Device
from pengumon.devices.registry import build_device
from pengumon.devices.settings import DeviceConfig
from pengumon.drivers.http import DeviceHttp
from pengumon.secrets import CredentialSources, password_env_var

from device_helpers import Clock, build_modem
from test_devices import FakeDriver, FakeMonitor
from test_openwrt import FakeUbus
from test_snmp import router_mib, snmp_driver, start_agent

HOST = "192.0.2.1"  # FakeDriver's host
LOGIN = "svc-login"


def _exported(caplog) -> str:
    """Everything the exported records say (those of the "pengumon" loggers, export.LOGGERS; not httpx's own request
    lines, which the app keeps at warning): message, context, and the traceback text."""
    return "\n".join(r.getMessage() + " " + repr(getattr(r, "pengumon_context", {})) + " " + (r.exc_text or "")
                     for r in caplog.records if r.name == "pengumon" or r.name.startswith("pengumon."))


def _device(driver, clock=None):
    clock = clock or Clock()
    return Device("router", driver, FakeMonitor(), interval_seconds=10.0, clock=clock, monotonic=clock)


@pytest.fixture
def tracer(monkeypatch):
    fake = obs_helpers.FakeTracer()
    monkeypatch.setattr(device_module, "tracer", fake)
    return fake


async def test_a_failed_poll_is_logged_and_traced_with_the_device_id_not_its_address(tracer, caplog):
    device = _device(FakeDriver([DriverError("protocol", f"{HOST} answered HTTP 500")]))
    with caplog.at_level(logging.DEBUG, logger="pengumon"):
        await device.poll(force=True)
    assert device.monitor.events == [("error", "protocol", f"{HOST} answered HTTP 500", False)]  # the page keeps it
    shown = _exported(caplog)
    assert "router answered HTTP 500" in shown and HOST not in shown
    status = tracer.spans[0].status
    assert status.status_code == StatusCode.ERROR and HOST not in status.description
    assert "router answered HTTP 500" in status.description


async def test_a_reason_a_capability_is_missing_is_logged_without_the_address(caplog):
    partial = PollResult(readings={}, missing={Capability.WAN_COUNTERS: f"{HOST} sent no counters"})
    device = _device(FakeDriver([partial]))
    with caplog.at_level(logging.DEBUG, logger="pengumon"):
        await device.poll(force=True)
    shown = _exported(caplog)
    assert "router sent no counters" in shown and HOST not in shown


async def test_a_failed_counter_read_for_a_speed_test_is_logged_without_the_address(caplog):
    class Driver(FakeDriver):
        async def read_wan_counters(self):
            raise DriverError("protocol", f"{HOST} refused the read")

    device = _device(Driver())
    with caplog.at_level(logging.DEBUG, logger="pengumon"):
        assert await device.read_wan_counters() is None
    shown = _exported(caplog)
    assert "router refused the read" in shown and HOST not in shown


async def test_the_gateway_driver_logs_no_address_and_the_page_still_gets_it(caplog):
    """A real driver through a real device: every record of a poll that fails with an error naming the gateway."""
    def login_page(request):
        return httpx.Response(200, text="<html>Please Login First</html>")

    device = build_modem(password="pw", client=httpx.AsyncClient(transport=httpx.MockTransport(login_page)))
    with caplog.at_level(logging.DEBUG, logger="pengumon"):
        snapshot = await device.poll(force=True)
    assert snapshot.error_kind == "auth" and "192.168.100.1" in snapshot.error_message  # the owner sees which address
    shown = _exported(caplog)
    assert "still shows its login page" in shown  # (the failure was logged, and it was logged with the id)
    assert "192.168.100.1" not in shown
    assert any(r.name == "pengumon.drivers.xfinity_xb" for r in caplog.records)  # (the debug lines were seen)
    assert any(r.getMessage() == "request" for r in caplog.records)  # (the HTTP exchange lines were seen too)


async def test_the_openwrt_driver_logs_neither_the_address_nor_the_login_name(caplog):
    cfg = DeviceConfig(id="owrt", driver="openwrt", url="http://192.0.2.1", username=LOGIN)
    secrets = CredentialSources(env={password_env_var("owrt"): "wrong"}, secrets_dir=None, store={})
    client = httpx.AsyncClient(transport=httpx.MockTransport(FakeUbus()))
    device = build_device(cfg, None, secrets, client=client, clock=Clock(), wall_clock=lambda: 1_700_000_000.0)
    with caplog.at_level(logging.DEBUG, logger="pengumon"):
        snapshot = await device.poll(force=True)
    assert snapshot.error_kind == "auth" and LOGIN in snapshot.error_message and "192.0.2.1" in snapshot.error_message
    shown = _exported(caplog)
    assert "refused the login of the login" in shown  # logged, and with the placeholders
    assert LOGIN not in shown and "192.0.2.1" not in shown


@pytest.mark.parametrize("config", [
    {"id": "a", "driver": "asuswrt", "url": "http://192.0.2.1"},
    {"id": "b", "driver": "openwrt", "url": "http://192.0.2.2"},
    {"id": "c", "driver": "xfinity_xb", "url": "http://192.0.2.3"},
    {"id": "d", "driver": "upnp_igd", "url": "http://192.0.2.4"},
    {"id": "e", "driver": "snmp", "url": "snmp://192.0.2.5"},
])
def test_no_driver_binds_the_address_to_its_log_context(config):
    device = build_device(DeviceConfig(**config), None,
                          CredentialSources(env={password_env_var(config["id"]): "pw"}, secrets_dir=None, store={}))
    loggers = [device._log, getattr(device.driver, "_log", None), getattr(getattr(device.driver, "http", None), "_log", None)]
    assert any(loggers[1:]), "the driver has a logger of its own"
    for log in filter(None, loggers):
        assert "192.0.2" not in repr(log.extra) and "host" not in log.extra, log.logger.name


async def test_the_http_exchange_lines_leave_the_address_out_of_a_transport_error_text(caplog):
    def refuse(request):
        raise httpx.ConnectError("cannot reach 192.0.2.9:80")

    http = DeviceHttp("http://192.0.2.9", lambda: None, driver="test",
                      client=httpx.AsyncClient(transport=httpx.MockTransport(refuse)))
    with caplog.at_level(logging.DEBUG, logger="pengumon.drivers.test"), pytest.raises(httpx.ConnectError):
        await http.get("status")
    shown = _exported(caplog)
    assert "request failed" in shown and "cannot reach" in shown and "192.0.2.9" not in shown


async def test_the_snmp_driver_logs_its_layout_without_the_agents_address(caplog):
    agent, port = await start_agent(router_mib())
    device = snmp_driver(port)
    try:
        with caplog.at_level(logging.DEBUG, logger="pengumon"):
            await device.driver.poll()
    finally:
        await device.aclose()
    shown = _exported(caplog)
    assert "SNMP agent: WAN interface" in shown and "127.0.0.1" not in shown
