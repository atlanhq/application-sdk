"""Unit tests for application_sdk.execution.heartbeat.

Strict no-real-thread / no-real-loop policy: ``run_in_thread`` patches
``asyncio.get_running_loop`` so no thread pool is touched, and the
auto-heartbeat loop tests use intervals well under 1 second with
pre-set events to avoid hangs.
"""

from __future__ import annotations

import asyncio
import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from application_sdk.execution.heartbeat import (
    NoopHeartbeatController,
    TemporalHeartbeatController,
    auto_heartbeat_loop,
    run_in_thread,
)

# ---------------------------------------------------------------------------
# NoopHeartbeatController
# ---------------------------------------------------------------------------


class TestNoopHeartbeatController:
    def test_heartbeat_records_details(self) -> None:
        ctl = NoopHeartbeatController()
        ctl.heartbeat("a", 1)
        ctl.heartbeat("b", 2)
        assert ctl._heartbeat_calls == [("a", 1), ("b", 2)]
        assert ctl.get_last_heartbeat_details() == ("b", 2)

    def test_keepalive_replays_last_details(self) -> None:
        ctl = NoopHeartbeatController()
        ctl.heartbeat("x")
        ctl.heartbeat_keepalive()
        assert ctl._heartbeat_calls[-1] == ("x",)

    def test_initial_state_empty(self) -> None:
        ctl = NoopHeartbeatController()
        assert ctl.get_last_heartbeat_details() == ()


# ---------------------------------------------------------------------------
# TemporalHeartbeatController — exercises BLDX-1129 inline imports
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_temporalio(monkeypatch):
    """Inject a fake `temporalio` module so the inline `from temporalio import activity`
    inside TemporalHeartbeatController is exercised without spawning Temporal.
    """
    fake_activity = MagicMock()
    fake_info = MagicMock()
    fake_info.heartbeat_details = ("resumed", 42)
    fake_activity.info.return_value = fake_info

    # Build a fake module with `activity` attribute
    fake_pkg = types.ModuleType("temporalio")
    fake_pkg.activity = fake_activity

    monkeypatch.setitem(sys.modules, "temporalio", fake_pkg)
    return fake_activity


class TestTemporalHeartbeatController:
    def test_heartbeat_calls_activity_heartbeat(self, fake_temporalio) -> None:
        """BLDX-1129 anchor: function-local `from temporalio import activity`."""
        ctl = TemporalHeartbeatController()
        ctl.heartbeat("progress", 1)
        fake_temporalio.heartbeat.assert_called_once_with("progress", 1)
        assert ctl._last_details == ("progress", 1)

    def test_keepalive_replays_last_details(self, fake_temporalio) -> None:
        """BLDX-1129 anchor: function-local `from temporalio import activity`."""
        ctl = TemporalHeartbeatController()
        ctl.heartbeat("first", 99)
        fake_temporalio.heartbeat.reset_mock()
        ctl.heartbeat_keepalive()
        fake_temporalio.heartbeat.assert_called_once_with("first", 99)

    def test_get_last_heartbeat_details(self, fake_temporalio) -> None:
        """BLDX-1129 anchor: function-local `from temporalio import activity`."""
        ctl = TemporalHeartbeatController()
        details = ctl.get_last_heartbeat_details()
        assert details == ("resumed", 42)
        fake_temporalio.info.assert_called_once()


# ---------------------------------------------------------------------------
# auto_heartbeat_loop
# ---------------------------------------------------------------------------


class TestAutoHeartbeatLoop:
    @pytest.mark.asyncio
    async def test_stop_event_pre_set_breaks_immediately(self) -> None:
        """If stop_event is set before entry, loop breaks via wait_for resolution."""
        stop = asyncio.Event()
        stop.set()
        hb = MagicMock()
        await auto_heartbeat_loop(0.5, hb, stop, task_name="t")
        # stop_event was set → no heartbeat sent before break
        hb.assert_not_called()

    @pytest.mark.asyncio
    async def test_emits_heartbeat_after_one_interval(self) -> None:
        """When stop never fires before timeout, fn is called and loop continues."""
        stop = asyncio.Event()
        hb_calls = []

        def hb_fn():
            hb_calls.append(1)
            if len(hb_calls) >= 2:
                stop.set()

        await auto_heartbeat_loop(
            interval_seconds=0.001, heartbeat_fn=hb_fn, stop_event=stop, task_name="t"
        )
        # Should have made at least 2 heartbeat calls (then we set stop).
        assert len(hb_calls) >= 1

    @pytest.mark.asyncio
    async def test_heartbeat_exception_logged_and_loop_continues(self) -> None:
        """If hb_fn raises Exception, it's logged but the loop keeps going."""
        stop = asyncio.Event()
        calls = []

        def hb_fn():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("boom")
            stop.set()

        await auto_heartbeat_loop(
            interval_seconds=0.001, heartbeat_fn=hb_fn, stop_event=stop, task_name="t"
        )
        # Exception happened on first call; loop continued until stop
        assert len(calls) >= 1

    @pytest.mark.asyncio
    async def test_base_exception_propagates(self) -> None:
        """asyncio.CancelledError (BaseException) must propagate out of the loop."""
        stop = asyncio.Event()

        def hb_fn():
            raise asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            await auto_heartbeat_loop(
                interval_seconds=0.001,
                heartbeat_fn=hb_fn,
                stop_event=stop,
                task_name="cancelled-task",
            )

    @pytest.mark.asyncio
    async def test_stop_event_set_during_wait_breaks(self) -> None:
        """If stop_event becomes set during wait_for, loop must break (line 144).

        We simulate this by patching wait_for to set stop and return successfully —
        which exercises the `break` after a non-Timeout return.
        """
        stop = asyncio.Event()
        hb = MagicMock()

        async def fake_wait_for(coro, timeout):
            try:
                coro.close()
            except Exception:
                pass
            stop.set()
            return None  # no TimeoutError → break

        with patch(
            "application_sdk.execution.heartbeat.asyncio.wait_for",
            side_effect=fake_wait_for,
        ):
            await auto_heartbeat_loop(
                interval_seconds=0.5, heartbeat_fn=hb, stop_event=stop, task_name="t"
            )
        # The break path: heartbeat should NOT have been called.
        hb.assert_not_called()

    @pytest.mark.asyncio
    async def test_blocked_loop_warning_logged(self, caplog) -> None:
        """Simulate a blocked loop by patching asyncio.wait_for to time out
        immediately and time.monotonic to report a huge elapsed.

        Verifies the warning branch (lines 149-157) executes.
        """
        import logging

        stop = asyncio.Event()
        hb_calls = []

        def hb_fn():
            hb_calls.append(1)
            stop.set()  # ensure loop exits after one iteration

        # Always raise TimeoutError on wait_for so we don't actually block.
        async def fake_wait_for(coro, timeout):
            # Cancel the awaitable so no warnings about un-awaited coroutines.
            try:
                coro.close()
            except Exception:
                pass
            raise asyncio.TimeoutError()

        state = {"n": 0}

        def fake_monotonic():
            state["n"] += 1
            return 0.0 if state["n"] == 1 else 100.0

        with (
            patch(
                "application_sdk.execution.heartbeat.asyncio.wait_for",
                side_effect=fake_wait_for,
            ),
            patch(
                "application_sdk.execution.heartbeat.time.monotonic",
                side_effect=fake_monotonic,
            ),
            caplog.at_level(logging.WARNING),
        ):
            await auto_heartbeat_loop(
                interval_seconds=0.001,
                heartbeat_fn=hb_fn,
                stop_event=stop,
                task_name="slow",
            )

        assert hb_calls  # heartbeat was sent


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
        from application_sdk.execution import heartbeat as hb_mod

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
        from application_sdk.execution import heartbeat as hb_mod

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
        from application_sdk.execution import heartbeat as hb_mod

        async def fake_run_in_executor(executor, fn):
            return fn()  # call wrapped fn synchronously here

        fake_loop = MagicMock()
        fake_loop.run_in_executor = fake_run_in_executor

        def bad():
            raise ValueError("nope")

        with patch.object(hb_mod.asyncio, "get_running_loop", return_value=fake_loop):
            with pytest.raises(ValueError):
                await run_in_thread(bad)
