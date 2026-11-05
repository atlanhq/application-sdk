"""``get_batched_results`` holds its connection until the chunks are read, and reads them off the loop.

Against a real SQLite engine: the behaviour under test is when the pooled
connection goes back and which thread runs each ``fetchmany``, and a mock has
neither. The public contract is unchanged — the result is still a plain
iterator of DataFrames — so the sync tests pin that too.
"""

import asyncio
import threading
from collections.abc import Iterator
from pathlib import Path

import pandas as pd
import pytest
import sqlalchemy as sa
from sqlalchemy import text

from application_sdk.clients.sql import BaseSQLClient

_ROWS = 10
_CHUNK = 3


@pytest.fixture
def client(tmp_path: Path) -> Iterator[BaseSQLClient]:
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'db.sqlite'}")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE t (a INTEGER)"))
        conn.execute(
            text("INSERT INTO t VALUES " + ",".join(f"({i})" for i in range(_ROWS)))
        )
    sql_client = BaseSQLClient(chunk_size=_CHUNK)
    sql_client.engine = engine
    yield sql_client
    engine.dispose()


@pytest.fixture
def fetch_threads(monkeypatch: pytest.MonkeyPatch) -> set[str]:
    """Names of the threads every ``fetchmany`` ran on."""
    seen: set[str] = set()
    real = sa.engine.CursorResult.fetchmany

    def spy(self: sa.engine.CursorResult, *args: object, **kwargs: object) -> object:
        seen.add(threading.current_thread().name)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(sa.engine.CursorResult, "fetchmany", spy)
    return seen


def _checked_out(client: BaseSQLClient) -> int:
    return client.engine.pool.checkedout()  # type: ignore[union-attr]


async def test_sync_iteration_is_unchanged_and_holds_the_connection_until_done(
    client: BaseSQLClient,
) -> None:
    batches = await client.get_batched_results("SELECT a FROM t ORDER BY a")
    assert isinstance(batches, Iterator)
    # Before: the connection was back in the pool while chunks were unread.
    assert _checked_out(client) == 1

    frames = list(batches)

    assert [len(frame) for frame in frames] == [3, 3, 3, 1]
    assert pd.concat(frames)["a"].tolist() == list(range(_ROWS))
    assert _checked_out(client) == 0


async def test_async_iteration_reads_every_chunk_off_the_event_loop(
    client: BaseSQLClient, fetch_threads: set[str]
) -> None:
    batches = await client.get_batched_results("SELECT a FROM t ORDER BY a")
    fetch_threads.clear()  # only the chunk reads, not the execute

    frames = [frame async for frame in batches]  # type: ignore[union-attr]

    assert pd.concat(frames)["a"].tolist() == list(range(_ROWS))
    assert fetch_threads
    assert threading.current_thread().name not in fetch_threads
    assert all(name.startswith("sdk-blocking-") for name in fetch_threads)
    assert _checked_out(client) == 0


async def test_cancelling_async_iteration_releases_the_connection(
    client: BaseSQLClient,
) -> None:
    batches = await client.get_batched_results("SELECT a FROM t ORDER BY a")
    got_first = asyncio.Event()

    async def consume() -> None:
        async for _frame in batches:  # type: ignore[union-attr]
            got_first.set()
            await asyncio.sleep(30)

    task = asyncio.ensure_future(consume())
    await asyncio.wait_for(got_first.wait(), 10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await batches.aclose()  # type: ignore[union-attr]
    assert _checked_out(client) == 0


async def test_a_cancelled_fetch_closes_the_connection_once_the_fetch_returns(
    client: BaseSQLClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    batches = await client.get_batched_results("SELECT a FROM t ORDER BY a")
    in_fetch = threading.Event()
    release = threading.Event()
    # Slow the driver read itself, under the fetch's lock, as a real one is.
    holder = batches._frames  # type: ignore[union-attr]
    pandas_frames = holder._frames

    def slow_frames() -> Iterator[pd.DataFrame]:
        in_fetch.set()
        release.wait(30)
        yield from pandas_frames

    monkeypatch.setattr(holder, "_frames", slow_frames())
    task = asyncio.ensure_future(batches.__anext__())  # type: ignore[union-attr]
    assert await asyncio.to_thread(in_fetch.wait, 30)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # The fetch is still running; the close waits for it rather than racing it.
    assert _checked_out(client) == 1
    release.set()
    for _ in range(300):
        if _checked_out(client) == 0:
            break
        await asyncio.sleep(0.01)
    assert _checked_out(client) == 0


async def test_the_writer_takes_the_async_path(
    client: BaseSQLClient, fetch_threads: set[str], tmp_path: Path
) -> None:
    from application_sdk.storage.formats import Writer

    written: list[int] = []

    class _Collect(Writer):
        async def _write_dataframe(self, dataframe: pd.DataFrame, **_: object) -> None:
            written.append(len(dataframe))

    writer = _Collect.__new__(_Collect)
    writer._is_closed = False  # type: ignore[attr-defined]
    batches = await client.get_batched_results("SELECT a FROM t ORDER BY a")
    fetch_threads.clear()

    await writer._write_batched_dataframe(batches)  # type: ignore[arg-type]

    assert written == [3, 3, 3, 1]
    assert threading.current_thread().name not in fetch_threads


async def test_closing_before_the_first_chunk_releases_the_connection(
    client: BaseSQLClient,
) -> None:
    batches = await client.get_batched_results("SELECT a FROM t ORDER BY a")
    assert _checked_out(client) == 1
    batches.close()  # type: ignore[union-attr]
    assert _checked_out(client) == 0
    assert list(batches) == []


async def test_a_cancelled_execute_drops_its_connection(
    client: BaseSQLClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nobody will read or close the result of an execute whose caller is gone."""
    in_execute = threading.Event()
    release = threading.Event()
    connections: list[sa.engine.Connection] = []
    real_execute = client._execute_pandas_query

    def slow_execute(conn: sa.engine.Connection, query: str, chunksize: int) -> object:
        connections.append(conn)
        in_execute.set()
        release.wait(30)
        return real_execute(conn, query, chunksize)

    monkeypatch.setattr(client, "_execute_pandas_query", slow_execute)
    invalidated: list[sa.engine.Connection] = []
    real_invalidate = sa.engine.Connection.invalidate

    def spy_invalidate(self: sa.engine.Connection, *args: object) -> None:
        invalidated.append(self)
        real_invalidate(self, *args)

    monkeypatch.setattr(sa.engine.Connection, "invalidate", spy_invalidate)
    task = asyncio.ensure_future(client.get_batched_results("SELECT a FROM t"))
    assert await asyncio.to_thread(in_execute.wait, 30)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    for _ in range(300):
        if _checked_out(client) == 0:
            break
        await asyncio.sleep(0.01)
    assert _checked_out(client) == 0
    # Dropped, not pooled: it was cancelled mid-execute.
    assert invalidated == connections
