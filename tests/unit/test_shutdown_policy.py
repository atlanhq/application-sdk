"""Unit tests for the shutdown policy (``application_sdk._shutdown_policy``): the
post-drain exit deadline, the shutdown-budget log, and the event-loop stall bound.

Moved from ``tests/unit/test_main.py`` with the code (FND-4689)."""

from __future__ import annotations

import asyncio
import threading
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from application_sdk._loop_stall_watchdog import timer_seconds
from application_sdk._shutdown_policy import (
    _log_exit_deadline_reached,
    _log_shutdown_budget,
    _loop_stall_bound,
    _LoopStallBound,
    _PostDrainExitArm,
    _start_post_drain_exit_deadline,
)
from application_sdk.app.registry import TaskRegistry
from application_sdk.app.task import TaskMetadata
from application_sdk.contracts.base import Input, Output
from tests.unit.private_stdlib import private_stdlib


class TestPostDrainExitDeadline:
    """The bound on shutdown steps that run after the worker has drained."""

    def test_expiry_exits_with_distinct_nonzero_code(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATLAN_POST_DRAIN_EXIT_TIMEOUT_SECONDS", "1")
        exited = threading.Event()
        codes: list[int] = []

        def exit_fn(code: int) -> None:
            codes.append(code)
            exited.set()

        timer = _start_post_drain_exit_deadline(exit_fn=exit_fn)

        assert timer is not None and timer.daemon
        assert exited.wait(timeout=5)
        assert codes == [3]

    def test_expiry_exits_even_when_stderr_is_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATLAN_POST_DRAIN_EXIT_TIMEOUT_SECONDS", "1")
        exited = threading.Event()
        codes: list[int] = []

        def exit_fn(code: int) -> None:
            codes.append(code)
            exited.set()

        def closed_stderr(fd: int, data: bytes) -> int:
            raise OSError(9, "Bad file descriptor")

        with private_stdlib("application_sdk._shutdown_policy", "os") as local_os:
            monkeypatch.setattr(local_os, "write", closed_stderr)
            _start_post_drain_exit_deadline(exit_fn=exit_fn)

            assert exited.wait(timeout=5)
        assert codes == [3]

    def test_non_positive_timeout_disables(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATLAN_POST_DRAIN_EXIT_TIMEOUT_SECONDS", "0")
        assert _start_post_drain_exit_deadline(exit_fn=MagicMock()) is None

    def test_default_bound_is_two_minutes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ATLAN_POST_DRAIN_EXIT_TIMEOUT_SECONDS", raising=False)
        timer = _start_post_drain_exit_deadline(exit_fn=MagicMock())
        try:
            assert timer is not None and timer.interval == 120
        finally:
            assert timer is not None
            timer.cancel()

    def test_bound_derives_from_declared_grace(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A 30 s grace cannot afford the fixed 120 s post-drain bound."""
        monkeypatch.delenv("ATLAN_POST_DRAIN_EXIT_TIMEOUT_SECONDS", raising=False)
        monkeypatch.setenv("ATLAN_SHUTDOWN_GRACE_SECONDS", "30")
        timer = _start_post_drain_exit_deadline(exit_fn=MagicMock())
        try:
            assert timer is not None and timer.interval == 8
        finally:
            assert timer is not None
            timer.cancel()

    def test_explicit_bound_beats_declared_grace(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATLAN_POST_DRAIN_EXIT_TIMEOUT_SECONDS", "0")
        monkeypatch.setenv("ATLAN_SHUTDOWN_GRACE_SECONDS", "30")
        assert _start_post_drain_exit_deadline(exit_fn=MagicMock()) is None


class TestPostDrainExitArm:
    """Arms the post-drain deadline when the drain ends, once, on shutdown only."""

    def test_drained_on_shutdown_arms_once(self) -> None:
        event = asyncio.Event()
        event.set()
        arm = _PostDrainExitArm(enabled=True, shutdown_event=event)
        with patch(
            "application_sdk._shutdown_policy._start_post_drain_exit_deadline"
        ) as start:
            arm.on_worker_drained()
            arm.on_worker_drained()
            arm.start()
        start.assert_called_once_with()

    def test_drained_without_shutdown_signal_does_not_arm(self) -> None:
        """A supervised restart drains the worker too; it must not end the process."""
        arm = _PostDrainExitArm(enabled=True, shutdown_event=asyncio.Event())
        with patch(
            "application_sdk._shutdown_policy._start_post_drain_exit_deadline"
        ) as start:
            arm.on_worker_drained()
        start.assert_not_called()

    def test_disabled_never_arms(self) -> None:
        event = asyncio.Event()
        event.set()
        arm = _PostDrainExitArm(enabled=False, shutdown_event=event)
        with patch(
            "application_sdk._shutdown_policy._start_post_drain_exit_deadline"
        ) as start:
            arm.on_worker_drained()
            arm.start()
        start.assert_not_called()


class TestLogShutdownBudget:
    """The effective shutdown bounds are visible at startup."""

    def test_warns_when_explicit_bounds_overrun_grace(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATLAN_SHUTDOWN_GRACE_SECONDS", "30")
        monkeypatch.setenv("TEMPORAL_GRACEFUL_SHUTDOWN_TIMEOUT", "3600")
        monkeypatch.delenv("ATLAN_POST_DRAIN_EXIT_TIMEOUT_SECONDS", raising=False)
        with patch("application_sdk._shutdown_policy.logger") as log:
            _log_shutdown_budget()
        log.info.assert_called_once()
        log.warning.assert_called_once()
        assert "overrun" in log.warning.call_args.args[0]

    def test_overrun_counts_the_daprd_headroom(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """5 s delay + 18 s drain + 8 s post-drain overruns a 30 s grace even
        before the 2 s kept back for daprd's stop is counted."""
        monkeypatch.setenv("ATLAN_SHUTDOWN_GRACE_SECONDS", "30")
        monkeypatch.setenv("TEMPORAL_GRACEFUL_SHUTDOWN_TIMEOUT", "18")
        monkeypatch.delenv("ATLAN_POST_DRAIN_EXIT_TIMEOUT_SECONDS", raising=False)
        with patch("application_sdk._shutdown_policy.logger") as log:
            _log_shutdown_budget()
        log.warning.assert_called_once()
        assert 33 in log.warning.call_args.args

    def test_derived_bounds_fit_the_grace(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATLAN_SHUTDOWN_GRACE_SECONDS", "30")
        monkeypatch.delenv("TEMPORAL_GRACEFUL_SHUTDOWN_TIMEOUT", raising=False)
        monkeypatch.delenv("ATLAN_POST_DRAIN_EXIT_TIMEOUT_SECONDS", raising=False)
        with patch("application_sdk._shutdown_policy.logger") as log:
            _log_shutdown_budget()
        log.warning.assert_not_called()

    def test_undeclared_grace_logs_without_warning(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ATLAN_SHUTDOWN_GRACE_SECONDS", raising=False)
        with patch("application_sdk._shutdown_policy.logger") as log:
            _log_shutdown_budget()
        assert "undeclared" in log.info.call_args.args
        log.warning.assert_not_called()


def test_exit_deadline_log_says_the_dump_is_not_a_stall() -> None:
    with patch("application_sdk._shutdown_policy.logger") as log:
        _log_exit_deadline_reached(7.5)
    log.warning.assert_called_once()
    message, seconds = log.warning.call_args.args
    assert seconds == 7.5
    assert "not a stalled loop" in message


class TestLoopStallBound:
    """The stall bound: past the longest heartbeat timeout, within the budget."""

    @pytest.fixture(autouse=True)
    def _no_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATLAN_WORKER_LOOP_STALL_TIMEOUT_SECONDS", raising=False)
        monkeypatch.delenv("ATLAN_SHUTDOWN_GRACE_SECONDS", raising=False)

    @staticmethod
    def _register(
        name: str,
        *,
        heartbeat: int | None,
        timeout: int,
        module: str = "app.activities",
    ) -> None:
        def func(*_: Any) -> None:
            return None

        func.__module__ = module
        TaskRegistry.get_instance().register(
            "stall-app",
            TaskMetadata(
                name=name,
                func=func,
                input_type=Input,
                output_type=Output,
                heartbeat_timeout_seconds=heartbeat,
                timeout_seconds=timeout,
            ),
        )

    def test_default_tasks_give_a_bound_past_the_default_heartbeat(self) -> None:
        self._register("quick", heartbeat=30, timeout=600)

        bound = _loop_stall_bound()

        assert (bound.seconds, bound.longest_heartbeat_seconds) == (75, 60)
        assert bound.heartbeat_task is None
        assert bound.limit_seconds is None
        assert bound.exit_by_seconds is None
        assert bound.unwatched_tasks == ()
        assert bound.in_order()

    def test_tasks_without_heartbeat_do_not_count_and_are_named(self) -> None:
        """A worker shaped like a connector's: 120 s heartbeats plus the SDK's
        own 300 s cleanup task and an app task with no heartbeat timeout and
        the 86 400 s start-to-close default. Counting those would push the
        bound past a day and switch the watchdog off in practice."""
        self._register("extract", heartbeat=120, timeout=7200)
        self._register("transform", heartbeat=120, timeout=7200)
        self._register(
            "cleanup_storage",
            heartbeat=None,
            timeout=300,
            module="application_sdk.app.base",
        )
        self._register("publish", heartbeat=None, timeout=86400)
        # Finishes within the bound even if it holds the loop, so not named.
        self._register("tidy", heartbeat=None, timeout=60)

        bound = _loop_stall_bound()

        assert bound.seconds == 135
        assert bound.heartbeat_task == "stall-app.extract"
        assert timer_seconds(bound.seconds) == 150
        assert bound.unwatched_tasks == ("stall-app.publish (86400s)",)
        assert bound.in_order()

    def test_unwatched_tasks_are_named_in_a_warning(self) -> None:
        bound = _LoopStallBound(
            135,
            120,
            None,
            overridden=False,
            unwatched_tasks=("stall-app.publish (86400s)",),
        )
        with patch("application_sdk._shutdown_policy.logger") as log:
            _log_shutdown_budget(bound)

        log.warning.assert_called_once()
        message = log.warning.call_args.args[0] % log.warning.call_args.args[1:]
        assert "stall-app.publish (86400s)" in message
        assert "heartbeat timeout" in message

    def test_long_grace_keeps_the_task_derived_bound(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATLAN_SHUTDOWN_GRACE_SECONDS", "86400")
        self._register("long", heartbeat=3600, timeout=7200)

        bound = _loop_stall_bound()

        assert bound.seconds == 3615
        assert bound.in_order()

    def test_short_grace_still_fires_before_sigkill(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A 30 s grace keeps 2 s for daprd: the timer, bound plus one re-arm
        interval, must run out within 28 s of the signal."""
        monkeypatch.setenv("ATLAN_SHUTDOWN_GRACE_SECONDS", "30")

        bound = _loop_stall_bound()

        assert bound.limit_seconds == pytest.approx(22.4)
        assert bound.seconds == pytest.approx(22.4)
        assert timer_seconds(bound.seconds) == pytest.approx(28)
        assert bound.exit_by_seconds == 28
        assert bound.heartbeat_task is None
        assert not bound.in_order()

    def test_override_replaces_the_derived_bound(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATLAN_WORKER_LOOP_STALL_TIMEOUT_SECONDS", "0")
        assert _loop_stall_bound().seconds == 0
        assert _loop_stall_bound().in_order()

        monkeypatch.setenv("ATLAN_WORKER_LOOP_STALL_TIMEOUT_SECONDS", "90")
        bound = _loop_stall_bound()
        assert (bound.seconds, bound.overridden) == (90, True)

    def test_budget_line_names_the_bound(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with patch("application_sdk._shutdown_policy.logger") as log:
            _log_shutdown_budget(_LoopStallBound(75, 60, None, overridden=False))
        assert "75s" in log.info.call_args.args
        log.warning.assert_not_called()

    @pytest.mark.parametrize(
        ("bound", "advice"),
        [
            # An override at or below the longest heartbeat timeout.
            (_LoopStallBound(30, 60, None, overridden=True), "Unset"),
            # An override past what still fires before SIGKILL.
            (_LoopStallBound(500, 60, 400, overridden=True), "Unset"),
            # A derived bound the grace cut below a task's own heartbeat timeout.
            (
                _LoopStallBound(
                    135,
                    300,
                    135,
                    overridden=False,
                    heartbeat_task="stall-app.extract",
                ),
                "lower the heartbeat timeout of stall-app.extract below 135s",
            ),
            # Cut below the default too: lowering the task alone cannot fix it.
            (
                _LoopStallBound(
                    46.4,
                    120,
                    46.4,
                    overridden=False,
                    heartbeat_task="stall-app.extract",
                ),
                "does not restore the order",
            ),
        ],
    )
    def test_out_of_order_bound_warns(
        self, bound: _LoopStallBound, advice: str
    ) -> None:
        with patch("application_sdk._shutdown_policy.logger") as log:
            _log_shutdown_budget(bound)
        log.warning.assert_called_once()
        message = log.warning.call_args.args[0] % log.warning.call_args.args[1:]
        assert "out of order" in message
        assert advice in message

    def test_short_grace_below_the_default_heartbeat_logs_info(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only the SDK's default heartbeat timeout outlasts a 30 s grace: no
        app change can fix that, so it is not a WARNING on every start."""
        monkeypatch.setenv("ATLAN_SHUTDOWN_GRACE_SECONDS", "30")
        bound = _loop_stall_bound()
        assert not bound.in_order()

        with patch("application_sdk._shutdown_policy.logger") as log:
            _log_shutdown_budget(bound)

        log.warning.assert_not_called()
        messages = [c.args[0] % c.args[1:] for c in log.info.call_args_list]
        assert any(
            "below the default heartbeat timeout (60s, ATLAN_HEARTBEAT_TIMEOUT_SECONDS)"
            in m
            for m in messages
        )
