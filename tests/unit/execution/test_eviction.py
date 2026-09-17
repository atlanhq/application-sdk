"""Worker-eviction path: SIGTERM mid-activity → ApplicationError(type="WorkerEvicted") →
workflow-side eviction retry loop without burning the application-error
retry budget.

Covers:
- ``application_sdk.execution.shutdown`` flag get/set/reset
- Activity wrapper converts ``asyncio.CancelledError`` to
  ``ApplicationError(type="WorkerEvicted")`` only when the shutdown flag is set
- ``execute_activity_with_eviction_retry`` increments its own counter on
  eviction and propagates non-eviction failures unchanged
- Retry policy auto-adds ``"WorkerEvicted"`` to ``non_retryable_error_types``
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest import mock

import pytest
from pydantic import Field

from application_sdk.app.base import App
from application_sdk.app.registry import AppRegistry, TaskRegistry
from application_sdk.app.task import task
from application_sdk.contracts.base import Input, Output
from application_sdk.errors.leaves import WORKER_EVICTED_TYPE
from application_sdk.execution import heartbeat as heartbeat_module
from application_sdk.execution import shutdown as shutdown_module
from application_sdk.execution._temporal import activities as activities_module
from application_sdk.execution._temporal.activities import (
    TaskContext,
    create_activity_from_task,
    get_activity_options,
)
from application_sdk.execution._temporal.eviction_retry import (
    _is_worker_evicted,
    execute_activity_with_eviction_retry,
)
from application_sdk.execution.errors import ApplicationError
from application_sdk.execution.retry import RetryPolicy, _to_temporal_retry_policy


class _EvIn(Input, allow_unbounded_fields=True):
    name: str = "x"


class _EvOut(Output, allow_unbounded_fields=True):
    msg: str = ""


# ---------------------------------------------------------------------------
# shutdown flag
# ---------------------------------------------------------------------------


class TestShutdownFlag:
    def setup_method(self) -> None:
        shutdown_module.reset_worker_shutting_down()

    def teardown_method(self) -> None:
        shutdown_module.reset_worker_shutting_down()

    def test_default_is_false(self) -> None:
        assert shutdown_module.is_worker_shutting_down() is False

    def test_mark_flips_to_true(self) -> None:
        shutdown_module.mark_worker_shutting_down()
        assert shutdown_module.is_worker_shutting_down() is True

    def test_mark_is_idempotent(self) -> None:
        shutdown_module.mark_worker_shutting_down()
        shutdown_module.mark_worker_shutting_down()
        assert shutdown_module.is_worker_shutting_down() is True

    def test_reset_flips_back_to_false(self) -> None:
        shutdown_module.mark_worker_shutting_down()
        shutdown_module.reset_worker_shutting_down()
        assert shutdown_module.is_worker_shutting_down() is False


# ---------------------------------------------------------------------------
# activity wrapper: CancelledError attribution
# ---------------------------------------------------------------------------


class TestActivityCancelledAttribution:
    """When the worker is shutting down, an ``asyncio.CancelledError`` raised
    inside the activity body must be re-raised as
    ``ApplicationError(type="WorkerEvicted", non_retryable=True)`` so the
    workflow-side eviction retry loop can recognise and re-dispatch it.
    """

    def setup_method(self) -> None:
        AppRegistry.reset()
        TaskRegistry.reset()
        shutdown_module.reset_worker_shutting_down()

    def teardown_method(self) -> None:
        AppRegistry.reset()
        TaskRegistry.reset()
        shutdown_module.reset_worker_shutting_down()

    def _build_cancel_activity(self) -> object:
        class _CancelApp(App):
            @task(timeout_seconds=60)
            async def boom(self, input: _EvIn) -> _EvOut:
                raise asyncio.CancelledError()

            async def run(self, input: _EvIn) -> _EvOut:
                return await self.boom(input)

        boom_task = next(
            t
            for t in TaskRegistry.get_instance().get_tasks_for_app("_cancel-app")
            if t.name == "boom"
        )
        return create_activity_from_task(boom_task)

    async def test_cancel_with_shutdown_flag_raises_worker_evicted(self) -> None:
        activity_fn = self._build_cancel_activity()
        ctx = TaskContext(
            app_name="_cancel-app",
            task_name="boom",
            run_id="run-1",
            heartbeat_timeout_seconds=None,
            auto_heartbeat_seconds=None,
        )
        shutdown_module.mark_worker_shutting_down()

        with (
            mock.patch.object(
                activities_module.activity,
                "info",
                return_value=mock.MagicMock(workflow_id="wf-cancel"),
            ),
            mock.patch(
                "application_sdk.infrastructure.context.get_infrastructure",
                return_value=None,
            ),
            pytest.raises(ApplicationError) as exc_info,
        ):
            await activity_fn(ctx, _EvIn(name="x"))

        assert exc_info.value.type == WORKER_EVICTED_TYPE
        assert exc_info.value.non_retryable is True

    async def test_cancel_without_shutdown_flag_propagates(self) -> None:
        activity_fn = self._build_cancel_activity()
        ctx = TaskContext(
            app_name="_cancel-app",
            task_name="boom",
            run_id="run-1",
            heartbeat_timeout_seconds=None,
            auto_heartbeat_seconds=None,
        )

        with (
            mock.patch.object(
                activities_module.activity,
                "info",
                return_value=mock.MagicMock(workflow_id="wf-cancel"),
            ),
            mock.patch(
                "application_sdk.infrastructure.context.get_infrastructure",
                return_value=None,
            ),
            pytest.raises(asyncio.CancelledError),
        ):
            await activity_fn(ctx, _EvIn(name="x"))


# ---------------------------------------------------------------------------
# eviction retry helper
# ---------------------------------------------------------------------------


def _make_activity_error_with_app_error_cause(
    app_error_type: str = WORKER_EVICTED_TYPE,
) -> Exception:
    """Build a ``temporalio.exceptions.ActivityError`` whose ``cause`` is an
    ``ApplicationError`` with the given ``type`` attribute, mirroring what
    the workflow sees when the activity wrapper raises
    ``ApplicationError(type=WORKER_EVICTED_TYPE)``.
    """
    from temporalio.exceptions import ActivityError

    cause = ApplicationError("evicted", type=app_error_type, non_retryable=True)
    err = ActivityError(
        "Activity task failed",
        scheduled_event_id=1,
        started_event_id=2,
        identity="test",
        activity_type="dummy",
        activity_id="dummy-1",
        retry_state=None,
    )
    err.__cause__ = cause
    return err


class TestEvictionRetryHelper:
    """Workflow-side eviction loop. Runs ``workflow.execute_activity`` and
    ``workflow.logger`` on the temporal workflow runtime in production; both
    are patched out here so the helper can be exercised as plain async code.
    """

    def _patch_workflow(
        self, exec_side_effects: list[object] | object
    ) -> tuple[mock.AsyncMock, mock._patch, mock._patch]:
        if isinstance(exec_side_effects, list):
            exec_mock = mock.AsyncMock(side_effect=exec_side_effects)
        else:
            exec_mock = mock.AsyncMock(return_value=exec_side_effects)
        exec_patch = mock.patch(
            "application_sdk.execution._temporal.eviction_retry.workflow.execute_activity",
            exec_mock,
        )
        logger_patch = mock.patch(
            "application_sdk.execution._temporal.eviction_retry.workflow.logger",
            mock.MagicMock(),
        )
        return exec_mock, exec_patch, logger_patch

    async def test_returns_result_on_first_success(self) -> None:
        exec_mock, exec_patch, logger_patch = self._patch_workflow("ok")
        with exec_patch, logger_patch:
            result = await execute_activity_with_eviction_retry("act-name")
        assert result == "ok"

    async def test_eviction_retries_then_succeeds(self) -> None:
        evict = _make_activity_error_with_app_error_cause()
        exec_mock, exec_patch, logger_patch = self._patch_workflow([evict, evict, "ok"])
        with exec_patch, logger_patch:
            result = await execute_activity_with_eviction_retry(
                "act-name", max_eviction_retries=3
            )
        assert result == "ok"
        assert exec_mock.await_count == 3

    async def test_eviction_cap_raises_after_max(self) -> None:
        evict = _make_activity_error_with_app_error_cause()
        # 4 evictions, cap = 3 → 4th eviction propagates
        exec_mock, exec_patch, logger_patch = self._patch_workflow(
            [evict, evict, evict, evict]
        )
        with exec_patch, logger_patch, pytest.raises(Exception) as exc_info:
            await execute_activity_with_eviction_retry(
                "act-name", max_eviction_retries=3
            )
        assert _is_worker_evicted(exc_info.value)
        assert exec_mock.await_count == 4

    async def test_non_eviction_failure_propagates_unchanged(self) -> None:
        non_evict = _make_activity_error_with_app_error_cause(
            app_error_type="ValueError"
        )
        exec_mock, exec_patch, logger_patch = self._patch_workflow([non_evict])
        with exec_patch, logger_patch, pytest.raises(Exception) as exc_info:
            await execute_activity_with_eviction_retry("act-name")
        assert not _is_worker_evicted(exc_info.value)
        assert exec_mock.await_count == 1


# ---------------------------------------------------------------------------
# retry policy: WorkerEvicted always non-retryable at Temporal layer
# ---------------------------------------------------------------------------


class TestRetryPolicyWiresWorkerEvicted:
    def test_to_temporal_retry_policy_appends_worker_evicted(self) -> None:
        policy = RetryPolicy(max_attempts=3)
        temporal_policy = _to_temporal_retry_policy(policy)
        assert WORKER_EVICTED_TYPE in (temporal_policy.non_retryable_error_types or [])

    def test_to_temporal_retry_policy_preserves_user_non_retryables(self) -> None:
        policy = RetryPolicy(max_attempts=3, non_retryable_errors=("ValueError",))
        temporal_policy = _to_temporal_retry_policy(policy)
        types = temporal_policy.non_retryable_error_types or []
        assert "ValueError" in types
        assert WORKER_EVICTED_TYPE in types

    def test_to_temporal_retry_policy_no_duplicate_when_user_already_added(
        self,
    ) -> None:
        policy = RetryPolicy(
            max_attempts=3, non_retryable_errors=(WORKER_EVICTED_TYPE,)
        )
        temporal_policy = _to_temporal_retry_policy(policy)
        types = temporal_policy.non_retryable_error_types or []
        assert types.count(WORKER_EVICTED_TYPE) == 1

    def test_get_activity_options_appends_worker_evicted_for_default_policy(
        self,
    ) -> None:
        from application_sdk.app.task import TaskMetadata

        meta = TaskMetadata(
            name="t",
            app_name="_a",
            func=lambda: None,
            input_type=_EvIn,
            output_type=_EvOut,
            timeout_seconds=60,
            retry_max_attempts=3,
            retry_max_interval_seconds=300,
            heartbeat_timeout_seconds=60,
            auto_heartbeat_seconds=10,
            retry_policy=None,
        )
        opts = get_activity_options(meta)
        types = opts["retry_policy"].non_retryable_error_types or []
        assert WORKER_EVICTED_TYPE in types


# ---------------------------------------------------------------------------
# Heartbeat details survive the eviction re-dispatch
# ---------------------------------------------------------------------------
#
# The eviction loop re-dispatches the task as a NEW activity execution, and
# Temporal scopes heartbeat details to an execution — so, without help, the
# documented resume-on-retry pattern (``get_heartbeat_details()``) sees nothing
# after a pod shutdown and the task silently restarts from zero. The activity
# wrapper knows the attempt's last details when it raises ``WorkerEvicted``;
# they ride on that failure, the loop hands them to the re-dispatched
# ``TaskContext``, and the heartbeat controller falls back to them.


class _CarryIn(Input, allow_unbounded_fields=True):
    name: str


class _CarryOut(Output, allow_unbounded_fields=True):
    carried: list[Any] = Field(default_factory=list)


class TestEvictionCarriesHeartbeatDetails:
    def setup_method(self) -> None:
        AppRegistry.reset()
        TaskRegistry.reset()
        shutdown_module.reset_worker_shutting_down()

    def teardown_method(self) -> None:
        AppRegistry.reset()
        TaskRegistry.reset()
        shutdown_module.reset_worker_shutting_down()

    @staticmethod
    def _activity(app_key: str, task_name: str) -> object:
        t = next(
            t
            for t in TaskRegistry.get_instance().get_tasks_for_app(app_key)
            if t.name == task_name
        )
        return create_activity_from_task(t)

    async def test_worker_evicted_failure_carries_last_heartbeat_details(self) -> None:
        class _CarryApp(App):
            @task(timeout_seconds=60)
            async def boom(self, input: _CarryIn) -> _CarryOut:
                self.heartbeat({"position": 7})
                raise asyncio.CancelledError()

            async def run(self, input: _CarryIn) -> _CarryOut:
                return await self.boom(input)

        activity_fn = self._activity("_carry-app", "boom")
        ctx = TaskContext(
            app_name="_carry-app",
            task_name="boom",
            run_id="run-1",
            heartbeat_timeout_seconds=None,
            auto_heartbeat_seconds=None,
        )
        shutdown_module.mark_worker_shutting_down()
        with (
            mock.patch.object(
                activities_module.activity,
                "info",
                return_value=mock.MagicMock(workflow_id="wf-carry"),
            ),
            mock.patch(
                "application_sdk.infrastructure.context.get_infrastructure",
                return_value=None,
            ),
            pytest.raises(ApplicationError) as exc_info,
        ):
            await activity_fn(ctx, _CarryIn(name="x"))
        assert exc_info.value.type == WORKER_EVICTED_TYPE
        assert tuple(exc_info.value.details) == ({"position": 7},)

    async def test_unencodable_details_are_dropped_not_raised(self) -> None:
        # ``ApplicationError.details`` are serialised by the data converter at
        # completion time. temporalio handles a converter failure by discarding
        # the whole failure and substituting a bare ``ApplicationFailureInfo``
        # with NO type — which ``_is_worker_evicted`` does not recognise, so the
        # eviction stops being re-dispatched and burns the task's retry budget
        # instead. Carrying nothing is strictly better than losing the type.
        # Unvalidated on this path in particular: ``heartbeat_timeout_seconds=None``
        # means a NoopHeartbeatController, whose details Temporal never encoded.
        class _Unencodable:
            pass

        class _BadCarryApp(App):
            @task(timeout_seconds=60)
            async def boom(self, input: _CarryIn) -> _CarryOut:
                self.heartbeat(_Unencodable())
                raise asyncio.CancelledError()

            async def run(self, input: _CarryIn) -> _CarryOut:
                return await self.boom(input)

        activity_fn = self._activity("_bad-carry-app", "boom")
        ctx = TaskContext(
            app_name="_bad-carry-app",
            task_name="boom",
            run_id="run-1",
            heartbeat_timeout_seconds=None,
            auto_heartbeat_seconds=None,
        )
        converter = mock.MagicMock()
        converter.to_payloads.side_effect = TypeError("cannot encode _Unencodable")
        shutdown_module.mark_worker_shutting_down()
        with (
            mock.patch.object(
                activities_module.activity,
                "info",
                return_value=mock.MagicMock(workflow_id="wf-bad"),
            ),
            mock.patch.object(
                activities_module.activity,
                "payload_converter",
                return_value=converter,
            ),
            mock.patch(
                "application_sdk.infrastructure.context.get_infrastructure",
                return_value=None,
            ),
            pytest.raises(ApplicationError) as exc_info,
        ):
            await activity_fn(ctx, _CarryIn(name="x"))
        # The type is what makes the eviction loop re-dispatch at all.
        assert exc_info.value.type == WORKER_EVICTED_TYPE
        assert tuple(exc_info.value.details) == ()

    async def test_controller_failure_does_not_mask_the_eviction(self) -> None:
        class _RaisingCarryApp(App):
            @task(timeout_seconds=60)
            async def boom(self, input: _CarryIn) -> _CarryOut:
                raise asyncio.CancelledError()

            async def run(self, input: _CarryIn) -> _CarryOut:
                return await self.boom(input)

        activity_fn = self._activity("_raising-carry-app", "boom")
        ctx = TaskContext(
            app_name="_raising-carry-app",
            task_name="boom",
            run_id="run-1",
            heartbeat_timeout_seconds=None,
            auto_heartbeat_seconds=None,
        )
        shutdown_module.mark_worker_shutting_down()
        with (
            mock.patch.object(
                activities_module.activity,
                "info",
                return_value=mock.MagicMock(workflow_id="wf-raise"),
            ),
            mock.patch.object(
                heartbeat_module.NoopHeartbeatController,
                "last_sent_details",
                side_effect=RuntimeError("controller is broken"),
            ),
            mock.patch(
                "application_sdk.infrastructure.context.get_infrastructure",
                return_value=None,
            ),
            pytest.raises(ApplicationError) as exc_info,
        ):
            await activity_fn(ctx, _CarryIn(name="x"))
        assert exc_info.value.type == WORKER_EVICTED_TYPE
        assert tuple(exc_info.value.details) == ()

    async def test_empty_beat_supersedes_the_carried_checkpoint(self) -> None:
        # The attempt resumed at position 7, finished it, and beat with no
        # details. Re-carrying position 7 would make the next execution redo
        # work this one completed.
        class _SupersedeApp(App):
            @task(timeout_seconds=60)
            async def boom(self, input: _CarryIn) -> _CarryOut:
                self.heartbeat()
                raise asyncio.CancelledError()

            async def run(self, input: _CarryIn) -> _CarryOut:
                return await self.boom(input)

        activity_fn = self._activity("_supersede-app", "boom")
        ctx = TaskContext(
            app_name="_supersede-app",
            task_name="boom",
            run_id="run-1",
            heartbeat_timeout_seconds=None,
            auto_heartbeat_seconds=None,
            evicted_heartbeat_details=[{"position": 7}],
        )
        shutdown_module.mark_worker_shutting_down()
        with (
            mock.patch.object(
                activities_module.activity,
                "info",
                return_value=mock.MagicMock(workflow_id="wf-supersede"),
            ),
            mock.patch(
                "application_sdk.infrastructure.context.get_infrastructure",
                return_value=None,
            ),
            pytest.raises(ApplicationError) as exc_info,
        ):
            await activity_fn(ctx, _CarryIn(name="x"))
        assert exc_info.value.type == WORKER_EVICTED_TYPE
        assert tuple(exc_info.value.details) == ()

    async def test_attempt_that_never_beat_recarries_the_previous_checkpoint(
        self,
    ) -> None:
        class _RecarryApp(App):
            @task(timeout_seconds=60)
            async def boom(self, input: _CarryIn) -> _CarryOut:
                raise asyncio.CancelledError()

            async def run(self, input: _CarryIn) -> _CarryOut:
                return await self.boom(input)

        activity_fn = self._activity("_recarry-app", "boom")
        ctx = TaskContext(
            app_name="_recarry-app",
            task_name="boom",
            run_id="run-1",
            heartbeat_timeout_seconds=None,
            auto_heartbeat_seconds=None,
            evicted_heartbeat_details=[{"position": 7}],
        )
        shutdown_module.mark_worker_shutting_down()
        with (
            mock.patch.object(
                activities_module.activity,
                "info",
                return_value=mock.MagicMock(workflow_id="wf-recarry"),
            ),
            mock.patch(
                "application_sdk.infrastructure.context.get_infrastructure",
                return_value=None,
            ),
            pytest.raises(ApplicationError) as exc_info,
        ):
            await activity_fn(ctx, _CarryIn(name="x"))
        assert exc_info.value.type == WORKER_EVICTED_TYPE
        assert tuple(exc_info.value.details) == ({"position": 7},)

    async def test_redispatched_execution_reads_carried_details(self) -> None:
        class _ResumeApp(App):
            @task(timeout_seconds=60)
            async def resume(self, input: _CarryIn) -> _CarryOut:
                return _CarryOut(carried=list(self.get_last_heartbeat_details()))

            async def run(self, input: _CarryIn) -> _CarryOut:
                return await self.resume(input)

        activity_fn = self._activity("_resume-app", "resume")
        ctx = TaskContext(
            app_name="_resume-app",
            task_name="resume",
            run_id="run-1",
            heartbeat_timeout_seconds=None,
            auto_heartbeat_seconds=None,
            evicted_heartbeat_details=[{"position": 7}],
        )
        with (
            mock.patch.object(
                activities_module.activity,
                "info",
                return_value=mock.MagicMock(workflow_id="wf-resume"),
            ),
            mock.patch(
                "application_sdk.infrastructure.context.get_infrastructure",
                return_value=None,
            ),
        ):
            out = await activity_fn(ctx, _CarryIn(name="x"))
        assert out.carried == [{"position": 7}]


def _evicted_with_details(*details: object) -> Exception:
    from temporalio.exceptions import ActivityError

    cause = ApplicationError(
        "evicted", *details, type=WORKER_EVICTED_TYPE, non_retryable=True
    )
    err = ActivityError(
        "Activity task failed",
        scheduled_event_id=1,
        started_event_id=2,
        identity="test",
        activity_type="dummy",
        activity_id="dummy-1",
        retry_state=None,
    )
    err.__cause__ = cause
    return err


class TestEvictionRetryCarriesDetails:
    _patch_workflow = TestEvictionRetryHelper._patch_workflow

    async def test_redispatch_hands_details_to_the_task_context(self) -> None:
        evict = _evicted_with_details({"position": 7})
        exec_mock, exec_patch, logger_patch = self._patch_workflow([evict, "ok"])
        ctx = TaskContext(app_name="a", task_name="t", run_id="r")
        with exec_patch, logger_patch:
            result = await execute_activity_with_eviction_retry(
                "act-name", args=[ctx, "input"], max_eviction_retries=3
            )
        assert result == "ok"
        first, second = exec_mock.await_args_list
        assert first.kwargs["args"][0].evicted_heartbeat_details is None
        assert second.kwargs["args"][0].evicted_heartbeat_details == [{"position": 7}]
        assert second.kwargs["args"][1] == "input"  # the rest of args untouched
        assert ctx.evicted_heartbeat_details is None  # caller's object not mutated

    async def test_redispatch_without_details_carries_nothing(self) -> None:
        evict = _evicted_with_details()
        exec_mock, exec_patch, logger_patch = self._patch_workflow([evict, "ok"])
        ctx = TaskContext(app_name="a", task_name="t", run_id="r")
        with exec_patch, logger_patch:
            await execute_activity_with_eviction_retry("act-name", args=[ctx])
        assert (
            exec_mock.await_args_list[1].kwargs["args"][0].evicted_heartbeat_details
            is None
        )

    async def test_empty_beat_clears_a_previous_carry_across_three_dispatches(
        self,
    ) -> None:
        """``kwargs`` is reused across iterations — an empty failure must clear it.

        Three dispatches, because the bug only appears on the second re-dispatch:
        #1 beats position 7 and is evicted, #2 resumes there, finishes it, beats
        clean and is evicted, #3 must start from nothing. Skipping the write on an
        empty failure would leave #1's carry in ``kwargs`` and hand position 7 to
        #3, which would redo work #2 completed. The activity-level tests cover the
        failure payload each attempt produces; only this one covers what the loop
        does with two of them in sequence.
        """
        first = _evicted_with_details({"position": 7})
        second = _evicted_with_details()  # the attempt beat with no details
        exec_mock, exec_patch, logger_patch = self._patch_workflow(
            [first, second, "ok"]
        )
        ctx = TaskContext(app_name="a", task_name="t", run_id="r")
        with exec_patch, logger_patch:
            result = await execute_activity_with_eviction_retry(
                "act-name", args=[ctx, "input"], max_eviction_retries=3
            )
        assert result == "ok"
        assert exec_mock.await_count == 3
        d1, d2, d3 = exec_mock.await_args_list
        assert d1.kwargs["args"][0].evicted_heartbeat_details is None
        assert d2.kwargs["args"][0].evicted_heartbeat_details == [{"position": 7}]
        assert d3.kwargs["args"][0].evicted_heartbeat_details is None
        assert d3.kwargs["args"][1] == "input"  # the rest of args still untouched
        assert ctx.evicted_heartbeat_details is None  # caller's object not mutated

    async def test_successive_carries_replace_rather_than_accumulate(self) -> None:
        first = _evicted_with_details({"position": 7})
        second = _evicted_with_details({"position": 74610})
        exec_mock, exec_patch, logger_patch = self._patch_workflow(
            [first, second, "ok"]
        )
        ctx = TaskContext(app_name="a", task_name="t", run_id="r")
        with exec_patch, logger_patch:
            await execute_activity_with_eviction_retry(
                "act-name", args=[ctx], max_eviction_retries=3
            )
        d1, d2, d3 = exec_mock.await_args_list
        assert d1.kwargs["args"][0].evicted_heartbeat_details is None
        assert d2.kwargs["args"][0].evicted_heartbeat_details == [{"position": 7}]
        assert d3.kwargs["args"][0].evicted_heartbeat_details == [{"position": 74610}]

    async def test_redispatch_with_non_task_context_args_is_unchanged(self) -> None:
        evict = _evicted_with_details({"position": 7})
        exec_mock, exec_patch, logger_patch = self._patch_workflow([evict, "ok"])
        with exec_patch, logger_patch:
            await execute_activity_with_eviction_retry("act-name", args=["plain", 1])
        assert exec_mock.await_args_list[1].kwargs["args"] == ["plain", 1]
