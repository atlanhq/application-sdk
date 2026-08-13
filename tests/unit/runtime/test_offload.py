"""Unit tests for application_sdk._runtime.offload — dispatch mechanics.

Strict no-real-thread policy: ``asyncio.get_running_loop`` is patched so the
SDK's blocking pool is never touched, and ``_BLOCKING_EXECUTOR`` is swapped for
an inline or pending stand-in where the detached path is under test.

The hold behaviour these primitives layer on top of the progress tracker lives
in ``tests/unit/execution/test_offload_holds.py``, which exercises it through a
real task rather than through the dispatch internals here.
"""

from __future__ import annotations

import concurrent.futures
import functools
from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from application_sdk._runtime.offload import run_in_thread

# ---------------------------------------------------------------------------
# run_in_thread — must NOT use real threads in tests
# ---------------------------------------------------------------------------


class TestRunInThread:
    @pytest.mark.asyncio
    async def test_dispatches_through_loop_executor(self) -> None:
        """Verify run_in_thread uses asyncio.run_in_executor + the SDK pool.

        We patch asyncio.get_running_loop to return a Mock whose
        run_in_executor is an AsyncMock. The blocking executor passed
        in must be the module-level _BLOCKING_EXECUTOR.
        """
        from application_sdk._runtime import offload as hb_mod

        sentinel = object()
        fake_loop = MagicMock()
        fake_loop.run_in_executor = AsyncMock(return_value=sentinel)

        with patch.object(hb_mod.asyncio, "get_running_loop", return_value=fake_loop):
            result = await run_in_thread(lambda x: x, "ignored")

        assert result is sentinel
        # Ensure the SDK-owned executor was the one passed.
        called_args, _ = fake_loop.run_in_executor.call_args
        assert called_args[0] is hb_mod._BLOCKING_EXECUTOR
        # And a callable (not the raw lambda — wrapped via partial(ctx.run, partial(...)))
        assert callable(called_args[1])

    @pytest.mark.asyncio
    async def test_propagates_function_kwargs(self) -> None:
        """The wrapped callable must close over the original args and kwargs."""
        from application_sdk._runtime import offload as hb_mod

        captured = {}

        async def fake_run_in_executor(executor, fn):
            # Invoke the wrapped callable directly to confirm args propagate.
            captured["result"] = fn()
            return captured["result"]

        fake_loop = MagicMock()
        fake_loop.run_in_executor = fake_run_in_executor

        def adder(a, b, c=0):
            return a + b + c

        with patch.object(hb_mod.asyncio, "get_running_loop", return_value=fake_loop):
            result = await run_in_thread(adder, 1, 2, c=10)

        assert result == 13
        assert captured["result"] == 13

    @pytest.mark.asyncio
    async def test_exception_inside_func_propagates(self) -> None:
        """If the wrapped function raises, the exception bubbles to the caller."""
        from application_sdk._runtime import offload as hb_mod

        async def fake_run_in_executor(executor, fn):
            return fn()  # call wrapped fn synchronously here

        fake_loop = MagicMock()
        fake_loop.run_in_executor = fake_run_in_executor

        def bad():
            raise ValueError("nope")

        with patch.object(hb_mod.asyncio, "get_running_loop", return_value=fake_loop):
            with pytest.raises(ValueError):
                await run_in_thread(bad)


# ---------------------------------------------------------------------------
# submit_in_thread — detached cleanup, also no real threads
# ---------------------------------------------------------------------------


class InlineExecutor:
    """Stand-in for the SDK pool that runs submitted work on the calling thread."""

    def __init__(self) -> None:
        self.submissions: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def submit(self, fn, *args, **kwargs):
        self.submissions.append((args, kwargs))
        future: concurrent.futures.Future[Any] = concurrent.futures.Future()
        future.set_running_or_notify_cancel()
        try:
            future.set_result(fn(*args, **kwargs))
        except BaseException as exc:  # noqa: BLE001 — mirrors executor semantics
            future.set_exception(exc)
        return future


class TestSubmitInThread:
    def test_submits_to_the_sdk_pool_with_args_propagated(self) -> None:
        """Detached dispatch goes to the SDK-owned pool, not the default executor."""
        from application_sdk._runtime import offload as hb_mod

        executor = InlineExecutor()
        calls: list[tuple[int, int]] = []

        with patch.object(hb_mod, "_BLOCKING_EXECUTOR", executor):
            future = hb_mod.submit_in_thread(lambda a, b: calls.append((a, b)), 1, b=2)

        assert calls == [(1, 2)]
        assert future.done() and future.exception() is None
        assert len(executor.submissions) == 1

    def test_a_failure_is_reported_rather_than_swallowed(self) -> None:
        """Nothing awaits the future, so the failure has to surface in the log."""
        from application_sdk._runtime import offload as hb_mod

        def boom() -> None:
            raise OSError("handle already gone")

        with patch.object(hb_mod, "_BLOCKING_EXECUTOR", InlineExecutor()):
            with patch.object(hb_mod.logger, "warning") as warning:
                future = hb_mod.submit_in_thread(boom)

        assert isinstance(future.exception(), OSError)
        warning.assert_called_once()
        assert "handle already gone" in str(warning.call_args)

    def test_submission_is_detached_the_callable_does_not_run_inline(self) -> None:
        """The contract InlineExecutor cannot see: submit returns *before* running.

        ``InlineExecutor`` runs the callable synchronously inside ``submit``, so
        a regression that ran cleanup inline would still pass every other test
        here. A pool that holds the callable pending makes the detached contract
        observable: submission returns with the callable not yet run, and the
        failure-callback path fires when the recorded callable is later driven.
        """
        from application_sdk._runtime import offload as hb_mod

        submitted: list[Callable[[], Any]] = []

        class PendingExecutor:
            """A pool that records the callable and returns an unfinished future."""

            def submit(
                self, fn: Callable[..., Any], *args: Any, **kwargs: Any
            ) -> concurrent.futures.Future[Any]:
                submitted.append(functools.partial(fn, *args, **kwargs))
                return concurrent.futures.Future()

        calls: list[str] = []
        with patch.object(hb_mod, "_BLOCKING_EXECUTOR", PendingExecutor()):
            future = hb_mod.submit_in_thread(lambda: calls.append("ran"))

        assert calls == [], "submit_in_thread ran the cleanup inline"
        assert submitted and not future.done()

        # Drive the recorded callable's future to failure; the done-callback logs it.
        with patch.object(hb_mod.logger, "warning") as warning:
            future.set_exception(OSError("drive failure"))
        warning.assert_called_once()
        assert "drive failure" in str(warning.call_args)

    def test_a_shut_down_pool_does_not_raise_at_the_unwinding_caller(self) -> None:
        """Callers submit from a ``finally``; interpreter exit must not raise there."""
        from application_sdk._runtime import offload as hb_mod

        shut_down = MagicMock()
        shut_down.submit.side_effect = RuntimeError(
            "cannot schedule new futures after shutdown"
        )

        with patch.object(hb_mod, "_BLOCKING_EXECUTOR", shut_down):
            with patch.object(hb_mod.logger, "warning") as warning:
                future = hb_mod.submit_in_thread(lambda: None)

        assert future.cancelled(), "a skipped cleanup call must report as cancelled"
        warning.assert_not_called()

    def test_a_cancelled_submission_is_not_reported_as_a_failure(self) -> None:
        """A future cancelled before it ran never failed, so it must stay quiet."""
        from application_sdk._runtime import offload as hb_mod

        cancelled: concurrent.futures.Future[Any] = concurrent.futures.Future()
        cancelled.cancel()

        with patch.object(hb_mod.logger, "warning") as warning:
            hb_mod._report_detached_failure(cancelled)

        warning.assert_not_called()
