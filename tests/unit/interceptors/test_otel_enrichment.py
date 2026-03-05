"""Unit tests for the OTel enrichment interceptor.

Tests the enrichment of OpenTelemetry spans with Temporal activity context,
tenant information, and exception details on failure.
"""

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Mapping, Sequence
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from temporalio.api.common.v1 import Payload

from application_sdk.interceptors.otel_enrichment import (
    OTelEnrichmentActivityInboundInterceptor,
    OTelEnrichmentInterceptor,
)
from application_sdk.observability.context import correlation_context


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

    attempt: int = 1
    task_queue: str = "test-task-queue"
    workflow_id: str = "test-workflow-123"
    workflow_run_id: str = "test-run-456"
    schedule_to_close_timeout: timedelta | None = None
    start_to_close_timeout: timedelta | None = None
    heartbeat_timeout: timedelta | None = None


class TestOTelEnrichmentActivityInboundInterceptor:
    """Tests for OTelEnrichmentActivityInboundInterceptor."""

    @pytest.fixture
    def mock_next_activity(self):
        """Create a mock next activity interceptor."""
        mock_next = AsyncMock()
        mock_next.execute_activity = AsyncMock(return_value="activity_result")
        return mock_next

    @pytest.fixture
    def interceptor(self, mock_next_activity):
        """Create the activity interceptor instance."""
        return OTelEnrichmentActivityInboundInterceptor(mock_next_activity)

    @pytest.fixture
    def mock_span(self):
        """Create a mock span that is recording."""
        span = MagicMock()
        span.is_recording.return_value = True
        span.set_attribute = MagicMock()
        span.record_exception = MagicMock()
        return span

    @pytest.fixture
    def mock_non_recording_span(self):
        """Create a mock span that is not recording."""
        span = MagicMock()
        span.is_recording.return_value = False
        return span

    @pytest.mark.asyncio
    @patch("temporalio.activity.info")
    @patch("opentelemetry.trace.get_current_span")
    async def test_sets_activity_metadata(
        self, mock_get_span, mock_info, interceptor, mock_next_activity, mock_span
    ):
        """Test that activity metadata is set on the span."""
        mock_get_span.return_value = mock_span
        mock_info.return_value = MockActivityInfo(
            attempt=3,
            task_queue="my-task-queue",
            workflow_id="wf-123",
            workflow_run_id="run-456",
        )
        correlation_context.set(None)
        input_data = MockExecuteActivityInput()

        await interceptor.execute_activity(input_data)

        mock_span.set_attribute.assert_any_call("temporal.activity.attempt", 3)
        mock_span.set_attribute.assert_any_call(
            "temporal.activity.task_queue", "my-task-queue"
        )
        mock_span.set_attribute.assert_any_call("temporal.workflow.id", "wf-123")
        mock_span.set_attribute.assert_any_call("temporal.workflow.run_id", "run-456")
        mock_next_activity.execute_activity.assert_called_once_with(input_data)

    @pytest.mark.asyncio
    @patch("temporalio.activity.info")
    @patch("opentelemetry.trace.get_current_span")
    async def test_sets_timeout_attributes(
        self, mock_get_span, mock_info, interceptor, mock_next_activity, mock_span
    ):
        """Test that timeout attributes are set when present."""
        mock_get_span.return_value = mock_span
        mock_info.return_value = MockActivityInfo(
            schedule_to_close_timeout=timedelta(hours=2),
            start_to_close_timeout=timedelta(minutes=30),
            heartbeat_timeout=timedelta(seconds=60),
        )
        correlation_context.set(None)
        input_data = MockExecuteActivityInput()

        await interceptor.execute_activity(input_data)

        mock_span.set_attribute.assert_any_call(
            "temporal.activity.schedule_to_close_timeout", str(timedelta(hours=2))
        )
        mock_span.set_attribute.assert_any_call(
            "temporal.activity.start_to_close_timeout", str(timedelta(minutes=30))
        )
        mock_span.set_attribute.assert_any_call(
            "temporal.activity.heartbeat_timeout", str(timedelta(seconds=60))
        )

    @pytest.mark.asyncio
    @patch("temporalio.activity.info")
    @patch("opentelemetry.trace.get_current_span")
    async def test_sets_tenant_id_from_correlation_context(
        self, mock_get_span, mock_info, interceptor, mock_next_activity, mock_span
    ):
        """Test that tenant ID is set from correlation context."""
        mock_get_span.return_value = mock_span
        mock_info.return_value = MockActivityInfo()
        correlation_context.set({"atlan-tenant-id": "tenant-abc"})
        input_data = MockExecuteActivityInput()

        await interceptor.execute_activity(input_data)

        mock_span.set_attribute.assert_any_call("tenant.id", "tenant-abc")

    @pytest.mark.asyncio
    @patch("temporalio.activity.info")
    @patch("opentelemetry.trace.get_current_span")
    async def test_handles_none_correlation_context(
        self, mock_get_span, mock_info, interceptor, mock_next_activity, mock_span
    ):
        """Test that None correlation context is handled gracefully."""
        mock_get_span.return_value = mock_span
        mock_info.return_value = MockActivityInfo()
        correlation_context.set(None)
        input_data = MockExecuteActivityInput()

        await interceptor.execute_activity(input_data)

        tenant_calls = [
            call
            for call in mock_span.set_attribute.call_args_list
            if call[0][0] == "tenant.id"
        ]
        assert len(tenant_calls) == 0
        mock_next_activity.execute_activity.assert_called_once()

    @pytest.mark.asyncio
    @patch("temporalio.activity.info")
    @patch("opentelemetry.trace.get_current_span")
    async def test_handles_empty_tenant_id(
        self, mock_get_span, mock_info, interceptor, mock_next_activity, mock_span
    ):
        """Test that empty tenant ID is not set on span."""
        mock_get_span.return_value = mock_span
        mock_info.return_value = MockActivityInfo()
        correlation_context.set({"atlan-tenant-id": ""})
        input_data = MockExecuteActivityInput()

        await interceptor.execute_activity(input_data)

        tenant_calls = [
            call
            for call in mock_span.set_attribute.call_args_list
            if call[0][0] == "tenant.id"
        ]
        assert len(tenant_calls) == 0

    @pytest.mark.asyncio
    @patch("temporalio.activity.info")
    @patch("opentelemetry.trace.get_current_span")
    async def test_handles_no_timeouts(
        self, mock_get_span, mock_info, interceptor, mock_next_activity, mock_span
    ):
        """Test that missing timeout attributes are not set."""
        mock_get_span.return_value = mock_span
        mock_info.return_value = MockActivityInfo(
            schedule_to_close_timeout=None,
            start_to_close_timeout=None,
            heartbeat_timeout=None,
        )
        correlation_context.set(None)
        input_data = MockExecuteActivityInput()

        await interceptor.execute_activity(input_data)

        timeout_calls = [
            call
            for call in mock_span.set_attribute.call_args_list
            if "timeout" in call[0][0]
        ]
        assert len(timeout_calls) == 0

    @pytest.mark.asyncio
    @patch("traceback.format_exc")
    @patch("temporalio.activity.info")
    @patch("opentelemetry.trace.get_current_span")
    async def test_records_exception_on_failure(
        self,
        mock_get_span,
        mock_info,
        mock_format_exc,
        interceptor,
        mock_next_activity,
        mock_span,
    ):
        """Test that exception details are recorded on activity failure."""
        mock_get_span.return_value = mock_span
        mock_info.return_value = MockActivityInfo()
        correlation_context.set(None)
        mock_format_exc.return_value = "Traceback (most recent call last):\n  File..."

        error = ConnectionRefusedError("Connection refused")
        mock_next_activity.execute_activity = AsyncMock(side_effect=error)
        input_data = MockExecuteActivityInput()

        with pytest.raises(ConnectionRefusedError):
            await interceptor.execute_activity(input_data)

        mock_span.set_attribute.assert_any_call(
            "exception.type", "ConnectionRefusedError"
        )
        mock_span.set_attribute.assert_any_call(
            "exception.message", "Connection refused"
        )
        mock_span.set_attribute.assert_any_call(
            "exception.stacktrace", "Traceback (most recent call last):\n  File..."
        )
        mock_span.record_exception.assert_called_once_with(error)

    @pytest.mark.asyncio
    @patch("traceback.format_exc")
    @patch("temporalio.activity.info")
    @patch("opentelemetry.trace.get_current_span")
    async def test_re_raises_exception(
        self,
        mock_get_span,
        mock_info,
        mock_format_exc,
        interceptor,
        mock_next_activity,
        mock_span,
    ):
        """Test that the original exception is re-raised after enrichment."""
        mock_get_span.return_value = mock_span
        mock_info.return_value = MockActivityInfo()
        correlation_context.set(None)
        mock_format_exc.return_value = "Traceback..."

        error = ValueError("Invalid input")
        mock_next_activity.execute_activity = AsyncMock(side_effect=error)
        input_data = MockExecuteActivityInput()

        with pytest.raises(ValueError, match="Invalid input"):
            await interceptor.execute_activity(input_data)

    @pytest.mark.asyncio
    @patch("temporalio.activity.info")
    @patch("opentelemetry.trace.get_current_span")
    async def test_skips_enrichment_when_no_span(
        self, mock_get_span, mock_info, interceptor, mock_next_activity
    ):
        """Test that enrichment is skipped when no span exists."""
        mock_get_span.return_value = None
        input_data = MockExecuteActivityInput()

        result = await interceptor.execute_activity(input_data)

        mock_info.assert_not_called()
        mock_next_activity.execute_activity.assert_called_once()
        assert result == "activity_result"

    @pytest.mark.asyncio
    @patch("temporalio.activity.info")
    @patch("opentelemetry.trace.get_current_span")
    async def test_skips_enrichment_when_span_not_recording(
        self,
        mock_get_span,
        mock_info,
        interceptor,
        mock_next_activity,
        mock_non_recording_span,
    ):
        """Test that enrichment is skipped when span is not recording."""
        mock_get_span.return_value = mock_non_recording_span
        input_data = MockExecuteActivityInput()

        result = await interceptor.execute_activity(input_data)

        mock_non_recording_span.set_attribute.assert_not_called()
        mock_next_activity.execute_activity.assert_called_once()
        assert result == "activity_result"

    @pytest.mark.asyncio
    async def test_handles_opentelemetry_import_error(self, mock_next_activity):
        """Test that missing opentelemetry is handled gracefully."""
        import sys
        from unittest.mock import MagicMock

        interceptor = OTelEnrichmentActivityInboundInterceptor(mock_next_activity)
        input_data = MockExecuteActivityInput()

        original_modules = sys.modules.copy()

        try:
            if "opentelemetry" in sys.modules:
                sys.modules["opentelemetry"] = MagicMock()
                sys.modules["opentelemetry.trace"] = MagicMock()

            result = await interceptor.execute_activity(input_data)
            assert result == "activity_result"
        finally:
            sys.modules.clear()
            sys.modules.update(original_modules)


class TestOTelEnrichmentInterceptor:
    """Tests for the main OTelEnrichmentInterceptor class."""

    @pytest.fixture
    def interceptor(self):
        """Create the main interceptor instance."""
        return OTelEnrichmentInterceptor()

    def test_intercept_activity_returns_correct_class(self, interceptor):
        """Test that intercept_activity returns OTelEnrichmentActivityInboundInterceptor."""
        mock_next = MagicMock()

        result = interceptor.intercept_activity(mock_next)

        assert isinstance(result, OTelEnrichmentActivityInboundInterceptor)

    def test_workflow_interceptor_class_returns_none(self, interceptor):
        """Test that workflow_interceptor_class returns None (activity-only)."""
        mock_input = MagicMock()

        result = interceptor.workflow_interceptor_class(mock_input)

        assert result is None
