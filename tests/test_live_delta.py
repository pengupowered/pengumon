"""The live stream's deltas: a `?delta=1` connection gets each target's `recent_latencies` in full once and then only the
probes since its last message (`latency_tail`), and asks for the whole state when it saw a gap. A reference receiver
(`Receiver`, the same rules as web/static/js/live-delta.js) must end up with exactly what a full message carries."""

import asyncio
import json
import time

from fastapi.testclient import TestClient

from pengumon.api.live import wants_full
from pengumon.incidents.model import SystemDiagnosis
from pengumon.live.message import LiveStreamMessage
from pengumon.live.ws import RESYNC_MIN_SECONDS, ConnectionManager, latency_delta
from pengumon.models.probes import TargetStats

from device_helpers import Clock
from test_obs_api import FakeSocket

WINDOW = 5


def stat(tid="a", **kw):
    return TargetStats(target_id=tid, name=kw.pop("name", tid.upper()), category="public", address="192.0.2.1", enabled=True, **kw)


def probe(stat_, value):
    """What NetworkProber._update_target_stats does to the window and the counter."""
    stat_.sent += 1
    stat_.recent_latencies.append(value)
    if len(stat_.recent_latencies) > WINDOW:
        stat_.recent_latencies.pop(0)


def message(*targets, t=1.0):
    return LiveStreamMessage(timestamp=t, diagnosis=SystemDiagnosis(), targets=list(targets))


async def sent_counts(expected, *sockets, deadline=5.0):
    """Waits until the sockets have been sent `expected` messages each (polling, up to `deadline` seconds), then returns
    the counts for the assert. A fixed sleep is not enough: on Windows the loop's clock ticks every 15.6 ms, so a 10 ms
    sleep can return before the broadcaster has run."""
    end = time.monotonic() + deadline
    while [len(s.sent) for s in sockets] != expected and time.monotonic() < end:
        await asyncio.sleep(0.005)
    return [len(s.sent) for s in sockets]


async def settle():
    """For asserting that nothing more is sent: lets the broadcaster run first, as long as a slow loop needs."""
    await asyncio.sleep(0.05)


class Receiver:
    """The page's side, as live-delta.js rebuilds it. `gap` is set when a message did not follow what is held."""

    def __init__(self):
        self.held = {}
        self.gap = False

    def take(self, text):
        data = json.loads(text)
        out, held = [], {}
        for t in data["targets"]:
            window = t.get("recent_latencies")
            if window is None:
                was = self.held.get(t["target_id"])
                tail = t.get("latency_tail")
                if was is None or tail is None or was[1] + len(tail) != t["sent"]:
                    self.gap = True
                    return None
                window = was[0][t["latency_drop"]:] + tail
                if len(window) != t["latency_len"]:
                    self.gap = True
                    return None
            held[t["target_id"]] = (window, t["sent"])
            out.append({k: v for k, v in t.items() if not k.startswith("latency_")} | {"recent_latencies": window})
        self.held = held
        return out


def full_targets(msg):
    return json.loads(msg.model_dump_json())["targets"]


async def test_a_delta_connection_gets_the_window_once_then_only_the_new_probes():
    manager, sock, recv = ConnectionManager(), FakeSocket(), Receiver()
    await manager.connect(sock, delta=True)
    a = stat()
    for v in (1.0, 2.0, 3.0):
        probe(a, v)
    for tick in range(12):  # past the window's size: values fall out of it
        probe(a, float(10 + tick) if tick != 4 else None)
        msg = message(a)
        await manager.broadcast(msg)
        assert recv.take(sock.sent[-1]) == full_targets(msg), tick
    first, second = json.loads(sock.sent[0])["targets"][0], json.loads(sock.sent[1])["targets"][0]
    assert first["recent_latencies"] and "latency_tail" not in first
    assert "recent_latencies" not in second and len(second["latency_tail"]) == 1


async def test_a_delta_message_is_much_smaller_with_a_full_window():
    manager, sock = ConnectionManager(), FakeSocket()
    await manager.connect(sock, delta=True)
    targets = [stat(f"t{i}", sent=60, recent_latencies=[12.345 + j / 7 for j in range(60)]) for i in range(8)]
    await manager.broadcast(message(*targets))
    for t in targets:
        t.sent += 1
        t.recent_latencies[:] = (t.recent_latencies + [12.5])[-60:]
    await manager.broadcast(message(*targets))
    assert len(sock.sent[1]) < len(sock.sent[0]) / 2


async def test_messages_the_broadcaster_skipped_are_not_a_gap():
    """publish replaces an unsent message: the delta is against what this connection was sent, not the previous tick."""
    manager, sock, recv = ConnectionManager(), FakeSocket(), Receiver()
    await manager.connect(sock, delta=True)
    a = stat()
    probe(a, 1.0)
    await manager.broadcast(message(a))
    recv.take(sock.sent[-1])
    for v in (2.0, 3.0, 4.0):  # three probes, one message sent
        probe(a, v)
    msg = message(a)
    await manager.broadcast(msg)
    assert json.loads(sock.sent[-1])["targets"][0]["latency_tail"] == [2.0, 3.0, 4.0]
    assert recv.take(sock.sent[-1]) == full_targets(msg) and not recv.gap


async def test_more_new_probes_than_the_window_holds_is_sent_in_full():
    manager, sock, recv = ConnectionManager(), FakeSocket(), Receiver()
    await manager.connect(sock, delta=True)
    a = stat()
    probe(a, 1.0)
    await manager.broadcast(message(a))
    recv.take(sock.sent[-1])
    for v in range(WINDOW + 3):
        probe(a, float(v))
    await manager.broadcast(message(a))
    assert "recent_latencies" in json.loads(sock.sent[-1])["targets"][0]
    assert recv.take(sock.sent[-1])[0]["recent_latencies"] == a.recent_latencies


async def test_a_restarted_target_a_new_one_a_removed_one_and_a_rename_leave_the_receiver_with_the_full_state():
    manager, sock, recv = ConnectionManager(), FakeSocket(), Receiver()
    await manager.connect(sock, delta=True)
    a, b = stat("a"), stat("b")
    for _ in range(4):
        probe(a, 1.0)
        probe(b, 2.0)
    await manager.broadcast(message(a, b))
    recv.take(sock.sent[-1])
    scenes = []
    # the prober restarted: new stats objects, counters from zero (smaller than before)
    a2, b2 = stat("a"), stat("b")
    probe(a2, 9.0)
    probe(b2, 8.0)
    scenes.append(message(a2, b2))
    # a target is added, with its history so far
    c = stat("c")
    probe(c, 5.0)
    probe(a2, 9.5)
    probe(b2, 8.5)
    scenes.append(message(a2, b2, c))
    # one is removed, one renamed
    probe(a2, 9.7)
    renamed = a2.model_copy(update={"name": "Renamed"}, deep=True)
    scenes.append(message(renamed, c))
    # removed and added again under the same id with a counter that happens to reach the old one
    again = stat("b")
    for v in (7.0, 7.1, 7.2, 7.3):
        probe(again, v)
    scenes.append(message(renamed, again, c))
    for msg in scenes:
        await manager.broadcast(msg)
        assert recv.take(sock.sent[-1]) == full_targets(msg)
    assert not recv.gap


async def test_the_same_counter_with_other_values_is_caught_by_the_newest_value_check():
    manager, sock, recv = ConnectionManager(), FakeSocket(), Receiver()
    await manager.connect(sock, delta=True)
    a = stat()
    for v in (1.0, 2.0, 3.0):
        probe(a, v)
    await manager.broadcast(message(a))
    recv.take(sock.sent[-1])
    other = stat()  # a different target of the same id whose counter equals the old one
    for v in (5.0, 6.0, 7.0):
        probe(other, v)
    msg = message(other)
    await manager.broadcast(msg)
    assert recv.take(sock.sent[-1]) == full_targets(msg)


async def test_a_smaller_or_larger_window_after_a_settings_change_is_followed():
    manager, sock, recv = ConnectionManager(), FakeSocket(), Receiver()
    await manager.connect(sock, delta=True)
    a = stat()
    for v in range(5):
        probe(a, float(v))
    await manager.broadcast(message(a))
    recv.take(sock.sent[-1])
    a.recent_latencies[:] = a.recent_latencies[-3:]  # the window shrank to 3 (prober.history_window)
    a.sent += 1
    a.recent_latencies.append(9.0)
    a.recent_latencies.pop(0)
    msg = message(a)
    await manager.broadcast(msg)
    assert recv.take(sock.sent[-1]) == full_targets(msg) and not recv.gap
    a.sent += 1
    a.recent_latencies.append(10.0)  # it grew again: the window got longer by one
    msg = message(a)
    await manager.broadcast(msg)
    assert recv.take(sock.sent[-1]) == full_targets(msg) and not recv.gap


async def test_a_gap_is_detected_and_a_resync_sends_the_full_window_to_that_connection_only():
    manager, mine, other = ConnectionManager(), FakeSocket(), FakeSocket()
    await manager.connect(mine, delta=True)
    await manager.connect(other, delta=True)
    a = stat()
    probe(a, 1.0)
    first = message(a)
    await manager.broadcast(first)
    probe(a, 2.0)
    await manager.broadcast(message(a))
    recv = Receiver()
    assert recv.take(mine.sent[1]) is None and recv.gap  # it never saw the first message: a gap
    task = asyncio.create_task(manager.run_broadcaster())
    try:
        await asyncio.sleep(0)
        manager.request_full(mine)
        assert await sent_counts([3, 2], mine, other) == [3, 2]
        await settle()
    finally:
        task.cancel()
    assert len(mine.sent) == 3 and len(other.sent) == 2  # the answer is one more message, to that connection alone
    got = json.loads(mine.sent[-1])["targets"][0]
    assert got["recent_latencies"] == [1.0, 2.0] and "latency_tail" not in got
    recv = Receiver()
    assert recv.take(mine.sent[-1]) is not None and not recv.gap
    probe(a, 3.0)
    await manager.broadcast(message(a))  # both in step again: deltas for both
    assert recv.take(mine.sent[-1]) is not None and not recv.gap and "latency_tail" in mine.sent[-1]
    assert "latency_tail" in other.sent[-1]


async def test_resync_requests_from_one_connection_send_nothing_to_the_others_and_are_limited():
    clock = Clock(100.0)
    manager, loud, quiet, plain = ConnectionManager(clock=clock), FakeSocket(), FakeSocket(), FakeSocket()
    await manager.connect(loud, delta=True)
    await manager.connect(quiet, delta=True)
    await manager.connect(plain)
    a = stat()
    probe(a, 1.0)
    task = asyncio.create_task(manager.run_broadcaster())
    try:
        await asyncio.sleep(0)
        manager.publish(message(a))
        assert await sent_counts([1, 1, 1], loud, quiet, plain) == [1, 1, 1]
        for _ in range(200):  # 200 {"resync": true} frames, as fast as they come
            manager.request_full(loud)
            for _ in range(3):
                await asyncio.sleep(0)
        assert await sent_counts([2, 1, 1], loud, quiet, plain) == [2, 1, 1]
        await settle()
        assert [len(s.sent) for s in (loud, quiet, plain)] == [2, 1, 1]  # one answer, to that connection alone
        clock.now += 1.0
        manager.request_full(loud)  # within RESYNC_MIN_SECONDS: no answer of its own...
        await settle()
        assert len(loud.sent) == 2
        probe(a, 2.0)
        manager.publish(message(a))  # ...the next regular message carries the full window
        assert await sent_counts([3, 2, 2], loud, quiet, plain) == [3, 2, 2]
        assert json.loads(loud.sent[-1])["targets"][0]["recent_latencies"] == [1.0, 2.0]
        assert "latency_tail" in quiet.sent[-1]
        clock.now += RESYNC_MIN_SECONDS
        manager.request_full(loud)  # later again: answered on its own
        assert await sent_counts([4, 2, 2], loud, quiet, plain) == [4, 2, 2]
        await settle()
        assert [len(s.sent) for s in (loud, quiet, plain)] == [4, 2, 2]
    finally:
        task.cancel()
    manager.disconnect(loud)
    assert loud not in manager._resync and loud not in manager._resynced


async def test_a_connection_that_did_not_ask_for_deltas_gets_full_messages_as_before():
    manager, plain, delta = ConnectionManager(), FakeSocket(), FakeSocket()
    await manager.connect(plain)
    await manager.connect(delta, delta=True)
    a = stat()
    for _ in range(3):
        probe(a, 1.0)
        msg = message(a)
        await manager.broadcast(msg)
        assert plain.sent[-1] == msg.model_dump_json()
    assert "latency_tail" not in plain.sent[-1] and "latency_tail" in delta.sent[-1]
    manager.request_full(plain)  # not a delta connection: nothing to do
    assert not manager._seen.get(plain)


async def test_two_delta_connections_are_tracked_apart_and_a_dropped_one_is_forgotten():
    manager, early, late = ConnectionManager(), FakeSocket(), FakeSocket()
    await manager.connect(early, delta=True)
    a = stat()
    probe(a, 1.0)
    await manager.broadcast(message(a))
    await manager.connect(late, delta=True)  # joins in between: its first message is a full one
    probe(a, 2.0)
    await manager.broadcast(message(a))
    assert "latency_tail" in early.sent[-1] and "recent_latencies" in json.loads(late.sent[-1])["targets"][0]
    manager.disconnect(early)
    assert early not in manager._seen and late in manager._seen


def test_latency_delta_unit():
    t = {"sent": 7}
    assert latency_delta(t, (5, 4, 3.0), [1.0, 2.0, 3.0, 4.0]) is False  # 2 new probes but the value before them differs
    t = {"sent": 7}
    assert latency_delta(t, (5, 4, 2.0), [1.0, 2.0, 3.0, 4.0]) is True
    assert (t["latency_tail"], t["latency_drop"], t["latency_len"]) == ([3.0, 4.0], 2, 4)
    assert latency_delta({"sent": 3}, (5, 4, 2.0), [1.0, 2.0]) is False  # the counter went back
    assert latency_delta({"sent": 5}, None, [1.0]) is False


def test_only_the_resync_request_is_read_from_a_page():
    assert wants_full('{"resync": true}') and wants_full('{"resync":true}')
    assert not wants_full("anything") and not wants_full("[]") and not wants_full('{"resync": false}')
    assert not wants_full('{"resync": true, "x": "' + "x" * 100 + '"}')


def test_the_websocket_route_opts_in_with_the_query_flag_and_answers_a_resync():
    from pengumon.live.ws import ws_manager
    from pengumon.main import app

    client = TestClient(app)
    origin = {"Origin": "http://testserver"}
    with client.websocket_connect("/ws?delta=1", headers=origin) as ws:
        assert len(ws_manager._seen) >= 1
        sock = next(iter(ws_manager._seen))
        ws.send_text('{"resync": true}')  # reaches request_full: the connection's state is empty
        assert ws_manager._seen[sock] == {}
    before = set(map(id, ws_manager._seen))
    with client.websocket_connect("/ws", headers=origin):
        assert set(map(id, ws_manager._seen)) == before  # a plain connection is not a delta one


class _CodedSocket(FakeSocket):
    async def close(self, code=1000):
        self.closed = code


async def test_a_connection_over_the_cap_is_refused_with_1013_and_not_added(monkeypatch):
    from pengumon.live import ws as live_ws

    monkeypatch.setattr(live_ws, "MAX_CONNECTIONS", 2)
    manager = ConnectionManager()
    held = [_CodedSocket(), _CodedSocket()]
    for s in held:
        assert await manager.connect(s, delta=True) is True
    extra = _CodedSocket()
    assert await manager.connect(extra, delta=True) is False
    assert extra.closed == 1013
    assert extra not in manager.active_connections and extra not in manager._seen
    assert manager.active_connections == held and not any(s.closed for s in held)
    manager.disconnect(held[0])  # a place frees up
    assert await manager.connect(_CodedSocket()) is True
