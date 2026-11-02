"""WebSocket manager for streaming real-time network telemetry to web GUI."""

import asyncio
import logging
from typing import List, Optional
from fastapi import WebSocket

from pengumon.live.message import LiveStreamMessage

logger = logging.getLogger("pengumon.ws")

# A browser that stops reading (suspended tab, dead Wi-Fi) fills its socket buffer and `send` then waits
# for it. The client is dropped after this long rather than holding up everyone else.
SEND_TIMEOUT_SECONDS = 2.0


class ConnectionManager:
    """Manages active WebSocket connections.

    The message carries `incidents_version`; a dashboard fetches the incident list when it changes.

    Sending runs in its own task (`run_broadcaster`), fed through `publish`: the probe loop only drops
    the newest message into a single slot and moves on. If sending falls behind, older messages are
    skipped rather than queued; a live view only needs the latest state.
    """

    def __init__(self, send_timeout: float = SEND_TIMEOUT_SECONDS):
        self.active_connections: List[WebSocket] = []
        self.send_timeout = send_timeout
        self._closers: set = set()
        self._pending: Optional[LiveStreamMessage] = None
        self._wakeup: Optional[asyncio.Event] = None  # created by the running broadcaster, so it binds to its loop

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)
        logger.debug(f"Client connected. Active clients: {len(self.active_connections)}")

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)
            logger.debug(f"Client disconnected. Active clients: {len(self.active_connections)}")

    def publish(self, message: LiveStreamMessage) -> None:
        """Hands the newest message to the broadcaster task; never waits, replaces an unsent one."""
        self._pending = message
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
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Broadcast failed")

    async def _send(self, connection: WebSocket, text: str) -> bool:
        try:
            await asyncio.wait_for(connection.send_text(text), self.send_timeout)
            return True
        except Exception:  # timeout, closed socket, anything else: the client is gone or hopeless
            return False

    async def broadcast(self, message: LiveStreamMessage):
        """Pushes real-time diagnostic telemetry to all connected browsers, concurrently. A client that
        fails or takes longer than the send timeout is disconnected."""
        connections = list(self.active_connections)
        if not connections:
            return

        text = message.model_dump_json()
        for connection, ok in zip(connections, await asyncio.gather(*(self._send(c, text) for c in connections))):
            if not ok:
                self.disconnect(connection)
                # Frees the socket. It may hang as well, so do not wait for it here.
                closer = asyncio.create_task(self._close(connection))
                self._closers.add(closer)
                closer.add_done_callback(self._closers.discard)

    async def _close(self, connection: WebSocket) -> None:
        try:
            await asyncio.wait_for(connection.close(), self.send_timeout)
        except Exception:
            pass


ws_manager = ConnectionManager()
