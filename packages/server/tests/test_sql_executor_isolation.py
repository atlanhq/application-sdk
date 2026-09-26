"""One app's stuck source must not stall the other seven.

`run_in_executor(None, ...)` and `asyncio.to_thread(...)` both use the event
loop's single default ThreadPoolExecutor, sized min(32, cpu + 4) — eight
threads on a 4-core pod. On the consolidated host that one pool backed every
app's routes AND the config store's boto3 calls, so a connector whose source
stopped answering filled it with hung queries and every other app's /config
blocked waiting for a thread.
"""

from __future__ import annotations

import asyncio
import time

from server_sdk.clients.sql import _SQL_EXECUTOR_MAX_WORKERS, _sql_executor


def test_the_sql_pool_is_not_the_default_pool() -> None:
    async def scenario() -> bool:
        loop = asyncio.get_running_loop()
        # Touch the default executor so it exists, then compare identity.
        await asyncio.to_thread(lambda: None)
        return loop._default_executor is not _sql_executor()  # type: ignore[attr-defined]

    assert asyncio.run(scenario())


def test_the_pool_is_bounded_and_usable() -> None:
    assert 4 <= _SQL_EXECUTOR_MAX_WORKERS <= 16


def test_a_saturated_sql_pool_does_not_block_another_app() -> None:
    """The whole point: fill the SQL pool past capacity, then check an
    unrelated app's thread-offloaded work still runs promptly."""

    async def scenario() -> float:
        loop = asyncio.get_running_loop()
        release = asyncio.Event()

        def hung() -> None:
            while not release.is_set():
                time.sleep(0.01)

        held = [
            loop.run_in_executor(_sql_executor(), hung)
            for _ in range(_SQL_EXECUTOR_MAX_WORKERS + 4)
        ]
        await asyncio.sleep(0.2)
        try:
            started = time.perf_counter()
            await asyncio.wait_for(asyncio.to_thread(lambda: "ok"), timeout=5)
            return time.perf_counter() - started
        finally:
            release.set()
            await asyncio.gather(*held)

    assert asyncio.run(scenario()) < 1.0
