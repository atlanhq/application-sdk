"""Tests for the shared observability background loop (FND-4648).

Each test builds its own :class:`ObservabilityLoop`, so none touches the
process-wide one the adapters use.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest

from application_sdk.observability.background_loop import (
    THREAD_NAME,
    ObservabilityLoop,
)
from application_sdk.observability.observability import AtlanObservability


@pytest.fixture
def obs_loop() -> Iterator[ObservabilityLoop]:
    loop = ObservabilityLoop()
    try:
        yield loop
    finally:
        loop.shutdown(timeout=5)


async def _until_cancelled(log: list[str], label: str) -> None:
    """A periodic-flush shape: run until cancelled, then a final step."""
    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        # The final flush; awaits, so a second cancel would interrupt it.
        await asyncio.sleep(0.05)
        log.append(label)


class TestOneThreadOneLoop:
    def test_every_task_runs_on_the_one_named_thread(self, obs_loop):
        seen: list[tuple[str, asyncio.AbstractEventLoop]] = []

        async def record() -> None:
            seen.append((threading.current_thread().name, asyncio.get_running_loop()))

        handles = [obs_loop.start(record(), name=f"t{i}") for i in range(4)]
        assert all(h is not None and h.wait(5) for h in handles)
        assert {name for name, _ in seen} == {THREAD_NAME}
        assert len({id(loop) for _, loop in seen}) == 1
        named = [t for t in threading.enumerate() if t.name == THREAD_NAME]
        assert len(named) >= 1

    def test_nothing_starts_until_first_use(self):
        loop = ObservabilityLoop()
        assert loop._thread is None
        loop.shutdown(timeout=1)

    def test_in_loop_thread(self, obs_loop):
        flags: list[bool] = []

        async def probe() -> None:
            flags.append(obs_loop.in_loop_thread())

        handle = obs_loop.start(probe(), name="probe")
        assert handle is not None and handle.wait(5)
        assert flags == [True]
        assert obs_loop.in_loop_thread() is False


class TestShutdown:
    def test_cancels_each_started_task_once_and_waits_for_its_final_step(
        self, obs_loop
    ):
        log: list[str] = []
        a = obs_loop.start(_until_cancelled(log, "a"), name="a")
        b = obs_loop.start(_until_cancelled(log, "b"), name="b")
        assert a is not None and b is not None
        # A second cancel while the final step runs must not interrupt it.
        a.cancel()
        a.cancel()

        assert obs_loop.shutdown(timeout=5) is True
        assert sorted(log) == ["a", "b"]
        assert a.done() and b.done()

    def test_awaits_other_tasks_instead_of_cancelling_them(self, obs_loop):
        """A batch flush started on the loop (not via ``start``) is awaited:
        cancelling one would delete its half-written file and lose the batch."""
        finished = threading.Event()

        async def batch_flush() -> None:
            await asyncio.sleep(0.1)
            finished.set()

        async def spawn() -> None:
            asyncio.get_running_loop().create_task(batch_flush())

        future = obs_loop.submit(spawn())
        assert future is not None
        future.result(5)

        assert obs_loop.shutdown(timeout=5) is True
        assert finished.is_set()

    def test_is_bounded_by_one_timeout(self, obs_loop):
        """A final flush that hangs (an unreachable store) cannot hold exit
        open past the one bound."""

        async def hangs_on_cancel() -> None:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                await asyncio.sleep(3600)

        obs_loop.start(hangs_on_cancel(), name="stuck")
        started = time.monotonic()
        assert obs_loop.shutdown(timeout=0.3) is False
        assert time.monotonic() - started < 2.0

    def test_is_idempotent(self, obs_loop):
        obs_loop.start(_until_cancelled([], "x"), name="x")
        assert obs_loop.shutdown(timeout=5) is True
        assert obs_loop.shutdown(timeout=5) is True

    def test_starts_nothing_after_shutdown(self, obs_loop):
        obs_loop.shutdown(timeout=5)
        ran: list[int] = []

        async def work() -> None:
            ran.append(1)

        coro = work()
        assert obs_loop.start(coro, name="late") is None
        # The refused coroutine was closed, not left un-awaited.
        assert coro.cr_frame is None
        assert obs_loop.submit(work()) is None
        assert obs_loop.closed
        assert ran == []

    def test_a_cancel_racing_the_start_still_lands(self, obs_loop):
        """``cancel()`` called before the task is created on the loop."""
        gate = threading.Event()
        ready = obs_loop.submit(asyncio.to_thread(gate.wait, 5))
        assert ready is not None  # the loop is busy until ``gate`` is set
        log: list[str] = []
        handle = obs_loop.start(_until_cancelled(log, "raced"), name="raced")
        assert handle is not None
        handle.cancel()
        gate.set()
        assert handle.wait(5)
        assert log == ["raced"]


class _RecordingSink(AtlanObservability[Any]):
    """A real ``AtlanObservability`` whose writes are recorded, not stored."""

    def __init__(self, tmp_path: Any, flushed: list[list[dict[str, Any]]]) -> None:
        super().__init__(
            batch_size=1000,
            flush_interval=3600,
            retention_days=1,
            cleanup_enabled=False,
            data_dir=str(tmp_path),
            file_name="test.parquet",
        )
        self._flushed = flushed
        self.first_flush_done = threading.Event()

    async def _flush_buffer(self, force: bool = False) -> None:
        await super()._flush_buffer(force=force)
        self.first_flush_done.set()

    def process_record(self, record: Any) -> dict[str, Any]:
        return record

    def export_record(self, record: Any) -> None:
        return None

    async def _flush_records(self, records: list[dict[str, Any]]) -> None:
        self._flushed.append(list(records))


class TestSinksShareTheLoop:
    def test_one_shutdown_makes_every_sinks_final_flush(
        self, obs_loop, tmp_path, monkeypatch
    ):
        """Two sinks, one loop, one shutdown: both buffers reach storage."""
        monkeypatch.setattr(
            "application_sdk.observability.observability.observability_loop",
            obs_loop,
        )
        flushed: list[list[dict[str, Any]]] = []
        sinks = [_RecordingSink(tmp_path / str(i), flushed) for i in range(2)]
        try:
            handles = [sink._start_periodic_flush() for sink in sinks]
            assert all(h is not None for h in handles)
            # Past the initial flush, so only the final one can write these.
            assert all(s.first_flush_done.wait(5) for s in sinks)
            for i, sink in enumerate(sinks):
                with sink._buffer_lock:
                    sink._buffer.append({"sink": i})

            assert obs_loop.shutdown(timeout=5) is True
        finally:
            for sink in sinks:
                AtlanObservability._instances.remove(sink)

        assert sorted(r["sink"] for batch in flushed for r in batch) == [0, 1]
