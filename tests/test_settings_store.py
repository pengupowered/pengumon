"""The settings store (db/settings.py), the one-time import (settings/importer.py), lenient reading
(settings/lenient.py) and the manager's start and save (settings/manager.py)."""

import json
import logging

import pytest

from pengumon.config import AppConfig, load_config
from pengumon.db.settings import MAX_VERSIONS, SettingsConflict
from pengumon.settings import lenient, registry
from pengumon.settings.document import SettingsDocument
from pengumon.settings.importer import get_path, jsonable, read_settings
from pengumon.settings.manager import SettingsInvalid

from settings_helpers import OWNER_SHAPED, WEBHOOKS, make_manager, write_config


# --- lenient reading -----------------------------------------------------------------------------------------------

def test_lenient_reading_leaves_out_only_what_is_refused():
    config, dropped = lenient.validate_leniently(AppConfig, {
        "database": {"retention_days": 0, "rollup_retention_days": 500},
        "classifier": {"busy_load_pct": 70, "consecutive_failures_for_incident": 0},
        "devices": [{"id": "a", "driver": "asuswrt", "url": "http://192.0.2.1"},
                    {"id": "b", "driver": "nope", "url": "http://192.0.2.2"},
                    {"id": "c", "driver": "asuswrt", "url": "http://192.0.2.3"}],
    })
    assert config.database.retention_days == 7 and config.database.rollup_retention_days == 500
    assert config.classifier.busy_load_pct == 70 and config.classifier.consecutive_failures_for_incident == 3
    assert [d.id for d in config.devices] == ["a", "c"]
    assert sorted(path for path, _ in dropped) == ["classifier.consecutive_failures_for_incident",
                                                   "database.retention_days", "devices[1]"]


def test_a_rule_across_values_costs_the_value_that_breaks_it():
    # degraded 50 with the default down (40): only degraded goes; down 30 alone (default degraded 15) is fine
    config, dropped = lenient.validate_leniently(AppConfig, {"classifier": {"degraded_loss_pct": 50,
                                                                             "public_latency_spike_threshold_ms": 90}})
    assert [p for p, _ in dropped] == ["classifier.degraded_loss_pct"]
    assert config.classifier.public_latency_spike_threshold_ms == 90
    # across sections: the WAN critical level must be above the busy level
    config, dropped = lenient.validate_leniently(AppConfig, {"classifier": {"busy_load_pct": 80},
                                                             "router_levels": {"wan_critical_pct": 70}})
    assert len(dropped) == 1 and config.router_levels.wan_critical_pct > config.classifier.busy_load_pct


def test_duplicate_device_ids_cost_the_second_one_and_keep_the_file_index():
    config, dropped = lenient.validate_leniently(AppConfig, {"devices": [
        {"id": "x", "driver": "nope", "url": "http://192.0.2.9"},
        {"id": "a", "driver": "asuswrt", "url": "http://192.0.2.1"},
        {"id": "a", "driver": "asuswrt", "url": "http://192.0.2.2"}]})
    assert [d.url for d in config.devices] == ["http://192.0.2.1"]
    assert [p for p, _ in dropped] == ["devices[0]", "devices[2]"]  # indexes in the file, not after removals


def test_load_config_lenient_keeps_the_start_going_but_not_for_a_bad_bootstrap_key(tmp_path, caplog):
    path = write_config(tmp_path, "database: {retention_days: 0}\nclassifier: {busy_load_pct: 5}\n")
    with pytest.raises(ValueError):
        load_config(str(path))  # the whole file, as the build before the store read it
    with caplog.at_level(logging.WARNING, logger="pengumon.config"):
        config = load_config(str(path), lenient_rest=True)
    assert config.database.retention_days == 7 and config.classifier.busy_load_pct == 60
    assert "classifier.busy_load_pct, database.retention_days" in caplog.text
    bad = write_config(tmp_path, "server: {port: not-a-port}\n", "bad.yaml")
    with pytest.raises(ValueError, match="server.port"):
        load_config(str(bad), lenient_rest=True)


def test_a_planted_secret_never_reaches_the_lenient_log(tmp_path, caplog):
    planted = "Planted-Pass-123"
    path = write_config(tmp_path, f'devices: [{{id: r, driver: asuswrt, url: "http://admin:{planted}@192.0.2.1"}}]\n'
                                  f'notifications: {{webhooks: [{{name: w, url: "ftp://{planted}.example.com"}}]}}\n')
    with caplog.at_level(logging.DEBUG):
        config = load_config(str(path), lenient_rest=True)
        result = read_settings(load_config.__globals__["read_config_file"](path), move_secrets=True)
    assert config.devices == [] and config.notifications.webhooks == []
    assert planted not in caplog.text and planted not in json.dumps(result.report)
    assert {r["key"] for r in result.report} == {"devices[0]", "notifications.webhooks[0]"}


# --- the one-time import -------------------------------------------------------------------------------------------

async def test_the_import_of_an_owner_shaped_config_equals_the_old_load_key_by_key(tmp_path):
    manager, path = await make_manager(tmp_path)
    old = load_config(str(path))
    for setting in registry.SETTINGS:
        assert jsonable(get_path(manager.config, setting.key)) == jsonable(get_path(old, setting.key)), setting.key
    for name, spec in registry.LISTS.items():
        assert jsonable(list(get_path(manager.config, spec.path))) == jsonable(list(get_path(old, spec.path))), name
    stored = manager.document()
    assert stored.values["classifier.busy_load_pct"] == 70
    assert stored.values["classifier.consecutive_failures_for_incident"] == 3  # equal to the default: still stored
    assert "server.host" not in stored.values and "prober.interval_seconds" not in stored.values
    assert "prober.ping_count_per_probe" not in stored.values
    assert set(stored.lists) == {"devices", "services", "dns_probes"}  # webhooks: not in the file, default
    versions = await manager.versions()
    assert [(v["revision"], v["source"]) for v in versions] == [(1, "import")]
    assert versions[0]["summary"] == ("Imported from config.yaml: 15 values; devices 2, services 2, "
                                      "DNS lookups 2")
    assert manager.import_report() == []


async def test_an_out_of_range_value_is_skipped_reported_and_the_start_goes_on(tmp_path, caplog):
    text = OWNER_SHAPED.replace("retention_days: 7", "retention_days: 0").replace("busy_load_pct: 70",
                                                                                   "busy_load_pct: 5")
    with caplog.at_level(logging.INFO, logger="pengumon.settings"):
        manager, _ = await make_manager(tmp_path, text)
    assert manager.config.database.retention_days == 7 and manager.config.classifier.busy_load_pct == 60
    assert [r["key"] for r in manager.import_report()] == ["classifier.busy_load_pct", "database.retention_days"]
    assert all(r["reason"] for r in manager.import_report())
    assert "2 left out (defaults kept): classifier.busy_load_pct, database.retention_days" in caplog.text
    assert "database.retention_days" not in manager.document().values


async def test_the_import_runs_once(tmp_path):
    manager, path = await make_manager(tmp_path)
    write_config(tmp_path, OWNER_SHAPED.replace("busy_load_pct: 70", "busy_load_pct: 80"))
    again, _ = await make_manager(tmp_path, OWNER_SHAPED.replace("busy_load_pct: 70", "busy_load_pct: 80"))
    assert again.config.classifier.busy_load_pct == 70 and again.revision == 1
    assert [v["revision"] for v in await again.versions()] == [1]
    assert again.differing_keys == ["classifier.busy_load_pct"]


async def test_an_onset_count_without_a_recovery_count_keeps_recovering_after_the_onset_count(tmp_path):
    """Before the store, recovery reused the loss onset count: a file with onset 5 and no recovery key recovered
    after 5 good rounds, and still does after the import."""
    manager, _ = await make_manager(tmp_path, OWNER_SHAPED.replace("consecutive_failures_for_incident: 3",
                                                                   "consecutive_failures_for_incident: 5"))
    assert manager.document().values["classifier.consecutive_successes_for_recovery"] == 5
    assert manager.config.classifier.consecutive_successes_for_recovery == 5
    (tmp_path / "set").mkdir()
    both, _ = await make_manager(tmp_path / "set", "classifier: {consecutive_failures_for_incident: 5, "
                                                   "consecutive_successes_for_recovery: 2}\n")
    assert both.config.classifier.consecutive_successes_for_recovery == 2  # the file's own recovery count wins
    (tmp_path / "default").mkdir()
    same, _ = await make_manager(tmp_path / "default", OWNER_SHAPED)  # onset 3 = the recovery default: nothing added
    assert "classifier.consecutive_successes_for_recovery" not in same.document().values
    (tmp_path / "bad").mkdir()
    bad, _ = await make_manager(tmp_path / "bad", "classifier: {consecutive_failures_for_incident: 0}\n")
    assert bad.config.classifier.consecutive_successes_for_recovery == 3  # a refused onset is not copied


async def test_the_store_refuses_a_second_import(tmp_path):
    manager, _ = await make_manager(tmp_path)
    again = SettingsDocument(values={"classifier.busy_load_pct": 90})
    assert await manager.repository.initialise_settings(again, "again", []) is False
    revision, stored = await manager.repository.load_settings()
    assert revision == 1 and stored.values["classifier.busy_load_pct"] == 70


async def test_an_absent_list_stays_default_and_an_empty_one_is_set(tmp_path):
    manager, _ = await make_manager(tmp_path, "service_checks: {services: []}\n")
    assert manager.document().lists == {"services": []}
    assert [p.id for p in manager.config.dns_probes] == ["dns_system_google", "dns_direct_cloudflare"]
    (tmp_path / "x").mkdir()
    empty, _ = await make_manager(tmp_path / "x", "dns_probes: []\n")
    assert empty.config.dns_probes == [] and empty.document().lists == {"dns_probes": []}


async def test_no_file_starts_with_the_defaults_as_revision_1(tmp_path):
    manager, _ = await make_manager(tmp_path, "")
    assert manager.revision == 1 and manager.document() == SettingsDocument()
    assert (await manager.versions())[0]["summary"] == "Started with the defaults"


async def test_a_dns_lookup_with_a_hop_id_is_skipped(tmp_path):
    text = OWNER_SHAPED.replace("{id: dns_a,", "{id: gw,")
    manager, _ = await make_manager(tmp_path, text)
    assert [p["id"] for p in manager.document().lists["dns_probes"]] == ["dns_b"]
    assert manager.import_report() == [{"key": "dns_probes[0]", "reason": "the id is taken by a hop of the probe path"}]


async def test_webhook_urls_and_inline_header_values_move_to_secrets_json(tmp_path):
    manager, _ = await make_manager(tmp_path, OWNER_SHAPED + WEBHOOKS, secrets={"webhooks": {"ha_token": "t-1"}})
    (item,) = manager.document().lists["webhooks"]
    assert item["id"] == "Home" and "url" not in item
    assert item["headers"] == {"X-Plain": "secret:Home_X-Plain", "X-Ref": "secret:ha_token"}
    stored = json.loads((tmp_path / "secrets.json").read_text())
    assert stored["webhook_urls"] == {"Home": "https://hooks.example.com/path-token-abc?k=q-secret"}
    assert stored["webhooks"] == {"ha_token": "t-1", "Home_X-Plain": "inline-secret-xyz"}
    # the Notifier still runs on the effective config: the webhook with its URL from the slot
    (hook,) = manager.config.notifications.webhooks
    assert hook.url == "https://hooks.example.com/path-token-abc?k=q-secret"
    raw = (tmp_path / "pengumon.db").read_bytes()
    for secret in (b"path-token-abc", b"q-secret", b"inline-secret-xyz"):
        assert secret not in raw


async def test_the_start_line_names_differing_keys_never_values(tmp_path, caplog):
    await make_manager(tmp_path)
    changed = OWNER_SHAPED.replace("isp_name: Example ISP", "isp_name: Secret-Name-ISP").replace(
        "busy_load_pct: 70", "busy_load_pct: 75")
    with caplog.at_level(logging.INFO, logger="pengumon.settings"):
        manager, _ = await make_manager(tmp_path, changed)
    lines = [r.getMessage() for r in caplog.records if "config.yaml is ignored" in r.getMessage()]
    assert len(lines) == 1 and "2 keys differ: classifier.busy_load_pct, line.isp_name" in lines[0]
    assert "Secret-Name-ISP" not in caplog.text and "75" not in lines[0]
    assert manager.config_yaml_state()["differing_keys"] == 2


async def test_unknown_stored_keys_are_kept_ignored_and_logged(tmp_path, caplog):
    manager, _ = await make_manager(tmp_path)
    async with manager.repository._db.write() as db:
        await db.execute("INSERT INTO settings_values (key, value, updated) VALUES ('future.knob', '1', 0)")
    with caplog.at_level(logging.INFO, logger="pengumon.settings"):
        again, _ = await make_manager(tmp_path)
    assert "future.knob" in caplog.text and "future.knob" not in again.document().values
    await again.apply(again.revision, {"classifier.busy_load_pct": 65}, {})
    _, stored = await again.repository.load_settings()
    assert stored.values["future.knob"] == 1  # a save touches only what it changes


async def test_a_stored_value_this_build_refuses_costs_that_value_only(tmp_path):
    manager, _ = await make_manager(tmp_path)
    async with manager.repository._db.write() as db:
        await db.execute("UPDATE settings_values SET value = '500' WHERE key = 'classifier.busy_load_pct'")
    again, _ = await make_manager(tmp_path)
    assert again.config.classifier.busy_load_pct == 60 and again.config.line.isp_name == "Example ISP"


# --- saves ---------------------------------------------------------------------------------------------------------

async def test_save_applies_in_place_and_versions_with_a_summary(tmp_path):
    manager, _ = await make_manager(tmp_path)
    live_classifier = manager.config.classifier
    hop_status = manager.config.prober.hop_status
    result = await manager.apply(1, {"classifier.down_loss_pct": 35, "prober.hop_status.public_latency_ms": 70,
                                     "line.isp_name": None}, {})
    assert result.revision == 2 and manager.revision == 2
    assert manager.config.classifier is live_classifier and live_classifier.down_loss_pct == 35
    assert manager.config.prober.hop_status is hop_status and hop_status.public_latency_ms == 70
    assert manager.config.line.isp_name is None  # reset to the default
    assert "40 % → 35 %" in result.summary and "60 ms → 70 ms" in result.summary
    assert set(result.applied) == {"classifier.down_loss_pct", "line.isp_name", "prober.hop_status.public_latency_ms"}
    versions = await manager.versions()
    assert [(v["revision"], v["source"]) for v in versions] == [(2, "save"), (1, "import")]


async def test_the_conflict_rule_refuses_only_touched_parts_changed_elsewhere(tmp_path):
    manager, _ = await make_manager(tmp_path)
    await manager.apply(1, {"classifier.busy_load_pct": 65}, {})        # revision 2, elsewhere
    await manager.apply(1, {"line.isp_name": "Other"}, {})               # an untouched key changed: no conflict
    assert manager.revision == 3
    await manager.apply(1, {}, {"services": []})                         # a list unchanged since 1: no conflict
    with pytest.raises(SettingsConflict) as refused:
        await manager.apply(1, {"classifier.busy_load_pct": 75}, {})     # touched, and changed since 1
    assert (refused.value.current, refused.value.conflicts) == (4, ["classifier.busy_load_pct"])
    assert manager.config.classifier.busy_load_pct == 65


async def test_a_base_version_no_longer_kept_makes_every_touched_part_a_conflict(tmp_path):
    manager, _ = await make_manager(tmp_path)
    for n in range(MAX_VERSIONS + 2):
        await manager.apply(manager.revision, {"uptime.outage_dead_minutes": 2 + n % 5}, {})
    versions = await manager.versions()
    assert len(versions) == MAX_VERSIONS and versions[-1]["revision"] == manager.revision - MAX_VERSIONS + 1
    with pytest.raises(SettingsConflict) as refused:
        await manager.apply(1, {"line.isp_name": "X"}, {})
    assert refused.value.conflicts == ["line.isp_name"]


async def test_restoring_a_version_brings_its_values_and_lists_back(tmp_path):
    manager, _ = await make_manager(tmp_path)
    await manager.apply(1, {"classifier.down_loss_pct": 35, "alerts.delay_seconds": 120},
                        {"services": [], "dns_probes": None})
    draft = await manager.version_draft(1)
    assert draft["values"]["alerts.delay_seconds"] is None  # stored now, not then: reset
    result = await manager.apply(manager.revision, draft["values"], draft["lists"], source="restore")
    assert manager.config.classifier.down_loss_pct == 40.0 and manager.config.alerts.delay_seconds == 0
    assert [s.id for s in manager.config.service_checks.services] == ["app1", "app2"]
    assert [p.id for p in manager.config.dns_probes] == ["dns_a", "dns_b"]
    assert (await manager.versions())[0]["source"] == "restore" and result.revision == 3
    assert await manager.version_draft(99) is None


async def test_validation_refuses_with_a_place_and_never_the_value(tmp_path):
    manager, _ = await make_manager(tmp_path)
    checked = await manager.validate({"database.retention_days": 0, "classifier.degraded_loss_pct": 45,
                                      "no.such": 1}, {})
    assert not checked.ok and checked.errors == [{"where": "values.no.such", "message": "no such setting"}]
    checked = await manager.validate({"database.retention_days": 0, "classifier.degraded_loss_pct": 45}, {})
    wheres = {e["where"] for e in checked.errors}
    assert wheres == {"values.database.retention_days", "values.classifier.degraded_loss_pct"}
    checked = await manager.validate({}, {"devices": [{"id": "r", "driver": "asuswrt",
                                                       "url": "http://admin:Planted-77@192.0.2.1"}]})
    assert [e["where"] for e in checked.errors] == ["lists.devices[0].url"]
    assert "Planted-77" not in json.dumps(checked.errors)
    with pytest.raises(SettingsInvalid):
        await manager.apply(1, {"database.retention_days": 0}, {})
    assert manager.revision == 1 and manager.config.database.retention_days == 7


async def test_validation_warnings_are_not_refusals(tmp_path):
    manager, _ = await make_manager(tmp_path)
    checked = await manager.validate({"database.retention_days": 3, "privacy.client_details": "full"}, {})
    assert checked.ok
    messages = [w["message"] for w in checked.warnings]
    assert "retention under 7 days: line-capacity learning needs 7" in messages
    assert ("lowering raw retention from 7 to 3 days deletes 4 days of samples at the next hourly cleanup"
            in messages)
    assert any("client details full" in m for m in messages)


async def test_list_rules_ids_renames_and_hop_ids(tmp_path):
    manager, _ = await make_manager(tmp_path)
    devices = manager.list_items("devices")
    renamed = [dict(devices[0], id="modem2"), devices[1]]
    checked = await manager.validate({}, {"devices": renamed})
    assert any("can't be renamed" in e["message"] and e["where"] == "lists.devices[0].id" for e in checked.errors)
    twice = await manager.validate({}, {"services": manager.list_items("services") * 2})
    assert {e["where"] for e in twice.errors} >= {"lists.services[2].id", "lists.services[3].id"}
    hop = await manager.validate({}, {"dns_probes": [{"id": "gw", "name": "Taken"}]})
    assert [e["where"] for e in hop.errors] == ["lists.dns_probes[0].id"]
    ca = await manager.validate({}, {"services": [{"id": "s", "name": "S", "url": "https://app.example.com/",
                                                   "ca_file": str(tmp_path / "nope.pem")}]})
    assert [e["message"] for e in ca.errors] == ["CA file not found"]


async def test_an_env_pinned_key_or_slot_refuses_a_write(tmp_path):
    manager, _ = await make_manager(tmp_path, environ={"PENGUMON_LOG_LEVEL": "debug",
                                                       "PENGUMON_NOTIFY_URL": "https://ntfy.example.com/t"})
    assert manager.config.server.log_level == "debug"
    assert manager.value_states()["server.log_level"]["source"] == "env"
    with pytest.raises(SettingsInvalid) as refused:
        await manager.apply(1, {"server.log_level": "error"}, {})
    assert refused.value.status == 400 and "PENGUMON_LOG_LEVEL" in refused.value.errors[0]["message"]
    with pytest.raises(SettingsInvalid) as slot:
        await manager.apply(1, {}, {}, {"ntfy.url": "https://ntfy.example.com/other"})
    assert slot.value.status == 400 and "PENGUMON_NOTIFY_URL" in slot.value.errors[0]["message"]


async def test_secret_slots_are_written_cleared_and_never_stored(tmp_path):
    manager, _ = await make_manager(tmp_path)
    hook = {"name": "Alerts", "format": "slack", "headers": {"Authorization": "secret:alerts_auth"}}
    missing = await manager.validate({}, {"webhooks": [hook]})
    assert {e["where"] for e in missing.errors} == {"secrets.webhook.Alerts.url", "secrets.webhook_header.alerts_auth"}
    plain = await manager.validate({}, {"webhooks": [dict(hook, headers={"X-Key": "plain-value"})]})
    assert any(e["where"] == "lists.webhooks[0].headers.X-Key" for e in plain.errors)
    await manager.apply(1, {}, {"webhooks": [hook]},
                        {"webhook.Alerts.url": "https://hooks.example.com/T0K3N", "webhook_header.alerts_auth": "Bearer s3",
                         "ntfy.url": "https://ntfy.example.com/my-topic", "ntfy.token": "tk_9"})
    states = manager.secret_states()
    assert states["webhook.Alerts.url"] == {"set": True, "source": "secrets_file", "host": "hooks.example.com"}
    assert states["ntfy.token"] == {"set": True, "source": "secrets_file"}
    assert manager.secret_value("ntfy.url") == "https://ntfy.example.com/my-topic"
    assert manager.config.notifications.webhooks[0].url == "https://hooks.example.com/T0K3N"
    await manager.apply(manager.revision, {}, {}, {"ntfy.token": ""})
    assert manager.secret_states()["ntfy.token"] == {"set": False, "source": None}
    await manager.apply(manager.revision, {}, {"webhooks": []})  # removing the webhook removes its secrets
    stored = json.loads((tmp_path / "secrets.json").read_text())
    assert "webhook_urls" not in stored and "alerts_auth" not in stored.get("webhooks", {})
    assert stored["ntfy"] == {"url": "https://ntfy.example.com/my-topic"}


async def test_a_secrets_write_failure_commits_nothing(tmp_path, monkeypatch):
    from pengumon.settings.manager import SecretsWriteFailed
    manager, _ = await make_manager(tmp_path)

    def broken(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("pengumon.secrets.write_secrets", broken)
    with pytest.raises(SecretsWriteFailed):
        await manager.apply(1, {"line.isp_name": "X"}, {}, {"ntfy.token": "tk"})
    assert manager.revision == 1 and await manager.repository.settings_revision() == 1


async def test_export_round_trips_through_import(tmp_path):
    manager, _ = await make_manager(tmp_path, OWNER_SHAPED + WEBHOOKS)
    text = manager.export_yaml()
    assert text.startswith("# Pengumon settings")
    draft = await manager.import_yaml(text)
    assert draft["values"] == manager.document().values
    assert draft["lists"] == manager.document().lists
    assert draft["notes"] == []
    (tmp_path / "other").mkdir()
    other, _ = await make_manager(tmp_path / "other", "")
    await other.apply(other.revision, draft["values"], draft["lists"],
                      {"webhook.Home.url": "https://hooks.example.com/new", "webhook_header.Home_X-Plain": "v",
                       "webhook_header.ha_token": "t"},
                      source="yaml_import")
    assert other.document().values == manager.document().values


async def test_import_of_a_whole_config_yaml_leaves_secrets_bootstrap_and_targets_out(tmp_path):
    manager, _ = await make_manager(tmp_path, "")
    draft = await manager.import_yaml(OWNER_SHAPED + WEBHOOKS)
    assert "server.host" not in draft["values"] and "prober.interval_seconds" not in draft["values"]
    (hook,) = draft["lists"]["webhooks"]
    assert "url" not in hook and hook["headers"]["X-Plain"] == "secret:Home_X-Plain"
    text = json.dumps(draft)
    for secret in ("path-token-abc", "q-secret", "inline-secret-xyz"):
        assert secret not in text
    assert any("1 webhook URL in the file was not imported" in n for n in draft["notes"])
    assert any("left in config.yaml" in n or "targets" in n for n in draft["notes"])
    with pytest.raises(ValueError, match="(?i)anchors and aliases"):
        await manager.import_yaml("a: &x 1\nb: *x\n")
