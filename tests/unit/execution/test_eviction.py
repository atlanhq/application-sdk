"""Worker-eviction path: SIGTERM mid-activity → typed WorkerEvictedError →
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
from unittest import mock

import pytest

from application_sdk.app.base import App
from application_sdk.app.registry import AppRegistry, TaskRegistry
from application_sdk.app.task import task
from application_sdk.contracts.base import Input, Output
from application_sdk.errors.leaves import WORKER_EVICTED_TYPE
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

    @pytest.mark.asyncio
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

    @pytest.mark.asyncio
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
    the workflow sees when the activity raises ``WorkerEvictedError``.
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

    @pytest.mark.asyncio
    async def test_returns_result_on_first_success(self) -> None:
        exec_mock, exec_patch, logger_patch = self._patch_workflow("ok")
        with exec_patch, logger_patch:
            result = await execute_activity_with_eviction_retry("act-name")
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_eviction_retries_then_succeeds(self) -> None:
        evict = _make_activity_error_with_app_error_cause()
        exec_mock, exec_patch, logger_patch = self._patch_workflow([evict, evict, "ok"])
        with exec_patch, logger_patch:
            result = await execute_activity_with_eviction_retry(
                "act-name", max_eviction_retries=3
            )
        assert result == "ok"
        assert exec_mock.await_count == 3

    @pytest.mark.asyncio
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

    @pytest.mark.asyncio
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
