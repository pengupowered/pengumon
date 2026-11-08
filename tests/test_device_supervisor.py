"""Devices applied live (devices/component.py): the supervisor keeps the Device and task of an unchanged device,
restarts a changed one, closes a removed one's driver, leaves out a device that cannot be built (and says why on the
registry), unlinks the path hops of a removed device, and re-wires the line load and the classifier through the hub.
Real drivers are built but never polled over a network: the devices here have no password."""

import asyncio

import pytest

from pengumon.state import AppState
from pengumon.capabilities import Capability
from pengumon.components import Context
from pengumon.config import AppConfig
from pengumon.db.repository import DatabaseRepository
from pengumon.devices import component as devices_component
from pengumon.devices.component import DEVICES_REBUILT, DeviceSupervisor, unlink_hops
from pengumon.devices.registry import DeviceRegistry, build_device, unique_device_ids
from pengumon.devices.settings import DeviceConfig
from pengumon.devices.summary import LiveSummaries
from pengumon.devices.validation import RENAME_MESSAGE, validate_devices
from pengumon.engine import line_load as line_load_component
from pengumon.engine.classifier import component as classifier_component
from pengumon.engine.path import PathManager
from pengumon.lines import wording
from pengumon.models.probes import TargetConfig
from pengumon.settings.hub import Change, SettingsHub

MODEM = DeviceConfig(id="modem", driver="xfinity_xb", url="http://192.0.2.10")
ROUTER = DeviceConfig(id="router", driver="asuswrt", url="http://192.0.2.1")
OTHER = DeviceConfig(id="router2", driver="asuswrt", url="http://192.0.2.2")


async def _settle():
    for _ in range(5):
        await asyncio.sleep(0)


@pytest.fixture
async def supervisor():
    registry = DeviceRegistry()
    sup = DeviceSupervisor(registry)
    sup.build_all([MODEM, ROUTER])
    sup.start()
    await _settle()
    yield sup
    await sup.stop()


def _cfg(base: DeviceConfig, **fields) -> DeviceConfig:
    return DeviceConfig(**{**base.model_dump(), **fields})


async def test_an_unchanged_device_keeps_its_device_and_task_and_a_changed_one_is_restarted(supervisor):
    sup, registry = supervisor, supervisor.registry
    modem, router = registry.get("modem"), registry.get("router")
    modem_task, router_task = sup._tasks["modem"], sup._tasks["router"]
    closed = []
    real_close = router.aclose

    async def close():
        closed.append("router")
        await real_close()

    router.aclose = close
    result = await sup.apply([MODEM, _cfg(ROUTER, url="http://192.0.2.99")])
    assert (result.added, result.changed, result.removed) == ([], ["router"], [])
    assert registry.get("modem") is modem and sup._tasks["modem"] is modem_task and not modem_task.done()
    new_router = registry.get("router")
    assert new_router is not router and sup._tasks["router"] is not router_task
    assert router_task.done() and closed == ["router"]  # the old task ended and its driver was closed
    assert new_router.driver.host == "192.0.2.99"
    assert [d.id for d in registry] == ["modem", "router"]


async def test_a_removed_device_is_canceled_and_its_driver_closed_and_an_added_one_starts(supervisor):
    sup, registry = supervisor, supervisor.registry
    modem = registry.get("modem")
    task = sup._tasks["modem"]
    closed = []
    real_close = modem.aclose

    async def close():
        closed.append(True)
        await real_close()

    modem.aclose = close
    result = await sup.apply([OTHER, ROUTER])
    assert (result.added, result.changed, result.removed) == (["router2"], [], ["modem"])
    assert task.done() and closed == [True] and "modem" not in sup._tasks
    assert registry.get("modem") is None
    assert [d.id for d in registry] == ["router2", "router"]  # the config's order
    await _settle()
    assert not sup._tasks["router2"].done()


async def test_the_registry_object_stays_and_holders_see_the_new_set(supervisor):
    holder = supervisor.registry
    await supervisor.apply([ROUTER])
    assert [d.id for d in holder] == ["router"] and len(holder) == 1
    assert holder.providing(Capability.ACCESS_LINE) == ()


async def test_a_build_that_fails_leaves_the_device_out_and_names_why(supervisor, caplog):
    bad = _cfg(ROUTER, url="https://192.0.2.1", tls={"ca_file": "/nope.pem"})
    result = await supervisor.apply([MODEM, bad])
    registry = supervisor.registry
    assert [d.id for d in registry] == ["modem"]
    assert "CA file '/nope.pem' cannot be used" in registry.errors["router"]
    assert result.errors == registry.errors and result.removed == [] and result.changed == []
    assert "Device router cannot run" in caplog.text
    assert "router" not in supervisor._tasks
    ok = await supervisor.apply([MODEM, ROUTER])  # fixed: it runs and the error is gone
    assert ok.added == ["router"] and registry.errors == {} and registry.get("router") is not None


async def test_the_first_build_reports_a_bad_device_instead_of_raising():
    registry = DeviceRegistry()
    DeviceSupervisor(registry).build_all([_cfg(ROUTER, url="https://192.0.2.1", tls={"ca_file": "/nope.pem"}), MODEM])
    assert [d.id for d in registry] == ["modem"] and set(registry.errors) == {"router"}


async def test_ids_that_share_credential_variables_are_refused():
    with pytest.raises(ValueError, match="share the credential variables"):
        unique_device_ids([_cfg(ROUTER, id="my-router"), _cfg(OTHER, id="my_router")])
    with pytest.raises(ValueError, match="share the credential variables"):
        AppConfig(devices=[_cfg(ROUTER, id="my-router"), _cfg(OTHER, id="my_router")])


async def test_replace_swaps_the_tuple_and_the_id_map_together_and_refuses_duplicates():
    a, b = build_device(MODEM), build_device(ROUTER)
    registry = DeviceRegistry([a])
    registry.replace([b])
    assert registry.all() == (b,) and registry.get("modem") is None and registry.get("router") is b
    with pytest.raises(Exception):
        registry.replace([a, a])
    assert registry.all() == (b,)  # unchanged by the refused swap


async def test_live_summaries_forget_devices_that_are_gone():
    registry = DeviceRegistry([build_device(MODEM), build_device(ROUTER)])
    summaries = LiveSummaries(registry)
    summaries._cache.update({"modem": (None, {}), "router": (None, {}), "gone": (None, {})})
    summaries.forget_missing()
    assert set(summaries._cache) == {"modem", "router"}


# --- the path -------------------------------------------------------------------------------------------------------

def _hop(id, name, address, category, device_id=None):
    return TargetConfig(id=id, name=name, address=address, category=category, device_id=device_id)


async def _path(tmp_path, devices, hops):
    repo = DatabaseRepository(db_path=str(tmp_path / "p.db"), sample_flush_seconds=0)
    await repo.initialize()
    config = AppConfig(targets=hops, devices=devices)
    manager = PathManager(config, repo)
    await manager.load()
    return repo, config, manager


async def test_removing_a_device_unlinks_its_hops_in_one_path_version(tmp_path):
    hops = [_hop("hop1", "Hop 1", "192.0.2.1", "gateway", "router2"), _hop("hop2", "Hop 2", "192.0.2.2", "isp", "router"),
            _hop("pub", "Public", "203.0.113.53", "public")]
    repo, config, manager = await _path(tmp_path, [ROUTER, OTHER], hops)
    before = await repo.path_revision()
    config.devices[:] = [ROUTER]  # the settings manager has already swapped the list
    revision = await unlink_hops(manager, {"router2"})
    assert revision == before + 1
    assert [(t.id, t.device_id) for t in config.targets] == [("hop1", None), ("hop2", "router"), ("pub", None)]
    assert (await repo.load_path())[1][0].device_id is None  # stored, not only live
    assert (await repo.path_versions())[0]["summary"] == "Unlinked Hop 1 from device router2"
    assert await unlink_hops(manager, {"router2"}) is None  # nothing left to unlink: no new version
    assert await repo.path_revision() == revision


# --- through the hub ------------------------------------------------------------------------------------------------

@pytest.fixture
async def wired(tmp_path):
    hops = [_hop("hop1", "Hop 1", "192.0.2.1", "gateway", "router")]
    repo, config, manager = await _path(tmp_path, [MODEM, ROUTER], hops)
    state = AppState(config=config, repository=repo)
    state.path = manager
    ctx = Context(app=None, config=config, state=state)
    ctx.settings = SettingsHub()
    component = await devices_component.register(ctx)
    await line_load_component.register(ctx)
    await classifier_component.register(ctx)
    await component.start()
    await _settle()
    yield ctx, state, config, manager
    for task in ctx.tasks:
        task.cancel()
    await component.stop()
    await repo.close()


async def _save_devices(ctx, config, devices):
    config.devices[:] = devices
    return await ctx.settings.publish(Change.of(lists=["devices"]))


async def test_a_devices_save_rebuilds_unlinks_and_rewires_the_line_load_and_the_classifier(wired):
    ctx, state, config, manager = wired
    seen = []
    ctx.settings.on(events=[DEVICES_REBUILT], name="probe.test")(lambda change: seen.append(change.events))
    assert ctx.line_load.device_id == "router"
    assert state.classifier.line == wording(state.devices.line_technology())
    old_router = state.devices.get("router")
    assert state.devices.line_technology() is not None  # the modem

    assert await _save_devices(ctx, config, [MODEM, OTHER]) == []
    assert [d.id for d in state.devices] == ["modem", "router2"]
    assert seen == [frozenset({DEVICES_REBUILT})]
    assert ctx.line_load.device_id == "router2"  # the WAN history follows the new router
    assert ctx.line_load.rates_source.__self__ is state.devices.get("router2").monitor
    assert state.devices.get("router2").monitor is not old_router.monitor
    assert config.targets[0].device_id is None  # hop 1 was linked to the removed router

    assert await _save_devices(ctx, config, [OTHER]) == []  # the modem goes: no access line left
    assert state.devices.line_technology() is None
    assert state.classifier.line == wording(None)
    assert state.devices.get("modem") is None


async def test_the_modem_wiring_follows_when_a_changed_device_gets_a_new_monitor(wired):
    ctx, state, config, _ = wired
    old = state.devices.get("router").monitor
    await _save_devices(ctx, config, [MODEM, _cfg(ROUTER, url="http://192.0.2.77")])
    new = state.devices.get("router").monitor
    assert new is not old
    assert ctx.line_load.rates_source.__self__ is new
    assert config.targets[0].device_id == "router"  # changed, not removed: the hop stays linked


# --- validation -----------------------------------------------------------------------------------------------------

def _raw(cfg):
    return cfg.model_dump(mode="json")


async def test_validate_accepts_a_good_list_and_checks_each_item():
    assert await validate_devices([_raw(MODEM), _raw(ROUTER)], [MODEM]) == []
    problems = await validate_devices([{**_raw(ROUTER), "url": "ftp://192.0.2.1"}])
    assert problems and problems[0][0] == "devices.router.url"
    assert "pw" not in str(await validate_devices([{**_raw(ROUTER), "url": "http://admin:pw@192.0.2.1"}]))
    assert (await validate_devices([{**_raw(ROUTER), "password": "x"}]))[0][0].startswith("devices.router")


async def test_validate_dry_builds_and_refuses_a_missing_ca_file_per_item():
    bad = {**_raw(ROUTER), "url": "https://192.0.2.1", "tls": {"verify": True, "ca_file": "/nope.pem"}}
    problems = await validate_devices([_raw(MODEM), bad])
    assert [w for w, _ in problems] == ["devices.router"]
    assert "CA file '/nope.pem' cannot be used" in problems[0][1]


async def test_validate_refuses_duplicate_ids_and_a_bad_option():
    assert (await validate_devices([_raw(ROUTER), _raw(ROUTER)]))[0][0] == "devices"
    problems = await validate_devices([{**_raw(ROUTER), "options": {"no_such_option": 1}}])
    assert problems and problems[0][0].startswith("devices.router")


async def test_validate_refuses_a_rename_but_allows_remove_and_add_with_another_url():
    renamed_item = {**_raw(ROUTER), "id": "router_new"}
    problems = await validate_devices([_raw(MODEM), renamed_item], [MODEM, ROUTER])
    assert problems == [("devices.router_new.id", RENAME_MESSAGE)]
    assert "kept under the old id" in RENAME_MESSAGE
    other_address = {**_raw(ROUTER), "id": "router_new", "url": "http://192.0.2.50"}
    assert await validate_devices([_raw(MODEM), other_address], [MODEM, ROUTER]) == []
    assert await validate_devices([_raw(MODEM)], [MODEM, ROUTER]) == []  # a plain removal
    assert await validate_devices([_raw(MODEM), renamed_item], [MODEM]) == []  # nothing live was lost



# --- the poll gate across a rebuild ---------------------------------------------------------------------------------

class _GateDriver:
    """A driver whose poll blocks until released; counts the polls it was asked for."""
    name = "fake"
    offers = frozenset()

    def __init__(self, host="192.0.2.1"):
        self.host = host
        self.polls = 0
        self.release = asyncio.Event()

    def missing_configuration(self):
        return None

    async def poll(self):
        self.polls += 1
        await self.release.wait()
        from pengumon.capabilities import PollResult
        return PollResult(readings={})

    async def aclose(self):
        pass


class _ForgetfulMonitor:
    """Like LineMonitor.restore after a canceled poll: nothing stored younger than the interval."""
    latest_snapshot = None

    async def restore(self, oldest_ts):
        return None

    async def on_result(self, now, result):
        return ("ok", now)

    async def on_error(self, now, error):
        return ("error", now)


async def _rebuild_during_a_poll(host_after):
    from pengumon.devices.device import Device
    clock = [1_000_000.0]  # the fake wall clock: frozen, so the gate can only open by what the code hands over
    drivers = []

    def build(cfg, repository, credentials):
        driver = _GateDriver(host_after if drivers else "192.0.2.1")
        drivers.append(driver)
        return Device(cfg.id, driver, _ForgetfulMonitor(), interval_seconds=1800.0, min_interval_seconds=600.0,
                      clock=lambda: clock[0], config=cfg)

    sup = DeviceSupervisor(DeviceRegistry(), build=build)
    sup.build_all([MODEM])
    sup.start()
    try:
        for _ in range(20):
            await asyncio.sleep(0.005)
        assert drivers[0].polls == 1  # the first poll is in flight (blocked in the driver)
        await sup.apply([_cfg(MODEM, url="http://192.0.2.11")])  # a settings save rebuilds the device mid-poll
        clock[0] += 0.5
        for _ in range(20):
            await asyncio.sleep(0.005)
        return drivers
    finally:
        for d in (drivers or []):
            d.release.set()
        await sup.stop()


async def test_a_rebuild_during_a_poll_does_not_start_a_second_poll_inside_the_minimum_interval():
    drivers = await _rebuild_during_a_poll("192.0.2.1")
    assert len(drivers) == 2 and drivers[1].polls == 0


async def test_a_rebuild_with_another_address_polls_at_once():
    drivers = await _rebuild_during_a_poll("192.0.2.77")
    assert len(drivers) == 2 and drivers[1].polls == 1


async def test_ctx_tasks_do_not_keep_the_old_tasks_of_rebuilt_devices():
    ctx = Context(app=None, config=None, state=None)
    sup = DeviceSupervisor(DeviceRegistry(), spawn=ctx.spawn)
    sup.build_all([MODEM])
    sup.start()
    await _settle()
    assert len(ctx.tasks) == 1
    for n in range(5):
        await sup.apply([_cfg(MODEM, url=f"http://192.0.2.{20 + n}")])
        await _settle()
    assert len(ctx.tasks) == 1 and ctx.tasks[0] is sup._tasks["modem"]
    await sup.stop()
    await _settle()
    assert ctx.tasks == []
