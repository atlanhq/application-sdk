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
from collections.abc import Iterator
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


# ---------------------------------------------------------------------------
# Cancelling at the driver (FND-3269): a real engine, a real driver cancel.
#
# SQLite stands in for a warehouse: ``slow()`` is a UDF that makes the scan
# take as long as the test wants, and ``sqlite3.Connection.interrupt()`` is a
# driver cancel documented as safe from another thread. Each test releases the
# UDF on the way out, so a regression drains in milliseconds instead of hanging.
# ---------------------------------------------------------------------------

#: A scan that only finishes once ``slow()`` is released, or is interrupted.
_SLOW_QUERY = (
    "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c "
    "WHERE x < 1000000) SELECT x FROM c WHERE slow(x) < 0"
)


class _InterruptingClient(BaseSQLClient):
    """A client whose per-dialect cancel interrupts the SQLite connection."""

    def __init__(self) -> None:
        super().__init__()
        self.cancel_threads: list[str] = []

    def cancel_cursor(self, dbapi_cursor) -> None:  # type: ignore[override]
        self.cancel_threads.append(threading.current_thread().name)
        dbapi_cursor.connection.interrupt()


class _SlowSqlite:
    """A file-backed SQLite engine whose ``slow()`` UDF blocks until released."""

    def __init__(self, path: str) -> None:
        from sqlalchemy import create_engine, event

        self.started = threading.Event()
        self.release = threading.Event()
        self.invalidated = threading.Event()
        self.engine = create_engine(
            f"sqlite:///{path}", connect_args={"check_same_thread": False}
        )

        def _slow(x: int) -> int:
            self.started.set()
            self.release.wait(0.001)
            return x

        @event.listens_for(self.engine, "connect")
        def _register_udf(dbapi_connection, _record) -> None:
            dbapi_connection.create_function("slow", 1, _slow)

        @event.listens_for(self.engine, "invalidate")
        def _record_invalidate(_dbapi_connection, _record, _exception) -> None:
            self.invalidated.set()


@pytest.fixture
def slow_sqlite(tmp_path) -> "Iterator[_SlowSqlite]":
    db = _SlowSqlite(str(tmp_path / "cancel.sqlite"))
    try:
        yield db
    finally:
        db.release.set()
        db.engine.dispose()


@pytest.fixture
def interrupting_client(slow_sqlite: _SlowSqlite) -> _InterruptingClient:
    client = _InterruptingClient()
    client.engine = slow_sqlite.engine
    return client


async def _assert_cancelled_at_the_driver(
    client: _InterruptingClient, db: _SlowSqlite
) -> None:
    # The driver stopped well inside the time the scan would otherwise take,
    # and the connection it ran on was dropped rather than pooled.
    assert db.invalidated.wait(_PROMPT), "cancelled connection was not invalidated"
    assert not db.release.is_set()
    assert len(client.cancel_threads) == 1
    assert client.cancel_threads[0].startswith("sdk-cancel-")
    # ...and the worker thread let go of it, so its pool slot is free again.
    deadline = time.monotonic() + _PROMPT
    while db.engine.pool.checkedout():  # type: ignore[attr-defined]
        assert time.monotonic() < deadline, "connection never released"
        await asyncio.sleep(0.01)


async def test_cancelling_get_results_cancels_the_driver_and_invalidates(
    interrupting_client: _InterruptingClient, slow_sqlite: _SlowSqlite
):
    task = asyncio.ensure_future(interrupting_client.get_results(_SLOW_QUERY))
    await _wait_for_thread(slow_sqlite.started, task)

    elapsed = await _cancel_and_time(task)

    assert elapsed < _PROMPT, f"loop blocked {elapsed:.2f}s on cancel"
    await _assert_cancelled_at_the_driver(interrupting_client, slow_sqlite)


async def test_cancelling_run_query_cancels_the_driver_and_invalidates(
    interrupting_client: _InterruptingClient, slow_sqlite: _SlowSqlite
):
    async def consume() -> None:
        async for _ in interrupting_client.run_query(_SLOW_QUERY):
            pass

    task = asyncio.ensure_future(consume())
    await _wait_for_thread(slow_sqlite.started, task)

    elapsed = await _cancel_and_time(task)

    assert elapsed < _PROMPT, f"loop blocked {elapsed:.2f}s on cancel"
    await _assert_cancelled_at_the_driver(interrupting_client, slow_sqlite)


async def test_uncancelled_read_returns_its_connection_to_the_pool(
    interrupting_client: _InterruptingClient, slow_sqlite: _SlowSqlite
):
    slow_sqlite.release.set()

    frame = await interrupting_client.get_results("SELECT slow(1) AS x")

    assert frame["x"].tolist() == [1]
    assert slow_sqlite.engine.pool.checkedin() == 1  # type: ignore[attr-defined]
    assert not slow_sqlite.invalidated.is_set()
    assert interrupting_client.cancel_threads == []
