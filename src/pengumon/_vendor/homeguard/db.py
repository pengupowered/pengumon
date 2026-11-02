"""homeguard.db: one writer and one read-only reader connection for an aiosqlite database.

The usual alternatives are a connection per call, or one connection shared by reads and writes (where a reader can
see another task's uncommitted rows). `Database` keeps exactly two
long-lived connections:

* the writer, used through `write()`: one transaction at a time (an asyncio.Lock), committed when the block
  ends and rolled back on any exception;
* the reader (`PRAGMA query_only=ON`), used through `read()` / `reader()`: it never sees uncommitted rows of
  the writer, because it is a different connection.

A read session finishes every statement before it returns: the rows are fetched and the cursor is closed
right away. An unfinished SELECT would keep a read transaction open on the long-lived reader, which would
pin an old snapshot (stale reads) and stop WAL checkpoints.

In-memory databases (":memory:" or a "mode=memory" URI) cannot have a second connection (it would be a
different, empty database), so there the reader *is* the writer. That is the only mode in which a read can
see uncommitted rows; it exists for tests.

Use `write()` for every statement that changes data. `writer()` hands out the raw connection without the
lock, for migrations, maintenance and tests. Do not call `write()` from inside a `write()` block (it raises
instead of deadlocking), and do not start child tasks inside the block that call it either.
"""

import asyncio
import logging
import re
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Iterable, List, Optional, Sequence, Union

import aiosqlite

logger = logging.getLogger("homeguard.db")

_PRAGMA = re.compile(r"^[A-Za-z_]+\s*=\s*[A-Za-z0-9_+-]+$")


class Prefetched:
    """The result of a read statement: every row already fetched, the cursor already closed."""

    def __init__(self, rows: List[Any], description: Any, rowcount: int, lastrowid: Optional[int]):
        self._rows = rows
        self._pos = 0
        self.description = description
        self.rowcount = rowcount
        self.lastrowid = lastrowid

    async def fetchone(self) -> Any:
        if self._pos >= len(self._rows):
            return None
        row = self._rows[self._pos]
        self._pos += 1
        return row

    async def fetchall(self) -> List[Any]:
        rows, self._pos = self._rows[self._pos:], len(self._rows)
        return rows

    async def fetchmany(self, size: Optional[int] = None) -> List[Any]:
        count = 1 if size is None else size
        rows = self._rows[self._pos:self._pos + count]
        self._pos += len(rows)
        return rows

    async def close(self) -> None:
        """Nothing to release; here so the object can stand in for a cursor."""

    async def __aiter__(self) -> AsyncIterator[Any]:
        while self._pos < len(self._rows):
            row = self._rows[self._pos]
            self._pos += 1
            yield row


class _Statement:
    """Awaitable and async context manager, like aiosqlite's execute(): `await s.execute(..)` and
    `async with s.execute(..) as cursor` both give the `Prefetched`."""

    def __init__(self, coro):
        self._coro = coro
        self._result: Optional[Prefetched] = None

    def __await__(self):
        return self._coro.__await__()

    async def __aenter__(self) -> Prefetched:
        self._result = await self._coro
        return self._result

    async def __aexit__(self, *exc) -> None:
        self._result = None


class ReadSession:
    """Read access to the reader connection that looks like an aiosqlite connection.

    `row_factory` belongs to the session: set it on a session from `Database.read()` and no other session
    sees it. (The session from `Database.reader()` is shared; leave its row_factory alone.) Anything else is
    forwarded to the underlying connection.
    """

    def __init__(self, connection: aiosqlite.Connection):
        self._connection = connection
        self.row_factory: Optional[Callable] = None  # None: the connection's own (the Database's row_factory)

    def execute(self, sql: str, parameters: Optional[Iterable[Any]] = None) -> _Statement:
        return _Statement(self._run("execute", sql, parameters))

    def executemany(self, sql: str, parameters: Iterable[Iterable[Any]]) -> _Statement:
        return _Statement(self._run("executemany", sql, parameters))

    async def _run(self, method: str, sql: str, parameters: Any) -> Prefetched:
        connection = self._connection
        if method == "execute":
            cursor = await connection.execute(sql, () if parameters is None else parameters)
        else:
            cursor = await connection.executemany(sql, parameters)
        try:
            if self.row_factory is not None:
                cursor.row_factory = self.row_factory
            rows = await cursor.fetchall()
            return Prefetched(rows, cursor.description, cursor.rowcount, cursor.lastrowid)
        finally:
            await cursor.close()

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._connection, name)


class Database:
    def __init__(
        self,
        path: Union[str, Path],
        *,
        pragmas: Sequence[str] = ("synchronous=NORMAL",),
        wal: bool = True,
        row_factory: Optional[Callable] = None,
        foreign_keys: bool = False,
    ):
        """`pragmas` ("name=value") are applied to both connections; `wal` switches file databases to
        journal_mode=WAL (in WAL, synchronous=NORMAL cannot corrupt the file on a crash); `row_factory` is set
        on both connections; `foreign_keys` is for the writer. The default 5 s busy timeout is kept."""
        for pragma in pragmas:
            if not _PRAGMA.match(pragma):
                raise ValueError(f"not a 'name=value' pragma: {pragma!r}")
        self.path = str(path)
        self._pragmas = tuple(pragmas)
        self._wal = wal
        self._row_factory = row_factory
        self._foreign_keys = foreign_keys
        self._writer: Optional[aiosqlite.Connection] = None
        self._reader: Optional[aiosqlite.Connection] = None
        self._session: Optional[ReadSession] = None
        self._open_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._owner: Optional[asyncio.Task] = None
        self._closed = False

    @property
    def in_memory(self) -> bool:
        return self.path == ":memory:" or "mode=memory" in self.path

    @property
    def is_open(self) -> bool:
        return self._writer is not None

    @property
    def is_closed(self) -> bool:
        """True once `close()` has run: a closed Database is final and raises RuntimeError on any later use."""
        return self._closed

    def _check_not_closed(self) -> None:
        if self._closed:
            raise RuntimeError(f"Database {self.path} is closed; create a new Database to use it again")

    async def _connect(self, *, read_only: bool) -> aiosqlite.Connection:
        connection = await aiosqlite.connect(self.path, uri=self.path.startswith("file:"))
        try:
            if not read_only:
                if self._wal and not self.in_memory:
                    try:
                        await connection.execute("PRAGMA journal_mode=WAL")
                    except sqlite3.Error:
                        logger.warning("could not switch %s to WAL; continuing with the default journal", self.path)
                if self._foreign_keys:
                    await connection.execute("PRAGMA foreign_keys=ON")
            for pragma in self._pragmas:
                await connection.execute(f"PRAGMA {pragma}")
            if read_only:
                await connection.execute("PRAGMA query_only=ON")
            if self._row_factory is not None:
                connection.row_factory = self._row_factory
        except BaseException:
            await connection.close()
            raise
        return connection

    async def open(self) -> None:
        """Opens the writer (which creates a file database), then the reader. Idempotent; every other method
        calls it when needed. Raises RuntimeError after `close()`."""
        self._check_not_closed()
        if self._writer is not None:
            return
        async with self._open_lock:
            self._check_not_closed()
            if self._writer is not None:
                return
            writer = await self._connect(read_only=False)
            try:
                reader = writer if self.in_memory else await self._connect(read_only=True)
            except BaseException:
                await writer.close()
                raise
            self._reader = reader
            self._session = ReadSession(reader)
            self._writer = writer

    async def writer(self) -> aiosqlite.Connection:
        """The raw writer connection, without the lock or the commit/rollback: for migrations, maintenance
        and tests. Application writes go through `write()`."""
        await self.open()
        assert self._writer is not None
        return self._writer

    async def reader(self) -> ReadSession:
        """The shared, long-lived read session."""
        await self.open()
        assert self._session is not None
        return self._session

    @asynccontextmanager
    async def write(self) -> AsyncIterator[aiosqlite.Connection]:
        """One write transaction: exclusive, committed when the block ends, rolled back on any exception
        (including cancellation). An explicit `commit()` inside the block is allowed. Not re-entrant."""
        task = asyncio.current_task()
        if task is not None and self._owner is task:
            raise RuntimeError("Database.write() is not re-entrant")
        async with self._write_lock:
            self._owner = task
            try:
                db = await self.writer()
                try:
                    yield db
                    await db.commit()
                except BaseException:
                    try:
                        await db.rollback()
                    except Exception:
                        logger.warning("rollback failed", exc_info=True)
                    raise
            finally:
                self._owner = None

    @asynccontextmanager
    async def read(self) -> AsyncIterator[ReadSession]:
        """A fresh read session over the shared reader (its own row_factory)."""
        await self.open()
        assert self._reader is not None
        yield ReadSession(self._reader)

    async def close(self) -> None:
        """Waits for the transaction in flight, then closes the reader and the writer (the last connection
        to close checkpoints the WAL and removes it). Closing is final: any later use raises RuntimeError, so a
        task that outlives shutdown cannot silently reopen the database. Closing twice is fine."""
        if self._owner is not None and self._owner is asyncio.current_task():
            raise RuntimeError("Database.close() cannot be called inside a write() block")
        async with self._write_lock:
            async with self._open_lock:
                self._closed = True
                reader, writer = self._reader, self._writer
                self._reader = self._writer = self._session = None
                try:
                    if reader is not None and reader is not writer:
                        await reader.close()
                finally:
                    if writer is not None:
                        await writer.close()
