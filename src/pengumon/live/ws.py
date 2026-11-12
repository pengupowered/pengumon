"""WebSocket manager for streaming real-time network telemetry to web GUI."""

import asyncio
import time
from typing import Any, Dict, List, Optional, Tuple
from fastapi import WebSocket
from pydantic_core import to_json

from pengumon.devices.summary import viewer_devices
from pengumon.live.message import LiveStreamMessage
from pengumon.observability import get_logger
from pengumon.observability.telemetry import ws_clients_dropped, ws_message_size, ws_send_duration

logger = get_logger("pengumon.ws")
_SENT, _FAILED = {"outcome": "sent"}, {"outcome": "failed"}

# A browser that stops reading (suspended tab, dead Wi-Fi) fills its socket buffer and `send` then waits
# for it. The client is dropped after this long rather than holding up everyone else.
SEND_TIMEOUT_SECONDS = 2.0
# A connection's request for the whole state ({"resync": true}) is answered on its own at most this often; one asked
# for sooner gets it with the next regular message. What one page sends never makes the others get anything.
RESYNC_MIN_SECONDS = 2.0
# Live connections held at once. One more is refused (close code 1013, "try again later"): each delta connection costs a
# JSON build a second, and nothing else bounds how many a client may open.
MAX_CONNECTIONS = 64


# What a delta connection has been sent of one target: (probes sent so far, window length, newest value in the window).
_Seen = Tuple[int, int, Optional[float]]
_WINDOW = "recent_latencies"


def latency_delta(target: dict, seen: Optional[_Seen], window: List[Optional[float]]) -> bool:
    """Fills `target` (the dumped TargetStats, `recent_latencies` left out) with what a client that holds the window as
    `seen` describes lacks: `latency_tail` (the probes since then, oldest first), `latency_drop` (how many of its oldest
    values fall out of the window) and `latency_len` (the window's length after, for the client to check) and returns
    True. False when it cannot be said that way (nothing sent before, the counter went back, a target restarted,
    more new probes than the window holds, or the value before the tail is not the one the client has): the caller sends
    the whole window."""
    if seen is None:
        return False
    sent, was_len, was_last = seen
    size = len(window)
    k = target["sent"] - sent
    if k < 0 or k > size or (k == 0 and size != was_len):
        return False
    drop = was_len + k - size
    if drop < 0:
        return False
    # the newest value the client already has must still sit where it was: k from the end
    if was_len and size - k - 1 >= 0 and window[size - k - 1] != was_last:
        return False
    target["latency_tail"] = window[size - k:] if k else []
    target["latency_drop"] = drop
    target["latency_len"] = size
    return True


class ConnectionManager:
    """Manages active WebSocket connections.

    The message carries `incidents_version`; a dashboard fetches the incident list when it changes.

    Sending runs in its own task (`run_broadcaster`), fed through `publish`: the probe loop only drops
    the newest message into a single slot and moves on. If sending falls behind, older messages are
    skipped rather than queued; a live view only needs the latest state.

    A connection opened with `?delta=1` (the dashboard) gets each target's `recent_latencies` in full only in its first
    message and when it asks for it (a text frame `{"resync": true}`, `request_full`); otherwise only the probes since
    the message before, as `latency_tail` (`latency_delta`). The state of that is kept per connection, from what was
    actually sent to it, so messages the broadcaster skipped leave no gap. Every other connection gets full messages.
    A request for the whole state is served to that connection alone, by the broadcaster task (never two sends to one
    socket at once): at once, at most every RESYNC_MIN_SECONDS, else with the next regular message.

    A connection that may not edit (`connect(..., editor=False)`: a viewer, the read token) gets the message with its
    devices' failed-poll messages in fixed words (`viewer_devices`), built once per broadcast however many such
    connections there are, and only when some device has a message to reword.
    """

    def __init__(self, send_timeout: float = SEND_TIMEOUT_SECONDS, clock=time.monotonic):
        self.active_connections: List[WebSocket] = []
        self.send_timeout = send_timeout
        self._closers: set = set()
        self._pending: Optional[LiveStreamMessage] = None
        self._wakeup: Optional[asyncio.Event] = None  # created by the running broadcaster, so it binds to its loop
        self._latest: Optional[LiveStreamMessage] = None  # the last one broadcast, for a connection that asks for a full one
        # delta connections -> {target_id: what was sent of its window}; absent: sends full messages
        self._seen: Dict[WebSocket, Dict[str, _Seen]] = {}
        self._viewers: set = set()  # connections that may not edit: they get the reworded device messages
        self._resync: Dict[WebSocket, None] = {}  # delta connections waiting for a full message of their own
        self._resynced: Dict[WebSocket, float] = {}  # when each one's last was let through (clock)
        self._clock = clock

    async def connect(self, websocket: WebSocket, delta: bool = False, editor: bool = True) -> bool:
        """Accepts the connection and adds it; False (closed with 1013, not added) when MAX_CONNECTIONS are held."""
        await websocket.accept()
        if len(self.active_connections) >= MAX_CONNECTIONS:
            logger.warning("Live connection refused: too many open", clients=len(self.active_connections))
            await self._close(websocket, 1013)
            return False
        self.active_connections.append(websocket)
        if delta:
            self._seen[websocket] = {}
        if not editor:
            self._viewers.add(websocket)
        logger.debug("Client connected", clients=len(self.active_connections))
        return True

    def disconnect(self, websocket: WebSocket):
        self._seen.pop(websocket, None)
        self._viewers.discard(websocket)
        self._resync.pop(websocket, None)
        self._resynced.pop(websocket, None)
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)
            logger.debug("Client disconnected", clients=len(self.active_connections))

    def publish(self, message: LiveStreamMessage) -> None:
        """Hands the newest message to the broadcaster task; never waits, replaces an unsent one."""
        self._pending = message
        if self._wakeup is not None:
            self._wakeup.set()

    def request_full(self, websocket: WebSocket) -> None:
        """A delta connection asks for the whole state (it saw a gap): its next message carries every window in full.
        That message goes to it alone, now (at most one waiting per connection, and one every RESYNC_MIN_SECONDS), else
        it is the next regular one. The other connections get nothing more."""
        if websocket not in self._seen:
            return
        self._seen[websocket] = {}
        if websocket in self._resync:
            return
        now = self._clock()
        last = self._resynced.get(websocket)
        if last is not None and now - last < RESYNC_MIN_SECONDS:
            return
        self._resynced[websocket] = now
        self._resync[websocket] = None
        if self._wakeup is not None:
            self._wakeup.set()

    async def run_broadcaster(self) -> None:
        """Background task: sends each published message (or the latest one, if several arrived)."""
        self._wakeup = wakeup = asyncio.Event()
        if self._pending:
            wakeup.set()
        while True:
            try:
                await wakeup.wait()
                wakeup.clear()
                pending, self._pending = self._pending, None
                if pending:
                    await self.broadcast(pending)
                elif self._resync:
                    await self._send_resyncs()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.error("Broadcast failed", exc_info=True)

    async def _send(self, connection: WebSocket, text: str) -> Optional[str]:
        """Sends to one client. None when it went out, else why not: "timeout" (the client does not read) or "error"."""
        started = time.perf_counter()
        reason: Optional[str] = None
        try:
            await asyncio.wait_for(connection.send_text(text), self.send_timeout)
        except asyncio.TimeoutError:
            reason = "timeout"
        except Exception:  # a closed socket, anything else: the client is gone or hopeless
            reason = "error"
        ws_send_duration.record(time.perf_counter() - started, _SENT if reason is None else _FAILED)
        return reason

    async def _send_resyncs(self) -> None:
        """The last message, to each delta connection that asked for the whole state, and to no other."""
        waiting, self._resync = [c for c in self._resync if c in self._seen], {}
        message = self._latest
        if message is None or not waiting:
            return  # (nothing was sent yet: the first message is a full one anyway)
        texts = self._texts(message, waiting)
        for text in texts:
            ws_message_size.record(len(text.encode("utf-8")))
        await self._send_all(waiting, texts)

    async def broadcast(self, message: LiveStreamMessage):
        """Pushes real-time diagnostic telemetry to all connected browsers, concurrently. A client that
        fails or takes longer than the send timeout is disconnected."""
        connections = list(self.active_connections)
        self._resync = {}  # (this message serves them: their state was emptied)
        if not connections:
            return

        self._latest = message
        texts = self._texts(message, connections)
        recorded: set = set()  # (a full message once, however many clients get it)
        for text in texts:
            if id(text) not in recorded:
                recorded.add(id(text))
                ws_message_size.record(len(text.encode("utf-8")))
        await self._send_all(connections, texts)

    def _texts(self, message: LiveStreamMessage, connections: List[WebSocket]) -> List[str]:
        """The text for each connection: the full message, or for a delta connection its delta, of the message as that
        connection may see it. Each form (full text, window-less dump) is built once per version of the message."""
        viewer_message: Optional[LiveStreamMessage] = None
        built: Dict[int, list] = {}  # id(message version) -> [message, full text, window-less dump]
        texts = []
        for connection in connections:
            shown = message
            if connection in self._viewers:
                if viewer_message is None:
                    devices = viewer_devices(message.devices)
                    viewer_message = message if devices is message.devices else message.model_copy(update={"devices": devices})
                shown = viewer_message
            forms = built.setdefault(id(shown), [shown, None, None])
            if connection not in self._seen:
                if forms[1] is None:
                    forms[1] = shown.model_dump_json()
                texts.append(forms[1])
            else:
                if forms[2] is None:
                    forms[2] = shown.model_dump(mode="json", exclude={"targets": {"__all__": {_WINDOW}}})
                texts.append(self._delta_text(shown, forms[2], self._seen[connection]))
        return texts

    async def _send_all(self, connections: List[WebSocket], texts: List[str]) -> None:
        """Sends each its text, concurrently; drops the clients whose send failed."""
        for connection, reason in zip(connections, await asyncio.gather(*(self._send(c, t) for c, t in zip(connections, texts)))):
            if reason is not None:
                ws_clients_dropped.add(1, {"reason": reason})
                logger.info("Dashboard client dropped: its send failed", reason=reason, clients=len(connections) - 1)
                self.disconnect(connection)
                # Frees the socket. It may hang as well, so do not wait for it here.
                closer = asyncio.create_task(self._close(connection))
                self._closers.add(closer)
                closer.add_done_callback(self._closers.discard)

    @staticmethod
    def _delta_text(message: LiveStreamMessage, light: Dict[str, Any], seen: Dict[str, _Seen]) -> str:
        """The message for one delta connection; updates `seen` to what it now holds."""
        targets = []
        for dumped, stat in zip(light["targets"], message.targets):
            window = list(stat.recent_latencies)
            dumped = dict(dumped)  # `light` is shared by every delta connection
            if not latency_delta(dumped, seen.get(stat.target_id), window):
                dumped[_WINDOW] = window
            targets.append(dumped)
            seen[stat.target_id] = (stat.sent, len(window), window[-1] if window else None)
        for gone in set(seen) - {t.target_id for t in message.targets}:
            del seen[gone]
        return to_json({**light, "targets": targets}).decode("utf-8")

    async def close_all(self) -> None:
        """Ends every live connection (code 1012, "restarting": each page reconnects in a moment, and the server checks
        it again). For a change of who may connect (pengumon.access): those let in under the old rule are not checked
        again otherwise. Not 1008, which sends a page to the sign-in page even when it holds the new sign-in."""
        connections, self.active_connections = list(self.active_connections), []
        if connections:
            logger.info("Closing every live connection: the sign-in changed", clients=len(connections))
            await asyncio.gather(*(self._close(c, 1012) for c in connections))

    async def _close(self, connection: WebSocket, code: Optional[int] = None) -> None:
        try:
            await asyncio.wait_for(connection.close() if code is None else connection.close(code), self.send_timeout)
        except Exception:
            pass


ws_manager = ConnectionManager()
