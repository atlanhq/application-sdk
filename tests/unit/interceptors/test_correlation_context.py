"""Unit tests for the correlation context interceptor.

Tests the propagation of atlan-* correlation context fields from workflow
arguments to activities via Temporal headers.
"""

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence
from unittest import mock

import pytest
from temporalio.api.common.v1 import Payload
from temporalio.converter import default as default_converter

from application_sdk.interceptors.correlation_context import (
    ATLAN_HEADER_PREFIX,
    CorrelationContextActivityInboundInterceptor,
    CorrelationContextInterceptor,
    CorrelationContextOutboundInterceptor,
    CorrelationContextWorkflowInboundInterceptor,
)
from application_sdk.observability.context import correlation_context


@dataclass
class MockExecuteWorkflowInput:
    """Mock ExecuteWorkflowInput for testing."""

    args: Sequence[Any] = field(default_factory=list)
    headers: Mapping[str, Payload] = field(default_factory=dict)


@dataclass
class MockStartActivityInput:
    """Mock StartActivityInput for testing."""

    activity: str = "test_activity"
    args: Sequence[Any] = field(default_factory=list)
    headers: Mapping[str, Payload] = field(default_factory=dict)


@dataclass
class MockExecuteActivityInput:
    """Mock ExecuteActivityInput for testing."""

    fn: Any = None
    args: Sequence[Any] = field(default_factory=list)
    headers: Mapping[str, Payload] = field(default_factory=dict)
    executor: Any = None


class TestCorrelationContextWorkflowInboundInterceptor:
    """Tests for CorrelationContextWorkflowInboundInterceptor."""

    @pytest.fixture
    def mock_next_inbound(self):
        """Create a mock next inbound interceptor."""
        mock_next = mock.AsyncMock()
        mock_next.execute_workflow = mock.AsyncMock(return_value="workflow_result")
        return mock_next

    @pytest.fixture
    def interceptor(self, mock_next_inbound):
        """Create the interceptor instance."""
        return CorrelationContextWorkflowInboundInterceptor(mock_next_inbound)

    @pytest.mark.asyncio
    async def test_extracts_atlan_fields_from_workflow_args(
        self, interceptor, mock_next_inbound
    ):
        """Test that atlan-* fields are extracted from workflow config."""
        workflow_config = {
            "workflow_id": "test-workflow-123",
            "atlan-ignore": "redshift-test-1.41",
            "atlan-argo-workflow-id": "redshift-test-1.09",
            "atlan-argo-workflow-node": "redshift-test.1(0).(2).(3)",
            "other_field": "should_be_ignored",
        }
        input_data = MockExecuteWorkflowInput(args=[workflow_config])

        await interceptor.execute_workflow(input_data)

        # Verify correlation data was extracted
        assert interceptor.correlation_data == {
            "atlan-ignore": "redshift-test-1.41",
            "atlan-argo-workflow-id": "redshift-test-1.09",
            "atlan-argo-workflow-node": "redshift-test.1(0).(2).(3)",
        }

    @pytest.mark.asyncio
    async def test_extracts_trace_id_from_workflow_args(
        self, interceptor, mock_next_inbound
    ):
        """Test that trace_id is extracted from workflow config."""
        workflow_config = {
            "workflow_id": "test-workflow-123",
            "trace_id": "my-trace-id-abc",
            "atlan-ignore": "redshift-test-1.41",
            "other_field": "should_be_ignored",
        }
        input_data = MockExecuteWorkflowInput(args=[workflow_config])

        await interceptor.execute_workflow(input_data)

        # Verify trace_id was extracted along with atlan-* fields
        assert interceptor.correlation_data["trace_id"] == "my-trace-id-abc"
        assert interceptor.correlation_data["atlan-ignore"] == "redshift-test-1.41"

    @pytest.mark.asyncio
    async def test_handles_workflow_config_without_trace_id(
        self, interceptor, mock_next_inbound
    ):
        """Test workflow config without trace_id is handled gracefully."""
        workflow_config = {
            "workflow_id": "test-workflow-123",
            "atlan-ignore": "redshift-test-1.41",
        }
        input_data = MockExecuteWorkflowInput(args=[workflow_config])

        await interceptor.execute_workflow(input_data)

        # Verify trace_id is not in correlation data
        assert "trace_id" not in interceptor.correlation_data
        assert interceptor.correlation_data["atlan-ignore"] == "redshift-test-1.41"

    @pytest.mark.asyncio
    async def test_handles_empty_workflow_args(self, interceptor, mock_next_inbound):
        """Test that empty workflow args are handled gracefully."""
        input_data = MockExecuteWorkflowInput(args=[])

        await interceptor.execute_workflow(input_data)

        assert interceptor.correlation_data == {}

    @pytest.mark.asyncio
    async def test_handles_non_dict_workflow_args(self, interceptor, mock_next_inbound):
        """Test that non-dict workflow args are handled gracefully."""
        input_data = MockExecuteWorkflowInput(args=["not_a_dict"])

        await interceptor.execute_workflow(input_data)

        assert interceptor.correlation_data == {}

    @pytest.mark.asyncio
    async def test_handles_workflow_config_without_atlan_fields(
        self, interceptor, mock_next_inbound
    ):
        """Test workflow config without any atlan-* fields."""
        workflow_config = {
            "workflow_id": "test-workflow-123",
            "other_field": "value",
        }
        input_data = MockExecuteWorkflowInput(args=[workflow_config])

        await interceptor.execute_workflow(input_data)

        assert interceptor.correlation_data == {}

    @pytest.mark.asyncio
    async def test_filters_out_none_values(self, interceptor, mock_next_inbound):
        """Test that None values in atlan-* fields are filtered out."""
        workflow_config = {
            "atlan-ignore": "valid-value",
            "atlan-empty": None,
            "atlan-also-empty": "",
        }
        input_data = MockExecuteWorkflowInput(args=[workflow_config])

        await interceptor.execute_workflow(input_data)

        assert interceptor.correlation_data == {
            "atlan-ignore": "valid-value",
        }

    @pytest.mark.asyncio
    async def test_sets_correlation_context(self, interceptor, mock_next_inbound):
        """Test that correlation context is set for workflow-level logging."""
        workflow_config = {
            "atlan-ignore": "test-value",
        }
        input_data = MockExecuteWorkflowInput(args=[workflow_config])

        # Reset correlation context before test
        correlation_context.set({})

        await interceptor.execute_workflow(input_data)

        # Verify correlation context was set
        ctx = correlation_context.get()
        assert ctx == {"atlan-ignore": "test-value"}


class TestCorrelationContextOutboundInterceptor:
    """Tests for CorrelationContextOutboundInterceptor."""

    @pytest.fixture
    def mock_next_outbound(self):
        """Create a mock next outbound interceptor."""
        mock_next = mock.MagicMock()
        mock_next.start_activity = mock.MagicMock(return_value="activity_handle")
        return mock_next

    @pytest.fixture
    def mock_inbound(self):
        """Create a mock inbound interceptor with correlation data."""
        mock_inbound = mock.MagicMock()
        mock_inbound.correlation_data = {
            "atlan-ignore": "test-value",
            "atlan-argo-workflow-id": "workflow-123",
        }
        return mock_inbound

    @pytest.fixture
    def mock_inbound_with_trace_id(self):
        """Create a mock inbound interceptor with correlation data including trace_id."""
        mock_inbound = mock.MagicMock()
        mock_inbound.correlation_data = {
            "trace_id": "my-trace-id-123",
            "atlan-ignore": "test-value",
        }
        return mock_inbound

    @pytest.fixture
    def interceptor(self, mock_next_outbound, mock_inbound):
        """Create the outbound interceptor instance."""
        return CorrelationContextOutboundInterceptor(mock_next_outbound, mock_inbound)

    def test_injects_headers_into_activity_calls(
        self, interceptor, mock_next_outbound, mock_inbound
    ):
        """Test that atlan-* headers are injected into activity calls."""
        input_data = MockStartActivityInput(headers={})

        interceptor.start_activity(input_data)

        # Verify that start_activity was called
        mock_next_outbound.start_activity.assert_called_once()

        # Get the modified input
        called_input = mock_next_outbound.start_activity.call_args[0][0]

        # Verify headers were injected
        payload_converter = default_converter().payload_converter
        assert "atlan-ignore" in called_input.headers
        assert "atlan-argo-workflow-id" in called_input.headers

        # Verify payload values
        ignore_value = payload_converter.from_payload(
            called_input.headers["atlan-ignore"], type_hint=str
        )
        assert ignore_value == "test-value"

    def test_preserves_existing_headers(
        self, interceptor, mock_next_outbound, mock_inbound
    ):
        """Test that existing headers are preserved."""
        payload_converter = default_converter().payload_converter
        existing_payload = payload_converter.to_payload("existing-value")
        input_data = MockStartActivityInput(
            headers={"existing-header": existing_payload}
        )

        interceptor.start_activity(input_data)

        called_input = mock_next_outbound.start_activity.call_args[0][0]

        # Verify existing header is preserved
        assert "existing-header" in called_input.headers
        # Verify new headers were added
        assert "atlan-ignore" in called_input.headers

    def test_handles_empty_correlation_data(self, mock_next_outbound):
        """Test that empty correlation data is handled gracefully."""
        mock_inbound = mock.MagicMock()
        mock_inbound.correlation_data = {}
        interceptor = CorrelationContextOutboundInterceptor(
            mock_next_outbound, mock_inbound
        )

        input_data = MockStartActivityInput(headers={})

        interceptor.start_activity(input_data)

        mock_next_outbound.start_activity.assert_called_once()

    def test_injects_trace_id_into_activity_headers(
        self, mock_next_outbound, mock_inbound_with_trace_id
    ):
        """Test that trace_id is injected into activity headers."""
        interceptor = CorrelationContextOutboundInterceptor(
            mock_next_outbound, mock_inbound_with_trace_id
        )
        input_data = MockStartActivityInput(headers={})

        interceptor.start_activity(input_data)

        # Get the modified input
        called_input = mock_next_outbound.start_activity.call_args[0][0]

        # Verify trace_id was injected
        payload_converter = default_converter().payload_converter
        assert "trace_id" in called_input.headers
        assert "atlan-ignore" in called_input.headers

        # Verify trace_id payload value
        trace_id_value = payload_converter.from_payload(
            called_input.headers["trace_id"], type_hint=str
        )
        assert trace_id_value == "my-trace-id-123"


class TestCorrelationContextActivityInboundInterceptor:
    """Tests for CorrelationContextActivityInboundInterceptor."""

    @pytest.fixture
    def mock_next_activity(self):
        """Create a mock next activity interceptor."""
        mock_next = mock.AsyncMock()
        mock_next.execute_activity = mock.AsyncMock(return_value="activity_result")
        return mock_next

    @pytest.fixture
    def interceptor(self, mock_next_activity):
        """Create the activity interceptor instance."""
        return CorrelationContextActivityInboundInterceptor(mock_next_activity)

    @pytest.mark.asyncio
    async def test_extracts_headers_and_sets_context(
        self, interceptor, mock_next_activity
    ):
        """Test that atlan-* headers are extracted and correlation context is set."""
        payload_converter = default_converter().payload_converter

        headers = {
            "atlan-ignore": payload_converter.to_payload("test-value"),
            "atlan-argo-workflow-id": payload_converter.to_payload("workflow-123"),
            "other-header": payload_converter.to_payload("should-be-ignored"),
        }
        input_data = MockExecuteActivityInput(headers=headers)

        # Reset correlation context before test
        correlation_context.set({})

        await interceptor.execute_activity(input_data)

        # Verify correlation context was set with only atlan-* headers
        ctx = correlation_context.get()
        assert ctx == {
            "atlan-ignore": "test-value",
            "atlan-argo-workflow-id": "workflow-123",
        }

    @pytest.mark.asyncio
    async def test_extracts_trace_id_from_headers(
        self, interceptor, mock_next_activity
    ):
        """Test that trace_id is extracted from headers and set in correlation context."""
        payload_converter = default_converter().payload_converter

        headers = {
            "trace_id": payload_converter.to_payload("my-trace-id-456"),
            "atlan-ignore": payload_converter.to_payload("test-value"),
        }
        input_data = MockExecuteActivityInput(headers=headers)

        # Reset correlation context before test
        correlation_context.set({})

        await interceptor.execute_activity(input_data)

        # Verify trace_id was extracted and set in correlation context
        ctx = correlation_context.get()
        assert ctx["trace_id"] == "my-trace-id-456"
        assert ctx["atlan-ignore"] == "test-value"

    @pytest.mark.asyncio
    async def test_handles_empty_headers(self, interceptor, mock_next_activity):
        """Test that empty headers are handled gracefully."""
        input_data = MockExecuteActivityInput(headers={})

        # Reset correlation context before test
        correlation_context.set({})

        await interceptor.execute_activity(input_data)

        # Verify activity was still executed
        mock_next_activity.execute_activity.assert_called_once()

    @pytest.mark.asyncio
    async def test_calls_next_interceptor(self, interceptor, mock_next_activity):
        """Test that the next interceptor is always called."""
        input_data = MockExecuteActivityInput(headers={})

        result = await interceptor.execute_activity(input_data)

        mock_next_activity.execute_activity.assert_called_once_with(input_data)
        assert result == "activity_result"


class TestCorrelationContextInterceptor:
    """Tests for the main CorrelationContextInterceptor class."""

    @pytest.fixture
    def interceptor(self):
        """Create the main interceptor instance."""
        return CorrelationContextInterceptor()

    def test_returns_workflow_interceptor_class(self, interceptor):
        """Test that workflow_interceptor_class returns the correct class."""
        mock_input = mock.MagicMock()

        result = interceptor.workflow_interceptor_class(mock_input)

        assert result == CorrelationContextWorkflowInboundInterceptor

    def test_intercept_activity_wraps_next(self, interceptor):
        """Test that intercept_activity wraps the next interceptor."""
        mock_next = mock.MagicMock()

        result = interceptor.intercept_activity(mock_next)

        assert isinstance(result, CorrelationContextActivityInboundInterceptor)


class TestAtlanHeaderPrefix:
    """Tests for the ATLAN_HEADER_PREFIX constant."""

    def test_prefix_value(self):
        """Test that the prefix constant has the correct value."""
        assert ATLAN_HEADER_PREFIX == "atlan-"
