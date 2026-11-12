"""What one settings save, check or import may cost: every list has a longest length, refused in plain words,
and the search for the value that breaks a rule across values is bounded."""

import json
import time

import pytest
from httpx import ASGITransport, AsyncClient

from pengumon.config import AppConfig
from pengumon.main import app, security_config
from pengumon.settings import lenient, registry

from settings_helpers import OWNER_SHAPED, make_manager

LOOSE = 20.0  # seconds: far under the minute the unbounded search took, far over what a loaded machine needs


@pytest.fixture(autouse=True)
def _open(monkeypatch):
    monkeypatch.setattr(security_config, "access_token", "")
    monkeypatch.setattr(security_config, "allowed_hosts", [])
    monkeypatch.setattr(security_config, "allowed_origins", [])
    monkeypatch.setattr(security_config, "view_without_token", False)


def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _install(app_state, tmp_path):
    manager, _ = await make_manager(tmp_path, OWNER_SHAPED)
    app_state.config, app_state.repository = manager.config, manager.repository
    app_state.settings, app_state.secret_store = manager, manager.secrets
    return manager


def _lookups(n):
    return [{"id": f"d{i}", "name": f"n{i}", "domain": "example.com"} for i in range(n)]


def test_every_list_has_a_longest_length():
    assert set(registry.LIST_MAX) == set(registry.LISTS)
    assert all(0 < n <= 200 for n in registry.LIST_MAX.values())


@pytest.mark.parametrize("name", sorted(registry.LISTS))
async def test_a_list_over_its_longest_length_is_refused_in_plain_words(app_state, tmp_path, name):
    await _install(app_state, tmp_path)
    items = [{"id": f"i{n}", "name": f"n{n}"} for n in range(registry.LIST_MAX[name] + 1)]
    started = time.monotonic()
    async with _client() as ac:
        res = await ac.post("/api/settings/validate", json={"values": {}, "lists": {name: items}})
    body = res.json()
    assert body["ok"] is False
    assert body["errors"][0]["where"] == f"lists.{name}"
    assert body["errors"][0]["message"].startswith(f"too many: at most {registry.LIST_MAX[name]} ")
    assert time.monotonic() - started < LOOSE


async def test_a_long_list_with_a_rule_across_values_is_answered_fast(app_state, tmp_path):
    """3000 lookups and a rule that fails at the top of the config used to cost one full validation per lookup."""
    await _install(app_state, tmp_path)
    change = {"values": {"router_levels.wan_critical_pct": 10}, "lists": {"dns_probes": _lookups(3000)}}
    started = time.monotonic()
    async with _client() as ac:
        res = await ac.post("/api/settings/validate", json=change)
    assert time.monotonic() - started < LOOSE
    assert res.json()["ok"] is False


async def test_importing_a_long_list_with_a_rule_across_values_is_answered_fast(app_state, tmp_path):
    await _install(app_state, tmp_path)
    text = json.dumps({"router_levels": {"wan_critical_pct": 10}, "dns_probes": _lookups(3000)})
    started = time.monotonic()
    async with _client() as ac:
        res = await ac.post("/api/settings/import", content=text.encode())
    assert time.monotonic() - started < LOOSE
    assert res.status_code == 200
    assert len(res.json()["lists"].get("dns_probes") or []) <= registry.LIST_MAX["dns_probes"]


def test_the_search_for_a_culprit_tries_a_bounded_number_of_candidates():
    data = {"dns_probes": _lookups(2500), "router_levels": {"wan_critical_pct": 10}}
    error = {"loc": (), "msg": "router_levels.wan_critical_pct must be above classifier.busy_load_pct"}
    candidates = [("dns_probes", i) for i in range(2500)]
    calls = []
    real = lenient._errors
    lenient._errors = lambda model, d: calls.append(1) or real(model, d)
    try:
        started = time.monotonic()
        lenient.culprits(AppConfig, data, error, (), candidates)
    finally:
        lenient._errors = real
    assert len(calls) <= lenient.MAX_TRIALS and time.monotonic() - started < LOOSE


# --- Infinity and NaN --------------------------------------------------------------------------------------

def _raw(extra):
    return ('{"values": {' + extra + '}, "lists": {}}').encode()


@pytest.mark.parametrize("number", ["Infinity", "-Infinity", "NaN"])
async def test_a_setting_that_is_not_a_finite_number_is_refused_by_every_settings_route(app_state, tmp_path, number):
    manager = await _install(app_state, tmp_path)
    body = _raw(f'"line.down_mbps": {number}')
    headers = {"Content-Type": "application/json"}
    async with _client() as ac:
        validate = await ac.post("/api/settings/validate", content=body, headers=headers)
        put = await ac.put("/api/settings", content=body.replace(b"{", b'{"base_revision": 1, "secrets": {}, ', 1),
                           headers=headers)
        preview = await ac.post("/api/settings/preview", content=body, headers=headers)
    assert validate.status_code == 200 and validate.json()["ok"] is False  # (this route answers ok: false)
    assert validate.json()["errors"] == [{"where": "values.line.down_mbps", "message": "must be a finite number"}]
    assert put.status_code == 422 and preview.status_code == 422
    assert manager.revision == 1


async def test_a_list_item_that_is_not_a_finite_number_is_refused_and_a_device_poll_interval_too(app_state, tmp_path):
    manager = await _install(app_state, tmp_path)
    device = {"id": "gw2", "driver": "xfinity_xb", "url": "http://192.168.88.9", "poll_interval_seconds": float("inf")}
    # the manager itself (the API refuses it earlier): validated, not stored
    checked = await manager.validate({"line.down_mbps": float("inf")}, {"devices": [device]})
    assert not checked.ok and {e["where"] for e in checked.errors} == {"values.line.down_mbps", "lists.devices"}
    from pengumon.devices.settings import DeviceConfig
    with pytest.raises(ValueError):
        DeviceConfig(id="gw2", driver="xfinity_xb", url="http://192.168.88.9", poll_interval_seconds=float("inf"))


async def test_importing_an_infinite_number_leaves_the_setting_as_it_is(app_state, tmp_path):
    await _install(app_state, tmp_path)
    text = b"line:\n  down_mbps: .inf\n  isp_name: Example ISP\n"
    async with _client() as ac:
        res = await ac.post("/api/settings/import", content=text)
    assert res.status_code == 200
    body = res.json()
    assert "line.down_mbps" in body["kept"] and "line.down_mbps" not in body["values"]
    assert body["values"]["line.isp_name"] == "Example ISP"
    assert any("finite number" in n for n in body["notes"])


# --- values are stored as validated ------------------------------------------------------------------------

async def test_a_value_is_stored_as_the_model_reads_it_not_as_it_was_sent(app_state, tmp_path):
    manager = await _install(app_state, tmp_path)
    async with _client() as ac:
        res = await ac.put("/api/settings", json={
            "base_revision": 1, "values": {"classifier.down_loss_pct": "40", "alerts.quiet_hours.start": " 23:00 "}})
        assert res.status_code == 200, res.text
    stored = manager.document().values
    assert stored["classifier.down_loss_pct"] == 40 and not isinstance(stored["classifier.down_loss_pct"], str)
    assert stored["alerts.quiet_hours.start"] == "23:00"


# --- items of the wrong shape answer 422, not 500 ----------------------------------------------------------

@pytest.mark.parametrize("name, item", [
    ("webhooks", {"name": 5}),
    ("webhooks", {"name": "Home", "headers": ["x"]}),
    ("webhooks", {"id": 7, "name": "Home"}),
    ("webhooks", {"name": "Home", "headers": {"X": 5}}),
    ("devices", {"id": "gw2", "driver": "xfinity_xb", "url": 5}),
    ("devices", {"id": "gw2", "driver": "xfinity_xb", "url": ["x"]}),
    ("services", {"id": "s", "name": "S", "url": "http://192.0.2.9/h", "ca_file": 5}),
    ("dns_probes", {"id": ["x"], "name": "n", "domain": "example.com"}),
])
async def test_a_list_item_of_the_wrong_shape_is_refused_not_a_500(app_state, tmp_path, name, item):
    manager = await _install(app_state, tmp_path)
    change = {"values": {}, "lists": {name: [item]}}
    async with _client() as ac:
        validate = await ac.post("/api/settings/validate", json=change)
        put = await ac.put("/api/settings", json={"base_revision": 1, **change})
    assert validate.status_code == 200 and validate.json()["ok"] is False
    assert put.status_code in (400, 422) and put.json()["errors"]
    assert manager.revision == 1


# --- live_failed clears once the setting applies -----------------------------------------------------------

async def test_a_setting_that_failed_to_apply_live_is_no_longer_marked_once_it_applies(tmp_path):
    from pengumon.settings.hub import SettingsHub
    hub, calls = SettingsHub(), []

    @hub.on(["classifier.down_loss_pct"], name="flaky")
    def _apply(change):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")

    manager, _ = await make_manager(tmp_path, OWNER_SHAPED, hub=hub)
    first = await manager.apply(1, {"classifier.down_loss_pct": 35}, {})
    assert first.live_failed == ["classifier.down_loss_pct"]
    assert manager.value_states()["classifier.down_loss_pct"]["live_failed"] is True
    second = await manager.apply(first.revision, {"classifier.down_loss_pct": 30}, {})
    assert second.live_failed == []
    state = manager.value_states()["classifier.down_loss_pct"]
    assert state["live_failed"] is False and state["pending_restart"] is False


# --- secrets are removed after the commit, not before ------------------------------------------------------

async def test_a_commit_that_fails_leaves_the_secrets_of_a_removed_webhook(tmp_path, monkeypatch):
    from settings_helpers import WEBHOOKS
    manager, _ = await make_manager(tmp_path, OWNER_SHAPED + WEBHOOKS)
    before = json.dumps(manager.secrets.data(), sort_keys=True)
    assert manager.secret_value("webhook.Home.url")

    async def broken(*args, **kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(manager.repository, "save_settings", broken)
    with pytest.raises(OSError):
        await manager.apply(1, {}, {"webhooks": []})
    assert json.dumps(manager.secrets.data(), sort_keys=True) == before
    assert manager.secret_value("webhook.Home.url")


async def test_a_saved_removal_still_deletes_the_secrets_of_the_removed_webhook(tmp_path):
    from settings_helpers import WEBHOOKS
    manager, _ = await make_manager(tmp_path, OWNER_SHAPED + WEBHOOKS)
    assert manager.secret_value("webhook.Home.url")
    await manager.apply(1, {}, {"webhooks": []})
    assert manager.secret_value("webhook.Home.url") is None


# --- a failed commit puts back the secrets it replaced ---------------------------------------------------------

async def _failing_save(manager, monkeypatch):
    async def broken(*args, **kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(manager.repository, "save_settings", broken)


async def test_a_commit_that_fails_puts_back_a_replaced_secret_in_memory_and_on_disk(tmp_path, monkeypatch):
    from settings_helpers import WEBHOOKS
    manager, _ = await make_manager(tmp_path, OWNER_SHAPED + WEBHOOKS)
    old = manager.secret_value("webhook.Home.url")
    assert old
    on_disk = manager.secrets.path.read_text()
    await _failing_save(manager, monkeypatch)
    with pytest.raises(OSError):
        await manager.apply(1, {}, {}, {"webhook.Home.url": "https://hooks.example.com/replacement"})
    assert manager.secret_value("webhook.Home.url") == old
    assert json.loads(manager.secrets.path.read_text()) == json.loads(on_disk)


async def test_a_commit_that_fails_leaves_no_secret_of_a_slot_that_had_none(tmp_path, monkeypatch):
    manager, _ = await make_manager(tmp_path, OWNER_SHAPED)
    assert manager.secret_value("ntfy.url") is None
    await _failing_save(manager, monkeypatch)
    with pytest.raises(OSError):
        await manager.apply(1, {}, {}, {"ntfy.url": "https://ntfy.example.com/topic"})
    assert manager.secret_value("ntfy.url") is None
    assert "ntfy" not in json.loads(manager.secrets.path.read_text())


async def test_a_saved_replacement_is_written_before_the_commit_and_stays(tmp_path):
    from settings_helpers import WEBHOOKS
    manager, _ = await make_manager(tmp_path, OWNER_SHAPED + WEBHOOKS)
    seen = []
    real = manager.repository.save_settings

    async def watching(*args, **kwargs):
        seen.append(manager.secret_value("webhook.Home.url"))  # a crash here must find the new secret on disk
        return await real(*args, **kwargs)
    manager.repository.save_settings = watching
    await manager.apply(1, {}, {}, {"webhook.Home.url": "https://hooks.example.com/replacement"})
    assert seen == ["https://hooks.example.com/replacement"]
    assert manager.secret_value("webhook.Home.url") == "https://hooks.example.com/replacement"
