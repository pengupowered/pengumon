"""The repository reads on several connections (homeguard Database(readers=...)): a slow read session must not hold up a
quick one. With one reader (0.26 and before) every read waited for the one in front of it: a page load's small answers
took 300-500 ms behind the report."""

import asyncio

from pengumon.db.core import READERS


async def _count_incidents(repo) -> int:
    async with repo.read() as db:
        async with db.execute("SELECT COUNT(*) FROM incidents") as cursor:
            return (await cursor.fetchone())[0]


async def test_the_repository_reads_on_more_than_one_connection(repo):
    assert READERS >= 2


SLOW_QUERY = "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c WHERE x < 3000000) SELECT SUM(x) FROM c"


async def test_a_quick_read_finishes_while_a_slow_query_is_still_running(repo):
    finished = []
    started = asyncio.Event()

    async def slow_report():
        async with repo.read() as db:
            started.set()
            await db.execute(SLOW_QUERY)  # a report's long aggregate (about a second)
        finished.append("slow")

    async def quick():
        await started.wait()
        await asyncio.sleep(0.05)  # the slow statement is on its connection's thread by now
        assert await _count_incidents(repo) == 0
        finished.append("quick")

    await asyncio.gather(slow_report(), quick())
    # One reader (0.26): the quick query queued behind the slow one on the same connection and finished after it.
    assert finished == ["quick", "slow"]


async def test_a_read_nested_in_a_read_of_the_same_task_does_not_wait_for_itself(repo):
    async with repo.read() as outer:
        await outer.execute("SELECT 1")
        assert await asyncio.wait_for(_count_incidents(repo), 2) == 0
