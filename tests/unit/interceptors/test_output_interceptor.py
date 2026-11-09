"""Unit tests for the output interceptor.

Tests the OutputInterceptor and its components for collecting and merging
workflow outputs from activities.
"""

import asyncio
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence
from unittest import mock

import pytest

import application_sdk.outputs as outputs_module
from application_sdk.contracts.base import Output
from application_sdk.execution._temporal.interceptors.outputs import (
    OutputActivityInboundInterceptor,
    OutputInterceptor,
    OutputWorkflowInboundInterceptor,
)
from application_sdk.outputs import (
    Artifact,
    Metric,
    OutputCollector,
    _current_outputs,
    get_outputs,
)


class _TaskOutput(Output):
    """A task-style typed result."""

    rows: int = 0


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
        """Clean up ContextVar state before and after each test."""
        _current_outputs.set(None)
        yield
        _current_outputs.set(None)

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
    async def test_dict_result_carries_outputs(self, interceptor, mock_next_inbound):
        """Collected outputs are added to a dict result under their namespaces."""

        async def activity_with_outputs(input):
            outputs = get_outputs()
            outputs.add_metric(Metric(name="count", value=42))
            outputs.add_artifact(Artifact(name="dump", path="s3://b/dump"))
            return {"status": "ok"}

        mock_next_inbound.execute_activity.side_effect = activity_with_outputs

        result = await interceptor.execute_activity(MockExecuteActivityInput())

        assert result == {
            "status": "ok",
            "metrics": {"count": 42},
            "artifacts": {"dump": "s3://b/dump"},
        }

    @pytest.mark.asyncio
    async def test_output_model_carries_outputs(self, interceptor, mock_next_inbound):
        """An Output result carries outputs on its metrics/artifacts fields."""
        returned = _TaskOutput(rows=3)

        async def activity_with_outputs(input):
            get_outputs().add_metric(Metric(name="rows", value=3, display_name="Rows"))
            return returned

        mock_next_inbound.execute_activity.side_effect = activity_with_outputs

        result = await interceptor.execute_activity(MockExecuteActivityInput())

        assert isinstance(result, _TaskOutput)
        assert result.rows == 3
        assert result.metrics == {"rows": {"value": 3, "display_name": "Rows"}}
        assert result.artifacts is None
        # The activity's own object is not mutated.
        assert returned.metrics is None

    @pytest.mark.asyncio
    async def test_output_passthrough_does_not_carry_upstream_metrics(
        self, interceptor, mock_next_inbound
    ):
        """A task returning an upstream output unchanged must not re-carry its metrics.

        Otherwise the workflow would count the upstream task's metrics twice.
        """
        upstream = _TaskOutput(rows=1, metrics={"rows": 1}, artifacts={"a": "p"})
        mock_next_inbound.execute_activity.return_value = upstream

        result = await interceptor.execute_activity(MockExecuteActivityInput())

        assert result.metrics is None
        assert result.artifacts is None
        assert result.rows == 1

    @pytest.mark.asyncio
    async def test_result_that_cannot_carry_outputs_is_unchanged(
        self, interceptor, mock_next_inbound
    ):
        """A None result is returned as-is; its outputs are dropped with a warning."""

        async def activity_with_outputs(input):
            get_outputs().add_metric(Metric(name="count", value=1))
            return None

        mock_next_inbound.execute_activity.side_effect = activity_with_outputs

        with (
            mock.patch(
                "application_sdk.execution._temporal.interceptors.outputs.activity"
            ) as mock_activity,
            mock.patch(
                "application_sdk.execution._temporal.interceptors.outputs.logger"
            ) as mock_logger,
        ):
            mock_activity.info.return_value = MockActivityInfo()
            result = await interceptor.execute_activity(MockExecuteActivityInput())

        assert result is None
        mock_logger.warning.assert_called_once()

    @pytest.mark.asyncio
    async def test_dict_without_outputs_is_unchanged(
        self, interceptor, mock_next_inbound
    ):
        """No outputs collected: a dict result keeps its own keys untouched."""
        own = {"metrics": "not ours"}
        mock_next_inbound.execute_activity.return_value = own

        result = await interceptor.execute_activity(MockExecuteActivityInput())

        assert result is own

    def test_no_process_level_store(self):
        """Outputs must not be held in process memory keyed by run id.

        The activity and its workflow can run in different worker processes,
        so a process-local store silently loses them.
        """
        assert not hasattr(outputs_module, "_collected_outputs")


class TestOutputWorkflowInboundInterceptor:
    """Tests for OutputWorkflowInboundInterceptor and its outbound harvest."""

    @pytest.fixture
    def mock_next_inbound(self):
        """Create a mock next inbound interceptor."""
        mock_next = mock.AsyncMock()
        mock_next.execute_workflow = mock.AsyncMock(
            return_value={"transformed_data_prefix": "s3://bucket/path"}
        )
        mock_next.init = mock.MagicMock()
        return mock_next

    @pytest.fixture
    def interceptor(self, mock_next_inbound):
        """Create the interceptor instance."""
        return OutputWorkflowInboundInterceptor(mock_next_inbound)

    @pytest.fixture
    def mock_next_outbound(self):
        """The outbound chain below the output interceptor."""
        return mock.MagicMock()

    @pytest.fixture
    def outbound(self, interceptor, mock_next_inbound, mock_next_outbound):
        """The outbound interceptor the workflow's activity calls go through."""
        interceptor.init(mock_next_outbound)
        return mock_next_inbound.init.call_args.args[0]

    @pytest.fixture(autouse=True)
    def cleanup_state(self):
        """Clean up ContextVar state before and after each test."""
        _current_outputs.set(None)
        yield
        _current_outputs.set(None)

    @staticmethod
    def _completed(result: Any) -> "asyncio.Future[Any]":
        """A handle that resolves on a later loop tick, as Temporal's does."""
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        loop.call_soon(fut.set_result, result)
        return fut

    @pytest.mark.asyncio
    async def test_returns_original_result_when_no_outputs(
        self, interceptor, mock_next_inbound
    ):
        """Test that original result is returned when no outputs collected."""
        expected_result = {"key": "value"}
        mock_next_inbound.execute_workflow.return_value = expected_result

        result = await interceptor.execute_workflow(MockExecuteWorkflowInput())

        assert result == expected_result

    @pytest.mark.asyncio
    async def test_merges_outputs_carried_on_activity_results(
        self, interceptor, outbound, mock_next_inbound, mock_next_outbound
    ):
        """Outputs carried on completed activity results reach the workflow result."""
        handles = [
            self._completed(_TaskOutput(rows=1, metrics={"count": 50})),
            self._completed(
                {"x": 1, "metrics": {"count": 30, "other": 10}, "artifacts": {"a": "p"}}
            ),
        ]
        mock_next_outbound.start_activity.side_effect = handles

        async def run(input):
            for _ in handles:
                await outbound.start_activity(mock.MagicMock())
            return {"base": "data"}

        mock_next_inbound.execute_workflow.side_effect = run

        result = await interceptor.execute_workflow(MockExecuteWorkflowInput())

        assert result == {
            "base": "data",
            "metrics": {"count": 80, "other": 10},
            "artifacts": {"a": "p"},
        }

    @pytest.mark.asyncio
    async def test_handle_already_done_when_returned_is_still_merged(
        self, interceptor, outbound, mock_next_inbound, mock_next_outbound
    ):
        """A done handle's callback is only queued; the exit flush still merges it."""
        fut = asyncio.get_running_loop().create_future()
        fut.set_result(_TaskOutput(metrics={"early": 1}))
        mock_next_outbound.start_activity.return_value = fut

        async def run(input):
            await outbound.start_activity(mock.MagicMock())
            return {}

        mock_next_inbound.execute_workflow.side_effect = run

        result = await interceptor.execute_workflow(MockExecuteWorkflowInput())

        assert result == {"metrics": {"early": 1}}

    @pytest.mark.asyncio
    async def test_returns_the_same_handle(self, outbound, mock_next_outbound):
        """start_activity keeps returning Temporal's cancellable handle."""
        handle = self._completed(None)
        mock_next_outbound.start_activity.return_value = handle

        assert outbound.start_activity(mock.MagicMock()) is handle

    @pytest.mark.asyncio
    async def test_harvests_local_activities(
        self, interceptor, outbound, mock_next_inbound, mock_next_outbound
    ):
        """Local activity results carry outputs the same way."""
        mock_next_outbound.start_local_activity.return_value = self._completed(
            _TaskOutput(metrics={"local": 2})
        )

        async def run(input):
            await outbound.start_local_activity(mock.MagicMock())
            return {}

        mock_next_inbound.execute_workflow.side_effect = run

        result = await interceptor.execute_workflow(MockExecuteWorkflowInput())

        assert result == {"metrics": {"local": 2}}

    @pytest.mark.asyncio
    async def test_harvests_through_a_coroutine_returning_outbound(
        self, interceptor, outbound, mock_next_inbound, mock_next_outbound
    ):
        """An outbound below that returns a coroutine (the lock interceptor) still works."""

        async def locked_start(input):
            return _TaskOutput(metrics={"locked": 1})

        mock_next_outbound.start_activity.side_effect = locked_start

        async def run(input):
            result = await outbound.start_activity(mock.MagicMock())
            assert result.metrics == {"locked": 1}
            return {}

        mock_next_inbound.execute_workflow.side_effect = run

        result = await interceptor.execute_workflow(MockExecuteWorkflowInput())

        assert result == {"metrics": {"locked": 1}}

    @pytest.mark.asyncio
    async def test_failed_activity_contributes_nothing(
        self, interceptor, outbound, mock_next_inbound, mock_next_outbound
    ):
        """A failed activity's handle is ignored and its error still propagates."""
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        loop.call_soon(fut.set_exception, RuntimeError("boom"))
        mock_next_outbound.start_activity.return_value = fut

        async def run(input):
            with pytest.raises(RuntimeError):
                await outbound.start_activity(mock.MagicMock())
            return {"status": "recovered"}

        mock_next_inbound.execute_workflow.side_effect = run

        result = await interceptor.execute_workflow(MockExecuteWorkflowInput())

        assert result == {"status": "recovered"}

    @pytest.mark.asyncio
    async def test_ignores_keys_not_in_the_carried_shape(
        self, interceptor, outbound, mock_next_inbound, mock_next_outbound
    ):
        """A dict result whose own ``metrics`` key is not a mapping is not harvested."""
        mock_next_outbound.start_activity.return_value = self._completed(
            {"metrics": "a string of its own", "artifacts": ["x"]}
        )

        async def run(input):
            await outbound.start_activity(mock.MagicMock())
            return {"status": "done"}

        mock_next_inbound.execute_workflow.side_effect = run

        result = await interceptor.execute_workflow(MockExecuteWorkflowInput())

        assert result == {"status": "done"}

    @pytest.mark.asyncio
    async def test_activity_result_crosses_processes(
        self, interceptor, outbound, mock_next_inbound, mock_next_outbound
    ):
        """End to end at unit level: one activity interceptor, one workflow interceptor.

        They share nothing but the activity result, as when the activity runs
        in another worker process.
        """

        async def activity_body(input):
            get_outputs().add_metric(Metric(name="tables", value=100))
            return _TaskOutput(rows=100)

        activity_next = mock.AsyncMock()
        activity_next.execute_activity.side_effect = activity_body
        carried = await OutputActivityInboundInterceptor(
            activity_next
        ).execute_activity(MockExecuteActivityInput())

        mock_next_outbound.start_activity.return_value = self._completed(carried)

        async def run(input):
            await outbound.start_activity(mock.MagicMock())
            return {"connection_qualified_name": "default/redshift/prod"}

        mock_next_inbound.execute_workflow.side_effect = run

        result = await interceptor.execute_workflow(MockExecuteWorkflowInput())

        assert result["connection_qualified_name"] == "default/redshift/prod"
        assert result["metrics"] == {"tables": 100}

    @pytest.mark.asyncio
    async def test_handles_non_dict_result_with_outputs(
        self, interceptor, mock_next_inbound
    ):
        """Test handling when workflow returns non-dict but outputs exist."""

        async def run(input):
            get_outputs().add_metric(Metric(name="m", value=1))
            return "not a dict"

        mock_next_inbound.execute_workflow.side_effect = run

        result = await interceptor.execute_workflow(MockExecuteWorkflowInput())

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

        result = await interceptor.execute_workflow(MockExecuteWorkflowInput())

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
