"""Target colours and the ISP name come from config.yaml and reach the dashboard through /api/targets and /api/info."""

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from pengumon.config import AppConfig, load_config
from pengumon.engine.load_settings import LineConfig
from pengumon.engine.prober_settings import DnsProbeConfig
from pengumon.engine.prober import NetworkProber
from pengumon.main import app
from pengumon.models.probes import TargetConfig

# The example's targets in the order the UI had. They set no colour: the dashboard's validated series colours apply
# (the fixed ones it once carried gave two public servers near-identical purples, #bc8cff and #a371f7).
EXAMPLE_ORDER = ["local_router", "isp_gateway", "isp_hop_1", "isp_hop_2", "isp_dns", "cloudflare_dns", "google_dns",
                 "quad9_dns", "dns_system_google", "dns_direct_cloudflare"]


def test_color_is_validated_and_optional():
    assert TargetConfig(id="a", name="A", address="192.0.2.1").color is None
    assert TargetConfig(id="a", name="A", address="192.0.2.1", color="#ABCDEF").color == "#abcdef"
    for bad in ("red", "#fff", "#12345g", "ff7b72", "#ff7b72;x"):
        with pytest.raises(ValidationError):
            TargetConfig(id="a", name="A", address="192.0.2.1", color=bad)
        with pytest.raises(ValidationError):
            DnsProbeConfig(id="d", name="D", color=bad)


@pytest.mark.asyncio
async def test_colors_reach_targets_api_in_config_order(app_state):
    config = AppConfig(
        targets=[TargetConfig(id="b", name="B", address="192.0.2.2", color="#112233"),
                 TargetConfig(id="a", name="A", address="192.0.2.1")],
        dns_probes=[DnsProbeConfig(id="d", name="D", color="#445566")])
    prober = NetworkProber()
    for t in config.targets:
        prober.register_target(t)
    for d in config.dns_probes:
        prober.register_dns_target(d)
    app_state.config, app_state.prober = config, prober
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        listed = (await ac.get("/api/targets")).json()
    assert [(t["target_id"], t["color"]) for t in listed] == [("b", "#112233"), ("a", None), ("d", "#445566")]


@pytest.mark.asyncio
async def test_the_example_config_leaves_colors_to_the_dashboard(app_state):
    config = load_config(str(Path(__file__).resolve().parents[1] / "config.example.yaml"))
    assert {t.id: t.color for t in [*config.targets, *config.dns_probes]} == dict.fromkeys(EXAMPLE_ORDER)
    assert [t.id for t in [*config.targets, *config.dns_probes]] == EXAMPLE_ORDER
    assert config.line.isp_name == "Your ISP"


def test_isp_name_is_cleaned_and_optional():
    assert LineConfig().isp_name is None
    assert LineConfig(isp_name="  Acme\x00 Cable ").isp_name == "Acme Cable"
    assert LineConfig(isp_name="   ").isp_name is None
