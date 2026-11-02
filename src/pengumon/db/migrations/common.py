"""What the migrations share: three small queries and the size of the chunks that rebuild rollups."""

import aiosqlite

ROLLUP_CHUNK_SECONDS = 86400  # a multiple of 60, so no minute straddles two chunks


async def columns(db: aiosqlite.Connection, table: str) -> set:
    async with db.execute(f"PRAGMA table_info({table})") as cursor:
        return {row[1] for row in await cursor.fetchall()}


async def table_exists(db: aiosqlite.Connection, table: str) -> bool:
    async with db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)) as cursor:
        return await cursor.fetchone() is not None


async def scalar(db: aiosqlite.Connection, sql: str, params=()):
    async with db.execute(sql, params) as cursor:
        row = await cursor.fetchone()
        return row[0] if row else None
