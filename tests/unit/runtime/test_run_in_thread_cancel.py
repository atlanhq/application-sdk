"""``run_in_thread(..., cancel_handle=)`` fires a driver-level cancel when its task is cancelled.

Real threads throughout: the behaviour under test is which thread an action
runs on and whether it still runs when the ``sdk-blocking-`` pool is full, and
neither exists without one. Every blocked call waits on an event with a release
deadline, so a regression fails the test instead of hanging the suite.
"""

import asyncio
import threading
import time
from collections.abc import Iterator

import pytest

from application_sdk._runtime import offload
from application_sdk._runtime.offload import CancelHandle, run_in_thread

#: How long a blocked call waits before releasing itself.
_RELEASE_AFTER = 5.0

#: How quickly a cancel must take effect.
_PROMPT = 1.0


class _Driver:
    """A blocking driver call whose cancel is an event its thread waits on."""

    def __init__(self, handle: CancelHandle) -> None:
        self.handle = handle
        self.started = threading.Event()
        self.cancelled = threading.Event()
        self.finished = threading.Event()
        self.cancel_threads: list[str] = []

    def cancel(self) -> None:
        self.cancel_threads.append(threading.current_thread().name)
        self.cancelled.set()

    def execute(self) -> str:
        try:
            self.handle.set(self.cancel)
            self.started.set()
            self.cancelled.wait(_RELEASE_AFTER)
            return "cancelled" if self.cancelled.is_set() else "timed out"
        finally:
            self.finished.set()


async def _wait_for(event: threading.Event) -> None:
    """Wait for a thread-side event without blocking the loop."""
    deadline = time.monotonic() + _RELEASE_AFTER
    while not event.is_set():
        assert time.monotonic() < deadline, "event never set"
        await asyncio.sleep(0.01)


async def _cancel_and_time(task: "asyncio.Task[object]") -> float:
    start = time.monotonic()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    return time.monotonic() - start


@pytest.fixture
def saturated_pool() -> Iterator[int]:
    """Occupy every ``sdk-blocking-`` slot but one; yields the slots taken."""
    release = threading.Event()
    blockers = offload._BLOCKING_EXECUTOR._max_workers - 1
    started = threading.Barrier(blockers + 1)

    def _block() -> None:
        started.wait(_RELEASE_AFTER)
        release.wait(_RELEASE_AFTER)

    futures = [offload._BLOCKING_EXECUTOR.submit(_block) for _ in range(blockers)]
    started.wait(_RELEASE_AFTER)
    try:
        yield blockers
    finally:
        release.set()
        for future in futures:
            future.result(_RELEASE_AFTER)


async def test_cancel_fires_action_once_off_the_loop_and_unwinds_promptly() -> None:
    handle = CancelHandle()
    driver = _Driver(handle)
    task = asyncio.ensure_future(run_in_thread(driver.execute, cancel_handle=handle))
    await _wait_for(driver.started)

    elapsed = await _cancel_and_time(task)

    assert elapsed < _PROMPT, f"loop blocked {elapsed:.2f}s on cancel"
    await _wait_for(driver.finished)
    assert handle.requested
    assert len(driver.cancel_threads) == 1
    assert driver.cancel_threads[0] != threading.current_thread().name
    assert driver.cancel_threads[0].startswith("sdk-cancel-")

    # A second request (a retry of the unwind, a second cancel) does not re-fire.
    handle.request()
    await asyncio.sleep(0.05)
    assert len(driver.cancel_threads) == 1


async def test_set_after_request_fires_immediately() -> None:
    handle = CancelHandle()
    handle.request()
    fired = threading.Event()

    handle.set(fired.set)

    assert fired.wait(_PROMPT), "action set after the request never fired"


async def test_action_runs_while_blocking_pool_is_full(saturated_pool: int) -> None:
    """The cancel must not queue behind the calls it exists to stop."""
    # Take the last free slot, so the pool is wholly occupied by blocked calls.
    handle = CancelHandle()
    driver = _Driver(handle)
    task = asyncio.ensure_future(run_in_thread(driver.execute, cancel_handle=handle))
    await _wait_for(driver.started)

    await _cancel_and_time(task)

    assert driver.cancelled.wait(_PROMPT), "cancel queued behind a full pool"


async def test_failing_action_is_logged_not_raised(
    loguru_capture: list[dict[str, object]],
) -> None:
    handle = CancelHandle()
    attempted = threading.Event()

    def _broken_cancel() -> None:
        attempted.set()
        raise RuntimeError("driver refused the cancel")

    handle.set(_broken_cancel)
    handle.request()  # must not raise

    assert attempted.wait(_PROMPT)
    deadline = time.monotonic() + _PROMPT
    while True:
        failures = [r for r in loguru_capture if "Cancel action" in str(r["message"])]
        if failures:
            break
        assert time.monotonic() < deadline, "failure was not logged"
        await asyncio.sleep(0.01)
    (record,) = failures
    assert record["level"].name == "WARNING"  # type: ignore[union-attr]
    assert "_broken_cancel" in str(record["message"])
    assert isinstance(record["exception"].value, RuntimeError)  # type: ignore[union-attr]


async def test_unblocked_call_frees_its_pool_slot(saturated_pool: int) -> None:
    """Once the action unblocks the driver, its thread exits and the slot is reusable."""
    handle = CancelHandle()
    driver = _Driver(handle)
    task = asyncio.ensure_future(run_in_thread(driver.execute, cancel_handle=handle))
    await _wait_for(driver.started)

    await _cancel_and_time(task)

    await _wait_for(driver.finished)
    # Every other slot is still blocked, so this runs only on the freed one.
    start = time.monotonic()
    assert await asyncio.wait_for(run_in_thread(lambda: "ran"), _PROMPT) == "ran"
    assert time.monotonic() - start < _PROMPT


async def test_completed_call_never_fires_the_action() -> None:
    handle = CancelHandle()
    fired = threading.Event()

    def _work() -> int:
        handle.set(fired.set)
        return 42

    assert await run_in_thread(_work, cancel_handle=handle) == 42
    assert not handle.requested
    await asyncio.sleep(0.05)
    assert not fired.is_set()


async def test_without_cancel_func_kwargs_pass_through() -> None:
    def _echo(value: int, *, scale: int) -> int:
        return value * scale

    assert await run_in_thread(_echo, 2, scale=3) == 6


async def test_a_func_cancel_kwarg_is_forwarded_not_taken() -> None:
    """The SDK's keyword is ``cancel_handle``, so ``func``'s own ``cancel=`` survives.

    Before FND-3269 every keyword after ``func`` reached it. A reserved
    ``cancel`` keyword would have bound a caller's token to the SDK and dropped
    it from ``func``'s call.
    """
    token = object()

    def _execute(sql: str, *, cancel: object) -> object:
        return cancel

    assert await run_in_thread(_execute, "SELECT 1", cancel=token) is token
