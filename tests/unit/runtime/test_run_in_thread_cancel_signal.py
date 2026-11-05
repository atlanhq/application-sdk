"""Code inside ``run_in_thread`` can tell that its own call was abandoned.

A cancel stops only the coroutine awaiting the thread; the thread runs on. These
tests pin the signal that lets it stop itself: ``is_cancelled()`` and
``current_cancel_handle()`` inside the thread, scoped to the one call whose
caller stopped waiting, whoever cancelled it.

Real threads throughout, each waiting with a release deadline so a regression
fails instead of hanging the suite.
"""

import asyncio
import threading
import time

import pytest
from temporalio.testing import ActivityEnvironment

from application_sdk._runtime.offload import (
    CancelHandle,
    current_cancel_handle,
    is_cancelled,
    run_in_thread,
    submit_in_thread,
)

_RELEASE_AFTER = 30.0


def _poll_until_cancelled(started: threading.Event) -> str:
    """A long loop that checks for its cancel between steps, as apps should."""
    started.set()
    deadline = time.monotonic() + _RELEASE_AFTER
    while time.monotonic() < deadline:
        if is_cancelled():
            return "stopped"
        time.sleep(0.01)
    return "ran to the deadline"


async def test_a_local_timeout_reaches_the_abandoned_thread_only() -> None:
    started = threading.Event()
    outcome: list[str] = []

    def work() -> None:
        outcome.append(_poll_until_cancelled(started))

    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.2):
            await run_in_thread(work)

    deadline = time.monotonic() + _RELEASE_AFTER
    while not outcome:
        assert time.monotonic() < deadline, "abandoned thread never stopped"
        await asyncio.sleep(0.01)
    assert outcome == ["stopped"]

    # The caller caught its timeout and carries on: neither it nor its next
    # offload is cancelled.
    assert is_cancelled() is False
    assert await run_in_thread(is_cancelled) is False


async def test_concurrent_offloads_see_only_their_own_cancel() -> None:
    cancelled_started = threading.Event()
    survivor_release = threading.Event()

    def survivor() -> bool:
        survivor_release.wait(_RELEASE_AFTER)
        return is_cancelled()

    survivor_task = asyncio.ensure_future(run_in_thread(survivor))
    doomed = asyncio.ensure_future(
        run_in_thread(_poll_until_cancelled, cancelled_started)
    )
    assert await asyncio.to_thread(cancelled_started.wait, _RELEASE_AFTER)
    doomed.cancel()
    with pytest.raises(asyncio.CancelledError):
        await doomed

    survivor_release.set()
    assert await survivor_task is False


async def test_temporal_cancel_reaches_the_thread_through_the_module_function() -> None:
    environment = ActivityEnvironment()
    started = threading.Event()
    stopped = threading.Event()

    def work() -> None:
        if _poll_until_cancelled(started) == "stopped":
            stopped.set()

    async def activity_body() -> None:
        await run_in_thread(work)

    running = asyncio.ensure_future(environment.run(activity_body))
    assert await asyncio.to_thread(started.wait, _RELEASE_AFTER)
    environment.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert await asyncio.to_thread(stopped.wait, _RELEASE_AFTER)
    assert is_cancelled() is False


async def test_every_call_has_a_handle_reachable_from_its_thread() -> None:
    assert current_cancel_handle() is None

    first = await run_in_thread(current_cancel_handle)
    second = await run_in_thread(current_cancel_handle)
    assert isinstance(first, CancelHandle)
    assert isinstance(second, CancelHandle)
    assert first is not second

    bound = CancelHandle()
    assert await run_in_thread(bound.bind(current_cancel_handle)) is bound


async def test_a_driver_cancel_registered_without_bind_fires_on_cancel() -> None:
    started = threading.Event()
    driver_cancelled = threading.Event()

    def query() -> None:
        handle = current_cancel_handle()
        assert handle is not None
        handle.set(driver_cancelled.set)
        started.set()
        driver_cancelled.wait(_RELEASE_AFTER)

    task = asyncio.ensure_future(run_in_thread(query))
    assert await asyncio.to_thread(started.wait, _RELEASE_AFTER)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await asyncio.to_thread(driver_cancelled.wait, _RELEASE_AFTER)


async def test_cleanup_submitted_from_an_abandoned_call_is_not_cancelled() -> None:
    started = threading.Event()
    release = threading.Event()
    cleanup_saw: list[bool] = []
    cleanup_done = threading.Event()

    def cleanup() -> None:
        cleanup_saw.append(is_cancelled())
        cleanup_done.set()

    def work() -> None:
        started.set()
        release.wait(_RELEASE_AFTER)
        submit_in_thread(cleanup)

    task = asyncio.ensure_future(run_in_thread(work))
    assert await asyncio.to_thread(started.wait, _RELEASE_AFTER)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    assert await asyncio.to_thread(cleanup_done.wait, _RELEASE_AFTER)
    assert cleanup_saw == [False]
