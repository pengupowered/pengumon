"""Config: the devices: list and its validation, the shipped config.yaml, targets, vendor-neutral default targets and
the secrets file merge."""

import json
import logging
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from pengumon.engine import netinfo
from pengumon.config import AppConfig, load_config
from pengumon.devices.settings import DeviceConfig, TlsConfig
from pengumon.engine.netinfo import default_targets, get_default_gateway_ip, parse_default_gateway
from pengumon.secrets import update_secrets
from pengumon.models.probes import TargetConfig

ROOT = Path(__file__).resolve().parents[1]


def _config(text: str) -> AppConfig:
    return AppConfig(**(yaml.safe_load(text) or {}))


# --- the example config.yaml ---------------------------------------------------------------------------------------

def test_the_example_config_describes_a_modem_and_a_router():
    """config.example.yaml (the tracked template of config.yaml) lists the devices modem and router, the ids the stored
    history is kept under (schema v9 migrated it to them)."""
    config = load_config(str(ROOT / "config.example.yaml"))
    modem, router = config.devices
    assert modem.model_dump() == {
        "id": "modem", "driver": "xfinity_xb", "url": "http://192.168.100.1", "enabled": True,
        "poll_interval_seconds": 900.0, "username": "admin", "tls": {"verify": True, "ca_file": None},
        "model": None, "options": {}}
    assert router.model_dump() == {
        "id": "router", "driver": "asuswrt", "url": "http://192.168.1.1", "enabled": True,
        "poll_interval_seconds": 10.0, "username": "admin", "tls": {"verify": True, "ca_file": None},
        "model": None, "options": {"clientlist_interval_seconds": 60}}


def test_the_example_config_is_still_read_by_the_older_build():
    """A running container (an older build) mounts a config.yaml copied from this file: it must keep the keys that
    build reads (no `type` on targets is fine, its default is icmp) and add only keys its models ignore (`devices:`)."""
    data = yaml.safe_load((ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    assert set(data) <= {"server", "prober", "targets", "dns_probes", "classifier", "line", "database",
                         "service_checks", "devices", "privacy", "dashboard", "notifications"}
    assert "docsis" not in data and "router" not in data
    assert "auto_discover_gateway" not in data["prober"]
    assert all("type" not in t for t in data["targets"])


# --- devices: -------------------------------------------------------------------------------------------------------

def test_an_empty_devices_list_means_no_devices():
    assert _config("devices: []\n").devices == []


def test_without_a_devices_list_there_are_no_devices(tmp_path):
    assert load_config(str(tmp_path / "absent.yaml")).devices == []
    path = tmp_path / "config.yaml"
    path.write_text("targets: [{id: a, name: A, address: 1.1.1.1}]\n")
    assert load_config(str(path)).devices == []


def test_device_defaults_come_from_the_driver():
    modem, router = _config("""
devices:
  - {id: cable, driver: xfinity_xb, url: "http://192.168.100.1"}
  - {id: wifi, driver: asuswrt, url: "http://192.168.1.1", poll_interval_seconds: 30, tls: {ca_file: /ca.pem},
     options: {clientlist_interval_seconds: 120}}
""").devices
    assert modem.poll_interval_seconds == 1800.0 and modem.username == "admin" and modem.tls == TlsConfig()
    assert router.poll_interval_seconds == 30.0 and router.tls.ca_file == "/ca.pem" and router.tls.verify
    assert router.options == {"clientlist_interval_seconds": 120}


@pytest.mark.parametrize("entry, message", [
    ({"id": "x", "driver": "netgear", "url": "http://203.0.113.4"}, "unknown driver 'netgear'; available drivers: asuswrt, openwrt, snmp, xfinity_xb"),
    ({"id": "bad id", "driver": "asuswrt", "url": "http://203.0.113.4"}, "Device ID must be"),
    ({"id": "x", "driver": "asuswrt", "url": "192.168.1.1"}, "url must start with http:// or https:// and name a host"),
    ({"id": "x", "driver": "asuswrt", "url": "ftp://192.168.1.1"}, "url must start with http:// or https://"),
    ({"id": "x", "driver": "asuswrt", "url": "http://admin:pw@192.168.1.1"}, "url must not contain credentials"),
    ({"id": "x", "driver": "asuswrt", "url": "http://192.168.1.1/?a=1"}, "must not contain a query"),
    ({"id": "x", "driver": "asuswrt", "url": "http://-evil"}, "Host must be"),
    ({"id": "x", "driver": "asuswrt", "url": "http://203.0.113.4", "password": "pw"}, "devices: entries take no password"),
    ({"id": "x", "driver": "asuswrt", "url": "http://203.0.113.4", "pasword": "pw"}, "Extra inputs are not permitted"),
    ({"id": "x", "driver": "asuswrt", "url": "http://203.0.113.4", "poll_interval_seconds": 0}, "greater than 0"),
    ({"id": "x", "driver": "asuswrt", "url": "http://203.0.113.4", "options": {"clientlist": 5}},
     "options for driver asuswrt: clientlist: Extra inputs are not permitted"),
    ({"id": "x", "driver": "asuswrt", "url": "http://203.0.113.4", "options": {"clientlist_interval_seconds": -1}},
     "options for driver asuswrt: clientlist_interval_seconds: Input should be greater than 0"),
    ({"id": "x", "driver": "xfinity_xb", "url": "http://203.0.113.4", "options": {"a": 1}}, "options for driver xfinity_xb"),
    ({"id": "x", "driver": "asuswrt", "url": "https://203.0.113.4", "tls": {"verify": False, "ca_file": "/ca.pem"}},
     "tls.ca_file is set but tls.verify is false"),
], ids=lambda v: v if isinstance(v, str) else None)
def test_device_entries_are_validated_with_clear_messages(entry, message):
    with pytest.raises(ValidationError) as exc:
        DeviceConfig(**entry)
    assert message in str(exc.value)


def test_duplicate_device_ids_are_refused():
    with pytest.raises(ValidationError, match="id 'r' is used twice"):
        _config('devices: [{id: r, driver: asuswrt, url: "http://a"}, {id: r, driver: asuswrt, url: "http://b"}]\n')


def test_ids_that_share_credential_variables_are_refused():
    with pytest.raises(ValidationError, match="share the credential variables PENGUMON_DEVICE_MY_ROUTER_"):
        _config('devices: [{id: my-router, driver: asuswrt, url: "http://a"}, '
                '{id: my_router, driver: asuswrt, url: "http://b"}]\n')


# --- targets --------------------------------------------------------------------------------------------------------

def test_a_target_with_a_type_key_is_accepted_and_the_key_ignored():
    """The dashboard's add-target form still sends type: icmp; every target is pinged."""
    target = TargetConfig(id="i", name="ICMP", type="icmp", address="1.1.1.1")
    assert "type" not in target.model_dump()


# --- default targets ------------------------------------------------------------------------------------------------

ROUTE_TABLE = (
    "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
    "wlan0\t00000000\t0101A8C0\t0003\t0\t0\t600\t00000000\t0\t0\t0\n"
    "eth0\t00000000\t0114A8C0\t0003\t0\t0\t100\t00000000\t0\t0\t0\n"
    "eth0\t0014A8C0\t00000000\t0001\t0\t0\t100\t00FFFFFF\t0\t0\t0\n"
    "tun0\t00000000\t0200000A\t0001\t0\t0\t50\t00000000\t0\t0\t0\n"       # not a gateway route (flag missing)
    "eth1\t00000000\t0300000A\t0002\t0\t0\t10\t00000000\t0\t0\t0\n"       # down
)


def test_the_default_gateway_is_the_up_gateway_route_with_the_lowest_metric():
    assert parse_default_gateway(ROUTE_TABLE) == "192.168.20.1"


@pytest.mark.parametrize("table", [
    "",
    "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\tMTU\tWindow\tIRTT\n",
    "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\n"
    "eth0\t0014A8C0\t00000000\t0001\t0\t0\t100\t00FFFFFF\t0\t0\t0\n",
    "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\neth0\tzz\tyy\t0003\t0\t0\t1\t0\n",
], ids=["empty", "header only", "no default route", "garbage"])
def test_no_default_route_means_no_gateway(table):
    assert parse_default_gateway(table) is None


def test_an_unreadable_route_table_means_no_gateway(tmp_path):
    assert get_default_gateway_ip(tmp_path / "missing") is None
    (tmp_path / "route").write_text(ROUTE_TABLE)
    assert get_default_gateway_ip(tmp_path / "route") == "192.168.20.1"


def test_default_targets_are_vendor_neutral():
    targets = default_targets("192.168.1.254")
    assert [(t.id, t.name, t.category, t.address) for t in targets] == [
        ("local_router", "Local router (192.168.1.254)", "local", "192.168.1.254"),
        ("cloudflare_dns", "Public: Cloudflare (1.1.1.1)", "public", "1.1.1.1"),
        ("google_dns", "Public: Google (8.8.8.8)", "public", "8.8.8.8"),
    ]
    assert [t.id for t in default_targets(None)] == ["cloudflare_dns", "google_dns"]  # no guessed gateway


def test_load_config_without_targets_uses_the_route_table(tmp_path, monkeypatch):
    route = tmp_path / "route"
    route.write_text(ROUTE_TABLE)
    monkeypatch.setattr(netinfo, "PROC_NET_ROUTE", route)
    config = load_config(str(tmp_path / "absent.yaml"))
    assert [t.address for t in config.targets] == ["192.168.20.1", "1.1.1.1", "8.8.8.8"]
    assert not any(t.id == "isp_gateway" or t.address == "192.168.100.1" for t in config.targets)


# --- secrets file ---------------------------------------------------------------------------------------------------

def test_update_secrets_merges_device_entries_without_dropping_others(tmp_path):
    path = tmp_path / "secrets.json"
    update_secrets({"devices": {"modem": {"username": "a", "password": "m"}}}, path)
    update_secrets({"devices": {"router": {"username": "b", "password": "r"}}}, path)
    update_secrets({"devices": {"modem": {"password": "m2"}}}, path)
    assert json.loads(path.read_text()) == {
        "devices": {"modem": {"username": "a", "password": "m2"}, "router": {"username": "b", "password": "r"}},
    }


# --- no secret in validation errors ---------------------------------------------------------------------------------

PLANTED = "Pl4nted-S3cret"


@pytest.mark.parametrize("text", [
    f'devices: [{{id: r, driver: asuswrt, url: "http://192.168.1.1", password: "{PLANTED}"}}]\n',
    f'devices: [{{id: r, driver: asuswrt, url: "http://admin:{PLANTED}@192.168.1.1"}}]\n',
    f'devices: [{{id: r, driver: asuswrt, url: "admin:{PLANTED}@192.168.1.1"}}]\n',
    f'devices: [{{id: r, driver: asuswrt, url: "https://admin:{PLANTED}@[::1"}}]\n',
    f'devices: [{{id: r, driver: nope, url: "http://admin:{PLANTED}@192.168.1.1"}}]\n',
    f'devices: [{{id: a, driver: asuswrt, url: "http://a", username: "{PLANTED}"}}, '
    f'{{id: a, driver: asuswrt, url: "http://b"}}]\n',
    f'devices:\n  - id: a\n    username: "{PLANTED}\n    driver: asuswrt\n',  # a YAML syntax error on that line
], ids=["device-password", "device-userinfo", "device-schemeless-userinfo", "device-broken-ipv6",
        "device-bad-driver", "duplicate-ids", "yaml-syntax"])
def test_a_planted_password_never_reaches_the_error_or_the_log(tmp_path, caplog, text):
    path = tmp_path / "config.yaml"
    path.write_text(text)
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(ValueError) as exc:
            load_config(str(path))
    assert PLANTED not in str(exc.value) and PLANTED not in repr(exc.value)
    assert PLANTED not in caplog.text
    cause = exc.value.__cause__ or exc.value.__context__
    assert cause is None or PLANTED not in str(cause)
