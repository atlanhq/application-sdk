"""Routing goes through the SDK's own dispatch path, never around it."""

from __future__ import annotations

import asyncio
import inspect
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from application_sdk.app.base import _create_task_activity_wrapper
from application_sdk.app.task import task
from application_sdk.contracts.base import Input, Output
from application_sdk.tiering.dispatch import _accepts_task_queue, run_on_queue


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

    def test_an_empty_queue_is_treated_as_no_queue(self) -> None:
        """`resolve` answers None, but an app computing a queue itself can land on "".

        Both layers must agree, or an empty string dispatches a kwarg the
        wrapper then has to discard.
        """
        task = AsyncMock(return_value="done")
        app = MagicMock(transform=task)

        asyncio.run(run_on_queue(app, "transform", "arg", ""))
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


class RealInput(Input):
    value: str = ""


class RealOutput(Output):
    result: str = ""


class RealApp:
    """An app whose task is the raw decorated method — never wrapped for a run."""

    @task
    async def transform(self, input: RealInput) -> RealOutput:
        return RealOutput(result=input.value)


class TestDispatchOutsideAWorkflow:
    """Routing is meaningless where the body runs in process; it must not raise."""

    def test_an_unwrapped_task_ignores_the_queue_instead_of_raising(self) -> None:
        """Without this, local runs of an app that adopted tiering die on a TypeError.

        The raw decorated method is ``async def transform(self, input)``; handing
        it ``task_queue=`` raises `got an unexpected keyword argument`. There is
        no Temporal activity here and so no queue to choose.
        """
        out = asyncio.run(
            run_on_queue(RealApp(), "transform", RealInput(value="v"), "app-l")
        )
        assert out.result == "v"

    def test_it_is_the_same_result_as_no_routing(self) -> None:
        plain = asyncio.run(
            run_on_queue(RealApp(), "transform", RealInput(value="v"), None)
        )
        routed = asyncio.run(
            run_on_queue(RealApp(), "transform", RealInput(value="v"), "app-l")
        )
        assert plain.result == routed.result


class TestAcceptsTaskQueue:
    """The detection itself, including the shapes that fooled the first attempt."""

    def test_the_raw_decorated_method_does_not_accept_it(self) -> None:
        assert _accepts_task_queue(RealApp().transform) is False

    def test_a_kwargs_callable_accepts_it(self) -> None:
        async def anything(arg: object, **kwargs: object) -> None: ...

        assert _accepts_task_queue(anything) is True

    def test_a_mock_accepts_it(self) -> None:
        """A Mock manufactures attributes, so an `hasattr` marker test answers wrongly.

        Signature inspection gets it right: `AsyncMock` presents `(*args, **kwargs)`.
        """
        assert _accepts_task_queue(AsyncMock()) is True

    @pytest.mark.parametrize("raised", [TypeError, ValueError])
    def test_an_uninspectable_callable_is_assumed_to_accept_it(
        self, raised: type[Exception]
    ) -> None:
        """A failed dispatch is visible; silently not routing is not.

        Some builtins and C callables have no retrievable signature, and
        `inspect.signature` reports that as TypeError or ValueError.
        """

        async def anything(arg: object) -> None: ...

        with patch("inspect.signature", side_effect=raised("no signature")):
            assert _accepts_task_queue(anything) is True

    def test_it_agrees_with_the_sdk_wrapper(self) -> None:
        """The pin: if the SDK renames or drops the kwarg, this fails here.

        `run_on_queue` is only correct while the wrapper the SDK binds for a run
        actually takes `task_queue`. Asserting it against the real factory means
        a rename cannot quietly turn every routed dispatch into an unrouted one.
        """
        wrapper = _create_task_activity_wrapper(**_TASK)
        assert _accepts_task_queue(wrapper) is True
        assert "task_queue" in inspect.signature(wrapper).parameters

    def test_the_wrapper_takes_the_queue_by_keyword_only(self) -> None:
        """Positionally it would collide with a future parameter."""
        kind = (
            inspect.signature(_create_task_activity_wrapper(**_TASK))
            .parameters["task_queue"]
            .kind
        )
        assert kind is inspect.Parameter.KEYWORD_ONLY
