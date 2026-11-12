"""A tiny memo for the dashboard's slow aggregates (the reliability report, the loss grid, the uptime strips): each
built answer is kept per (route, parameters) for the rest of the current minute, at most MAX_AGE_SECONDS, and only
while the `stamp` it was built under (what the answer depends on besides the clock: the path, the settings revision,
the incidents version) is unchanged, so a path edit, a saved setting or a new incident shows at once. A dict of at most
MAX_ENTRIES answers: the oldest goes first.

At most MAX_BUILDS answers are built at once; other misses wait for a turn (not for a database reader, which every
other read of the app shares: the database has READERS of them), and one whose answer was built while it waited takes
that one."""

import asyncio
import time
from typing import Any, Awaitable, Callable, Dict, Hashable, Optional, Tuple

MAX_ENTRIES = 32
MAX_AGE_SECONDS = 60.0
MAX_BUILDS = 2  # fewer than db.core.READERS, so a quick read always finds a reader while answers are built

_MISS = object()


class AnswerCache:
    def __init__(self, clock: Callable[[], float] = time.time, max_builds: int = MAX_BUILDS):
        self._clock = clock
        self._entries: Dict[Hashable, Tuple[Hashable, float, Any]] = {}
        self._max_builds = max_builds
        self._builds: Optional[asyncio.Semaphore] = None  # made on first use, in the running loop
        self.building = 0  # builds under way, for tests

    def __len__(self) -> int:
        return len(self._entries)

    def clear(self) -> None:
        self._entries.clear()

    def _kept(self, key: Hashable, stamp: Hashable) -> Any:
        now = self._clock()
        hit = self._entries.get(key)
        if hit is not None and hit[0] == (int(now // 60), stamp) and now - hit[1] < MAX_AGE_SECONDS:
            return hit[2]
        return _MISS

    async def get(self, key: Hashable, stamp: Hashable, build: Callable[[], Awaitable[Any]], *,
                  admit: Optional[Callable[[], Awaitable[None]]] = None) -> Any:
        """The answer kept for `key` if it was built this minute, under the same `stamp`, less than MAX_AGE_SECONDS
        ago; else `await build()` (when a turn is free), kept. `admit` is awaited first on a miss only (a rate limit:
        it raises to refuse). The answer is shared: callers must not change it."""
        kept = self._kept(key, stamp)
        if kept is not _MISS:
            return kept
        if admit is not None:
            await admit()
        if self._builds is None:
            self._builds = asyncio.Semaphore(self._max_builds)
        async with self._builds:
            kept = self._kept(key, stamp)  # built by another request while this one waited
            if kept is not _MISS:
                return kept
            now = self._clock()  # (an answer belongs to the minute its build started in)
            self.building += 1
            try:
                value = await build()
            finally:
                self.building -= 1
        self._entries.pop(key, None)
        while len(self._entries) >= MAX_ENTRIES:
            del self._entries[next(iter(self._entries))]  # dicts keep insertion order: the oldest first
        self._entries[key] = ((int(now // 60), stamp), now, value)
        return value
