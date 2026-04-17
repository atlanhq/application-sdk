"""Unit tests for the output interceptor.

Tests the OutputInterceptor and its components for collecting and merging
workflow outputs from activities.
"""

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence
from unittest import mock

import pytest

from application_sdk.execution._temporal.interceptors.outputs import (
    OutputActivityInboundInterceptor,
    OutputInterceptor,
    OutputWorkflowInboundInterceptor,
)
from application_sdk.outputs import (
    Metric,
    OutputCollector,
    _collected_outputs,
    _current_outputs,
    _lock,
    get_outputs,
)


@dataclass
class MockExecuteWorkflowInput:
    """Mock ExecuteWorkflowInput for testing."""

    args: Sequence[Any] = field(default_factory=list)
    headers: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class MockExecuteActivityInput:
    """Mock ExecuteActivityInput for testing."""

    fn: Any = None
    args: Sequence[Any] = field(default_factory=list)
    headers: Mapping[str, Any] = field(default_factory=dict)
    executor: Any = None


@dataclass
class MockActivityInfo:
    """Mock activity.info() return value."""

    workflow_run_id: str = "test-workflow-run-id"
    activity_type: str = "test_activity"
    activity_id: str = "activity-123"


@dataclass
class MockWorkflowInfo:
    """Mock workflow.info() return value."""

    run_id: str = "test-workflow-run-id"
    workflow_id: str = "test-workflow-id"
    workflow_type: str = "TestWorkflow"


class TestOutputActivityInboundInterceptor:
    """Tests for OutputActivityInboundInterceptor."""

    @pytest.fixture
    def mock_next_inbound(self):
        """Create a mock next inbound interceptor."""
        mock_next = mock.AsyncMock()
        mock_next.execute_activity = mock.AsyncMock(return_value={"status": "success"})
        return mock_next

    @pytest.fixture
    def interceptor(self, mock_next_inbound):
        """Create the interceptor instance."""
        return OutputActivityInboundInterceptor(mock_next_inbound)

    @pytest.fixture(autouse=True)
    def cleanup_state(self):
        """Clean up global state before and after each test."""
        _current_outputs.set(None)
        with _lock:
            _collected_outputs.clear()
        yield
        _current_outputs.set(None)
        with _lock:
            _collected_outputs.clear()

    @pytest.mark.asyncio
    async def test_creates_fresh_collector_for_activity(
        self, interceptor, mock_next_inbound
    ):
        """Test that a fresh collector is created for each activity.

        The collector is set on _current_outputs during execution and reset
        to None afterwards (try/finally cleanup). Verify it exists during
        the activity via a side effect on the mock.
        """
        collector_during_activity: list = []

        async def capture_collector(input):
            collector_during_activity.append(_current_outputs.get())
            return {"status": "success"}

        mock_next_inbound.execute_activity.side_effect = capture_collector
        input_data = MockExecuteActivityInput()

        with mock.patch(
            "application_sdk.execution._temporal.interceptors.outputs.activity"
        ) as mock_activity:
            mock_activity.info.return_value = MockActivityInfo()
            await interceptor.execute_activity(input_data)

        # Collector was set during execution
        assert len(collector_during_activity) == 1
        assert collector_during_activity[0] is not None
        assert isinstance(collector_during_activity[0], OutputCollector)
        # Collector is reset to None after execution (try/finally cleanup)
        assert _current_outputs.get() is None

    @pytest.mark.asyncio
    async def test_returns_original_activity_result(
        self, interceptor, mock_next_inbound
    ):
        """Test that the original activity result is returned unchanged."""
        expected_result = {"status": "success", "data": [1, 2, 3]}
        mock_next_inbound.execute_activity.return_value = expected_result
        input_data = MockExecuteActivityInput()

        with mock.patch(
            "application_sdk.execution._temporal.interceptors.outputs.activity"
        ) as mock_activity:
            mock_activity.info.return_value = MockActivityInfo()
            result = await interceptor.execute_activity(input_data)

        assert result == expected_result

    @pytest.mark.asyncio
    async def test_stashes_collector_when_has_data(
        self, interceptor, mock_next_inbound
    ):
        """Test that collector is stashed when it has data."""
        workflow_run_id = "test-run-id-123"

        async def activity_with_outputs(input):
            outputs = get_outputs()
            outputs.add_metric(Metric(name="count", value=42))
            return {"status": "ok"}

        mock_next_inbound.execute_activity.side_effect = activity_with_outputs
        input_data = MockExecuteActivityInput()

        with mock.patch(
            "application_sdk.execution._temporal.interceptors.outputs.activity"
        ) as mock_activity:
            mock_activity.info.return_value = MockActivityInfo(
                workflow_run_id=workflow_run_id
            )
            await interceptor.execute_activity(input_data)

        with _lock:
            stashed = _collected_outputs.get(workflow_run_id, [])
        assert len(stashed) == 1
        assert stashed[0].has_data()

    @pytest.mark.asyncio
    async def test_does_not_stash_empty_collector(self, interceptor, mock_next_inbound):
        """Test that empty collectors are not stashed."""
        workflow_run_id = "test-run-id-empty"

        with mock.patch(
            "application_sdk.execution._temporal.interceptors.outputs.activity"
        ) as mock_activity:
            mock_activity.info.return_value = MockActivityInfo(
                workflow_run_id=workflow_run_id
            )
            await interceptor.execute_activity(MockExecuteActivityInput())

        with _lock:
            stashed = _collected_outputs.get(workflow_run_id, [])
        assert len(stashed) == 0


class TestOutputWorkflowInboundInterceptor:
    """Tests for OutputWorkflowInboundInterceptor."""

    @pytest.fixture
    def mock_next_inbound(self):
        """Create a mock next inbound interceptor."""
        mock_next = mock.AsyncMock()
        mock_next.execute_workflow = mock.AsyncMock(
            return_value={"transformed_data_prefix": "s3://bucket/path"}
        )
        return mock_next

    @pytest.fixture
    def interceptor(self, mock_next_inbound):
        """Create the interceptor instance."""
        return OutputWorkflowInboundInterceptor(mock_next_inbound)

    @pytest.fixture(autouse=True)
    def cleanup_state(self):
        """Clean up global state before and after each test."""
        _current_outputs.set(None)
        with _lock:
            _collected_outputs.clear()
        yield
        _current_outputs.set(None)
        with _lock:
            _collected_outputs.clear()

    @pytest.mark.asyncio
    async def test_returns_original_result_when_no_outputs(
        self, interceptor, mock_next_inbound
    ):
        """Test that original result is returned when no outputs collected."""
        expected_result = {"key": "value"}
        mock_next_inbound.execute_workflow.return_value = expected_result
        input_data = MockExecuteWorkflowInput()

        with mock.patch("application_sdk.execution._temporal.interceptors.outputs.workflow") as mock_wf:
            mock_wf.info.return_value = MockWorkflowInfo()
            result = await interceptor.execute_workflow(input_data)

        assert result == expected_result

    @pytest.mark.asyncio
    async def test_merges_activity_outputs_into_result(
        self, interceptor, mock_next_inbound
    ):
        """Test that activity outputs are merged into workflow result."""
        workflow_run_id = "test-merge-run-id"

        activity_collector = OutputCollector()
        activity_collector.add_metric(Metric(name="tables", value=100))
        with _lock:
            _collected_outputs[workflow_run_id].append(activity_collector)

        mock_next_inbound.execute_workflow.return_value = {
            "connection_qualified_name": "default/redshift/prod"
        }
        input_data = MockExecuteWorkflowInput()

        with mock.patch("application_sdk.execution._temporal.interceptors.outputs.workflow") as mock_wf:
            mock_wf.info.return_value = MockWorkflowInfo(run_id=workflow_run_id)
            result = await interceptor.execute_workflow(input_data)

        assert result["connection_qualified_name"] == "default/redshift/prod"
        assert result["metrics"] == {"tables": 100}

    @pytest.mark.asyncio
    async def test_merges_multiple_activity_outputs(
        self, interceptor, mock_next_inbound
    ):
        """Test that multiple activity outputs are merged correctly."""
        workflow_run_id = "test-multi-activity-run-id"

        collector1 = OutputCollector()
        collector1.add_metric(Metric(name="count", value=50))

        collector2 = OutputCollector()
        collector2.add_metric(Metric(name="count", value=30))
        collector2.add_metric(Metric(name="other", value=10))

        with _lock:
            _collected_outputs[workflow_run_id].extend([collector1, collector2])

        mock_next_inbound.execute_workflow.return_value = {"base": "data"}
        input_data = MockExecuteWorkflowInput()

        with mock.patch("application_sdk.execution._temporal.interceptors.outputs.workflow") as mock_wf:
            mock_wf.info.return_value = MockWorkflowInfo(run_id=workflow_run_id)
            result = await interceptor.execute_workflow(input_data)

        assert result["metrics"]["count"] == 80
        assert result["metrics"]["other"] == 10
        assert result["base"] == "data"

    @pytest.mark.asyncio
    async def test_removes_collected_outputs_after_merge(
        self, interceptor, mock_next_inbound
    ):
        """Test that collected outputs are removed after workflow completion."""
        workflow_run_id = "test-cleanup-run-id"

        activity_collector = OutputCollector()
        activity_collector.add_metric(Metric(name="m", value=1))
        with _lock:
            _collected_outputs[workflow_run_id].append(activity_collector)

        input_data = MockExecuteWorkflowInput()

        with mock.patch("application_sdk.execution._temporal.interceptors.outputs.workflow") as mock_wf:
            mock_wf.info.return_value = MockWorkflowInfo(run_id=workflow_run_id)
            await interceptor.execute_workflow(input_data)

        with _lock:
            remaining = _collected_outputs.get(workflow_run_id)
        assert remaining is None or len(remaining) == 0

    @pytest.mark.asyncio
    async def test_handles_non_dict_result_with_outputs(
        self, interceptor, mock_next_inbound
    ):
        """Test handling when workflow returns non-dict but outputs exist."""
        workflow_run_id = "test-non-dict-run-id"

        activity_collector = OutputCollector()
        activity_collector.add_metric(Metric(name="m", value=1))
        with _lock:
            _collected_outputs[workflow_run_id].append(activity_collector)

        mock_next_inbound.execute_workflow.return_value = "not a dict"
        input_data = MockExecuteWorkflowInput()

        with mock.patch("application_sdk.execution._temporal.interceptors.outputs.workflow") as mock_wf:
            mock_wf.info.return_value = MockWorkflowInfo(run_id=workflow_run_id)
            result = await interceptor.execute_workflow(input_data)

        assert result == {"metrics": {"m": 1}}

    @pytest.mark.asyncio
    async def test_workflow_level_outputs_are_included(
        self, interceptor, mock_next_inbound
    ):
        """Test that workflow-level outputs are included."""

        async def workflow_with_outputs(input):
            outputs = get_outputs()
            outputs.add_metric(Metric(name="workflow-metric", value=999))
            return {"status": "done"}

        mock_next_inbound.execute_workflow.side_effect = workflow_with_outputs
        input_data = MockExecuteWorkflowInput()

        with mock.patch("application_sdk.execution._temporal.interceptors.outputs.workflow") as mock_wf:
            mock_wf.info.return_value = MockWorkflowInfo()
            result = await interceptor.execute_workflow(input_data)

        assert result["metrics"]["workflow-metric"] == 999
        assert result["status"] == "done"


class TestOutputInterceptor:
    """Tests for the main OutputInterceptor class."""

    @pytest.fixture
    def interceptor(self):
        """Create the main interceptor instance."""
        return OutputInterceptor()

    def test_returns_workflow_interceptor_class(self, interceptor):
        """Test that workflow_interceptor_class returns the correct class."""
        mock_input = mock.MagicMock()
        result = interceptor.workflow_interceptor_class(mock_input)
        assert result == OutputWorkflowInboundInterceptor

    def test_intercept_activity_wraps_next(self, interceptor):
        """Test that intercept_activity wraps the next interceptor."""
        mock_next = mock.MagicMock()
        result = interceptor.intercept_activity(mock_next)
        assert isinstance(result, OutputActivityInboundInterceptor)


class TestGetOutputsFunction:
    """Tests for the get_outputs() function."""

    @pytest.fixture(autouse=True)
    def cleanup_state(self):
        """Clean up ContextVar state before and after each test."""
        _current_outputs.set(None)
        yield
        _current_outputs.set(None)

    def test_creates_collector_when_none_exists(self):
        """Test that get_outputs creates a new collector when none exists."""
        collector = get_outputs()
        assert collector is not None
        assert isinstance(collector, OutputCollector)

    def test_returns_same_collector_on_repeated_calls(self):
        """Test that get_outputs returns the same collector."""
        collector1 = get_outputs()
        collector2 = get_outputs()
        assert collector1 is collector2

    def test_returns_existing_collector(self):
        """Test that get_outputs returns an existing collector."""
        existing = OutputCollector()
        existing.add_metric(Metric(name="test", value=123))
        _current_outputs.set(existing)

        collector = get_outputs()
        assert collector is existing
        assert collector.to_dict()["metrics"]["test"] == 123
