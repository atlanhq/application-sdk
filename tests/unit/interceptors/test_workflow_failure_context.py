"""Unit tests for WorkflowFailureContextInterceptor.

Tests cover:
- Activity side: context is stashed on failure, not on success
- Activity side: ApplicationError details (config_failure, temporal_error_type) are propagated
- Activity side: stash is overwritten on successive failures (last-attempt semantics)
- Activity side: stash failure is swallowed (never masks original exception)
- Workflow side: failure is wrapped as ApplicationError with details[0] = stashed context
- Workflow side: when no context is stashed, original exception re-raised unchanged
- Workflow side: success path cleans up stash (no memory leak)
- Workflow side: wrapped ApplicationError preserves original message and type
- Integration: config_failure flag flows from activity details through to the workflow wrapper
"""

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Mapping, Sequence
from unittest import mock

import pytest
from temporalio.exceptions import ApplicationError

from application_sdk.interceptors.workflow_failure_context import (
    WorkflowFailureContextActivityInboundInterceptor,
    WorkflowFailureContextInterceptor,
    WorkflowFailureContextWorkflowInboundInterceptor,
    _failure_contexts,
    _failure_contexts_lock,
)


# ---------------------------------------------------------------------------
# Mock data classes matching the Temporal SDK input shapes
# ---------------------------------------------------------------------------


@dataclass
class MockExecuteActivityInput:
    fn: Any = None
    args: Sequence[Any] = field(default_factory=list)
    headers: Mapping[str, Any] = field(default_factory=dict)
    executor: Any = None


@dataclass
class MockExecuteWorkflowInput:
    args: Sequence[Any] = field(default_factory=list)
    headers: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class MockActivityInfo:
    activity_type: str = "fetch_metadata"
    attempt: int = 1
    workflow_type: str = "RedshiftMetadataExtractionWorkflow"
    workflow_id: str = "atlan-redshift-abc-123"
    workflow_run_id: str = "run-def-456"
    task_queue: str = "atlan-redshift-production"
    schedule_to_close_timeout: timedelta | None = timedelta(hours=1)
    start_to_close_timeout: timedelta | None = timedelta(minutes=30)
    heartbeat_timeout: timedelta | None = timedelta(seconds=30)


@dataclass
class MockWorkflowInfo:
    run_id: str = "run-def-456"
    workflow_id: str = "atlan-redshift-abc-123"
    workflow_type: str = "RedshiftMetadataExtractionWorkflow"


# ---------------------------------------------------------------------------
# State cleanup fixture — prevents test cross-contamination
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clean_failure_contexts():
    """Clear process-level stash before and after every test."""
    with _failure_contexts_lock:
        _failure_contexts.clear()
    yield
    with _failure_contexts_lock:
        _failure_contexts.clear()


# ---------------------------------------------------------------------------
# Activity interceptor tests
# ---------------------------------------------------------------------------


class TestWorkflowFailureContextActivityInboundInterceptor:
    @pytest.fixture
    def mock_next(self):
        nxt = mock.AsyncMock()
        nxt.execute_activity = mock.AsyncMock(return_value="activity_result")
        return nxt

    @pytest.fixture
    def interceptor(self, mock_next):
        return WorkflowFailureContextActivityInboundInterceptor(mock_next)

    @pytest.fixture
    def activity_info(self):
        return MockActivityInfo()

    # --- success path ---

    @pytest.mark.asyncio
    async def test_success_does_not_stash(self, interceptor, activity_info):
        """Successful activities must not pollute the stash."""
        with mock.patch(
            "application_sdk.interceptors.workflow_failure_context.activity"
        ) as mock_act:
            mock_act.info.return_value = activity_info
            result = await interceptor.execute_activity(MockExecuteActivityInput())

        assert result == "activity_result"
        assert activity_info.workflow_run_id not in _failure_contexts

    # --- failure path — basic stash ---

    @pytest.mark.asyncio
    async def test_failure_stashes_activity_context(self, interceptor, mock_next, activity_info):
        """On activity failure, stash is populated with structured context."""
        mock_next.execute_activity.side_effect = RuntimeError("connection refused")

        with mock.patch(
            "application_sdk.interceptors.workflow_failure_context.activity"
        ) as mock_act:
            mock_act.info.return_value = activity_info
            with pytest.raises(RuntimeError):
                await interceptor.execute_activity(MockExecuteActivityInput())

        ctx = _failure_contexts.get(activity_info.workflow_run_id)
        assert ctx is not None
        assert ctx["activity_type"] == "fetch_metadata"
        assert ctx["attempt"] == 1
        assert ctx["task_queue"] == "atlan-redshift-production"
        assert ctx["python_error_type"] == "RuntimeError"
        assert "connection refused" in ctx["error_message"]

    @pytest.mark.asyncio
    async def test_failure_stashes_timeout_config(self, interceptor, mock_next, activity_info):
        """Timeout fields are captured when present."""
        mock_next.execute_activity.side_effect = RuntimeError("timeout")

        with mock.patch(
            "application_sdk.interceptors.workflow_failure_context.activity"
        ) as mock_act:
            mock_act.info.return_value = activity_info
            with pytest.raises(RuntimeError):
                await interceptor.execute_activity(MockExecuteActivityInput())

        ctx = _failure_contexts[activity_info.workflow_run_id]
        assert ctx["schedule_to_close_timeout"] == str(timedelta(hours=1))
        assert ctx["start_to_close_timeout"] == str(timedelta(minutes=30))
        assert ctx["heartbeat_timeout"] == str(timedelta(seconds=30))

    @pytest.mark.asyncio
    async def test_failure_omits_none_timeouts(self, interceptor, mock_next):
        """Timeout fields are omitted when activity has no timeout configured."""
        info_no_timeouts = MockActivityInfo(
            schedule_to_close_timeout=None,
            start_to_close_timeout=None,
            heartbeat_timeout=None,
        )
        mock_next.execute_activity.side_effect = RuntimeError("err")

        with mock.patch(
            "application_sdk.interceptors.workflow_failure_context.activity"
        ) as mock_act:
            mock_act.info.return_value = info_no_timeouts
            with pytest.raises(RuntimeError):
                await interceptor.execute_activity(MockExecuteActivityInput())

        ctx = _failure_contexts[info_no_timeouts.workflow_run_id]
        assert "schedule_to_close_timeout" not in ctx
        assert "start_to_close_timeout" not in ctx
        assert "heartbeat_timeout" not in ctx

    @pytest.mark.asyncio
    async def test_original_exception_is_reraised(self, interceptor, mock_next, activity_info):
        """The original exception must propagate unchanged — never swallowed."""
        original_error = ValueError("bad credential")
        mock_next.execute_activity.side_effect = original_error

        with mock.patch(
            "application_sdk.interceptors.workflow_failure_context.activity"
        ) as mock_act:
            mock_act.info.return_value = activity_info
            with pytest.raises(ValueError) as exc_info:
                await interceptor.execute_activity(MockExecuteActivityInput())

        assert exc_info.value is original_error

    # --- ApplicationError fields ---

    @pytest.mark.asyncio
    async def test_application_error_fields_captured(self, interceptor, mock_next, activity_info):
        """temporal_error_type and non_retryable are captured from ApplicationError."""
        mock_next.execute_activity.side_effect = ApplicationError(
            "circuit breaker triggered",
            type="CircuitBreakerTriggered",
            non_retryable=True,
        )

        with mock.patch(
            "application_sdk.interceptors.workflow_failure_context.activity"
        ) as mock_act:
            mock_act.info.return_value = activity_info
            with pytest.raises(ApplicationError):
                await interceptor.execute_activity(MockExecuteActivityInput())

        ctx = _failure_contexts[activity_info.workflow_run_id]
        assert ctx["temporal_error_type"] == "CircuitBreakerTriggered"
        assert ctx["non_retryable"] is True

    @pytest.mark.asyncio
    async def test_config_failure_flag_propagated_from_details(self, interceptor, mock_next, activity_info):
        """config_failure=True in ApplicationError details is forwarded to the stash."""
        mock_next.execute_activity.side_effect = ApplicationError(
            "Preflight check failed: connection refused",
            {
                "config_failure": True,
                "config_failure_reason": "preflight_check_failed",
                "failed_checks": ["connectionCheck"],
            },
            type="PREFLIGHT_FAILURE",
            non_retryable=True,
        )

        with mock.patch(
            "application_sdk.interceptors.workflow_failure_context.activity"
        ) as mock_act:
            mock_act.info.return_value = activity_info
            with pytest.raises(ApplicationError):
                await interceptor.execute_activity(MockExecuteActivityInput())

        ctx = _failure_contexts[activity_info.workflow_run_id]
        assert ctx["config_failure"] is True
        assert ctx["config_failure_reason"] == "preflight_check_failed"

    @pytest.mark.asyncio
    async def test_no_config_failure_flag_when_not_set(self, interceptor, mock_next, activity_info):
        """config_failure key absent when the exception doesn't set it."""
        mock_next.execute_activity.side_effect = ApplicationError(
            "connection timeout",
            type="ConnectionError",
            non_retryable=False,
        )

        with mock.patch(
            "application_sdk.interceptors.workflow_failure_context.activity"
        ) as mock_act:
            mock_act.info.return_value = activity_info
            with pytest.raises(ApplicationError):
                await interceptor.execute_activity(MockExecuteActivityInput())

        ctx = _failure_contexts[activity_info.workflow_run_id]
        assert "config_failure" not in ctx

    # --- last-attempt semantics ---

    @pytest.mark.asyncio
    async def test_successive_failures_overwrite_stash(self, mock_next):
        """Each attempt's failure overwrites the previous — last attempt wins."""
        interceptor = WorkflowFailureContextActivityInboundInterceptor(mock_next)
        shared_run_id = "run-shared-789"

        for attempt in (1, 2, 3):
            info = MockActivityInfo(attempt=attempt, workflow_run_id=shared_run_id)
            mock_next.execute_activity.side_effect = RuntimeError(f"attempt {attempt} failed")
            with mock.patch(
                "application_sdk.interceptors.workflow_failure_context.activity"
            ) as mock_act:
                mock_act.info.return_value = info
                with pytest.raises(RuntimeError):
                    await interceptor.execute_activity(MockExecuteActivityInput())

        assert _failure_contexts[shared_run_id]["attempt"] == 3

    # --- stash errors don't mask exceptions ---

    @pytest.mark.asyncio
    async def test_stash_error_does_not_mask_activity_exception(self, interceptor, mock_next):
        """If context stashing fails internally, the original exception still propagates."""
        original_error = RuntimeError("real activity failure")
        mock_next.execute_activity.side_effect = original_error

        with mock.patch(
            "application_sdk.interceptors.workflow_failure_context.activity"
        ) as mock_act:
            # Make activity.info() raise to simulate stash failure
            mock_act.info.side_effect = Exception("info() unavailable")
            with pytest.raises(RuntimeError) as exc_info:
                await interceptor.execute_activity(MockExecuteActivityInput())

        assert exc_info.value is original_error


# ---------------------------------------------------------------------------
# Workflow interceptor tests
# ---------------------------------------------------------------------------


class TestWorkflowFailureContextWorkflowInboundInterceptor:
    @pytest.fixture
    def mock_next(self):
        nxt = mock.AsyncMock()
        nxt.execute_workflow = mock.AsyncMock(return_value={"status": "success"})
        return nxt

    @pytest.fixture
    def interceptor(self, mock_next):
        return WorkflowFailureContextWorkflowInboundInterceptor(mock_next)

    @pytest.fixture
    def workflow_info(self):
        return MockWorkflowInfo()

    @pytest.fixture
    def pre_stashed_ctx(self, workflow_info):
        """Pre-populate the stash as the activity interceptor would have done."""
        ctx = {
            "activity_type": "bulk_entity_create",
            "attempt": 3,
            "task_queue": "atlan-publish-production",
            "start_to_close_timeout": "0:30:00",
            "python_error_type": "ConnectionError",
            "non_retryable": False,
        }
        with _failure_contexts_lock:
            _failure_contexts[workflow_info.run_id] = ctx
        return ctx

    # --- failure path with stashed context ---

    @pytest.mark.asyncio
    async def test_failure_wrapped_as_application_error_with_details(
        self, interceptor, mock_next, workflow_info, pre_stashed_ctx
    ):
        """Workflow failure is re-raised as ApplicationError with activity context in details."""
        mock_next.execute_workflow.side_effect = RuntimeError("workflow failed")

        with mock.patch(
            "application_sdk.interceptors.workflow_failure_context.workflow"
        ) as mock_wf:
            mock_wf.info.return_value = workflow_info
            with pytest.raises(ApplicationError) as exc_info:
                await interceptor.execute_workflow(MockExecuteWorkflowInput())

        err = exc_info.value
        assert err.details == (pre_stashed_ctx,)  # Temporal SDK stores details as tuple
        assert err.non_retryable is True

    @pytest.mark.asyncio
    async def test_wrapped_error_preserves_original_message(
        self, interceptor, mock_next, workflow_info, pre_stashed_ctx
    ):
        """The ApplicationError wrapper preserves the original exception message."""
        mock_next.execute_workflow.side_effect = RuntimeError("original message here")

        with mock.patch(
            "application_sdk.interceptors.workflow_failure_context.workflow"
        ) as mock_wf:
            mock_wf.info.return_value = workflow_info
            with pytest.raises(ApplicationError) as exc_info:
                await interceptor.execute_workflow(MockExecuteWorkflowInput())

        assert "original message here" in exc_info.value.message

    @pytest.mark.asyncio
    async def test_wrapped_error_preserves_original_type_for_application_error(
        self, interceptor, mock_next, workflow_info, pre_stashed_ctx
    ):
        """The type= field on the ApplicationError wrapper uses the original error's type string."""
        mock_next.execute_workflow.side_effect = ApplicationError(
            "atlas not found",
            type="AtlasError",
            non_retryable=False,
        )

        with mock.patch(
            "application_sdk.interceptors.workflow_failure_context.workflow"
        ) as mock_wf:
            mock_wf.info.return_value = workflow_info
            with pytest.raises(ApplicationError) as exc_info:
                await interceptor.execute_workflow(MockExecuteWorkflowInput())

        assert exc_info.value.type == "AtlasError"

    @pytest.mark.asyncio
    async def test_wrapped_error_has_cause_chain(
        self, interceptor, mock_next, workflow_info, pre_stashed_ctx
    ):
        """Original exception is accessible via __cause__ on the wrapper."""
        original = RuntimeError("original cause")
        mock_next.execute_workflow.side_effect = original

        with mock.patch(
            "application_sdk.interceptors.workflow_failure_context.workflow"
        ) as mock_wf:
            mock_wf.info.return_value = workflow_info
            with pytest.raises(ApplicationError) as exc_info:
                await interceptor.execute_workflow(MockExecuteWorkflowInput())

        assert exc_info.value.__cause__ is original

    @pytest.mark.asyncio
    async def test_stash_cleared_after_failure(
        self, interceptor, mock_next, workflow_info, pre_stashed_ctx
    ):
        """Stash entry is removed after workflow failure so memory doesn't leak."""
        mock_next.execute_workflow.side_effect = RuntimeError("err")

        with mock.patch(
            "application_sdk.interceptors.workflow_failure_context.workflow"
        ) as mock_wf:
            mock_wf.info.return_value = workflow_info
            with pytest.raises(ApplicationError):
                await interceptor.execute_workflow(MockExecuteWorkflowInput())

        assert workflow_info.run_id not in _failure_contexts

    # --- failure path without stashed context ---

    @pytest.mark.asyncio
    async def test_failure_without_stash_reraises_unchanged(
        self, interceptor, mock_next, workflow_info
    ):
        """When no activity context was stashed, the original exception propagates as-is."""
        original = RuntimeError("non-activity failure")
        mock_next.execute_workflow.side_effect = original

        with mock.patch(
            "application_sdk.interceptors.workflow_failure_context.workflow"
        ) as mock_wf:
            mock_wf.info.return_value = workflow_info
            with pytest.raises(RuntimeError) as exc_info:
                await interceptor.execute_workflow(MockExecuteWorkflowInput())

        assert exc_info.value is original

    # --- success path ---

    @pytest.mark.asyncio
    async def test_success_returns_result_unchanged(
        self, interceptor, mock_next, workflow_info
    ):
        """Successful workflows return result without modification."""
        expected = {"assets_extracted": 1000}
        mock_next.execute_workflow.return_value = expected

        with mock.patch(
            "application_sdk.interceptors.workflow_failure_context.workflow"
        ) as mock_wf:
            mock_wf.info.return_value = workflow_info
            result = await interceptor.execute_workflow(MockExecuteWorkflowInput())

        assert result is expected

    @pytest.mark.asyncio
    async def test_success_cleans_up_stash(
        self, interceptor, mock_next, workflow_info, pre_stashed_ctx
    ):
        """On success, any stashed context is cleaned up (shouldn't exist but defensive cleanup)."""
        mock_next.execute_workflow.return_value = {"status": "ok"}

        with mock.patch(
            "application_sdk.interceptors.workflow_failure_context.workflow"
        ) as mock_wf:
            mock_wf.info.return_value = workflow_info
            await interceptor.execute_workflow(MockExecuteWorkflowInput())

        assert workflow_info.run_id not in _failure_contexts


# ---------------------------------------------------------------------------
# Top-level interceptor wiring tests
# ---------------------------------------------------------------------------


class TestWorkflowFailureContextInterceptor:
    def test_intercept_activity_returns_correct_type(self):
        """intercept_activity returns the activity interceptor subclass."""
        interceptor = WorkflowFailureContextInterceptor()
        mock_next = mock.MagicMock()
        result = interceptor.intercept_activity(mock_next)
        assert isinstance(result, WorkflowFailureContextActivityInboundInterceptor)

    def test_workflow_interceptor_class_returns_correct_type(self):
        """workflow_interceptor_class returns the workflow interceptor subclass."""
        interceptor = WorkflowFailureContextInterceptor()
        result = interceptor.workflow_interceptor_class(mock.MagicMock())
        assert result is WorkflowFailureContextWorkflowInboundInterceptor


# ---------------------------------------------------------------------------
# Integration: config_failure flows end-to-end through both interceptors
# ---------------------------------------------------------------------------


class TestConfigFailureEndToEnd:
    """Verify the full path: preflight_check raises tagged ApplicationError
    → activity interceptor stashes config_failure=True
    → workflow interceptor wraps failure with config_failure in details[0].
    """

    @pytest.mark.asyncio
    async def test_config_failure_present_in_workflow_wrapper_details(self):
        run_id = "run-config-fail-e2e"
        info = MockActivityInfo(workflow_run_id=run_id)
        wf_info = MockWorkflowInfo(run_id=run_id)

        # --- Activity side ---
        activity_next = mock.AsyncMock()
        activity_next.execute_activity = mock.AsyncMock(
            side_effect=ApplicationError(
                "Preflight check failed: invalid credentials",
                {
                    "config_failure": True,
                    "config_failure_reason": "preflight_check_failed",
                    "failed_checks": ["connectionCheck"],
                },
                type="PREFLIGHT_FAILURE",
                non_retryable=True,
            )
        )
        act_interceptor = WorkflowFailureContextActivityInboundInterceptor(activity_next)

        with mock.patch(
            "application_sdk.interceptors.workflow_failure_context.activity"
        ) as mock_act:
            mock_act.info.return_value = info
            with pytest.raises(ApplicationError):
                await act_interceptor.execute_activity(MockExecuteActivityInput())

        # Verify stash has config_failure
        assert _failure_contexts[run_id]["config_failure"] is True

        # --- Workflow side ---
        wf_next = mock.AsyncMock()
        wf_next.execute_workflow = mock.AsyncMock(
            side_effect=ApplicationError(
                "Preflight check failed: invalid credentials",
                type="PREFLIGHT_FAILURE",
                non_retryable=True,
            )
        )
        wf_interceptor = WorkflowFailureContextWorkflowInboundInterceptor(wf_next)

        with mock.patch(
            "application_sdk.interceptors.workflow_failure_context.workflow"
        ) as mock_wf:
            mock_wf.info.return_value = wf_info
            with pytest.raises(ApplicationError) as exc_info:
                await wf_interceptor.execute_workflow(MockExecuteWorkflowInput())

        # Wrapper details contain config_failure
        wrapper = exc_info.value
        assert wrapper.details[0]["config_failure"] is True
        assert wrapper.details[0]["config_failure_reason"] == "preflight_check_failed"
        assert wrapper.details[0]["activity_type"] == "fetch_metadata"

        # Stash cleaned up
        assert run_id not in _failure_contexts
