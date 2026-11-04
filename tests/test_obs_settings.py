"""Telemetry of the Settings: saves by source and outcome, live appliers that failed, imports, and the preview jobs."""

import asyncio
import logging

import pytest

import obs_helpers
from obs_helpers import FakeTracer
from pengumon.db.settings import SettingsConflict
from pengumon.settings import preview as preview_module
from pengumon.settings.hub import Change, SettingsHub
from pengumon.settings.manager import SecretsWriteFailed, SettingsInvalid

# Fixtures of the neighbouring suites: a settings manager on a temporary database, the preview's stand-in processes.
from settings_helpers import make_manager
from test_settings_preview import CRASH, HANG, OK, SPEC, jobs, settle

obs_helpers.install()


def _saves(source, outcome):
    return obs_helpers.total("pengumon.settings.saves", source=source, outcome=outcome)


# --- saves ----------------------------------------------------------------------------------------------------------

async def test_a_save_is_counted_by_its_source_and_outcome(tmp_path):
    manager, _ = await make_manager(tmp_path)
    saved, invalid, conflict = _saves("save", "saved"), _saves("save", "invalid"), _saves("save", "conflict")
    await manager.apply(1, {"classifier.busy_load_pct": 65}, {})
    assert _saves("save", "saved") == saved + 1
    with pytest.raises(SettingsInvalid):
        await manager.apply(manager.revision, {"database.retention_days": 0}, {})
    assert _saves("save", "invalid") == invalid + 1
    with pytest.raises(SettingsConflict):
        await manager.apply(1, {"classifier.busy_load_pct": 75}, {})
    assert _saves("save", "conflict") == conflict + 1
    assert _saves("save", "saved") == saved + 1  # the refusals are not saves


async def test_a_restore_and_a_bad_source_are_counted_under_their_own_labels(tmp_path):
    manager, _ = await make_manager(tmp_path)
    restored, unknown = _saves("restore", "saved"), _saves("unknown", "invalid")
    await manager.apply(1, {"classifier.busy_load_pct": 65}, {}, source="restore")
    assert _saves("restore", "saved") == restored + 1
    with pytest.raises(SettingsInvalid):
        await manager.apply(manager.revision, {}, {}, source="nonsense")
    assert _saves("unknown", "invalid") == unknown + 1  # (the label stays one of a few, whatever was sent)


async def test_a_secrets_file_that_cannot_be_written_is_counted(tmp_path, monkeypatch):
    manager, _ = await make_manager(tmp_path)

    def broken(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("pengumon.secrets.write_secrets", broken)
    failed = _saves("save", "secrets_failed")
    with pytest.raises(SecretsWriteFailed):
        await manager.apply(1, {"line.isp_name": "X"}, {}, {"ntfy.token": "tk"})
    assert _saves("save", "secrets_failed") == failed + 1


async def test_imports_are_counted_by_outcome(tmp_path):
    manager, _ = await make_manager(tmp_path)
    ok, invalid = (obs_helpers.total("pengumon.settings.imports", outcome=o) for o in ("ok", "invalid"))
    await manager.import_yaml(manager.export_yaml())
    assert obs_helpers.total("pengumon.settings.imports", outcome="ok") == ok + 1
    for text in ("- a\n- b\n", "key: [unclosed"):
        with pytest.raises(ValueError):
            await manager.import_yaml(text)
    assert obs_helpers.total("pengumon.settings.imports", outcome="invalid") == invalid + 2


# --- live appliers --------------------------------------------------------------------------------------------------

async def test_a_failing_applier_is_counted_by_section_and_logged_with_its_parts_not_a_value(caplog):
    hub = SettingsHub()

    @hub.on(["notifications.public_url"], slots=["ntfy.token"], name="notify.reconfigure")
    def _breaks(change):
        raise RuntimeError("could not use hunter2-secret-value")

    @hub.on(["a.x"], name="fine")
    def _fine(change):
        pass

    count = obs_helpers.total("pengumon.settings.live_failures", section="notify", applier="notify.reconfigure")
    caplog.set_level(logging.INFO, logger="pengumon.settings")  # (the traceback, which can quote a value, is DEBUG)
    failed = await hub.publish(Change.of(keys=["notifications.public_url", "a.x"], slots=["ntfy.token"]))
    assert failed == ["notify.reconfigure"]
    assert obs_helpers.total("pengumon.settings.live_failures", section="notify",
                             applier="notify.reconfigure") == count + 1
    assert obs_helpers.total("pengumon.settings.live_failures", applier="fine") == 0
    context = next(r for r in caplog.records if r.levelno == logging.ERROR).pengumon_context
    assert context["section"] == "notify" and context["error"] == "RuntimeError"
    assert context["where"].startswith("test_obs_settings.py:")  # where it was raised, without the message
    assert context["parts"] == "notifications.public_url,ntfy.token"
    assert "hunter2" not in caplog.text and "hunter2" not in str(context)


# --- previews -------------------------------------------------------------------------------------------------------

def _previews(outcome):
    return obs_helpers.total("pengumon.preview.runs", outcome=outcome)


@pytest.fixture
def spans(monkeypatch):
    tracer = FakeTracer()
    monkeypatch.setattr(preview_module, "tracer", tracer)
    return tracer


async def test_a_finished_preview_is_counted_timed_and_has_its_rounds_and_a_span(spans, caplog):
    done, took = _previews("done"), obs_helpers.total("pengumon.preview.duration", outcome="done")
    rounds = obs_helpers.histogram_sum("pengumon.preview.rounds")
    store = jobs(OK)
    caplog.set_level(logging.INFO, logger="pengumon.settings")
    try:
        job = store.start("k", {"start": 0.0, "end": 2 * 86400.0})
        assert await settle(job) == "done"
        await job.task
    finally:
        await store.aclose()
    assert _previews("done") == done + 1
    assert obs_helpers.total("pengumon.preview.duration", outcome="done") == took + 1
    assert obs_helpers.histogram_sum("pengumon.preview.rounds") == rounds + 3  # the stand-in's result says 3 rounds
    span = spans.named("settings.preview")[0]
    assert span.attributes == {"preview.outcome": "done", "preview.days": 2.0, "preview.rounds": 3}
    assert next(r for r in caplog.records if "preview ended" in r.getMessage()).pengumon_context["outcome"] == "done"


async def test_the_time_cap_a_crash_and_a_cancel_are_three_different_outcomes(spans):
    cap, failed, cancelled = _previews("cap"), _previews("failed"), _previews("cancelled")
    rounds = obs_helpers.histogram_sum("pengumon.preview.rounds")
    stores = [jobs(HANG, cap_seconds=0.2), jobs(CRASH), jobs(HANG)]
    try:
        job = stores[0].start("a", SPEC)
        assert await settle(job) == "failed"
        await job.task
        job = stores[1].start("b", SPEC)
        await settle(job)
        await job.task
        job = stores[2].start("c", SPEC)
        await asyncio.sleep(0.1)
        await stores[2].cancel(job.id)
        await job.task
    finally:
        for store in stores:
            await store.aclose()
    assert (_previews("cap"), _previews("failed"), _previews("cancelled")) == (cap + 1, failed + 1, cancelled + 1)
    assert sorted(s.attributes["preview.outcome"] for s in spans.named("settings.preview")) == [
        "cancelled", "cap", "failed"]
    assert obs_helpers.histogram_sum("pengumon.preview.rounds") == rounds  # none of the three replayed a round


async def test_a_second_preview_while_one_runs_is_counted_as_busy_and_not_as_a_run():
    busy, done = _previews("busy"), _previews("done")
    store = jobs(HANG)
    try:
        first = store.start("a", SPEC)
        with pytest.raises(preview_module.PreviewBusy):
            store.start("b", SPEC)
        assert _previews("busy") == busy + 1
        await store.cancel(first.id)
        await first.task
    finally:
        await store.aclose()
    assert _previews("done") == done
