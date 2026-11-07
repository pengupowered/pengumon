"""The environment rule (pengumon.settings.env: an empty variable counts as unset) and the settings hub
(pengumon.settings.hub: who is told about which change, in what order, and what a failing applier costs)."""

import pytest

from pengumon.settings.document import SettingsDocument
from pengumon.settings.env import (
    SECRET_ENV, SETTING_ENV, env_value, secret_override, setting_override, setting_overrides,
)
from pengumon.settings.hub import Change, SettingsHub


# -- env ------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("environ, expected", [
    ({}, None),
    ({"PENGUMON_LOG_LEVEL": ""}, None),           # compose passes ${VAR:-}: unset arrives as ""
    ({"PENGUMON_LOG_LEVEL": "   "}, None),
    ({"PENGUMON_LOG_LEVEL": "debug"}, "debug"),
    ({"PENGUMON_LOG_LEVEL": " debug\n"}, "debug"),
])
def test_an_empty_variable_counts_as_unset(environ, expected):
    assert env_value("PENGUMON_LOG_LEVEL", environ) == expected


def test_env_value_reads_the_process_environment_by_default(monkeypatch):
    monkeypatch.setenv("PENGUMON_GRAFANA_URL", "https://grafana.example.com")
    assert env_value("PENGUMON_GRAFANA_URL") == "https://grafana.example.com"
    monkeypatch.setenv("PENGUMON_GRAFANA_URL", "")
    assert env_value("PENGUMON_GRAFANA_URL") is None


def test_the_overrides_map_keys_and_slots_to_their_variables():
    assert SETTING_ENV == {"server.log_level": "PENGUMON_LOG_LEVEL", "server.log_levels": "PENGUMON_LOG_LEVELS",
                           "display.grafana_url": "PENGUMON_GRAFANA_URL",
                           "access.allow_viewers": "PENGUMON_VIEW_WITHOUT_TOKEN"}
    assert SECRET_ENV == {"ntfy.url": "PENGUMON_NOTIFY_URL", "ntfy.token": "PENGUMON_NOTIFY_TOKEN"}
    environ = {"PENGUMON_LOG_LEVEL": "debug", "PENGUMON_LOG_LEVELS": "", "PENGUMON_NOTIFY_TOKEN": "tk-example"}
    assert setting_overrides(environ) == {"server.log_level": "debug"}
    assert setting_override("server.log_levels", environ) is None
    assert setting_override("classifier.busy_load_pct", environ) is None  # no variable for it
    assert secret_override("ntfy.token", environ) == "tk-example" and secret_override("ntfy.url", environ) is None


# -- hub ------------------------------------------------------------------------------------------------------------

async def test_appliers_run_once_in_registration_order_when_their_parts_change():
    hub, calls = SettingsHub(), []

    @hub.on(["prober.timeout_seconds"], name="timeout")
    def _timeout(change):
        calls.append(("timeout", change.new.value("prober.timeout_seconds")))

    @hub.on(prefixes=["alerts."], slots=["ntfy.url", "webhook."], lists=["webhooks"], name="notifier")
    async def _notifier(change):
        calls.append(("notifier", sorted(change.keys)))

    @hub.on(lists=["devices"], events=["devices_rebuilt"], name="line_load")
    def _line(change):
        calls.append(("line_load", sorted(change.events)))

    doc = SettingsDocument(values={"prober.timeout_seconds": 0.5})
    assert await hub.publish(Change.of(keys=["prober.timeout_seconds", "alerts.quiet_hours.start"], new=doc)) == []
    assert calls == [("timeout", 0.5), ("notifier", ["alerts.quiet_hours.start", "prober.timeout_seconds"])]

    calls.clear()
    await hub.publish(Change.of(slots=["webhook.home.url"]))
    await hub.publish(Change.of(events=["devices_rebuilt"]))
    await hub.publish(Change.of(keys=["classifier.busy_load_pct"]))  # nobody subscribes: nothing runs
    assert calls == [("notifier", []), ("line_load", ["devices_rebuilt"])]
    assert hub.names == ["timeout", "notifier", "line_load"]


async def test_a_failing_applier_is_contained_and_named():
    hub, calls = SettingsHub(), []

    @hub.on(["a.x"], name="breaks")
    async def _breaks(change):
        raise RuntimeError("boom")

    @hub.on(["a.x", "a.y"], name="works")
    def _works(change):
        calls.append("works")

    change = Change.of(keys=["a.x", "a.y"])
    assert await hub.publish(change) == ["breaks"]
    assert calls == ["works"]  # the one after it still ran
    assert hub.touched_by("breaks", change) == ["a.x"] and hub.touched_by("works", change) == ["a.x", "a.y"]
    assert hub.subscribers(change) == ["breaks", "works"]


def test_a_subscription_must_name_something_and_be_unique():
    hub = SettingsHub()
    with pytest.raises(ValueError, match="subscribes to nothing"):
        hub.on(name="empty")
    hub.on(["k"], name="one")(lambda change: None)
    with pytest.raises(ValueError, match="twice"):
        hub.on(["k"], name="one")
    with pytest.raises(KeyError):
        hub.touched_by("nobody", Change.of(keys=["k"]))


def test_a_slot_prefix_needs_its_dot():
    hub = SettingsHub()
    hub.on(slots=["ntfy.url"], name="exact")(lambda change: None)
    assert hub.subscribers(Change.of(slots=["ntfy.url.extra"])) == []
    assert Change.of().empty and not Change.of(lists=["devices"]).empty


def test_the_document_round_trips_and_leaves_default_lists_out():
    doc = SettingsDocument(values={"classifier.down_loss_pct": 35.0}, lists={"devices": [], "dns_probes": None})
    assert doc.to_json() == {"values": {"classifier.down_loss_pct": 35.0}, "lists": {"devices": []}}
    again = SettingsDocument.from_json(doc.to_json())
    assert again.list_items("devices") == [] and again.list_items("dns_probes") is None
    assert again.value("classifier.busy_load_pct", 60.0) == 60.0
