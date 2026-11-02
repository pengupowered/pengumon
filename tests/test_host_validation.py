"""Host validation for probe targets and traceroute: option-like and malformed values are refused, and never reach
the ICMP backend."""

from pathlib import Path

import pytest
from httpx import AsyncClient, ASGITransport
from pydantic import ValidationError

from icmp_fakes import FakeBackend
from pengumon.config import load_config
from pengumon.engine.mtr import MtrEngine
from pengumon.engine.prober import NetworkProber
from pengumon.main import app
from pengumon.models.hosts import validate_host
from pengumon.models.probes import TargetConfig, TracerouteRequest

VALID_HOSTS = [
    "1.1.1.1", "192.168.1.1", "::1", "2606:4700:4700::1111", "fe80::1",
    "localhost", "router", "pc-1", "one.one.one.one", "example.com", "xn--bcher-kva.example",
    "my_nas", "My_NAS", "nas_1.local", "a_b-c_d",
]

REJECTED_HOSTS = [
    "-f", "--help", "-m30", "-c1", "_x", "-1.1.1.1", "999.1.1.1", "1.2.3", "256.0.0.1", "192.0.02.4",
    "::g", "[::1]", "a..b", "host-", "", "a" * 254, "1.1.1.1 && whoami",
    "-my_nas", "_my_nas", "-_x", "--my_nas", "my_nas_", "my_nas.-x",
]


@pytest.mark.parametrize("host", VALID_HOSTS)
def test_valid_hosts_accepted(host):
    assert TargetConfig(id="t", name="n", address=host).address == host
    assert TracerouteRequest(target_host=host).target_host == host


@pytest.mark.parametrize("host", REJECTED_HOSTS)
def test_option_like_and_bad_hosts_rejected(host):
    with pytest.raises(ValidationError):
        TargetConfig(id="t", name="n", address=host)
    with pytest.raises(ValidationError):
        TracerouteRequest(target_host=host)


def test_config_yaml_targets_still_validate():
    root = Path(__file__).resolve().parents[1]
    import yaml

    text = (root / "config.example.yaml").read_text(encoding="utf-8")
    block = text[text.index("# targets:"):text.index("dns_probes:")]  # the documented targets, commented out
    targets = yaml.safe_load("\n".join(l[2:] if l.startswith("# ") else "" for l in block.split("\n")))["targets"]
    assert targets
    for target in targets:
        assert validate_host(target["address"]) == target["address"]


@pytest.mark.asyncio
async def test_trace_endpoint_rejects_option_host():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        res = await ac.post("/api/mtr/trace", json={"target_host": "-f"})
        assert res.status_code == 422


@pytest.mark.asyncio
async def test_add_target_rejects_option_address():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        body = {"id": "opt", "name": "Opt", "address": "--help", "category": "custom", "type": "icmp"}
        res = await ac.post("/api/targets", json=body)
        assert res.status_code == 422


@pytest.mark.asyncio
async def test_an_option_like_or_bad_address_never_reaches_the_backend():
    """No program is started any more (the backend sends ICMP itself), but a target built without validation still
    never gets as far as a send: the prober only hands the backend an IPv4 literal."""
    backend = FakeBackend()
    prober = NetworkProber(ping_timeout=0.5, history_window=5, backend=backend)
    for address in ("-f", "--help", "1.1.1.1 && whoami"):
        sample = await prober.probe_icmp(TargetConfig.model_construct(id="t1", name="n", address=address, enabled=True))
        assert sample.success is False and sample.error == "invalid address"
    assert backend.sent == []


@pytest.mark.asyncio
async def test_an_option_like_trace_host_never_reaches_the_backend():
    backend = FakeBackend()
    engine = MtrEngine(target_host="1.1.1.1", backend=backend)
    engine.target_host = "-f"  # past the constructor's check
    assert await engine.run_trace() == []
    assert backend.sent == []


def test_mtr_engine_refuses_option_host():
    with pytest.raises(ValueError):
        MtrEngine(target_host="-f")
