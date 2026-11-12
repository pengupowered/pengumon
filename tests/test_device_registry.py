"""Building devices from config (pengumon.devices.registry), the DeviceRegistry, per-device TLS, and feature
availability (pengumon.capabilities.features)."""

import logging
import re
import ssl

import certifi
import httpx
import pytest

from pengumon.capabilities import Capability
from pengumon.capabilities.features import FEATURES, FeatureStatus, feature_status
from pengumon.config import AppConfig
from pengumon.devices.settings import DeviceConfig, TlsConfig
from pengumon.devices.registry import DeviceConfigError, DeviceRegistry, build_device, build_devices
from pengumon.devices.monitors import LineMonitor, RouterMonitor
from pengumon.devices.registry import monitor_class
from pengumon.drivers import asuswrt, xfinity_xb
from pengumon.drivers.asuswrt import AsusWrtDriver
from pengumon.drivers.http import tls_verify
from pengumon.drivers.xfinity_xb import XfinityXbDriver
from pengumon.secrets import CredentialSources

from device_helpers import build_modem, build_router

SECRET = "s3cret-pass"


def _cfg(device_id, driver, url="http://192.168.1.1", **kw) -> DeviceConfig:
    return DeviceConfig(id=device_id, driver=driver, url=url, **kw)


def _with_password(*device_ids) -> CredentialSources:
    env = {f"PENGUMON_DEVICE_{i.upper()}_PASSWORD": SECRET for i in device_ids}
    return CredentialSources(env=env, secrets_dir=None, store={})


# --- build_device ---------------------------------------------------------------------------------------------------

def test_the_driver_and_monitor_follow_the_config():
    router = build_device(_cfg("wifi", "asuswrt", model="RT-AX88U", poll_interval_seconds=20,
                               options={"clientlist_interval_seconds": 90}), secrets=_with_password("wifi"))
    assert isinstance(router.driver, AsusWrtDriver) and isinstance(router.monitor, RouterMonitor)
    assert (router.id, router.interval_seconds, router.enabled, router.driver.host) == ("wifi", 20.0, True, "192.168.1.1")
    assert router.driver._clientlist_interval == 90.0 and router.driver._password == SECRET
    assert router.monitor._model == "RT-AX88U"
    assert router.config.id == "wifi"
    assert router.has_credentials and router.credentials_source == "environment PENGUMON_DEVICE_WIFI_PASSWORD"

    modem = build_device(_cfg("cable", "xfinity_xb", url="http://192.168.100.1", enabled=False))
    assert isinstance(modem.driver, XfinityXbDriver) and isinstance(modem.monitor, LineMonitor)
    assert (modem.interval_seconds, modem.enabled, modem.has_credentials) == (1800.0, False, False)


def test_an_interval_below_the_drivers_minimum_is_raised_to_it_with_one_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="pengumon.devices"):
        modem = build_device(_cfg("cable", "xfinity_xb", url="http://192.168.100.1", poll_interval_seconds=60))
        fine = build_device(_cfg("cable", "xfinity_xb", url="http://192.168.100.1", poll_interval_seconds=600))
        router = build_device(_cfg("wifi", "asuswrt", poll_interval_seconds=1))
    assert (modem.interval_seconds, modem.min_interval_seconds) == (600.0, 600.0)
    assert (fine.interval_seconds, router.interval_seconds, router.min_interval_seconds) == (600.0, 1.0, 0.0)
    (warning,) = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert "poll_interval_seconds 60 is below the 600 s" in warning.getMessage()


def test_monitor_selection_by_capability():
    assert monitor_class("m", XfinityXbDriver) is LineMonitor
    assert monitor_class("r", AsusWrtDriver) is RouterMonitor

    class Both:
        name = "combo"
        offers = frozenset({Capability.ACCESS_LINE, Capability.WAN_COUNTERS})

    class Nothing:
        name = "empty"
        offers = frozenset()

    with pytest.raises(DeviceConfigError, match="device gw: driver combo offers access_line together with"):
        monitor_class("gw", Both)
    with pytest.raises(DeviceConfigError, match="offers nothing pengumon can monitor"):
        monitor_class("x", Nothing)


async def test_other_ids_name_themselves_and_their_variables():
    device = build_device(_cfg("upstairs-ap", "asuswrt"))
    snap = await device.poll(force=True)
    assert snap.status == "unconfigured"
    assert snap.error_message == ("Password for upstairs-ap not configured. Set PENGUMON_DEVICE_UPSTAIRS_AP_PASSWORD, "
                                  "add /run/secrets/pengumon_upstairs-ap_password, or enter it in the settings.")
    off = build_device(_cfg("upstairs-ap", "asuswrt", enabled=False))
    assert (await off.poll(force=True)).error_message == "Device upstairs-ap is disabled in configuration."


async def test_errors_name_the_device():
    def refuse(request):
        raise httpx.ConnectError("refused")

    device = build_device(_cfg("cable", "xfinity_xb"), secrets=_with_password("cable"),
                          client=httpx.AsyncClient(transport=httpx.MockTransport(refuse)))
    snap = await device.poll(force=True)
    assert snap.error_message == "Failed to communicate with cable: refused"
    modem = build_modem(password="pw", client=httpx.AsyncClient(transport=httpx.MockTransport(refuse)))
    assert (await modem.poll(force=True)).error_message == "Failed to communicate with modem: refused"


def test_build_devices_logs_one_line_per_device_without_secrets(caplog):
    config = AppConfig(devices=[_cfg("wifi", "asuswrt"), _cfg("cable", "xfinity_xb", url="http://192.168.100.1"),
                                _cfg("old", "asuswrt", url="http://192.168.2.1", enabled=False)])
    with caplog.at_level(logging.INFO, logger="pengumon.devices"):
        registry = build_devices(config.devices, None, _with_password("wifi"))
    assert [d.id for d in registry] == ["wifi", "cable", "old"]
    lines = [r.getMessage() for r in caplog.records]
    assert lines == [
        "Device wifi: asuswrt every 10 s, offers client_inventory,mesh,router_system,traffic_usage,"
        "wan_counters; password from environment PENGUMON_DEVICE_WIFI_PASSWORD",
        "Device cable: xfinity_xb every 1800 s, offers access_line; no password configured",
        "Device old (asuswrt) is disabled in configuration",
    ]
    contexts = [getattr(r, "pengumon_context") for r in caplog.records]
    assert contexts[0]["device"] == "wifi" and contexts[0]["credentials"] == "environment PENGUMON_DEVICE_WIFI_PASSWORD"
    assert contexts[1]["credentials"] == "none" and contexts[0]["interval_s"] == 10.0
    assert SECRET not in caplog.text
    for record in caplog.records:  # exported over OTLP with the other logs: no LAN address in the line or its context
        shown = record.getMessage() + repr(record.pengumon_context)
        assert not any(a in shown for a in ("192.168.1.1", "192.168.100.1", "192.168.2.1")), shown
        assert "host" not in record.pengumon_context


def test_the_supervisor_logs_each_built_device_with_the_same_line(caplog):
    """The devices component's supervisor (the running app) and build_devices write one line per device, the same."""
    from pengumon.devices.component import DeviceSupervisor
    configs = [_cfg("wifi", "asuswrt"), _cfg("old", "asuswrt", url="http://192.168.2.1", enabled=False)]
    with caplog.at_level(logging.INFO, logger="pengumon.devices"):
        build_devices(configs, None, _with_password("wifi"))
        built = [(r.getMessage(), r.pengumon_context) for r in caplog.records]
        caplog.clear()
        DeviceSupervisor(DeviceRegistry(), credentials=_with_password("wifi")).build_all(configs)
        supervised = [(r.getMessage(), r.pengumon_context) for r in caplog.records]
    assert len(built) == 2 and supervised == built


def test_two_enabled_devices_of_one_kind_are_accepted_and_the_first_one_feeds_the_line_load():
    """Storage is keyed by device id (schema v9), so nothing mixes their rows (tests/test_device_storage.py)."""
    configs = [_cfg("off", "asuswrt", url="http://192.168.3.1", enabled=False), _cfg("a", "asuswrt"),
               _cfg("b", "asuswrt", url="http://192.168.2.1"), _cfg("m1", "xfinity_xb"),
               _cfg("m2", "xfinity_xb", url="http://10.0.0.2")]
    registry = build_devices(configs)
    assert [d.id for d in registry.providing(Capability.WAN_COUNTERS)] == ["a", "b"]
    assert registry.wan_rates_device() is registry.get("a")
    assert registry.wan_rates_source() == registry.get("a").monitor.recent_rates
    assert registry.primary(Capability.ACCESS_LINE) is registry.get("m1")


# --- TLS ------------------------------------------------------------------------------------------------------------

def test_tls_verify_values():
    assert tls_verify(TlsConfig()) is True
    assert tls_verify(TlsConfig(verify=False)) is False
    context = tls_verify(TlsConfig(ca_file=certifi.where()))
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname and context.get_ca_certs()


def test_an_unusable_ca_file_stops_the_build_naming_the_device(tmp_path):
    bad = tmp_path / "not-a-cert.pem"
    bad.write_text("hello")
    for path, why in ((tmp_path / "missing.pem", "there is no such file"), (bad, "it holds no certificate in PEM form")):
        with pytest.raises(DeviceConfigError, match=re.escape(f"device wifi: CA file '{path}' cannot be used: {why}")):
            build_device(_cfg("wifi", "asuswrt", url="https://router.lan", tls={"ca_file": str(path)}))


@pytest.mark.parametrize("module, driver", [(asuswrt, "asuswrt"), (xfinity_xb, "xfinity_xb")])
@pytest.mark.parametrize("tls, expected", [({}, True), ({"verify": False}, False), ({"ca_file": certifi.where()}, ssl.SSLContext)])
async def test_the_drivers_http_client_gets_the_devices_verify(monkeypatch, module, driver, tls, expected):
    made = []
    real = httpx.AsyncClient

    def spy(*args, **kwargs):
        made.append(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(module.httpx, "AsyncClient", spy)
    device = build_device(_cfg("d", driver, url="https://192.168.1.1", tls=tls))
    device.driver.http.client  # created on first use
    (kwargs,) = made
    if expected is ssl.SSLContext:
        assert isinstance(kwargs["verify"], ssl.SSLContext)
    else:
        assert kwargs["verify"] is expected
    await device.aclose()


# --- registry -------------------------------------------------------------------------------------------------------

def _registry(*devices):
    return DeviceRegistry(devices)


def test_the_registry_finds_devices_by_id_and_capability():
    modem = build_modem(password="pw")
    router = build_router(password="pw")
    registry = _registry(modem, router)
    assert registry.get("router") is router and registry.get("nope") is None
    assert registry.all() == (modem, router) and list(registry) == [modem, router] and len(registry) == 2
    assert registry.providing(Capability.ACCESS_LINE) == (modem,)
    assert registry.providing(Capability.WAN_COUNTERS) == registry.providing(Capability.ROUTER_SYSTEM) == (router,)
    assert registry.primary(Capability.ACCESS_LINE) is modem
    assert registry.line_technology() == "docsis"
    assert registry.wan_rates_source() == router.monitor.recent_rates
    assert registry.wan_rates_device() is router


def test_primary_falls_back_to_a_disabled_device_so_it_can_say_so():
    off = build_modem(enabled=False)
    registry = _registry(off, build_router())
    assert registry.providing(Capability.ACCESS_LINE) == ()
    assert registry.primary(Capability.ACCESS_LINE) is off
    assert registry.line_technology() is None  # a disabled modem reports no line
    assert _registry().primary(Capability.ACCESS_LINE) is None
    assert _registry().wan_rates_source()() == []
    assert _registry().wan_rates_device() is None


def test_primary_prefers_the_first_enabled_device_in_config_order():
    off = build_device(_cfg("old", "xfinity_xb", enabled=False))
    on = build_device(_cfg("new", "xfinity_xb"))
    assert _registry(off, on).primary(Capability.ACCESS_LINE) is on


def test_duplicate_ids_are_refused():
    with pytest.raises(DeviceConfigError, match="'modem' is used twice"):
        _registry(build_modem(enabled=False), build_modem(enabled=False))


async def test_aclose_closes_every_driver_client():
    clients = [httpx.AsyncClient(), httpx.AsyncClient()]
    registry = _registry(build_modem(client=clients[0]), build_router(client=clients[1]))
    await registry.aclose()
    assert all(c.is_closed for c in clients)


# --- features -------------------------------------------------------------------------------------------------------

def _features(registry):
    return {s.feature: s for s in feature_status(registry)}


def test_the_declared_features():
    assert dict(FEATURES) == {"line_load": {Capability.WAN_COUNTERS}, "line_health": {Capability.ACCESS_LINE},
                              "router_system": {Capability.ROUTER_SYSTEM},
                              "clients": {Capability.CLIENT_INVENTORY}, "mesh": {Capability.MESH},
                              "traffic": {Capability.TRAFFIC_USAGE}}


def test_features_of_a_fully_configured_installation():
    status = _features(_registry(build_modem(password="pw"), build_router(password="pw")))
    assert status == {
        "line_load": FeatureStatus("line_load", True, "available", None, ("router",)),
        "line_health": FeatureStatus("line_health", True, "available", None, ("modem",)),
        "router_system": FeatureStatus("router_system", True, "available", None, ("router",)),
        "clients": FeatureStatus("clients", True, "available", None, ("router",)),
        "mesh": FeatureStatus("mesh", True, "available", None, ("router",)),
        "traffic": FeatureStatus("traffic", True, "available", None, ("router",)),
    }


def test_a_device_without_a_password_is_named_with_what_is_missing():
    status = _features(_registry(build_modem(password="pw"), build_router()))
    assert status["line_load"] == FeatureStatus(
        "line_load", False, "not_configured",
        "router (asuswrt) provides wan_counters but is not configured: no password", ())
    assert status["line_health"].available


def test_features_without_any_device_or_with_a_disabled_one():
    status = _features(_registry(build_modem(enabled=False, password="pw")))
    assert status["line_health"].reason == ("no enabled device provides access_line "
                                            "(modem (xfinity_xb) disabled in configuration)")
    assert status["line_load"].reason == "no enabled device provides wan_counters"
    assert (status["line_health"].reason_kind, status["line_load"].reason_kind) == ("disabled", "none")
    assert not any(s.available for s in status.values())
    assert [s.reason for s in feature_status(_registry())] == [
        "no enabled device provides wan_counters", "no enabled device provides access_line",
        "no enabled device provides router_system", "no enabled device provides client_inventory",
        "no enabled device provides mesh", "no enabled device provides traffic_usage"]


def test_a_feature_needing_several_capabilities_lists_every_gap():
    registry = _registry(build_router(password="pw"))
    (status,) = feature_status(registry, {"combo": frozenset({Capability.WAN_COUNTERS, Capability.ACCESS_LINE})})
    assert status == FeatureStatus("combo", False, "none", "no enabled device provides access_line", ("router",))


@pytest.mark.parametrize("url, enabled, why", [
    ("https://192.168.1.1", False, "is disabled"),
    ("http://192.168.1.1", True, "is reached over plain HTTP"),
])
def test_an_unusable_ca_file_only_warns_where_it_cannot_matter(tmp_path, caplog, url, enabled, why):
    missing = str(tmp_path / "missing.pem")
    with caplog.at_level(logging.WARNING, logger="pengumon.drivers"):
        device = build_device(_cfg("wifi", "asuswrt", url=url, enabled=enabled, tls={"ca_file": missing}))
    assert device.driver._verify is True  # falls back to verifying, never to no check
    (record,) = caplog.records
    assert record.levelname == "WARNING"
    assert f"CA file '{missing}' cannot be used" in record.getMessage() and why in record.getMessage()


def test_monitors_know_their_device():
    assert build_device(_cfg("wifi", "asuswrt")).monitor.device_id == "wifi"
    assert build_device(_cfg("cable", "xfinity_xb")).monitor.device_id == "cable"
