"""Tests for the shared observability background loop (FND-4648).

Each test builds its own :class:`ObservabilityLoop`, so none touches the
process-wide one the adapters use.
"""

from __future__ import annotations

import asyncio
import os
import threading
import warnings
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

    @pytest.mark.timeout(30)
    def test_is_bounded_by_one_timeout(self, obs_loop):
        """A final flush that hangs (an unreachable store) cannot hold exit
        open past the one bound: without it, this would wait an hour."""

        async def hangs_on_cancel() -> None:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                await asyncio.sleep(3600)

        obs_loop.start(hangs_on_cancel(), name="stuck")
        assert obs_loop.shutdown(timeout=0.3) is False

    def test_is_idempotent(self, obs_loop):
        obs_loop.start(_until_cancelled([], "x"), name="x")
        assert obs_loop.shutdown(timeout=5) is True
        assert obs_loop.shutdown(timeout=5) is True

    def test_stages_stop_in_order(self, obs_loop):
        """A higher ``stop_order`` task is cancelled only once every lower one
        has finished its final step (the Segment worker after the flushes)."""
        later = threading.Event()
        seen: list[bool] = []

        async def early() -> None:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                await asyncio.sleep(0.05)
                seen.append(later.is_set())

        async def late() -> None:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                later.set()

        obs_loop.start(late(), name="late", stop_order=1)
        obs_loop.start(early(), name="early")
        assert obs_loop.shutdown(timeout=5) is True
        assert seen == [False], "the later stage was cancelled too early"
        assert later.is_set()

    def test_a_start_and_a_shutdown_racing_still_drain_the_task(self, obs_loop):
        """``start`` schedules the task before ``shutdown`` can schedule its
        drain, so a task started just before shutdown still gets its final
        step."""
        gate = threading.Event()
        blocking = threading.Event()

        async def hold_the_loop() -> None:
            blocking.set()
            gate.wait(5)

        assert obs_loop.submit(hold_the_loop()) is not None
        assert blocking.wait(5)
        log: list[str] = []
        assert obs_loop.start(_until_cancelled(log, "late"), name="late") is not None
        stopper = threading.Thread(target=obs_loop.shutdown, args=(5,))
        stopper.start()
        gate.set()
        stopper.join(10)
        assert log == ["late"]

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
        blocking = threading.Event()

        async def hold_the_loop() -> None:
            # Blocks the loop's thread itself, so the raced task cannot take
            # its first step until ``gate`` is set.
            blocking.set()
            gate.wait(5)

        ready = obs_loop.submit(hold_the_loop())
        assert ready is not None
        assert blocking.wait(5), "the loop never started holding: vacuous"
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


@pytest.mark.skipif(not hasattr(os, "fork"), reason="no fork() on this platform")
class TestAfterFork:
    def test_child_restarts_an_inherited_sinks_flush_on_its_own_loop(
        self, obs_loop, tmp_path, monkeypatch
    ):
        """The parent started a sink's flush before forking. The child has no
        loop thread, yet its records still reach storage at the child's
        shutdown: the fork hook restarted the flush on a fresh loop."""
        monkeypatch.setattr(
            "application_sdk.observability.observability.observability_loop",
            obs_loop,
        )
        monkeypatch.setattr(
            "application_sdk.observability.background_loop.observability_loop",
            obs_loop,
        )
        flushed: list[list[dict[str, Any]]] = []
        sink = _RecordingSink(tmp_path, flushed)
        try:
            assert sink._start_periodic_flush() is not None
            assert sink.first_flush_done.wait(5)
            parent_thread = obs_loop._thread
            with warnings.catch_warnings():
                # 3.12+ warns that forking a multi-threaded process may
                # deadlock; the fork hook exists for exactly that case.
                warnings.simplefilter("ignore", DeprecationWarning)
                pid = os.fork()
            if pid == 0:  # pragma: no cover — runs in the child
                code = 1
                try:
                    with sink._buffer_lock:
                        sink._buffer.append({"child": True})
                    restarted = obs_loop._thread is not parent_thread
                    if (
                        restarted
                        and obs_loop.shutdown(timeout=5)
                        and any(r.get("child") for b in flushed for r in b)
                    ):
                        code = 0
                finally:
                    os._exit(code)
            _, status = os.waitpid(pid, 0)
            assert os.waitstatus_to_exitcode(status) == 0
        finally:
            AtlanObservability._instances.remove(sink)


class TestSegmentStopsAfterTheFlushes:
    def test_metrics_final_flush_drains_segment_through_an_open_client(
        self, obs_loop, monkeypatch
    ):
        """The metrics final flush drains the Segment queue; the Segment
        worker, which closes the HTTP client, is stopped only after it."""
        from application_sdk.observability import (  # noqa: PLC0415 — test-local
            segment_client as sc_module,
        )
        from application_sdk.observability.models import (  # noqa: PLC0415 — test-local
            MetricRecord,
            MetricType,
        )

        monkeypatch.setattr(sc_module, "observability_loop", obs_loop)
        client = sc_module.SegmentClient(
            write_key="fake-key", api_url="http://127.0.0.1:1/v1/batch"
        )
        sent_on_closed_client: list[bool] = []

        async def record_send(batch: list[Any], http: Any = None) -> None:
            http = http or client._client
            sent_on_closed_client.append(http.is_closed)

        monkeypatch.setattr(client, "_send_batch_to_segment", record_send)

        async def metrics_like_flush() -> None:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                # Give a worker stopped alongside this one time to close.
                await asyncio.sleep(0.05)
                assert client._queue is not None
                client._queue.put_nowait(
                    MetricRecord(
                        timestamp=1.0,
                        name="m",
                        value=1.0,
                        type=MetricType.COUNTER,
                        labels={},
                        description=None,
                        unit=None,
                    )
                )
                await client.flush()

        obs_loop.start(metrics_like_flush(), name="observability-metrics-flush")
        assert obs_loop.shutdown(timeout=5) is True
        assert sent_on_closed_client == [False]
