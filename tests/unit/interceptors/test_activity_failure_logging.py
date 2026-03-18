"""Unit tests for the activity failure logging interceptor.

Tests the emission of structured logs with Temporal context when activities fail.
"""

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Mapping, Sequence
from unittest import mock

import pytest
from temporalio.api.common.v1 import Payload

from application_sdk.interceptors.activity_failure_logging import (
    TaskFailureLoggingInterceptor,
    _TaskFailureLoggingActivityInboundInterceptor,
)
from application_sdk.observability.context import correlation_context

# backwards-compat aliases used in assertions below
ActivityFailureLoggingInterceptor = TaskFailureLoggingInterceptor
ActivityFailureLoggingActivityInboundInterceptor = (
    _TaskFailureLoggingActivityInboundInterceptor
)


@dataclass
class MockExecuteActivityInput:
    """Mock ExecuteActivityInput for testing."""

    fn: Any = None
    args: Sequence[Any] = field(default_factory=list)
    headers: Mapping[str, Payload] = field(default_factory=dict)
    executor: Any = None


@dataclass
class MockActivityInfo:
    """Mock activity.info() return value."""

    activity_type: str = "fetch_metadata"
    attempt: int = 1
    workflow_type: str = "SyncMetadataWorkflow"
    workflow_id: str = "sync-tenant-abc-123"
    workflow_run_id: str = "run-def-456"
    task_queue: str = "atlan-snowflake-production"
    schedule_to_close_timeout: timedelta | None = timedelta(minutes=5)
    start_to_close_timeout: timedelta | None = timedelta(minutes=5)
    heartbeat_timeout: timedelta | None = timedelta(seconds=30)


class TestActivityFailureLoggingActivityInboundInterceptor:
    """Tests for ActivityFailureLoggingActivityInboundInterceptor."""

    @pytest.fixture
    def mock_next_activity(self):
        """Create a mock next activity interceptor."""
        mock_next = mock.AsyncMock()
        mock_next.execute_activity = mock.AsyncMock(return_value="activity_result")
        return mock_next

    @pytest.fixture
    def interceptor(self, mock_next_activity):
        """Create the interceptor instance."""
        return ActivityFailureLoggingActivityInboundInterceptor(mock_next_activity)

    @pytest.fixture
    def mock_activity_info(self):
        """Create a mock activity info."""
        return MockActivityInfo()

    @pytest.mark.asyncio
    async def test_success_path_does_not_log(self, interceptor, mock_next_activity):
        """Test that successful activities do not trigger error logging."""
        input_data = MockExecuteActivityInput()

        with mock.patch(
            "application_sdk.interceptors.activity_failure_logging.logger"
        ) as mock_logger:
            result = await interceptor.execute_activity(input_data)

            assert result == "activity_result"
            mock_logger.error.assert_not_called()
            mock_next_activity.execute_activity.assert_called_once_with(input_data)

    @pytest.mark.asyncio
    async def test_failure_path_logs_with_temporal_context(
        self, interceptor, mock_next_activity, mock_activity_info
    ):
        """Test that failed activities log with full Temporal context."""
        input_data = MockExecuteActivityInput()
        test_error = ConnectionRefusedError("Connection refused")
        mock_next_activity.execute_activity.side_effect = test_error

        # Reset correlation context
        correlation_context.set({"atlan-tenant-id": "tenant-abc"})

        with mock.patch("temporalio.activity.info", return_value=mock_activity_info):
            with mock.patch(
                "application_sdk.interceptors.activity_failure_logging.logger"
            ) as mock_logger:
                with pytest.raises(ConnectionRefusedError):
                    await interceptor.execute_activity(input_data)

                # Verify logger.error was called with expected arguments
                mock_logger.error.assert_called_once()
                call_args = mock_logger.error.call_args
                assert call_args[0][0] == "Temporal activity failed"
                assert call_args[1]["exc_info"] is True

                # Verify Temporal context attributes
                kwargs = call_args[1]
                assert kwargs["temporal.activity.type"] == "fetch_metadata"
                assert kwargs["temporal.activity.attempt"] == 1
                assert kwargs["temporal.workflow.type"] == "SyncMetadataWorkflow"
                assert kwargs["temporal.workflow.id"] == "sync-tenant-abc-123"
                assert kwargs["temporal.workflow.run_id"] == "run-def-456"
                assert (
                    kwargs["temporal.activity.task_queue"]
                    == "atlan-snowflake-production"
                )
                assert (
                    kwargs["temporal.activity.schedule_to_close_timeout"] == "0:05:00"
                )
                assert kwargs["temporal.activity.start_to_close_timeout"] == "0:05:00"
                assert kwargs["temporal.activity.heartbeat_timeout"] == "0:00:30"
                assert kwargs["tenant.id"] == "tenant-abc"

    @pytest.mark.asyncio
    async def test_failure_reraises_original_exception(
        self, interceptor, mock_next_activity, mock_activity_info
    ):
        """Test that the original exception is always re-raised."""
        input_data = MockExecuteActivityInput()
        test_error = ValueError("Something went wrong")
        mock_next_activity.execute_activity.side_effect = test_error

        with mock.patch("temporalio.activity.info", return_value=mock_activity_info):
            with mock.patch(
                "application_sdk.interceptors.activity_failure_logging.logger"
            ):
                with pytest.raises(ValueError) as exc_info:
                    await interceptor.execute_activity(input_data)

                assert str(exc_info.value) == "Something went wrong"

    @pytest.mark.asyncio
    async def test_handles_none_correlation_context(
        self, interceptor, mock_next_activity, mock_activity_info
    ):
        """Test that None correlation context is handled gracefully."""
        input_data = MockExecuteActivityInput()
        test_error = RuntimeError("Test error")
        mock_next_activity.execute_activity.side_effect = test_error

        # Set correlation context to None
        correlation_context.set(None)

        with mock.patch("temporalio.activity.info", return_value=mock_activity_info):
            with mock.patch(
                "application_sdk.interceptors.activity_failure_logging.logger"
            ) as mock_logger:
                with pytest.raises(RuntimeError):
                    await interceptor.execute_activity(input_data)

                # Verify logging still works
                mock_logger.error.assert_called_once()
                kwargs = mock_logger.error.call_args[1]
                # tenant.id should not be present when correlation context is None
                assert "tenant.id" not in kwargs
                # But Temporal context should still be present
                assert kwargs["temporal.activity.type"] == "fetch_metadata"

    @pytest.mark.asyncio
    async def test_handles_empty_tenant_id(
        self, interceptor, mock_next_activity, mock_activity_info
    ):
        """Test that empty tenant ID is not included in log attributes."""
        input_data = MockExecuteActivityInput()
        test_error = RuntimeError("Test error")
        mock_next_activity.execute_activity.side_effect = test_error

        # Set correlation context with empty tenant ID
        correlation_context.set({"atlan-tenant-id": ""})

        with mock.patch("temporalio.activity.info", return_value=mock_activity_info):
            with mock.patch(
                "application_sdk.interceptors.activity_failure_logging.logger"
            ) as mock_logger:
                with pytest.raises(RuntimeError):
                    await interceptor.execute_activity(input_data)

                kwargs = mock_logger.error.call_args[1]
                # Empty tenant ID should not be included
                assert "tenant.id" not in kwargs

    @pytest.mark.asyncio
    async def test_handles_none_timeouts(self, interceptor, mock_next_activity):
        """Test that None timeouts are not included in log attributes."""
        input_data = MockExecuteActivityInput()
        test_error = RuntimeError("Test error")
        mock_next_activity.execute_activity.side_effect = test_error

        # Create activity info with no timeouts
        activity_info_no_timeouts = MockActivityInfo(
            schedule_to_close_timeout=None,
            start_to_close_timeout=None,
            heartbeat_timeout=None,
        )

        correlation_context.set({})

        with mock.patch(
            "temporalio.activity.info", return_value=activity_info_no_timeouts
        ):
            with mock.patch(
                "application_sdk.interceptors.activity_failure_logging.logger"
            ) as mock_logger:
                with pytest.raises(RuntimeError):
                    await interceptor.execute_activity(input_data)

                kwargs = mock_logger.error.call_args[1]
                # Timeout keys should not be present when timeouts are None
                assert "temporal.activity.schedule_to_close_timeout" not in kwargs
                assert "temporal.activity.start_to_close_timeout" not in kwargs
                assert "temporal.activity.heartbeat_timeout" not in kwargs
                # But other Temporal context should still be present
                assert kwargs["temporal.activity.type"] == "fetch_metadata"

    @pytest.mark.asyncio
    async def test_base_exception_is_logged(
        self, interceptor, mock_next_activity, mock_activity_info
    ):
        """Test that BaseException subclasses (e.g. timeout-driven cancellations) are logged.

        asyncio.CancelledError is a BaseException, not an Exception. The previous
        `except Exception` guard silently swallowed all timeout-driven cancellations
        (start-to-close, heartbeat, schedule-to-close, workflow execution timeout).
        """

        class _ActivityCancelled(BaseException):
            pass

        input_data = MockExecuteActivityInput()
        mock_next_activity.execute_activity.side_effect = _ActivityCancelled()

        correlation_context.set({"atlan-tenant-id": "tenant-abc"})

        with mock.patch("temporalio.activity.info", return_value=mock_activity_info):
            with mock.patch(
                "application_sdk.interceptors.activity_failure_logging.logger"
            ) as mock_logger:
                with pytest.raises(_ActivityCancelled):
                    await interceptor.execute_activity(input_data)

                mock_logger.error.assert_called_once()
                kwargs = mock_logger.error.call_args[1]
                assert kwargs["temporal.activity.type"] == "fetch_metadata"
                assert kwargs["tenant.id"] == "tenant-abc"

    @pytest.mark.asyncio
    async def test_logging_failure_does_not_swallow_activity_exception(
        self, interceptor, mock_next_activity
    ):
        """Test that logging failures don't prevent the original exception from being raised."""
        input_data = MockExecuteActivityInput()
        test_error = ValueError("Original activity error")
        mock_next_activity.execute_activity.side_effect = test_error

        # Make activity.info() throw an exception
        with mock.patch(
            "temporalio.activity.info", side_effect=RuntimeError("Info failed")
        ):
            with mock.patch(
                "application_sdk.interceptors.activity_failure_logging.logger"
            ) as mock_logger:
                with pytest.raises(ValueError) as exc_info:
                    await interceptor.execute_activity(input_data)

                # Original exception should still be raised
                assert str(exc_info.value) == "Original activity error"
                # Warning should be logged about the failure to extract info
                mock_logger.warning.assert_called()

    @pytest.mark.asyncio
    async def test_logger_error_failure_does_not_swallow_activity_exception(
        self, interceptor, mock_next_activity, mock_activity_info
    ):
        """Test that logger.error failure doesn't prevent original exception from being raised."""
        input_data = MockExecuteActivityInput()
        test_error = ValueError("Original activity error")
        mock_next_activity.execute_activity.side_effect = test_error

        correlation_context.set({})

        with mock.patch("temporalio.activity.info", return_value=mock_activity_info):
            with mock.patch(
                "application_sdk.interceptors.activity_failure_logging.logger"
            ) as mock_logger:
                # Make logger.error raise an exception
                mock_logger.error.side_effect = RuntimeError("Logger broken")

                with pytest.raises(ValueError) as exc_info:
                    await interceptor.execute_activity(input_data)

                # Original exception should still be raised
                assert str(exc_info.value) == "Original activity error"
                # Warning should be logged about the logging failure
                mock_logger.warning.assert_called()


class TestActivityFailureLoggingInterceptor:
    """Tests for the main ActivityFailureLoggingInterceptor class."""

    @pytest.fixture
    def interceptor(self):
        """Create the main interceptor instance."""
        return ActivityFailureLoggingInterceptor()

    def test_workflow_interceptor_class_returns_none(self, interceptor):
        """Test that workflow_interceptor_class returns None (activity-only)."""
        mock_input = mock.MagicMock()

        result = interceptor.workflow_interceptor_class(mock_input)

        assert result is None

    def test_intercept_activity_wraps_next(self, interceptor):
        """Test that intercept_activity wraps the next interceptor."""
        mock_next = mock.MagicMock()

        result = interceptor.intercept_activity(mock_next)

        assert isinstance(result, ActivityFailureLoggingActivityInboundInterceptor)


class TestCollectTemporalContext:
    """Tests for the _collect_temporal_context method."""

    @pytest.fixture
    def mock_next_activity(self):
        """Create a mock next activity interceptor."""
        mock_next = mock.AsyncMock()
        return mock_next

    @pytest.fixture
    def interceptor(self, mock_next_activity):
        """Create the interceptor instance."""
        return ActivityFailureLoggingActivityInboundInterceptor(mock_next_activity)

    def test_collects_all_temporal_attributes(self, interceptor):
        """Test that all Temporal attributes are collected correctly."""
        activity_info = MockActivityInfo(
            activity_type="test_activity",
            attempt=3,
            workflow_type="TestWorkflow",
            workflow_id="wf-123",
            workflow_run_id="run-456",
            task_queue="test-queue",
            schedule_to_close_timeout=timedelta(hours=1),
            start_to_close_timeout=timedelta(minutes=30),
            heartbeat_timeout=timedelta(seconds=60),
        )

        correlation_context.set({"atlan-tenant-id": "test-tenant"})

        with mock.patch("temporalio.activity.info", return_value=activity_info):
            attrs = interceptor._collect_temporal_context()

        assert attrs["temporal.activity.type"] == "test_activity"
        assert attrs["temporal.activity.attempt"] == 3
        assert attrs["temporal.workflow.type"] == "TestWorkflow"
        assert attrs["temporal.workflow.id"] == "wf-123"
        assert attrs["temporal.workflow.run_id"] == "run-456"
        assert attrs["temporal.activity.task_queue"] == "test-queue"
        assert attrs["temporal.activity.schedule_to_close_timeout"] == "1:00:00"
        assert attrs["temporal.activity.start_to_close_timeout"] == "0:30:00"
        assert attrs["temporal.activity.heartbeat_timeout"] == "0:01:00"
        assert attrs["tenant.id"] == "test-tenant"

    def test_handles_activity_info_exception(self, interceptor):
        """Test graceful handling when activity.info() fails."""
        correlation_context.set({"atlan-tenant-id": "test-tenant"})

        with mock.patch(
            "temporalio.activity.info", side_effect=RuntimeError("Not in activity")
        ):
            with mock.patch(
                "application_sdk.interceptors.activity_failure_logging.logger"
            ) as mock_logger:
                attrs = interceptor._collect_temporal_context()

        # Should still have tenant from correlation context
        assert attrs.get("tenant.id") == "test-tenant"
        # Should have logged a warning
        mock_logger.warning.assert_called()

    def test_handles_correlation_context_exception(self, interceptor):
        """Test graceful handling when correlation_context access fails."""
        activity_info = MockActivityInfo()

        # Create a mock that raises when accessed
        mock_corr_ctx = mock.MagicMock()
        mock_corr_ctx.get.side_effect = RuntimeError("Context error")

        with mock.patch("temporalio.activity.info", return_value=activity_info):
            with mock.patch(
                "application_sdk.interceptors.activity_failure_logging.correlation_context",
                mock_corr_ctx,
            ):
                with mock.patch(
                    "application_sdk.interceptors.activity_failure_logging.logger"
                ) as mock_logger:
                    attrs = interceptor._collect_temporal_context()

        # Should still have Temporal attributes
        assert attrs["temporal.activity.type"] == "fetch_metadata"
        # Should have logged a warning
        mock_logger.warning.assert_called()
