"""/api/history answers of many rows: shaped and encoded in a thread (the event loop keeps ticking), with the same
bytes as the small-answer path; compact answers are built from the query's tuples."""

import asyncio
import sys
import threading
import time

import pytest
from httpx import ASGITransport, AsyncClient

from pengumon.api.common import json_response
from pengumon.api import history as history_api
from pengumon.db import probes as probes_module
from pengumon.db.repository import DatabaseRepository
from pengumon.main import app
from pengumon.models.probes import ProbeSample

TARGETS = tuple(f"t{i}" for i in range(15))


def _samples(start, seconds, targets=TARGETS):
    out = []
    for i in range(seconds):
        for n, tid in enumerate(targets):
            ok = (i + n) % 7 != 0
            out.append(ProbeSample(target_id=tid, timestamp=start + i + n / 100, success=ok,
                                   latency_ms=5.0 + n if ok else None, jitter_ms=0.5 if ok else None,
                                   error=None if ok else "Zeitüberschreitung ü"))
    return out


@pytest.fixture
async def big_repo(tmp_path, app_state):
    repo = DatabaseRepository(db_path=str(tmp_path / "big.db"), sample_flush_seconds=3600)
    await repo.initialize()
    app_state.repository = repo
    yield repo
    await repo.close()


async def _get(path):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.get(path)


def _expected(rows, compact):
    """What the endpoint sent before the thread: the dicts of get_history through json_response, a compact answer
    with a row per list."""
    if compact:
        return json_response({"fields": list(rows[0]) if rows else [], "rows": [list(r.values()) for r in rows]}).body
    return json_response(rows).body


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["bucket_seconds=1", "bucket_seconds=10", "bucket_seconds=60"])
@pytest.mark.parametrize("compact", [False, True])
async def test_a_big_answer_has_the_bytes_of_a_small_one(big_repo, query, compact):
    start = int(time.time() // 60) * 60 - 3300
    await big_repo.insert_samples(_samples(start, 3300))
    await big_repo.flush_samples()
    big_repo.hidden_targets = {"t3"}
    url = f"/api/history?start_time={start}&end_time={start + 3300}&{query}&compact={str(compact).lower()}"
    args = dict(start_time=start, end_time=start + 3300, bucket_seconds=int(query.split("=")[1]))
    rows = await big_repo.get_history(**args)
    assert rows and all(r["target_id"] != "t3" for r in rows)
    expected = _expected(rows, compact)

    threaded = (await _get(url)).content
    assert len(rows) > probes_module.HISTORY_THREAD_ROWS or query != "bucket_seconds=1"
    assert threaded == expected
    # the same answer through the inline path
    old = probes_module.HISTORY_THREAD_ROWS
    probes_module.HISTORY_THREAD_ROWS = history_api.HISTORY_THREAD_ROWS = 10**9
    try:
        assert (await _get(url)).content == expected
    finally:
        probes_module.HISTORY_THREAD_ROWS = history_api.HISTORY_THREAD_ROWS = old


@pytest.mark.asyncio
async def test_the_encoder_pieces_join_to_the_plain_dumps():
    from pengumon.api.history import _encode
    for n in (0, 1, history_api._ENCODE_CHUNK_ROWS, history_api._ENCODE_CHUNK_ROWS + 1, 5001):
        rows = [{"a": i, "b": None, "c": "ü\"x"} for i in range(n)]
        assert _encode(rows, False).body == json_response(rows).body
        compact = {"fields": ["a", "b"], "rows": [(i, None) for i in range(n)]}
        assert _encode(compact, True).body == json_response(
            {"fields": ["a", "b"], "rows": [[i, None] for i in range(n)]}).body


async def _largest_gap(big_repo, url):
    gaps = []
    stop = False

    async def ticker():
        last = time.perf_counter()
        while not stop:
            await asyncio.sleep(0.002)
            now = time.perf_counter()
            gaps.append(now - last)
            last = now

    task = asyncio.create_task(ticker())
    await asyncio.sleep(0.05)
    body = (await _get(url)).content
    stop = True
    await task
    return max(gaps), body


@pytest.mark.asyncio
@pytest.mark.parametrize("compact", [False, True])
async def test_a_big_answer_is_shaped_and_encoded_off_the_loops_thread_and_a_small_one_on_it(big_repo, compact, monkeypatch):
    """What the thread is for, checked on every system without a clock: the rows of an answer over HISTORY_THREAD_ROWS
    are shaped and encoded in a worker thread, those of a small one on the loop's own thread (no hand-over for nothing)."""
    seen = {"shape": [], "encode": []}
    real_shape, real_encode = type(big_repo)._shape_history, history_api._encode

    def shape(self, *args, **kwargs):
        seen["shape"].append(threading.get_ident())
        return real_shape(self, *args, **kwargs)

    def encode(*args, **kwargs):
        seen["encode"].append(threading.get_ident())
        return real_encode(*args, **kwargs)

    monkeypatch.setattr(type(big_repo), "_shape_history", shape)
    monkeypatch.setattr(history_api, "_encode", encode)
    loop_thread = threading.get_ident()
    start = int(time.time() // 60) * 60 - 3300
    await big_repo.insert_samples(_samples(start, 3300))
    await big_repo.flush_samples()
    big = await _get(f"/api/history?start_time={start}&end_time={start + 3300}&compact={str(compact).lower()}")
    assert len(big.content) > 1_000_000
    assert seen["shape"] and seen["encode"] and loop_thread not in seen["shape"] + seen["encode"]
    seen["shape"].clear(), seen["encode"].clear()
    small = await _get(f"/api/history?start_time={start}&end_time={start + 60}&compact={str(compact).lower()}")
    assert small.status_code == 200 and 0 < len(small.content) < 200_000
    assert set(seen["shape"] + seen["encode"]) <= {loop_thread}


# How long the loop stands still is also measured, where the measurement means something: with spare cores and a fine
# timer the thread leaves about a tenth of the stall (Linux). On the macOS and Windows CI runners (three or four cores
# shared with the other test workers, a 15.6 ms timer on Windows) the worker thread and the loop take turns at the
# interpreter and the stall only shrank to 70-85 % (46 against 54 ms, 81 against 116 ms): a ratio there would test the
# runner, not the code. The test above holds on every system.
@pytest.mark.skipif(sys.platform != "linux", reason="the stall is only measured on Linux: on the macOS and Windows "
                    "runners it depends on the cores and the timer, not on the code (the threads are checked everywhere)")
@pytest.mark.asyncio
@pytest.mark.parametrize("compact", [False, True])
async def test_a_50_000_row_answer_does_not_block_the_event_loop(big_repo, compact, monkeypatch):
    """The threaded answer's longest stall is compared with the stall of the same answer built on the loop, measured
    here in the same test: a fixed bound in milliseconds depends on the machine, the ratio much less."""
    start = int(time.time() // 60) * 60 - 3300
    await big_repo.insert_samples(_samples(start, 3300))
    await big_repo.flush_samples()
    url = f"/api/history?start_time={start}&end_time={start + 3300}&compact={str(compact).lower()}"
    await _get(url)  # warm up
    gap, body = await _largest_gap(big_repo, url)
    assert len(body) > 1_000_000
    monkeypatch.setattr(probes_module, "HISTORY_THREAD_ROWS", 10**9)  # the old way: shaped and encoded on the loop
    monkeypatch.setattr(history_api, "HISTORY_THREAD_ROWS", 10**9)
    inline_gap, inline_body = await _largest_gap(big_repo, url)
    assert inline_body == body
    assert inline_gap > MIN_INLINE_GAP, f"the comparison is meaningless: building inline stalled only {inline_gap * 1000:.0f} ms"
    assert gap < inline_gap * GAP_FRACTION, (
        f"the event loop stood still for {gap * 1000:.0f} ms, building the same answer inline stalls it {inline_gap * 1000:.0f} ms")


GAP_FRACTION = 0.5  # the thread leaves a tenth of the stall here; half is far from the unfixed code
MIN_INLINE_GAP = 0.03
