"""Telemetry of the devices and the background tasks: polls the gate held back, backoff and failure gauges, devices the
supervisor rebuilt, gate handovers, and tasks that stopped on an exception."""

import asyncio
import logging
import time
from types import SimpleNamespace

import httpx
import pytest

import obs_helpers
from pengumon.state import AppState
from pengumon.capabilities import DriverError
from pengumon.components import Context, task_kind
from pengumon.devices.component import DeviceSupervisor
from pengumon.devices.device import Device
from pengumon.devices.registry import DeviceRegistry
from pengumon.devices.settings import DeviceConfig
from pengumon.observability import export
from pengumon.observability.gauges import GAUGES

# The fakes of the device tests: a driver that plays back outcomes, a monitor that keeps the last snapshot.
from test_devices import GOOD, Clock, FakeDriver, FakeMonitor

obs_helpers.install()


def _device(device_id, driver=None, **kwargs):
    clock = kwargs.pop("clock", None) or Clock()
    return Device(device_id, driver or FakeDriver(), FakeMonitor(), interval_seconds=kwargs.pop("interval", 10.0),
                  clock=clock, monotonic=clock, **kwargs)


def _skipped(device_id, reason):
    return obs_helpers.total("pengumon.device.polls.skipped", **{"device.id": device_id, "reason": reason})


# --- the gate -------------------------------------------------------------------------------------------------------

async def test_a_caller_inside_the_minimum_interval_and_a_too_early_credentials_test_are_counted():
    clock = Clock()
    device = _device("gate_a", FakeDriver([GOOD, GOOD, GOOD]), clock=clock, min_interval_seconds=600.0)
    await device.poll(force=True)
    assert _skipped("gate_a", "min_interval") == 0
    await device.poll(force=True)
    await device.poll(force=True)
    assert _skipped("gate_a", "min_interval") == 2
    clock.now += 30
    await device.poll(force=True, bypass_gate=True)
    assert _skipped("gate_a", "credentials_floor") == 1
    assert _skipped("gate_a", "min_interval") == 2


async def test_a_device_without_a_minimum_interval_skips_nothing():
    device = _device("gate_b", FakeDriver([GOOD, GOOD]))
    await device.poll(force=True)
    await device.poll(force=True)
    assert _skipped("gate_b", "min_interval") == 0


async def test_the_schedule_waiting_for_the_gate_is_counted():
    device = _device("gate_c", FakeDriver([GOOD, GOOD, GOOD]), interval=0.02, min_interval_seconds=0.3,
                     clock=time.time)
    await device.poll(force=True)
    task = asyncio.create_task(device.run())
    await asyncio.sleep(0.1)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert _skipped("gate_c", "gate_wait") >= 1


async def test_a_refused_login_stops_the_schedule_and_is_counted():
    driver = FakeDriver([DriverError("auth", "bad password")])
    device = _device("gate_d", driver, interval=0.02, min_interval_seconds=0.01, clock=time.time)
    task = asyncio.create_task(device.run())
    await asyncio.sleep(0.15)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert _skipped("gate_d", "auth_stopped") == 1  # once when it stopped, not once per second it waits
    assert driver.polls == 1


# --- failures, backoff, handover ------------------------------------------------------------------------------------

def _read(name, devices):
    gauge = next(g for g in GAUGES if g.otel == name)
    state = SimpleNamespace(devices=DeviceRegistry(devices))
    export.observe_state(state)
    try:
        return {o.attributes["device"]: o.value for o in export._guard(gauge)(None)}
    finally:
        export.observe_state(None)


async def test_the_failure_and_backoff_gauges_follow_the_polls():
    failing = _device("fail_a", FakeDriver([httpx.ConnectTimeout("t")] * 3 + [GOOD]), min_interval_seconds=5.0)
    healthy = _device("fail_b", FakeDriver([GOOD]), min_interval_seconds=5.0)
    plain = _device("fail_c", FakeDriver([httpx.ConnectTimeout("t")] * 3))  # no minimum interval: never backs off
    clock = failing._clock
    for device in (failing, plain):
        for _ in range(3):
            clock.now += 100
            await device.poll(force=True)
    await healthy.poll(force=True)
    assert _read("pengumon.device.failures", [failing, healthy, plain]) == {"fail_a": 3, "fail_b": 0, "fail_c": 3}
    backoff = _read("pengumon.device.backoff", [failing, healthy, plain])
    assert backoff == {"fail_a": 70.0, "fail_b": 0.0, "fail_c": 0.0}  # 10 s * 2**3, less the interval
    clock.now += 100
    await failing.poll(force=True)  # a good poll ends it
    assert _read("pengumon.device.backoff", [failing]) == {"fail_a": 0.0}


async def test_a_disabled_device_has_no_failure_or_backoff_sample():
    off = _device("fail_off", FakeDriver(), enabled=False)
    assert _read("pengumon.device.failures", [off]) == {}


def test_a_rebuilt_device_taking_over_the_gate_is_counted_and_keeps_the_failures():
    old = _device("hand_a", min_interval_seconds=5.0)
    old._last_attempt, old._failures = 900.0, 4
    new = _device("hand_a", min_interval_seconds=5.0)
    before = obs_helpers.total("pengumon.device.gate.handovers", **{"device.id": "hand_a"})
    new.inherit_gate(old)
    assert obs_helpers.total("pengumon.device.gate.handovers", **{"device.id": "hand_a"}) == before + 1
    assert new.consecutive_failures == 4 and new._last_attempt == 900.0


# --- the supervisor -------------------------------------------------------------------------------------------------

ROUTER = DeviceConfig(id="sup_router", driver="asuswrt", url="http://192.0.2.1")
MODEM = DeviceConfig(id="sup_modem", driver="xfinity_xb", url="http://192.0.2.10")


def _rebuilt(device_id, reason):
    return obs_helpers.total("pengumon.device.rebuilds", **{"device.id": device_id, "reason": reason})


async def test_added_changed_and_removed_devices_are_counted_by_reason():
    sup = DeviceSupervisor(DeviceRegistry())
    sup.build_all([MODEM, ROUTER])
    sup.start()
    try:
        before = {(i, r): _rebuilt(i, r) for i in ("sup_router", "sup_modem", "sup_new") for r in ("added", "changed",
                                                                                                    "removed")}
        new = DeviceConfig(id="sup_new", driver="asuswrt", url="http://192.0.2.3")
        changed = DeviceConfig(**{**ROUTER.model_dump(), "url": "http://192.0.2.99"})
        await sup.apply([changed, new])  # the modem goes, the router changes, a device is added
        assert _rebuilt("sup_modem", "removed") == before[("sup_modem", "removed")] + 1
        assert _rebuilt("sup_router", "changed") == before[("sup_router", "changed")] + 1
        assert _rebuilt("sup_new", "added") == before[("sup_new", "added")] + 1
        await sup.apply([changed, new])  # nothing changed: nothing counted
        assert _rebuilt("sup_router", "changed") == before[("sup_router", "changed")] + 1
    finally:
        await sup.stop()


async def test_a_device_that_cannot_be_built_is_counted_as_failed():
    def refuse(cfg, repository, credentials):
        raise ValueError("device sup_bad: the CA file is gone")

    sup = DeviceSupervisor(DeviceRegistry(), build=refuse)
    before = _rebuilt("sup_bad", "failed")
    result = await sup.apply([DeviceConfig(id="sup_bad", driver="asuswrt", url="http://192.0.2.4")])
    assert result.errors and _rebuilt("sup_bad", "failed") == before + 1


# --- background tasks -----------------------------------------------------------------------------------------------

async def _exits(kind, error):
    return obs_helpers.total("pengumon.task.exits", kind=kind, error=error)


async def test_a_task_that_dies_of_an_exception_is_counted_by_its_kind_not_its_name(caplog):
    async def broken_worker():
        raise ValueError("boom")

    ctx = Context(app=None, config=None, state=AppState())
    kind = broken_worker.__qualname__
    before = await _exits(kind, "ValueError")
    with caplog.at_level(logging.ERROR, logger="pengumon.components"):
        task = ctx.spawn(broken_worker())
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)
    assert await _exits(kind, "ValueError") == before + 1
    assert task_kind(task) == kind and task.get_name() not in kind
    assert next(r for r in caplog.records if "stopped" in r.getMessage()).pengumon_context["kind"] == kind


async def test_a_canceled_task_and_one_that_returns_are_not_exits():
    async def forever():
        await asyncio.sleep(3600)

    async def finishes():
        return 1

    ctx = Context(app=None, config=None, state=AppState())
    before = obs_helpers.total("pengumon.task.exits")
    a, b = ctx.spawn(forever()), ctx.spawn(finishes())
    await asyncio.sleep(0.01)
    a.cancel()
    await asyncio.gather(a, b, return_exceptions=True)
    await asyncio.sleep(0)
    assert obs_helpers.total("pengumon.task.exits") == before


async def test_the_supervisors_own_tasks_count_their_exits_too():
    async def broken():
        raise RuntimeError("x")

    before = await _exits(broken.__qualname__, "RuntimeError")
    task = DeviceSupervisor._create_task(broken())
    await asyncio.gather(task, return_exceptions=True)
    await asyncio.sleep(0)
    assert await _exits(broken.__qualname__, "RuntimeError") == before + 1
