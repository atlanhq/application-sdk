"""SDK work offloaded with ``run_in_thread`` stops when its caller is cancelled.

Each test runs the real helper on a real offload thread, cancels the awaiting
task, and asserts the thread stopped instead of running on for nobody. Every
wait has a release deadline so a regression fails rather than hangs.
"""

import asyncio
import threading
import time
from pathlib import Path

import duckdb
import pytest

from application_sdk._runtime import offload
from application_sdk._runtime.offload import CancelHandle, run_in_thread
from application_sdk.common.incremental import helpers
from application_sdk.common.incremental.state.state_writer import _interruptible
from application_sdk.common.incremental.storage.duckdb_utils import (
    DuckDBConnectionManager,
)
from application_sdk.storage.integrity import _sha256_file_sync

_RELEASE_AFTER = 30.0

#: A cross join DuckDB needs far longer than any test waits to finish.
_SLOW_QUERY = "SELECT sum(a.range * b.range) FROM range(1000000) a, range(1000000) b"


async def _cancel_once_started(
    task: "asyncio.Future[object]", started: threading.Event
) -> None:
    assert await asyncio.to_thread(started.wait, _RELEASE_AFTER)
    # Let the query get under way before the cancel lands.
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_a_connection_opened_inside_the_offload_is_interrupted(
    tmp_path: Path,
) -> None:
    started = threading.Event()
    outcome: list[str] = []

    def scan() -> None:
        with DuckDBConnectionManager(base_path=str(tmp_path)) as manager:
            started.set()
            try:
                manager.connection.execute(_SLOW_QUERY).fetchall()
                outcome.append("ran to completion")
            except duckdb.InterruptException:
                outcome.append("interrupted")

    await _cancel_once_started(asyncio.ensure_future(run_in_thread(scan)), started)
    deadline = time.monotonic() + _RELEASE_AFTER
    while not outcome:
        assert time.monotonic() < deadline, "scan never stopped"
        await asyncio.sleep(0.01)
    assert outcome == ["interrupted"]


async def test_a_shared_connection_is_interrupted_through_interruptible() -> None:
    conn = duckdb.connect()
    started = threading.Event()
    outcome: list[str] = []

    def scan(sql: str) -> None:
        started.set()
        try:
            conn.execute(sql).fetchall()
            outcome.append("ran to completion")
        except duckdb.InterruptException:
            outcome.append("interrupted")

    wrapped = _interruptible(scan, conn)
    assert wrapped.__qualname__ == scan.__qualname__
    await _cancel_once_started(
        asyncio.ensure_future(run_in_thread(wrapped, _SLOW_QUERY)), started
    )
    deadline = time.monotonic() + _RELEASE_AFTER
    while not outcome:
        assert time.monotonic() < deadline, "scan never stopped"
        await asyncio.sleep(0.01)
    assert outcome == ["interrupted"]
    conn.close()


async def test_a_cancelled_carry_forward_copy_stops_between_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src, dest = tmp_path / "src", tmp_path / "dest"
    src.mkdir()
    for index in range(5):
        (src / f"{index}.json").write_text("{}")
    first_copy_started = threading.Event()
    release_first_copy = threading.Event()
    copied: list[str] = []
    real_copy = helpers.atomic_copy

    def slow_first_copy(source: Path, target: Path, **kwargs: object) -> None:
        if not copied:
            first_copy_started.set()
            release_first_copy.wait(_RELEASE_AFTER)
        real_copy(source, target, **kwargs)
        copied.append(source.name)

    monkeypatch.setattr(helpers, "atomic_copy", slow_first_copy)
    finished = threading.Event()

    def copy() -> None:
        try:
            helpers.copy_directory_parallel(src, dest)
        finally:
            finished.set()

    task = asyncio.ensure_future(run_in_thread(copy))
    assert await asyncio.to_thread(first_copy_started.wait, _RELEASE_AFTER)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release_first_copy.set()
    assert await asyncio.to_thread(finished.wait, _RELEASE_AFTER)
    assert len(copied) == 1


def test_hashing_stops_once_its_offload_is_cancelled(tmp_path: Path) -> None:
    path = tmp_path / "blob"
    path.write_bytes(b"x" * (3 << 20))
    handle = CancelHandle()
    handle.request()
    token = offload._CURRENT_CANCEL_HANDLE.set(handle)
    try:
        with pytest.raises(offload.concurrent.futures.CancelledError):
            _sha256_file_sync(path)
    finally:
        offload._CURRENT_CANCEL_HANDLE.reset(token)
    # Outside a cancelled offload the same file hashes normally.
    assert len(_sha256_file_sync(path)) == 64
