"""The Settings API (api/settings.py) over the settings store: the shapes of api/settings_models.py, saves with the
conflict rule, versions, export and import, the write-only secrets and the viewer rules."""

import json

import pytest
from httpx import ASGITransport, AsyncClient

from pengumon import __version__, security
from pengumon.api.settings_models import (
    ConflictError, ImportDraft, PutResponse, SettingsError, SettingsSchema, SettingsState, SettingsValues,
    ValidateResponse, VersionDraft,
)
from pengumon.main import app, security_config
from pengumon.settings import registry

from settings_helpers import OWNER_SHAPED, WEBHOOKS, make_manager

TOKEN = "example-token"
BEARER = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture(autouse=True)
def _open(monkeypatch):
    monkeypatch.setattr(security_config, "access_token", "")
    monkeypatch.setattr(security_config, "allowed_hosts", [])
    monkeypatch.setattr(security_config, "allowed_origins", [])
    monkeypatch.setattr(security_config, "view_without_token", False)
    for name in ("PENGUMON_LOG_LEVEL", "PENGUMON_LOG_LEVELS", "PENGUMON_GRAFANA_URL", "PENGUMON_NOTIFY_URL",
                 "PENGUMON_NOTIFY_TOKEN"):
        monkeypatch.delenv(name, raising=False)


def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _install(app_state, tmp_path, text=OWNER_SHAPED, **kwargs):
    manager, _ = await make_manager(tmp_path, text, **kwargs)
    app_state.config, app_state.repository = manager.config, manager.repository
    app_state.settings, app_state.secret_store = manager, manager.secrets
    return manager


async def test_schema_serves_the_registry_with_ranges_and_an_etag(app_state, tmp_path):
    await _install(app_state, tmp_path)
    async with _client() as ac:
        res = await ac.get("/api/settings/schema")
        again = await ac.get("/api/settings/schema", headers={"If-None-Match": res.headers["etag"]})
    assert res.status_code == 200 and "no-cache" in res.headers["cache-control"]
    assert __version__ in res.headers["etag"] and again.status_code == 304
    body = SettingsSchema.model_validate(res.json())
    assert [s.key for s in body.settings] == [s.key for s in registry.SETTINGS]
    retention = next(s for s in body.settings if s.key == "database.retention_days")
    assert (retention.min, retention.max, retention.default, retention.unit) == (1, 90, 7, "days")
    assert set(body.lists) == {"devices", "services", "webhooks", "dns_probes"}
    assert "url" not in {f.name for f in body.lists["webhooks"].item_fields}


async def test_state_reports_stored_values_sources_lists_and_secrets_without_values(app_state, tmp_path):
    await _install(app_state, tmp_path, OWNER_SHAPED + WEBHOOKS, secrets={"webhooks": {"ha_token": "ha-secret-1"}},
                   environ={"PENGUMON_LOG_LEVEL": "DEBUG", "PENGUMON_NOTIFY_URL": "https://ntfy.example.com/t-123",
                            "PENGUMON_NOTIFY_TOKEN": ""})
    async with _client() as ac:
        res = await ac.get("/api/settings")
    assert res.status_code == 200
    for secret in ("path-token-abc", "q-secret", "inline-secret-xyz", "t-123", "ha-secret-1"):
        assert secret not in res.text
    state = SettingsState.model_validate(res.json())
    assert set(state.values) == set(registry.BY_KEY) and state.revision == 1 and state.can_edit
    assert state.values["database.retention_days"].model_dump() == {
        "value": 7, "stored": 7, "source": "stored", "pending_restart": False, "live_failed": False}
    assert (state.values["classifier.down_loss_pct"].source, state.values["classifier.down_loss_pct"].stored) == (
        "default", None)
    assert (state.values["server.log_level"].value, state.values["server.log_level"].source) == ("debug", "env")
    assert state.secrets["ntfy.url"].model_dump() == {"set": True, "source": "env", "host": "ntfy.example.com"}
    assert state.secrets["ntfy.token"].set is False
    assert state.secrets["webhook.Home.url"].model_dump() == {"set": True, "source": "secrets_file",
                                                              "host": "hooks.example.com"}
    assert state.secrets["webhook_header.ha_token"].set is True
    hook = state.lists["webhooks"].items[0]
    assert hook["headers"] == {"X-Plain": "secret:Home_X-Plain", "X-Ref": "secret:ha_token"} and "url" not in hook
    assert state.lists["devices"].set and [d["id"] for d in state.lists["devices"].items] == ["modem", "router"]
    assert state.about.import_report == [] and state.about.config_yaml.differing_keys == 0
    assert state.about.config_yaml.ignored_since is not None


async def test_a_viewer_gets_no_install_paths_cannot_save_and_cannot_export(app_state, tmp_path, monkeypatch):
    await _install(app_state, tmp_path)
    monkeypatch.setattr(security_config, "access_token", TOKEN)
    monkeypatch.setattr(security_config, "view_without_token", True)
    async with _client() as ac:
        viewer = (await ac.get("/api/settings")).json()
        editor = (await ac.get("/api/settings", headers=BEARER)).json()
        export = await ac.get("/api/settings/export")
        put = await ac.put("/api/settings", json={"base_revision": 1, "values": {"line.isp_name": "X"}})
        validate = await ac.post("/api/settings/validate", json={"values": {}})
        imported = await ac.post("/api/settings/import", content=b"line: {isp_name: X}\n")
        allowed = await ac.get("/api/settings/export", headers=BEARER)
    assert viewer["can_edit"] is False and viewer["about"] is None
    assert editor["can_edit"] is True and editor["about"]["token_set"] is True
    assert export.status_code in (401, 403) and put.status_code in (401, 403)
    assert validate.status_code in (401, 403) and imported.status_code in (401, 403)
    assert allowed.status_code == 200
    assert app_state.settings.revision == 1


def test_the_viewer_policy_denies_the_settings_export():
    assert not security.VIEWER_POLICY.permits("GET", "/api/settings/export")
    assert security.VIEWER_POLICY.permits("GET", "/api/settings")


async def test_values_are_the_effective_values(app_state, tmp_path):
    await _install(app_state, tmp_path, environ={"PENGUMON_GRAFANA_URL": "https://grafana.example.com:3000/"})
    async with _client() as ac:
        body = SettingsValues.model_validate((await ac.get("/api/settings/values")).json())
    assert body.revision == 1 and body.values["classifier.busy_load_pct"] == 70
    assert body.values["display.grafana_url"] == "https://grafana.example.com:3000"
    assert body.values["alerts.quiet_hours.start"] == "22:00" and body.values["dashboard.order"] == []


async def test_before_the_store_is_loaded_the_api_answers_503(app_state):
    async with _client() as ac:
        for path in ("/api/settings", "/api/settings/values", "/api/settings/schema", "/api/settings/versions",
                     "/api/settings/export"):
            assert (await ac.get(path)).status_code == 503, path


async def test_put_saves_validates_and_refuses_conflicts(app_state, tmp_path):
    manager = await _install(app_state, tmp_path)
    async with _client() as ac:
        ok = await ac.put("/api/settings", json={"base_revision": 1, "values": {"classifier.down_loss_pct": 35}})
        check = await ac.post("/api/settings/validate", json={"values": {"database.retention_days": 0}})
        bad = await ac.put("/api/settings", json={"base_revision": 2, "values": {"database.retention_days": 0}})
        stale = await ac.put("/api/settings", json={"base_revision": 1, "values": {"classifier.down_loss_pct": 30}})
        untouched = await ac.put("/api/settings", json={"base_revision": 1, "values": {"line.up_mbps": 40}})
        pinned = await ac.put("/api/settings", json={"base_revision": 3, "values": {}, "secrets": {"no.slot": "x"}})
        versions = (await ac.get("/api/settings/versions")).json()
    body = PutResponse.model_validate(ok.json())
    assert body.revision == 2 and body.applied == ["classifier.down_loss_pct"] and body.live_failed == []
    assert manager.config.classifier.down_loss_pct == 35
    checked = ValidateResponse.model_validate(check.json())
    assert not checked.ok and checked.errors[0].where == "values.database.retention_days"
    assert bad.status_code == 422
    assert SettingsError.model_validate(bad.json()).errors[0].where == "values.database.retention_days"
    assert stale.status_code == 409
    assert ConflictError.model_validate(stale.json()).conflicts == ["classifier.down_loss_pct"]
    assert untouched.status_code == 200 and untouched.json()["revision"] == 3
    assert pinned.status_code == 422 and pinned.json()["errors"][0]["where"] == "secrets.no.slot"
    assert [v["revision"] for v in versions] == [3, 2, 1]


async def test_a_version_loaded_and_saved_restores_it(app_state, tmp_path):
    manager = await _install(app_state, tmp_path)
    async with _client() as ac:
        await ac.put("/api/settings", json={"base_revision": 1, "values": {"classifier.busy_load_pct": 80}})
        draft = VersionDraft.model_validate((await ac.get("/api/settings/versions/1")).json())
        restored = await ac.put("/api/settings?source=restore", json={"base_revision": 2, "values": draft.values,
                                                                       "lists": draft.lists})
        wrong = await ac.put("/api/settings?source=hack", json={"base_revision": 3, "values": {}})
        gone = await ac.get("/api/settings/versions/77")
    assert restored.status_code == 200 and manager.config.classifier.busy_load_pct == 70
    assert (await manager.versions())[0]["source"] == "restore"
    assert wrong.status_code == 422 and gone.status_code == 404


async def test_the_store_its_versions_export_and_api_never_hold_a_secret(app_state, tmp_path, monkeypatch):
    """Every kind of secret: a device password, the ntfy URL and token, a webhook URL, a header secret, and an
    inline header value the import moved. None of them is in the export, a version, GET /api/settings or the
    database file."""
    from pengumon.secrets import credential_updates

    planted = {"device password": "Dev-Pass-111", "ntfy url": "ntfy-topic-222", "ntfy token": "tk-333",
               "webhook url": "hook-path-444", "header secret": "hdr-555", "imported inline": "inline-secret-xyz",
               "imported webhook url": "path-token-abc", "imported webhook query": "q-secret"}
    manager = await _install(app_state, tmp_path, OWNER_SHAPED + WEBHOOKS,
                             secrets={"webhooks": {"ha_token": "unused"}})
    manager.secrets.write(credential_updates("router", "admin", planted["device password"]))
    async with _client() as ac:
        saved = await ac.put("/api/settings", json={
            "base_revision": 1, "values": {"line.isp_name": "Changed"},
            "lists": {"webhooks": manager.list_items("webhooks") + [
                {"name": "Second", "headers": {"Authorization": "secret:second_auth"}}]},
            "secrets": {"ntfy.url": f"https://ntfy.example.com/{planted['ntfy url']}",
                        "ntfy.token": planted["ntfy token"],
                        "webhook.Home.url": f"https://hooks.example.com/{planted['webhook url']}",
                        "webhook.Second.url": f"https://hooks.example.com/{planted['webhook url']}-2",
                        "webhook_header.second_auth": planted["header secret"]}})
        assert saved.status_code == 200, saved.text
        texts = [(await ac.get("/api/settings")).text, (await ac.get("/api/settings/export")).text,
                 (await ac.get("/api/settings/versions")).text, (await ac.get("/api/settings/values")).text]
        for v in await manager.versions():
            texts.append((await ac.get(f"/api/settings/versions/{v['revision']}")).text)
    async with manager.repository._db.read() as db:
        async with db.execute("SELECT document FROM settings_versions") as cursor:
            texts += [row[0] for row in await cursor.fetchall()]
    await manager.repository.close()
    raw = b"".join(p.read_bytes() for p in tmp_path.glob("pengumon.db*"))
    for kind, secret in planted.items():
        for text in texts:
            assert secret not in text, kind
        assert secret.encode() not in raw, kind
    stored = json.loads((tmp_path / "secrets.json").read_text())
    assert stored["webhook_urls"]["Second"].endswith(planted["webhook url"] + "-2")


async def test_export_is_yaml_and_import_returns_a_draft(app_state, tmp_path):
    await _install(app_state, tmp_path)
    async with _client() as ac:
        export = await ac.get("/api/settings/export")
        draft = await ac.post("/api/settings/import", content=export.content)
        bomb = await ac.post("/api/settings/import", content=b"a: &x [1]\nb: *x\n")
        big = await ac.post("/api/settings/import", content=b"# x\n" * 70_000)
    assert export.status_code == 200 and export.headers["content-type"].startswith("application/yaml")
    assert "attachment" in export.headers["content-disposition"]
    body = ImportDraft.model_validate(draft.json())
    assert body.values["classifier.busy_load_pct"] == 70 and len(body.lists["devices"]) == 2
    assert bomb.status_code == 422 and "aliases" in bomb.json()["detail"]
    assert big.status_code == 413 or (big.status_code == 422 and "too big" in big.json()["detail"])


async def test_validate_counts_the_secrets_typed_but_not_saved(app_state, tmp_path):
    await _install(app_state, tmp_path)
    item = {"id": "chat", "name": "Chat"}
    async with _client() as ac:
        bare = await ac.post("/api/settings/validate", json={"values": {}, "lists": {"webhooks": [item]}})
        typed = await ac.post("/api/settings/validate", json={
            "values": {}, "lists": {"webhooks": [item]}, "secrets": {"webhook.chat.url": "https://hooks.example.test/x"}})
    assert [e["where"] for e in bare.json()["errors"]] == ["secrets.webhook.chat.url"]
    assert typed.json()["ok"] and typed.json()["errors"] == []


async def test_import_keeps_a_refused_value_and_says_what_it_left_out(app_state, tmp_path):
    await _install(app_state, tmp_path)
    text = "classifier:\n  busy_load_pct: 500\nntfy:\n  url: https://ntfy.example.test/t\nbogus:\n  thing: 1\n"
    async with _client() as ac:
        body = ImportDraft.model_validate((await ac.post("/api/settings/import", content=text.encode())).json())
    assert body.kept == ["classifier.busy_load_pct"] and "classifier.busy_load_pct" not in body.values
    notes = " | ".join(body.notes)
    assert "Busy line from: Enter at most 100" in notes and "Input should" not in notes
    assert "ntfy.url: secrets are entered on the page" in notes and "bogus.thing: not a setting" in notes
