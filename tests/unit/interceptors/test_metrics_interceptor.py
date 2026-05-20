"""Unit tests for the MetricsInterceptor."""

from __future__ import annotations

from dataclasses import dataclass, field
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from application_sdk.errors.categories import Audience, FailureCategory
from application_sdk.errors.leaves import AuthError, DependencyUnavailableError
from application_sdk.execution._temporal.interceptors.metrics import (
    _ALERTABLE_AUDIENCES,
    _INSTRUMENTS,
    MetricsInterceptor,
    _activity_errors,
    _activity_executions,
    _MetricsActivityInboundInterceptor,
    _MetricsWorkflowInboundInterceptor,
    _workflow_executions,
    _workflow_failures_classified,
)

_METER_TARGET = (
    "application_sdk.execution._temporal.interceptors.metrics._otel_metrics.get_meter"
)

assert _ALERTABLE_AUDIENCES == {Audience.PLATFORM.value, Audience.APP_OWNER.value}


@dataclass
class MockWorkflowInfo:
    workflow_type: str = "TestWorkflow"
    workflow_id: str = "wf-id"
    task_queue: str = "default"
    namespace: str = "ns"
    run_id: str = "run-id"
    attempt: int = 1


@dataclass
class MockActivityInfo:
    activity_type: str = "TestActivity"
    activity_id: str = "act-id"
    task_queue: str = "default"
    workflow_id: str = "wf-id"
    workflow_run_id: str = "run-id"
    workflow_type: str = "TestWorkflow"
    attempt: int = 1


@dataclass
class MockExecuteWorkflowInput:
    headers: dict = field(default_factory=dict)
    args: list = field(default_factory=list)


@dataclass
class MockExecuteActivityInput:
    headers: dict = field(default_factory=dict)
    args: list = field(default_factory=list)


@pytest.fixture(autouse=True)
def reset_instruments():
    _INSTRUMENTS.clear()
    yield
    _INSTRUMENTS.clear()


@pytest.fixture
def mock_meter():
    counter = MagicMock()
    histogram = MagicMock()
    m = MagicMock()
    m.create_counter.return_value = counter
    m.create_histogram.return_value = histogram
    with patch(_METER_TARGET, return_value=m):
        yield m


class TestInstrumentCaching:
    def test_workflow_executions_cached(self, mock_meter):
        c1 = _workflow_executions()
        c2 = _workflow_executions()
        assert c1 is c2
        mock_meter.create_counter.assert_called_once()

    def test_activity_errors_cached(self, mock_meter):
        e1 = _activity_errors()
        e2 = _activity_errors()
        assert e1 is e2
        mock_meter.create_counter.assert_called_once()

    def test_activity_executions_separate_from_workflow_executions(self, mock_meter):
        _workflow_executions()
        _activity_executions()
        assert mock_meter.create_counter.call_count == 2

    def test_workflow_failures_classified_cached(self, mock_meter):
        c1 = _workflow_failures_classified()
        c2 = _workflow_failures_classified()
        assert c1 is c2
        mock_meter.create_counter.assert_called_once()


class TestMetricsWorkflowInboundInterceptor:
    @pytest.fixture
    def mock_next(self):
        n = AsyncMock()
        n.execute_workflow = AsyncMock(return_value="result")
        return n

    @pytest.fixture
    def interceptor(self, mock_next):
        return _MetricsWorkflowInboundInterceptor(mock_next)

    async def test_replay_is_noop(self, interceptor, mock_meter):
        with patch(
            "application_sdk.execution._temporal.interceptors.metrics.workflow"
        ) as mock_wf:
            mock_wf.unsafe.is_replaying.return_value = True
            await interceptor.execute_workflow(MockExecuteWorkflowInput())
        mock_meter.create_counter.assert_not_called()
        mock_meter.create_histogram.assert_not_called()

    async def test_success_increments_counter_with_ok_tag(
        self, interceptor, mock_meter
    ):
        with patch(
            "application_sdk.execution._temporal.interceptors.metrics.workflow"
        ) as mock_wf:
            mock_wf.unsafe.is_replaying.return_value = False
            mock_wf.info.return_value = MockWorkflowInfo()
            await interceptor.execute_workflow(MockExecuteWorkflowInput())

        counter = mock_meter.create_counter.return_value
        counter.add.assert_called_once()
        _, kwargs = counter.add.call_args
        tags = counter.add.call_args[0][1]
        assert tags["otel.status_code"] == "OK"
        assert tags["temporal.workflow.type"] == "TestWorkflow"

    async def test_success_records_duration_histogram(self, interceptor, mock_meter):
        with patch(
            "application_sdk.execution._temporal.interceptors.metrics.workflow"
        ) as mock_wf:
            mock_wf.unsafe.is_replaying.return_value = False
            mock_wf.info.return_value = MockWorkflowInfo()
            await interceptor.execute_workflow(MockExecuteWorkflowInput())

        histogram = mock_meter.create_histogram.return_value
        histogram.record.assert_called_once()
        duration_arg = histogram.record.call_args[0][0]
        assert isinstance(duration_arg, float)
        assert duration_arg >= 0

    async def test_error_increments_counter_with_error_tag_and_reraises(
        self, mock_next, mock_meter
    ):
        mock_next.execute_workflow = AsyncMock(side_effect=RuntimeError("boom"))
        interceptor = _MetricsWorkflowInboundInterceptor(mock_next)

        with patch(
            "application_sdk.execution._temporal.interceptors.metrics.workflow"
        ) as mock_wf:
            mock_wf.unsafe.is_replaying.return_value = False
            mock_wf.info.return_value = MockWorkflowInfo()
            with pytest.raises(RuntimeError, match="boom"):
                await interceptor.execute_workflow(MockExecuteWorkflowInput())

        # Executions counter still fires with ERROR; raw exc also routes
        # through the classified counter with INTERNAL / APP_OWNER fallback,
        # so create_counter is invoked twice (one per instrument).
        exec_calls = [
            c
            for c in mock_meter.create_counter.return_value.add.call_args_list
            if c[0][1].get("otel.status_code") == "ERROR"
        ]
        assert len(exec_calls) == 1


class TestWorkflowFailuresClassified:
    """Behaviour of `temporal.workflow.failures.classified` counter.

    The counter must fire only for APP_OWNER / PLATFORM audiences. USER
    failures (auth, permission, invalid input) are intentionally excluded
    so alerts driven off this counter stay actionable.
    """

    @pytest.fixture
    def split_counters(self, mock_meter):
        """Return (exec, classified) counters so emits are distinguishable."""
        exec_counter = MagicMock()
        classified_counter = MagicMock()
        histogram = MagicMock()

        def make_counter(name, **kwargs):
            if "failures.classified" in name:
                return classified_counter
            return exec_counter

        mock_meter.create_counter.side_effect = make_counter
        mock_meter.create_histogram.return_value = histogram
        return exec_counter, classified_counter

    async def _run_failing_workflow(self, exc: BaseException, split_counters):
        mock_next = AsyncMock()
        mock_next.execute_workflow = AsyncMock(side_effect=exc)
        interceptor = _MetricsWorkflowInboundInterceptor(mock_next)
        with patch(
            "application_sdk.execution._temporal.interceptors.metrics.workflow"
        ) as mock_wf:
            mock_wf.unsafe.is_replaying.return_value = False
            mock_wf.info.return_value = MockWorkflowInfo()
            with pytest.raises(type(exc)):
                await interceptor.execute_workflow(MockExecuteWorkflowInput())

    async def test_app_owner_audience_fires_with_correct_labels(self, split_counters):
        _, classified = split_counters
        # Raw RuntimeError → fallback INTERNAL / APP_OWNER.
        await self._run_failing_workflow(RuntimeError("boom"), split_counters)

        classified.add.assert_called_once()
        tags = classified.add.call_args[0][1]
        assert tags["temporal.workflow.type"] == "TestWorkflow"
        assert tags["failure.category"] == FailureCategory.INTERNAL.value
        assert tags["failure.audience"] == Audience.APP_OWNER.value

    async def test_platform_audience_fires_with_dependency_unavailable(
        self, split_counters
    ):
        _, classified = split_counters
        exc = DependencyUnavailableError(message="dapr down")
        await self._run_failing_workflow(exc, split_counters)

        classified.add.assert_called_once()
        tags = classified.add.call_args[0][1]
        assert tags["failure.category"] == FailureCategory.DEPENDENCY_UNAVAILABLE.value
        assert tags["failure.audience"] == Audience.PLATFORM.value

    async def test_user_audience_does_not_fire(self, split_counters):
        _, classified = split_counters
        exc = AuthError(message="bad creds")
        await self._run_failing_workflow(exc, split_counters)

        classified.add.assert_not_called()

    async def test_success_does_not_fire(self, split_counters):
        _, classified = split_counters
        mock_next = AsyncMock()
        mock_next.execute_workflow = AsyncMock(return_value="ok")
        interceptor = _MetricsWorkflowInboundInterceptor(mock_next)
        with patch(
            "application_sdk.execution._temporal.interceptors.metrics.workflow"
        ) as mock_wf:
            mock_wf.unsafe.is_replaying.return_value = False
            mock_wf.info.return_value = MockWorkflowInfo()
            await interceptor.execute_workflow(MockExecuteWorkflowInput())

        classified.add.assert_not_called()

    async def test_replay_does_not_fire(self, split_counters):
        _, classified = split_counters
        mock_next = AsyncMock()
        mock_next.execute_workflow = AsyncMock(side_effect=RuntimeError("boom"))
        interceptor = _MetricsWorkflowInboundInterceptor(mock_next)
        with patch(
            "application_sdk.execution._temporal.interceptors.metrics.workflow"
        ) as mock_wf:
            mock_wf.unsafe.is_replaying.return_value = True
            # Under replay the interceptor delegates straight through without
            # touching counters; the underlying exc still propagates.
            with pytest.raises(RuntimeError, match="boom"):
                await interceptor.execute_workflow(MockExecuteWorkflowInput())

        classified.add.assert_not_called()

    async def test_raw_exception_falls_back_to_internal_app_owner(self, split_counters):
        _, classified = split_counters
        await self._run_failing_workflow(ValueError("oops"), split_counters)

        classified.add.assert_called_once()
        tags = classified.add.call_args[0][1]
        assert tags["failure.category"] == FailureCategory.INTERNAL.value
        assert tags["failure.audience"] == Audience.APP_OWNER.value

    async def test_classified_emit_failure_is_silent(self, mock_meter):
        """Metric exceptions must not propagate into workflow execution."""
        exec_counter = MagicMock()
        classified_counter = MagicMock()
        classified_counter.add.side_effect = RuntimeError("metrics broken")
        histogram = MagicMock()

        def make_counter(name, **kwargs):
            if "failures.classified" in name:
                return classified_counter
            return exec_counter

        mock_meter.create_counter.side_effect = make_counter
        mock_meter.create_histogram.return_value = histogram

        mock_next = AsyncMock()
        mock_next.execute_workflow = AsyncMock(side_effect=RuntimeError("boom"))
        interceptor = _MetricsWorkflowInboundInterceptor(mock_next)
        with patch(
            "application_sdk.execution._temporal.interceptors.metrics.workflow"
        ) as mock_wf:
            mock_wf.unsafe.is_replaying.return_value = False
            mock_wf.info.return_value = MockWorkflowInfo()
            with pytest.raises(RuntimeError, match="boom"):
                await interceptor.execute_workflow(MockExecuteWorkflowInput())


class TestMetricsActivityInboundInterceptor:
    @pytest.fixture
    def mock_next(self):
        n = AsyncMock()
        n.execute_activity = AsyncMock(return_value="ok")
        return n

    @pytest.fixture
    def interceptor(self, mock_next):
        return _MetricsActivityInboundInterceptor(mock_next)

    async def test_success_increments_activity_counter_ok(
        self, interceptor, mock_meter
    ):
        with patch(
            "application_sdk.execution._temporal.interceptors.metrics.activity"
        ) as mock_act:
            mock_act.info.return_value = MockActivityInfo()
            await interceptor.execute_activity(MockExecuteActivityInput())

        counter = mock_meter.create_counter.return_value
        counter.add.assert_called_once()
        tags = counter.add.call_args[0][1]
        assert tags["otel.status_code"] == "OK"
        assert tags["temporal.activity.type"] == "TestActivity"
        assert tags["temporal.task_queue"] == "default"

    async def test_success_records_activity_duration(self, interceptor, mock_meter):
        with patch(
            "application_sdk.execution._temporal.interceptors.metrics.activity"
        ) as mock_act:
            mock_act.info.return_value = MockActivityInfo()
            await interceptor.execute_activity(MockExecuteActivityInput())

        histogram = mock_meter.create_histogram.return_value
        histogram.record.assert_called_once()
        duration_arg = histogram.record.call_args[0][0]
        assert isinstance(duration_arg, float)
        assert duration_arg >= 0

    async def test_error_increments_counter_error_tag_and_reraises(
        self, mock_next, mock_meter
    ):
        mock_next.execute_activity = AsyncMock(side_effect=ValueError("bad"))
        interceptor = _MetricsActivityInboundInterceptor(mock_next)

        with patch(
            "application_sdk.execution._temporal.interceptors.metrics.activity"
        ) as mock_act:
            mock_act.info.return_value = MockActivityInfo()
            with pytest.raises(ValueError, match="bad"):
                await interceptor.execute_activity(MockExecuteActivityInput())

        exec_counter_calls = [
            c
            for c in mock_meter.create_counter.return_value.add.call_args_list
            if c[0][1].get("otel.status_code") == "ERROR"
        ]
        assert len(exec_counter_calls) >= 1

    async def test_error_increments_errors_counter_with_exception_type(
        self, mock_next, mock_meter
    ):
        exec_counter = MagicMock()
        errors_counter = MagicMock()
        histogram = MagicMock()

        def make_counter(name, **kwargs):
            if "errors" in name:
                return errors_counter
            return exec_counter

        mock_meter.create_counter.side_effect = make_counter
        mock_meter.create_histogram.return_value = histogram

        mock_next.execute_activity = AsyncMock(side_effect=ValueError("bad"))
        interceptor = _MetricsActivityInboundInterceptor(mock_next)

        with patch(
            "application_sdk.execution._temporal.interceptors.metrics.activity"
        ) as mock_act:
            mock_act.info.return_value = MockActivityInfo()
            with pytest.raises(ValueError):
                await interceptor.execute_activity(MockExecuteActivityInput())

        errors_counter.add.assert_called_once()
        err_tags = errors_counter.add.call_args[0][1]
        assert "exception.type" in err_tags
        assert err_tags["temporal.activity.type"] == "TestActivity"

    async def test_metric_exception_is_silent(self, mock_next, mock_meter):
        exec_counter = MagicMock()
        exec_counter.add.side_effect = RuntimeError("metrics broken")
        mock_meter.create_counter.return_value = exec_counter
        mock_meter.create_histogram.return_value = MagicMock()

        interceptor = _MetricsActivityInboundInterceptor(mock_next)

        with patch(
            "application_sdk.execution._temporal.interceptors.metrics.activity"
        ) as mock_act:
            mock_act.info.return_value = MockActivityInfo()
            result = await interceptor.execute_activity(MockExecuteActivityInput())

        assert result == "ok"


class TestMetricsInterceptor:
    def test_workflow_interceptor_class_returns_correct_type(self):
        interceptor = MetricsInterceptor()
        result = interceptor.workflow_interceptor_class(MagicMock())
        assert result is _MetricsWorkflowInboundInterceptor

    def test_intercept_activity_returns_metrics_inbound_interceptor(self):
        interceptor = MetricsInterceptor()
        mock_next = MagicMock()
        result = interceptor.intercept_activity(mock_next)
        assert isinstance(result, _MetricsActivityInboundInterceptor)
