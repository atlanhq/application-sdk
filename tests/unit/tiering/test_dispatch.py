"""Routing goes through the SDK's own dispatch path, never around it."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from application_sdk.app.base import _create_task_activity_wrapper
from application_sdk.contracts.base import Output
from application_sdk.tiering.dispatch import run_on_queue


class SimpleOutput(Output):
    result: str = ""


#: The arguments every task wrapper needs; tests override only what they vary.
_TASK: dict = {
    "app_name": "demo-app",
    "task_name": "transform",
    "timeout_seconds": 600,
    "retry_max_attempts": 3,
    "retry_max_interval_seconds": 30,
    "output_type": SimpleOutput,
    "context_data": {"run_id": "r1", "correlation_id": "c1"},
}


class TestRunOnQueue:
    def test_no_queue_takes_the_ordinary_bound_method_path(self) -> None:
        """Unrouted dispatch must be indistinguishable from a build without tiering.

        No extra kwarg means the call is byte-identical in workflow history,
        which is what makes an unset queue map a true no-op.
        """
        task = AsyncMock(return_value="done")
        app = MagicMock(transform=task)

        assert asyncio.run(run_on_queue(app, "transform", "arg", None)) == "done"
        task.assert_awaited_once_with("arg")

    def test_a_queue_is_threaded_to_the_task(self) -> None:
        task = AsyncMock(return_value="done")
        app = MagicMock(transform=task)

        assert asyncio.run(run_on_queue(app, "transform", "arg", "app-l")) == "done"
        task.assert_awaited_once_with("arg", task_queue="app-l")


class TestWrapperQueueOverride:
    """The per-call override on the SDK's one dispatch path."""

    def _dispatch(
        self,
        task: dict | None = None,
        *,
        input_data: object = None,
        **call_kwargs: object,
    ) -> MagicMock:
        """Build a wrapper and dispatch through it, capturing the activity call.

        The wrapper is created *inside* the patch on purpose: the factory binds
        ``execute_activity_with_eviction_retry`` into its closure at creation
        time, so a wrapper built earlier would hold the real function.
        """
        with patch(
            "application_sdk.execution._temporal.eviction_retry.execute_activity_with_eviction_retry",
            new_callable=AsyncMock,
        ) as mock_exec:
            mock_exec.return_value = MagicMock()
            wrapper = _create_task_activity_wrapper(**{**_TASK, **(task or {})})
            asyncio.run(
                wrapper(
                    MagicMock() if input_data is None else input_data, **call_kwargs
                )
            )
        return mock_exec

    def test_an_explicit_queue_is_dispatched_on(self) -> None:
        exec_ = self._dispatch(task_queue="app-xl")
        assert exec_.call_args.kwargs["task_queue"] == "app-xl"

    def test_no_queue_and_no_pool_sends_no_kwarg(self) -> None:
        """Regression guard: the unrouted call shape must not change."""
        exec_ = self._dispatch()
        assert "task_queue" not in exec_.call_args.kwargs

    def test_a_run_tier_beats_the_decorator_pool(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A size tier is a property of the run; ``pool=`` is fixed at decoration."""
        monkeypatch.setenv("ATLAN_TASK_QUEUE", "demo-app-queue")
        exec_ = self._dispatch({"pool": "heavy"}, task_queue="app-xl")
        assert exec_.call_args.kwargs["task_queue"] == "app-xl"

    def test_the_declared_pool_still_applies_when_nothing_is_routed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATLAN_TASK_QUEUE", "demo-app-queue")
        exec_ = self._dispatch({"pool": "heavy"})
        assert exec_.call_args.kwargs["task_queue"] == "demo-app-queue-heavy"

    def test_an_explicit_none_is_not_a_queue(self) -> None:
        exec_ = self._dispatch(task_queue=None)
        assert "task_queue" not in exec_.call_args.kwargs

    def test_routing_changes_the_queue_and_nothing_else(self) -> None:
        """Every other execution parameter is identical routed vs unrouted.

        This is the property that made moving dispatch into the SDK worth it: an
        app-side re-implementation drifts from the wrapper, and the parameters it
        drops — the retry policy's shape, the schedule-to-close ceiling, the
        progress watchdog, the run age the SLA alert reads — then apply to
        unrouted activities but not to routed ones.
        """
        # One input object for both, so `args` and `summary` compare equal and
        # only a genuine parameter difference can show up below.
        payload = MagicMock()
        plain = self._dispatch({"schedule_to_close_seconds": 3600}, input_data=payload)
        routed = self._dispatch(
            {"schedule_to_close_seconds": 3600}, input_data=payload, task_queue="app-l"
        )

        assert routed.call_args.args == plain.call_args.args
        differing = {
            key
            for key in set(plain.call_args.kwargs) | set(routed.call_args.kwargs)
            if plain.call_args.kwargs.get(key) != routed.call_args.kwargs.get(key)
        }
        assert differing == {"task_queue"}

    def test_the_task_context_is_unchanged_by_routing(self) -> None:
        """The activity receives the same TaskContext either way."""
        plain = self._dispatch()
        routed = self._dispatch(task_queue="app-l")

        assert routed.call_args.kwargs["args"][0] == plain.call_args.kwargs["args"][0]
