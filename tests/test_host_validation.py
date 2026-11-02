"""Host validation for ping and traceroute: option-like values are refused before any process starts."""

import asyncio
from pathlib import Path

import pytest
from httpx import AsyncClient, ASGITransport
from pydantic import ValidationError

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
    cfg = load_config(str(root / "config.example.yaml"))
    assert cfg.targets
    for target in cfg.targets:
        assert validate_host(target.address) == target.address


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


class _FakeProc:
    returncode = 1

    async def communicate(self):
        return b"", b""


def _recording_exec(calls):
    async def fake_exec(*cmd, **kwargs):
        calls.append(list(cmd))
        return _FakeProc()
    return fake_exec


@pytest.mark.asyncio
async def test_linux_ping_argv_has_double_dash(monkeypatch):
    calls = []
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _recording_exec(calls))
    prober = NetworkProber(ping_timeout=1.0, history_window=5)
    prober._is_windows = False
    await prober._system_ping_fallback(TargetConfig(id="t1", name="n", address="1.1.1.1"), 0.0)
    assert calls[0][-2:] == ["--", "1.1.1.1"]


@pytest.mark.asyncio
async def test_windows_ping_argv_unchanged(monkeypatch):
    calls = []
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _recording_exec(calls))
    prober = NetworkProber(ping_timeout=1.0, history_window=5)
    prober._is_windows = True
    await prober._system_ping_fallback(TargetConfig(id="t1", name="n", address="1.1.1.1"), 0.0)
    assert calls[0] == ["ping", "-n", "1", "-w", "1000", "1.1.1.1"]


@pytest.mark.asyncio
async def test_ping_guard_for_constructed_target(monkeypatch):
    calls = []
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _recording_exec(calls))
    prober = NetworkProber(ping_timeout=1.0, history_window=5)
    prober._is_windows = False
    target = TargetConfig.model_construct(id="t1", name="n", address="-f")
    sample = await prober._system_ping_fallback(target, 0.0)
    assert sample.success is False
    assert sample.error == "invalid address"
    assert calls == []


@pytest.mark.asyncio
async def test_linux_traceroute_argv_has_double_dash(monkeypatch):
    calls = []
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _recording_exec(calls))
    monkeypatch.delenv("PENGUMON_TRACE_BRIDGE", raising=False)
    engine = MtrEngine(target_host="1.1.1.1")
    engine._is_windows = False
    assert await engine._trace_linux() == []
    assert calls[0][-2:] == ["--", "1.1.1.1"]


def test_mtr_engine_refuses_option_host():
    with pytest.raises(ValueError):
        MtrEngine(target_host="-f")
