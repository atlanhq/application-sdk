"""Real, isolated pools exercise nested event loops without stranding SDK threads."""

import asyncio
import contextvars
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from application_sdk._runtime import offload


@pytest.mark.parametrize("workers", [1, 6])
async def test_nested_fsync_completes_with_saturated_pool(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, workers: int
) -> None:
    """More shards than slots must not queue fsync behind their own callers."""
    barrier = threading.Barrier(workers)
    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="sdk-blocking-")
    monkeypatch.setattr(offload, "_BLOCKING_EXECUTOR", pool)

    def shard(index: int) -> tuple[int, int]:
        if index < workers:
            # Every pool thread must hold a shard at once, so the pool is
            # saturated before any nested call happens.
            barrier.wait(timeout=30)
        caller_thread = threading.get_ident()

        async def download() -> tuple[int, int]:
            with (tmp_path / f"chunk-{index}").open("wb") as handle:
                handle.write(b"chunk")
                handle.flush()

                def sync() -> int:
                    os.fsync(handle.fileno())
                    return threading.get_ident()

                sync_thread = await asyncio.wait_for(offload.run_in_thread(sync), 30)
            return caller_thread, sync_thread

        return asyncio.run(download())

    try:
        results = await asyncio.gather(
            *(offload.run_in_thread(shard, index) for index in range(workers + 2))
        )
        assert all(caller == sync for caller, sync in results)
        contents = await asyncio.to_thread(
            lambda: [path.read_bytes() for path in sorted(tmp_path.iterdir())]
        )
        assert contents == [b"chunk"] * (workers + 2)
    finally:
        pool.shutdown(wait=True, cancel_futures=True)


async def test_nested_call_preserves_context_kwargs_and_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inline nested work retains copy semantics and normal exception propagation."""
    value: contextvars.ContextVar[str] = contextvars.ContextVar(
        "value", default="outer"
    )
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sdk-blocking-")
    monkeypatch.setattr(offload, "_BLOCKING_EXECUTOR", pool)

    def outer() -> tuple[str, str]:
        value.set("worker")

        async def nested() -> tuple[str, str]:
            def inner(*, suffix: str) -> str:
                before = value.get()
                value.set("inner")
                return before + suffix

            result = await asyncio.wait_for(
                offload.run_in_thread(inner, suffix="-result"), 30
            )

            def fail() -> None:
                raise ValueError("nested failure")

            with pytest.raises(ValueError, match="nested failure"):
                await asyncio.wait_for(offload.run_in_thread(fail), 30)
            return result, value.get()

        return asyncio.run(nested())

    try:
        assert await offload.run_in_thread(outer) == ("worker-result", "worker")
        assert value.get() == "outer"
    finally:
        pool.shutdown(wait=True, cancel_futures=True)


async def test_nested_call_inline_on_per_app_pool() -> None:
    """A per-app ``api-*`` pool thread runs nested calls inline too."""
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="api-test-")

    def outer() -> tuple[int, int]:
        outer_thread = threading.get_ident()

        async def nested() -> int:
            def inner() -> int:
                return threading.get_ident()

            # If the fix regresses, the nested loop awaits a future queued behind
            # this thread; wait_for then raises TimeoutError instead of hanging CI.
            return await asyncio.wait_for(offload.run_in_thread(inner), 30)

        return outer_thread, asyncio.run(nested())

    try:
        with offload.blocking_executor(pool):
            outer_thread, inner_thread = await offload.run_in_thread(outer)
        assert outer_thread == inner_thread
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
