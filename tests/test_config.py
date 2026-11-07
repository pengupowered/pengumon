"""Unit tests for configuration loading."""

import os
from pengumon.config import AppConfig, load_config


def test_load_default_config():
    config = load_config("non_existent_path.yaml")
    assert isinstance(config, AppConfig)
    assert len(config.targets) > 0
    assert len(config.dns_probes) > 0
    assert config.server.port == 8000


def test_config_target_categories(monkeypatch):
    # the gateway target needs a known gateway: the route table on Linux, the first router elsewhere (which a CI
    # runner's network may not show); a fixed one here
    import pengumon.config
    monkeypatch.setattr(pengumon.config, "get_default_gateway_ip", lambda: "192.168.1.1")
    config = load_config("non_existent_path.yaml")
    categories = {t.category for t in config.targets}
    assert "local" in categories
    assert "public" in categories


def test_hop_status_thresholds_parse_and_default():
    import yaml
    from pengumon.engine.prober_settings import HopStatusConfig

    config = AppConfig(**yaml.safe_load("prober:\n  hop_status:\n    gateway_jitter_ms: 5\n"))
    assert config.prober.hop_status.gateway_jitter_ms == 5.0
    assert config.prober.hop_status.local_latency_ms == 20.0  # the rest keeps its default
    assert AppConfig().prober.hop_status == HopStatusConfig()


def test_hop_status_thresholds_are_validated():
    import pytest
    from pydantic import ValidationError
    from pengumon.engine.prober_settings import HopStatusConfig

    HopStatusConfig(degraded_loss_fraction=0.0, down_loss_fraction=1.0)  # the extremes are fine
    for bad in (
        {"down_loss_fraction": 1.5},
        {"degraded_loss_fraction": -0.1},
        {"local_latency_ms": -1},
        {"dns_jitter_ms": -0.5},
        {"degraded_loss_fraction": 0.4},                         # equal to down (default 0.4)
        {"degraded_loss_fraction": 0.6, "down_loss_fraction": 0.5},
    ):
        with pytest.raises(ValidationError):
            HopStatusConfig(**bad)
    with pytest.raises(ValidationError):
        AppConfig(prober={"hop_status": {"down_loss_fraction": 0.05}})  # below the default degraded 0.1


def test_dashboard_layout_defaults_and_validation():
    import pytest
    from pydantic import ValidationError
    from pengumon.web.ui import panel_ids
    from pengumon.web.settings import DashboardConfig

    assert AppConfig().dashboard == DashboardConfig(order=[], hidden=[])
    config = AppConfig(dashboard={"order": ["matrix", "line"], "hidden": ["report"]})
    assert config.dashboard.order == ["matrix", "line"] and config.dashboard.hidden == ["report"]
    assert set(panel_ids()) >= {"diagnosis", "line", "router", "latency", "matrix", "services", "incidents", "report"}
    for bad in ({"order": ["nope"]}, {"hidden": ["line", "line"]}, {"hidden": ["Report"]}, {"colour": "red"}):
        with pytest.raises(ValidationError):
            AppConfig(dashboard=bad)


def test_the_example_config_has_a_dashboard_section():
    from pathlib import Path
    config = load_config(str(Path(__file__).resolve().parents[1] / "config.example.yaml"))
    assert config.dashboard.order == [] and config.dashboard.hidden == []


def test_dns_probes_left_out_are_the_defaults_and_an_empty_list_is_none(tmp_path):
    target = '  - {id: srv, name: My server, category: custom, address: "192.0.2.10"}\n'
    absent = tmp_path / "absent.yaml"
    absent.write_text(f"targets:\n{target}", encoding="utf-8")
    assert [p.id for p in load_config(str(absent)).dns_probes] == ["dns_system_google", "dns_direct_cloudflare"]
    empty = tmp_path / "empty.yaml"
    empty.write_text(f"targets:\n{target}dns_probes: []\n", encoding="utf-8")
    assert load_config(str(empty)).dns_probes == []


def test_a_line_break_never_rides_along_in_an_id_or_a_colour():
    """A pattern ending in $ also matches before a final line break. Ids are trimmed before they are checked (a device id
    becomes an environment variable's name and a secret's file name); a chart colour is checked as a whole string."""
    import pytest
    from pydantic import ValidationError
    from pengumon.devices.settings import DeviceConfig
    from pengumon.engine.prober_settings import DnsProbeConfig
    from pengumon.models.probes import TargetConfig
    assert TargetConfig(id="router\n", name="Router", address="192.0.2.1").id == "router"
    assert DeviceConfig(id="modem\n", driver="snmp", url="snmp://192.0.2.1").id == "modem"
    assert DnsProbeConfig(id="dns\n", name="DNS").id == "dns"
    with pytest.raises(ValidationError):
        TargetConfig(id="router", name="Router", address="192.0.2.1", color="#3987e5\n")
    assert TargetConfig(id="router", name="Router", address="192.0.2.1", color="#3987E5").color == "#3987e5"
