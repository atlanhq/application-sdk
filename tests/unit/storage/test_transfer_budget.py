"""The process-wide in-flight byte budget and the flat fan-out (FND-4082).

The storage concurrency limits nest — refs × files × range GETs — so their
product, not any one of them, is what a worker's memory holds. These tests pin
the one bound on that product, end to end through ``materialize_file_refs`` /
``persist_file_refs``, and that a large fan-out holds a bounded number of tasks
rather than one per key.
"""

from __future__ import annotations

import asyncio
import contextvars
import shutil
import tracemalloc
from pathlib import Path
from typing import Any

import obstore
import pytest

from application_sdk.contracts.base import Input
from application_sdk.contracts.types import FileReference
from application_sdk.storage import _budget
from application_sdk.storage._budget import ByteBudget, budget_bytes, transfer_budget
from application_sdk.storage._concurrency import _run_bounded
from application_sdk.storage.errors import StorageError
from application_sdk.storage.factory import create_memory_store
from application_sdk.storage.file_ref_sync import (
    materialize_file_refs,
    persist_file_refs,
)
from application_sdk.storage.ops import download_file

_KIB = 1024
_MIB = 1024 * _KIB

#: Bound for any wait a regression could make endless.
_TIMEOUT = 5.0


@pytest.fixture(autouse=True)
def _fresh_budgets():
    """Each test sizes its own budget; none inherits another's."""
    _budget._BUDGETS.clear()
    yield
    _budget._BUDGETS.clear()


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# ByteBudget
# ---------------------------------------------------------------------------


class TestByteBudget:
    @pytest.mark.parametrize("capacity", [0, -1])
    def test_rejects_a_non_positive_capacity(self, capacity: int) -> None:
        with pytest.raises(ValueError, match="positive"):
            ByteBudget(capacity)

    async def test_grants_up_to_capacity_then_queues(self) -> None:
        budget = ByteBudget(100)
        async with budget.reserve(60):
            second = asyncio.ensure_future(_hold(budget, 50, asyncio.Event()))
            await _settle()
            assert not second.done()
            assert budget.used == 60
        await asyncio.wait_for(second, _TIMEOUT)
        assert budget.used == 0
        assert budget.peak == 60

    async def test_first_come_first_served(self) -> None:
        """A queued large request is not overtaken by smaller ones that fit."""
        budget = ByteBudget(100)
        order: list[str] = []

        async def _take(name: str, nbytes: int) -> None:
            async with budget.reserve(nbytes):
                order.append(name)

        async with budget.reserve(70):
            big = asyncio.ensure_future(_take("big", 80))
            await _settle()
            small = asyncio.ensure_future(_take("small", 10))
            await _settle()
            # 10 fits beside the 70 held, but the 80 queued first.
            assert order == []
        await asyncio.wait_for(asyncio.gather(big, small), _TIMEOUT)
        assert order == ["big", "small"]

    async def test_request_over_capacity_runs_alone(self) -> None:
        budget = ByteBudget(100)
        async with budget.reserve(500) as held:
            assert held == 100
            assert budget.used == 100

    async def test_cancelled_waiter_unblocks_the_next(self) -> None:
        budget = ByteBudget(100)
        async with budget.reserve(50):
            big = asyncio.ensure_future(_hold(budget, 80, asyncio.Event()))
            await _settle()
            small_done = asyncio.Event()
            # Held until released: the assertion below must see the small
            # waiter's bytes, whichever task the loop resumes first.
            small_release = asyncio.Event()
            small = asyncio.ensure_future(
                _hold(budget, 40, small_done, release=small_release)
            )
            await _settle()
            assert not small_done.is_set()
            big.cancel()
            await asyncio.wait_for(small_done.wait(), _TIMEOUT)
            assert budget.used == 90
            small_release.set()
        await asyncio.wait_for(small, _TIMEOUT)
        with pytest.raises(asyncio.CancelledError):
            await big
        assert budget.used == 0

    async def test_cancel_after_grant_returns_the_bytes(self) -> None:
        """Granted then cancelled before resuming: the bytes go back."""
        budget = ByteBudget(100)
        holder = budget.reserve(100)
        await holder.__aenter__()
        waiter = asyncio.ensure_future(_hold(budget, 100, asyncio.Event()))
        await _settle()
        await holder.__aexit__(None, None, None)  # grants the waiter
        waiter.cancel()  # before it runs again
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert budget.used == 0

    async def test_released_on_failure(self) -> None:
        budget = ByteBudget(100)
        with pytest.raises(RuntimeError):
            async with budget.reserve(100):
                raise RuntimeError("boom")
        assert budget.used == 0

    def test_one_budget_per_loop(self) -> None:
        async def _get() -> ByteBudget:
            return transfer_budget()

        async def _same() -> bool:
            return transfer_budget() is transfer_budget()

        assert asyncio.run(_same())
        assert asyncio.run(_get()) is not asyncio.run(_get())


async def _hold(
    budget: ByteBudget,
    nbytes: int,
    entered: asyncio.Event,
    *,
    release: asyncio.Event | None = None,
) -> None:
    async with budget.reserve(nbytes):
        entered.set()
        if release is None:
            await asyncio.sleep(0)
        else:
            await release.wait()


class TestBudgetSizing:
    def test_override_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "application_sdk.constants.STORAGE_TRANSFER_MEMORY_BUDGET_BYTES", 5 * _MIB
        )
        monkeypatch.setattr(_budget.cgroup, "memory_limit_bytes", lambda: 8 << 30)
        assert budget_bytes() == 5 * _MIB

    def test_quarter_of_the_container_limit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "application_sdk.constants.STORAGE_TRANSFER_MEMORY_BUDGET_BYTES", 0
        )
        monkeypatch.setattr(_budget.cgroup, "memory_limit_bytes", lambda: 3 << 30)
        assert budget_bytes() == 768 * _MIB

    def test_floor_on_a_small_container(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "application_sdk.constants.STORAGE_TRANSFER_MEMORY_BUDGET_BYTES", 0
        )
        monkeypatch.setattr(_budget.cgroup, "memory_limit_bytes", lambda: 256 * _MIB)
        assert budget_bytes() == 128 * _MIB

    def test_no_known_limit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "application_sdk.constants.STORAGE_TRANSFER_MEMORY_BUDGET_BYTES", 0
        )
        monkeypatch.setattr(_budget.cgroup, "memory_limit_bytes", lambda: None)
        assert budget_bytes() == 1024 * _MIB


# ---------------------------------------------------------------------------
# Four directory refs, end to end
# ---------------------------------------------------------------------------

_CHUNK = 64 * _KIB
_FILES_PER_REF = 4
_CHUNKS_PER_FILE = 4


class _FourRefs(Input):
    a: FileReference
    b: FileReference
    c: FileReference
    d: FileReference


class _InFlightRanges:
    """Wraps obstore's GETs to measure the range bytes held at once.

    A range is held from the moment its GET is issued until its bytes are
    returned; ``_fetch_chunk`` writes them to disk before its next await, so
    nothing else can start in between. Each GET yields to the loop first, so
    every range the limits allow is outstanding at the same time.
    """

    def __init__(self) -> None:
        self.held = 0
        self.peak = 0
        self._get_async = obstore.get_async
        self._get_range_async = obstore.get_range_async

    def _add(self, nbytes: int) -> None:
        self.held += nbytes
        self.peak = max(self.peak, self.held)

    async def get_async(self, store: Any, key: str, *, options: Any = None) -> Any:
        rng = (options or {}).get("range")
        if rng is None:
            return await self._get_async(store, key, options=options)
        nbytes = rng[1] - rng[0]
        self._add(nbytes)
        await asyncio.sleep(0.005)
        result = await self._get_async(store, key, options=options)
        outer = self

        class _Result:
            async def bytes_async(self) -> Any:
                try:
                    return await result.bytes_async()
                finally:
                    outer.held -= nbytes

        return _Result()

    async def get_range_async(
        self, store: Any, key: str, *, start: int, length: int
    ) -> Any:
        self._add(length)
        try:
            await asyncio.sleep(0.005)
            return await self._get_range_async(store, key, start=start, length=length)
        finally:
            self.held -= length


async def _persisted_dirs(store: Any, root: Path) -> _FourRefs:
    refs: dict[str, FileReference] = {}
    for name in "abcd":
        directory = root / name
        directory.mkdir(parents=True)
        for i in range(_FILES_PER_REF):
            (directory / f"part-{i}.bin").write_bytes(
                bytes([i]) * (_CHUNK * _CHUNKS_PER_FILE)
            )
        refs[name] = FileReference.from_local(str(directory))
    persisted = await persist_file_refs(store, _FourRefs(**refs))
    for name in "abcd":
        shutil.rmtree(root / name)  # a different pod: nothing local to reuse
    return persisted


@pytest.fixture
def small_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("application_sdk.constants.FILE_REF_CHUNK_SIZE_BYTES", _CHUNK)


class TestStreamedDownloads:
    """``download_file``'s streaming GET reserves its buffer for the stream."""

    async def test_concurrent_streams_wait_on_the_budget(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        size = 64 * _KIB
        store = create_memory_store()
        for i in range(8):
            await obstore.put_async(store, f"small/{i}.bin", bytes([i]) * size)
        # Each stream reserves twice its object (the Rust buffer and its
        # Python copy), so this budget admits two streams at a time.
        monkeypatch.setattr(
            "application_sdk.constants.STORAGE_TRANSFER_MEMORY_BUDGET_BYTES",
            2 * (2 * size),
        )
        streaming = 0
        peak_streaming = 0
        real_get = obstore.get_async

        async def _counting_get(store_: Any, key: str, **kwargs: Any) -> Any:
            result = await real_get(store_, key, **kwargs)

            class _Counted:
                meta = result.meta

                async def stream(self, **stream_kwargs: Any) -> Any:
                    nonlocal streaming, peak_streaming
                    streaming += 1
                    peak_streaming = max(peak_streaming, streaming)
                    try:
                        async for chunk in result.stream(**stream_kwargs):
                            await asyncio.sleep(0.005)
                            yield chunk
                    finally:
                        streaming -= 1

            return _Counted()

        monkeypatch.setattr(obstore, "get_async", _counting_get)
        await asyncio.wait_for(
            asyncio.gather(
                *(
                    download_file(
                        f"small/{i}.bin",
                        tmp_path / f"{i}.bin",
                        store,
                        normalize=False,
                        verify=False,
                    )
                    for i in range(8)
                )
            ),
            _TIMEOUT,
        )
        assert peak_streaming == 2
        assert transfer_budget().peak == 4 * size
        assert all((tmp_path / f"{i}.bin").stat().st_size == size for i in range(8))


class TestFourDirectoryRefs:
    async def _materialize_peak(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, budget: int
    ) -> tuple[int, int]:
        store = create_memory_store()
        persisted = await _persisted_dirs(store, tmp_path / "src")
        _budget._BUDGETS.clear()
        monkeypatch.setattr(
            "application_sdk.constants.STORAGE_TRANSFER_MEMORY_BUDGET_BYTES", budget
        )
        ranges = _InFlightRanges()
        monkeypatch.setattr(obstore, "get_async", ranges.get_async)
        monkeypatch.setattr(obstore, "get_range_async", ranges.get_range_async)
        materialized = await asyncio.wait_for(
            materialize_file_refs(store, persisted), 30
        )
        for name in "abcd":
            local = Path(getattr(materialized, name).local_path)
            assert len(list(local.glob("part-*.bin"))) == _FILES_PER_REF
        return ranges.peak, transfer_budget().peak

    async def test_range_bytes_held_stay_within_the_budget(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, small_chunks: None
    ) -> None:
        budget = 4 * _CHUNK
        held_peak, reserved_peak = await self._materialize_peak(
            monkeypatch, tmp_path, budget
        )
        assert 0 < held_peak <= budget
        assert reserved_peak <= budget

    async def test_control_without_a_binding_budget_the_limits_multiply(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, small_chunks: None
    ) -> None:
        """Proves the instrument sees the nesting the budget exists to bound."""
        held_peak, _ = await self._materialize_peak(monkeypatch, tmp_path, 1 << 40)
        assert held_peak > 4 * _CHUNK

    async def test_upload_buffers_stay_within_the_budget(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        file_bytes = _CHUNK * _CHUNKS_PER_FILE
        # Each small upload reserves twice its file (one buffer, plus the read).
        budget = 2 * (2 * file_bytes)
        monkeypatch.setattr(
            "application_sdk.constants.STORAGE_TRANSFER_MEMORY_BUDGET_BYTES", budget
        )
        open_writers = 0
        peak_writers = 0
        real_open = obstore.open_writer_async

        def _counting_open(*args: Any, **kwargs: Any) -> Any:
            writer = real_open(*args, **kwargs)

            class _Counted:
                async def __aenter__(self) -> Any:
                    nonlocal open_writers, peak_writers
                    inner = await writer.__aenter__()
                    open_writers += 1
                    peak_writers = max(peak_writers, open_writers)
                    await asyncio.sleep(0.005)
                    return inner

                async def __aexit__(self, *exc: Any) -> Any:
                    nonlocal open_writers
                    open_writers -= 1
                    return await writer.__aexit__(*exc)

            return _Counted()

        monkeypatch.setattr(obstore, "open_writer_async", _counting_open)
        await asyncio.wait_for(
            _persisted_dirs(create_memory_store(), tmp_path / "src"), 30
        )
        assert peak_writers == 2
        assert transfer_budget().peak <= budget


# ---------------------------------------------------------------------------
# Flat fan-out
# ---------------------------------------------------------------------------


class TestFlatFanOut:
    @pytest.mark.perf
    async def test_100k_keys_hold_a_bounded_number_of_tasks(self) -> None:
        bound = 4
        peak_tasks = 0

        async def _item(i: int) -> None:
            nonlocal peak_tasks
            if i % 1000 == 0:  # all_tasks() is O(tasks); sampled, not per key
                peak_tasks = max(peak_tasks, len(asyncio.all_tasks()))
            await asyncio.sleep(0)

        tracemalloc.start()
        try:
            await _run_bounded((_item(i) for i in range(100_000)), bound)
            _, peak_bytes = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        # This test's task, the workers, and the one item each is running.
        assert peak_tasks <= 1 + 2 * bound
        # A task per key costs about 2 KB; what is left per key is its result.
        assert peak_bytes / 100_000 < 300

    async def test_results_keep_input_order(self) -> None:
        async def _item(i: int) -> int:
            await asyncio.sleep(0.001 * (i % 3))
            return i

        assert await _run_bounded((_item(i) for i in range(20)), 3) == list(range(20))

    async def test_unstarted_coroutines_are_closed_on_failure(self) -> None:
        async def _fail() -> None:
            raise StorageError("boom")

        async def _never() -> None:
            raise AssertionError("ran after the fan-out failed")

        rest = [_never() for _ in range(5)]
        with pytest.raises(StorageError):
            await _run_bounded([_fail(), *rest], 1)
        assert all(coro.cr_frame is None for coro in rest)  # closed

    @pytest.mark.parametrize("bound", [1, 2, None])
    async def test_an_item_that_cancels_itself_is_not_dropped(
        self, bound: int | None
    ) -> None:
        """Its result is missing, so the call raises — after the rest ran."""
        ran: list[int] = []

        async def _cancels_itself() -> int:
            raise asyncio.CancelledError

        async def _item(i: int) -> int:
            ran.append(i)
            return i

        with pytest.raises(asyncio.CancelledError):
            await _run_bounded([_cancels_itself(), _item(1), _item(2)], bound)
        assert ran == [1, 2]

    async def test_the_caller_can_still_cancel(self) -> None:
        started = asyncio.Event()

        async def _slow() -> None:
            started.set()
            await asyncio.sleep(_TIMEOUT)

        task = asyncio.ensure_future(_run_bounded([_slow() for _ in range(5)], 2))
        await asyncio.wait_for(started.wait(), _TIMEOUT)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert task.cancelled()

    async def test_each_coroutine_runs_in_its_own_context(self) -> None:
        var: contextvars.ContextVar[int | None] = contextvars.ContextVar(
            "fanout", default=None
        )
        seen: list[int | None] = []

        async def _item(i: int) -> None:
            seen.append(var.get())
            var.set(i)
            await asyncio.sleep(0)

        await _run_bounded((_item(i) for i in range(6)), 2)
        assert seen == [None] * 6
