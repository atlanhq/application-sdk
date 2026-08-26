"""Unit tests for the harness's one sync/async bridge.

The two properties that matter are the ones D1 was about: **one loop per thread,
reused across calls** (not a fresh loop per call, which is the bug the five
``asyncio.run`` sites in ``testing/e2e/client.py`` have), and **a clear error
rather than an opaque RuntimeError** when a loop is already running.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from application_sdk.testing.harness import (
    SyncBridgeInAsyncContextError,
    bridge,
    close_loop,
    run_sync,
)


@pytest.fixture(autouse=True)
def _close_bridge_loop():
    """Leave no loop behind, so one test's loop cannot serve the next one."""
    yield
    close_loop()


async def _answer() -> int:
    return 42


def test_run_sync_returns_the_coroutine_result() -> None:
    assert run_sync(_answer()) == 42


def test_run_sync_propagates_exceptions_unwrapped() -> None:
    """The bridge adds no wrapping: existing `except` clauses keep matching."""

    class Sentinel(ValueError):
        pass

    async def _boom() -> None:
        raise Sentinel("from the coroutine")

    with pytest.raises(Sentinel, match="from the coroutine"):
        run_sync(_boom())


def test_the_loop_is_reused_across_calls() -> None:
    """The whole point of D1's amendment: no new loop (and no new TLS handshake) per call."""

    async def _current_loop() -> asyncio.AbstractEventLoop:
        return asyncio.get_running_loop()

    first = run_sync(_current_loop())
    second = run_sync(_current_loop())
    assert first is second


def test_each_thread_gets_its_own_loop() -> None:
    """An event loop is not thread-safe, and pytest plugins do run code off-main."""

    async def _current_loop() -> asyncio.AbstractEventLoop:
        return asyncio.get_running_loop()

    main_loop = run_sync(_current_loop())
    seen: list[asyncio.AbstractEventLoop] = []

    def _worker() -> None:
        try:
            seen.append(run_sync(_current_loop()))
        finally:
            close_loop()

    thread = threading.Thread(target=_worker)
    thread.start()
    thread.join()

    assert len(seen) == 1
    assert seen[0] is not main_loop


def test_calling_from_a_running_loop_raises_and_names_the_twin() -> None:
    async def _reenter() -> None:
        run_sync(_answer())

    with pytest.raises(SyncBridgeInAsyncContextError) as caught:
        asyncio.run(_reenter())
    assert "_async" in str(caught.value)


def test_the_rejected_coroutine_is_closed_not_leaked() -> None:
    """A never-awaited coroutine that is merely GC'd emits a RuntimeWarning that
    lands next to the real error and reads as a second, unrelated bug."""
    coro = _answer()

    async def _reenter() -> None:
        with pytest.raises(SyncBridgeInAsyncContextError):
            run_sync(coro)

    asyncio.run(_reenter())
    # cr_frame is None once a coroutine has been closed.
    assert coro.cr_frame is None


def test_close_loop_is_idempotent() -> None:
    run_sync(_answer())
    close_loop()
    close_loop()
    assert getattr(bridge._state, "loop", None) is None


def test_run_sync_after_close_creates_a_fresh_loop() -> None:
    """Closing early must not poison the thread — the alternative is a
    `RuntimeError: Event loop is closed` from deep inside asyncio."""

    async def _current_loop() -> asyncio.AbstractEventLoop:
        return asyncio.get_running_loop()

    first = run_sync(_current_loop())
    close_loop()
    second = run_sync(_current_loop())
    assert second is not first
    assert first.is_closed()
