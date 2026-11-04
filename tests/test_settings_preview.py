"""The outage-rules preview jobs (settings/preview.py) and their API (api/settings_preview.py).

The replay itself is tested in tests/test_replay.py; here the process is a tiny stand-in script, so the limits (one job at
a time, the 60 s cap, a stale poll, cancel, the cache, the expiry) run in fractions of a second against real processes.
"""

import asyncio
import json
import os
import sys
import time
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from pengumon.api.settings_models import PreviewJob
from pengumon.config import AppConfig
from pengumon.main import app, security_config
from pengumon.settings.preview import PreviewBusy, PreviewInvalid, PreviewJobs, build_spec, draft_rules

COUNTS = {"incidents": 2, "critical": 1, "warning": 1, "busy_hidden": 0, "total_seconds": 30.0, "alerts": None}
RESULT = {"window": {"start": 1.0, "end": 2.0, "rounds": 3, "gaps": []}, "current": COUNTS,
          "draft": {**COUNTS, "incidents": 1}, "recorded": 2, "notes": ["n"],
          "changes": [{"start": 1.0, "end": 2.0, "kind": "x", "severity": "warning", "in": "current"}]}

# stand-ins for `python -m pengumon.engine.replay`: they read the spec on stdin like the real one
OK = (f"import sys,json; spec=json.load(sys.stdin); print(json.dumps({{'progress': 0.5}}), flush=True); "
      f"open(sys.argv[1], 'w').write(json.dumps(spec)) if len(sys.argv) > 1 else None; "
      f"print(json.dumps({{'result': {RESULT!r}}}), flush=True)")
HANG = "import sys,time; sys.stdin.read(); print('{\"progress\": 0.1}', flush=True); time.sleep(120)"
CRASH = "import sys; sys.stdin.read(); sys.stderr.write('boom'); sys.exit(3)"
SPEC = {"x": 1}


def jobs(code, *args, **limits):
    limits.setdefault("tick", 0.02)
    return PreviewJobs(command=(sys.executable, "-c", code, *args), **limits)


def alive(pid):
    """Whether the process is still running. On Windows os.kill(pid, 0) is no check (it would try to end the process,
    and an ended process's id stays valid while a handle is open), so ask for its exit code: 259 is STILL_ACTIVE."""
    if os.name == "nt":
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def settle(job, seconds=5.0):
    deadline = time.monotonic() + seconds
    while job.state == "running" and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    return job.state


async def test_a_replay_runs_to_a_result_the_page_can_read():
    store = jobs(OK)
    job = store.start("k", SPEC)
    assert await settle(job) == "done"
    body = store.describe(job)
    assert isinstance(body, PreviewJob) and body.progress == 1.0 and body.id == job.id
    assert body.draft.incidents == 1 and body.current.incidents == 2 and body.recorded == 2
    assert body.changes[0].in_ == "current" and body.notes == ["n"]
    assert json.loads(body.model_dump_json(by_alias=True))["changes"][0]["in"] == "current"
    await store.aclose()


async def test_a_failing_replay_is_a_failed_job_without_its_stderr():
    store = jobs(CRASH)
    job = store.start("k", SPEC)
    assert await settle(job) == "failed"
    assert store.describe(job).error == "The preview failed" and "boom" not in store.describe(job).error


async def test_only_one_replay_runs_at_a_time():
    store = jobs(HANG)
    first = store.start("a", SPEC)
    with pytest.raises(PreviewBusy) as busy:
        store.start("b", SPEC)
    assert busy.value.retry_after == 30
    await store.cancel(first.id)
    second = store.start("b", SPEC)  # free again
    assert second.id != first.id
    await store.aclose()


async def test_the_time_cap_kills_the_process_and_says_why():
    store = jobs(HANG, cap_seconds=0.3)
    job = store.start("k", SPEC)
    await asyncio.sleep(0.15)
    pid = job.process.pid
    assert alive(pid)
    assert await settle(job) == "failed"
    assert store.describe(job).error == "took too long"
    await job.task
    assert not alive(pid)


async def test_cancel_kills_the_process_before_it_answers_and_forgets_the_job():
    store = jobs(HANG)
    job = store.start("k", SPEC)
    await asyncio.sleep(0.15)
    pid = job.process.pid
    left = await store.cancel(job.id)
    assert left.state == "cancelled" and not alive(pid)
    assert store.get(job.id) is None
    assert await store.cancel(job.id) is None


async def test_a_job_nobody_polls_is_cancelled_and_its_process_killed():
    store = jobs(HANG, poll_timeout=0.3)
    job = store.start("k", SPEC)
    await asyncio.sleep(0.15)
    pid = job.process.pid
    assert await settle(job) == "cancelled"
    await job.task
    assert not alive(pid)
    assert store.get(job.id).state == "cancelled"  # still there to read, for the 10 minutes


async def test_polling_keeps_a_job_alive():
    store = jobs(HANG, poll_timeout=0.3, cap_seconds=30)
    job = store.start("k", SPEC)
    for _ in range(12):  # 0.6 s, twice the poll timeout
        await asyncio.sleep(0.05)
        assert store.get(job.id).state == "running"
    await store.aclose()
    assert job.state == "cancelled"


async def test_an_identical_request_returns_the_cached_job_and_a_different_one_does_not():
    store = jobs(OK)
    first = store.start("same", SPEC)
    assert store.start("same", SPEC) is first  # while it runs
    await settle(first)
    assert store.start("same", SPEC) is first  # and once it is done
    other = store.start("other", SPEC)
    assert other is not first
    await settle(other)


async def test_a_failed_or_cancelled_job_is_not_served_from_the_cache():
    store = jobs(CRASH)
    first = store.start("k", SPEC)
    await settle(first)
    assert store.start("k", SPEC) is not first
    await store.aclose()  # the new job is still starting


async def test_results_are_kept_for_ten_minutes_only():
    now = [1000.0]
    store = jobs(OK, clock=lambda: now[0], poll_timeout=10_000, cap_seconds=10_000)
    job = store.start("k", SPEC)
    await settle(job)
    now[0] += 599
    assert store.get(job.id) is job
    now[0] += 602  # ten minutes after the last time it was read is not the rule: after it finished is
    assert store.get(job.id) is None
    assert store.start("k", SPEC) is not job  # the cache entry went with it
    await store.aclose()


# --- the request -------------------------------------------------------------------------------------------------

def test_the_draft_is_the_current_rules_with_the_values_laid_over_and_null_resets_to_the_default():
    config = AppConfig(classifier={"down_loss_pct": 50}, line={"down_mbps": 500, "up_mbps": 40})
    draft = draft_rules(config, {"classifier.degraded_loss_pct": 20, "classifier.down_loss_pct": None,
                                 "line.down_mbps": 900, "alerts.quiet_hours.enabled": True})
    assert draft["classifier"]["degraded_loss_pct"] == 20 and draft["classifier"]["down_loss_pct"] == 40.0
    assert draft["line"] == {"down_mbps": 900, "up_mbps": 40}
    assert draft["alerts"]["quiet_hours"]["enabled"] is True


@pytest.mark.parametrize("values, where", [
    ({"database.retention_days": 3}, "values.database.retention_days"),
    ({"classifier.nonsense": 1}, "values.classifier.nonsense"),
    ({"prober.timeout_seconds": 0.5}, "values.prober.timeout_seconds"),
    ({"classifier.busy_load_pct": 5}, "values.classifier.busy_load_pct"),
    ({"classifier.degraded_loss_pct": 60}, "values.classifier"),  # above down_loss_pct: the section's own rule
])
def test_values_the_preview_cannot_replay_or_that_are_out_of_range_are_refused(values, where):
    with pytest.raises(PreviewInvalid) as refused:
        draft_rules(AppConfig(), values)
    assert any(e["where"].startswith(where) for e in refused.value.errors)


def _state(tmp_path, store, retention=7):
    async def flush():
        pass
    return SimpleNamespace(config=AppConfig(database={"retention_days": retention}), preview=store,
                           repository=SimpleNamespace(db_path=str(tmp_path / "x.db"), flush_samples=flush),
                           classifier=SimpleNamespace(load=SimpleNamespace(
                               learned_down_mbps=900.0, learned_up_mbps=40.0, device_id="router")), devices=None)


def test_the_spec_carries_the_window_the_path_both_rule_sets_and_the_capacity(tmp_path):
    state = _state(tmp_path, None, retention=5)
    spec, key = build_spec(state, {"classifier.down_loss_pct": 60}, None, now=1_700_000_123.0)
    assert spec["end"] == 1_700_000_100.0 and spec["end"] - spec["start"] == 5 * 86400  # min(7, retention), whole minute
    assert spec["rules"]["current"]["classifier"]["down_loss_pct"] == 40.0
    assert spec["rules"]["draft"]["classifier"]["down_loss_pct"] == 60.0
    assert spec["learned"] == {"down_mbps": 900.0, "up_mbps": 40.0} and spec["wan_device_id"] == "router"
    same, same_key = build_spec(state, {"classifier.down_loss_pct": 60}, None, now=1_700_000_159.0)
    assert same_key == key  # the same minute
    assert build_spec(state, {"classifier.down_loss_pct": 61}, None, now=1_700_000_123.0)[1] != key
    assert build_spec(state, {"classifier.down_loss_pct": 60}, None, now=1_700_000_163.0)[1] != key
    with pytest.raises(PreviewInvalid):
        build_spec(state, {}, 6, now=1_700_000_123.0)  # more days than are kept


# --- the API -----------------------------------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _open(monkeypatch):
    monkeypatch.setattr(security_config, "access_token", "")
    monkeypatch.setattr(security_config, "allowed_hosts", [])
    monkeypatch.setattr(security_config, "allowed_origins", [])
    monkeypatch.setattr(security_config, "view_without_token", False)


def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _install(app_state, tmp_path, store):
    s = _state(tmp_path, store)
    app_state.config, app_state.repository, app_state.preview = s.config, s.repository, store
    app_state.classifier = s.classifier


async def test_post_poll_and_the_draft_reaches_the_process(app_state, tmp_path):
    seen = tmp_path / "spec.json"
    store = jobs(OK, str(seen))
    _install(app_state, tmp_path, store)
    async with _client() as ac:
        started = await ac.post("/api/settings/preview", json={"values": {"classifier.down_loss_pct": 55}, "days": 2})
        assert started.status_code == 200 and set(started.json()) == {"id"}
        job_id = started.json()["id"]
        for _ in range(300):
            body = (await ac.get(f"/api/settings/preview/{job_id}")).json()
            if body["state"] != "running":
                break
            await asyncio.sleep(0.02)
        assert body["state"] == "done" and body["changes"][0]["in"] == "current" and body["recorded"] == 2
        PreviewJob.model_validate(body)
        again = await ac.post("/api/settings/preview", json={"values": {"classifier.down_loss_pct": 55}, "days": 2})
        assert again.json()["id"] == job_id  # the cache
    spec = json.loads(seen.read_text())
    assert spec["rules"]["draft"]["classifier"]["down_loss_pct"] == 55.0
    assert spec["rules"]["current"]["classifier"]["down_loss_pct"] == 40.0
    assert spec["end"] - spec["start"] == 2 * 86400


async def test_a_second_preview_is_a_429_with_retry_after(app_state, tmp_path):
    store = jobs(HANG)
    _install(app_state, tmp_path, store)
    async with _client() as ac:
        first = await ac.post("/api/settings/preview", json={"values": {"classifier.down_loss_pct": 55}})
        second = await ac.post("/api/settings/preview", json={"values": {"classifier.down_loss_pct": 56}})
        assert first.status_code == 200 and second.status_code == 429 and second.headers["retry-after"] == "30"
        gone = await ac.delete(f"/api/settings/preview/{first.json()['id']}")
        assert gone.status_code == 200 and gone.json()["state"] == "cancelled"
        assert (await ac.get(f"/api/settings/preview/{first.json()['id']}")).status_code == 404
        assert (await ac.delete(f"/api/settings/preview/{first.json()['id']}")).status_code == 404
    await store.aclose()


async def test_the_api_refuses_what_the_preview_cannot_replay_in_the_error_shape(app_state, tmp_path):
    store = jobs(OK)
    _install(app_state, tmp_path, store)
    async with _client() as ac:
        res = await ac.post("/api/settings/preview", json={"values": {"database.retention_days": 3}})
        assert res.status_code == 422 and res.json()["errors"][0]["where"] == "values.database.retention_days"
        res = await ac.post("/api/settings/preview", json={"values": {}, "days": 30})
        assert res.status_code == 422 and res.json()["errors"][0]["where"] == "days"
        res = await ac.post("/api/settings/preview", json={"values": {"classifier.busy_load_pct": 1}})
        assert res.status_code == 422
        res = await ac.post("/api/settings/preview", json={"values": {}, "extra": 1})
        assert res.status_code == 422
    assert store.running() is None  # nothing was started


async def test_the_api_is_503_before_the_app_is_up_and_404_for_an_unknown_id(app_state, tmp_path):
    async with _client() as ac:
        assert (await ac.post("/api/settings/preview", json={"values": {}})).status_code == 503
        assert (await ac.get("/api/settings/preview/abc")).status_code == 503
    _install(app_state, tmp_path, jobs(OK))
    async with _client() as ac:
        assert (await ac.get("/api/settings/preview/abc")).status_code == 404


async def test_the_preview_is_rate_limited_to_six_a_minute(app_state, tmp_path):
    store = jobs(OK)
    _install(app_state, tmp_path, store)
    async with _client() as ac:
        codes = []
        for i in range(7):
            codes.append((await ac.post("/api/settings/preview",
                                        json={"values": {"classifier.down_loss_pct": 41 + i}})).status_code)
            while store.running() is not None:  # one replay at a time: let it finish, so only the rate can refuse
                await asyncio.sleep(0.01)
    assert codes == [200] * 6 + [429]
    await store.aclose()
