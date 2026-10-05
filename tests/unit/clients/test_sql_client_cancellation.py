"""Cancelling an in-flight SQL read must hand control back to the event loop.

A blocking driver call runs on a worker thread that cannot be interrupted. When
the awaiting task is cancelled, the loop must stop waiting for that thread at
once — not join it, and not touch the connection it is still using. On a cold
warehouse the query can run for minutes; a loop blocked that long misses
heartbeats and the liveness probe.

Each test blocks a real worker thread on an event with a release deadline. The
deadline only exists so a regression fails instead of hanging the suite: code
that joins the thread on cancel takes ``_RELEASE_AFTER`` to return, well past
``_PROMPT``.
"""

import asyncio
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from application_sdk.clients.sql import BaseSQLClient

#: How long a blocked "query" waits before releasing itself.
_RELEASE_AFTER = 5.0

#: How quickly a cancelled read must give control back to the loop.
_PROMPT = 1.0


class _BlockingQuery:
    """A driver call that blocks its thread until released or the deadline passes."""

    def __init__(self, result: object = None) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        self.result = result

    def __call__(self, *args: object, **kwargs: object) -> object:
        self.started.set()
        try:
            self.release.wait(_RELEASE_AFTER)
            return self.result
        finally:
            self.finished.set()


async def _wait_for_thread(
    event: threading.Event, task: "asyncio.Task[object]"
) -> None:
    """Wait for a worker-thread event without blocking the loop."""
    deadline = time.monotonic() + _RELEASE_AFTER
    while not event.is_set():
        if task.done():
            task.result()  # surface why the read ended before reaching the call
        assert time.monotonic() < deadline, "worker thread never reached the call"
        await asyncio.sleep(0.01)


async def _cancel_and_time(task: "asyncio.Task[object]") -> float:
    """Cancel ``task`` and return how long the loop took to finish unwinding it."""
    start = time.monotonic()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    return time.monotonic() - start


@pytest.fixture
def sql_client() -> BaseSQLClient:
    client = BaseSQLClient()
    client.engine = MagicMock()
    return client


async def test_cancelling_pandas_read_returns_promptly(sql_client: BaseSQLClient):
    """get_results(): cancel lands while the query thread is still blocked."""
    query = _BlockingQuery()
    with patch.object(sql_client, "_execute_query", new=query):
        task = asyncio.ensure_future(
            sql_client._execute_async_read_operation("SELECT 1", None)
        )
        try:
            await _wait_for_thread(query.started, task)

            elapsed = await _cancel_and_time(task)

            assert elapsed < _PROMPT, f"loop blocked {elapsed:.2f}s on cancel"
            # The thread is left to drain on its own; nothing joined it.
            assert not query.finished.is_set()
        finally:
            query.release.set()


async def test_loop_stays_responsive_while_cancelled_read_drains(
    sql_client: BaseSQLClient,
):
    """Other tasks keep running on the loop after the cancel, not after the query."""
    query = _BlockingQuery()
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0.01)

    with patch.object(sql_client, "_execute_query", new=query):
        read = asyncio.ensure_future(
            sql_client._execute_async_read_operation("SELECT 1", None)
        )
        heartbeat = asyncio.ensure_future(ticker())
        try:
            await _wait_for_thread(query.started, read)
            await _cancel_and_time(read)
            before = ticks
            await asyncio.sleep(0.2)
            assert ticks > before, "loop did not run other tasks after the cancel"
            assert not query.finished.is_set()
        finally:
            heartbeat.cancel()
            query.release.set()


async def test_cancelling_run_query_returns_promptly_and_defers_close(
    sql_client: BaseSQLClient,
):
    """run_query(): no loop block on cancel, and the connection is closed only
    after the orphaned driver call has finished with it."""
    execute = _BlockingQuery()
    connection = MagicMock()
    connection.execute.side_effect = execute
    connection.execution_options.return_value = connection
    connection.__enter__.return_value = connection
    sql_client.engine.connect.return_value = connection

    async def consume() -> None:
        async for _ in sql_client.run_query("SELECT 1"):
            pass

    task = asyncio.ensure_future(consume())
    try:
        await _wait_for_thread(execute.started, task)

        elapsed = await _cancel_and_time(task)

        assert elapsed < _PROMPT, f"loop blocked {elapsed:.2f}s on cancel"
        # Closing now would race the driver call still running on the connection.
        connection.close.assert_not_called()
    finally:
        execute.release.set()

    deadline = time.monotonic() + _RELEASE_AFTER
    while not connection.close.called:
        assert time.monotonic() < deadline, "connection was never closed"
        await asyncio.sleep(0.01)
    assert execute.finished.is_set()
