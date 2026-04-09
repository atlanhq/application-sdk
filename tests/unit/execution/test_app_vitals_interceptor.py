"""Unit tests for the AppVitalsInterceptor.

Tests cover all P0 data points:
- Workflow/activity completion events with status and error_type
- Duration histograms
- Activity schedule-to-start (queue wait)
- Retry detection (attempt > 1)
- Input/output size tracking
- Memory peak/delta tracking
- Common attributes on every metric
- Error classification integration
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock

import pytest

from application_sdk.execution._temporal.interceptors.app_vitals import (
    AppVitalsInterceptor,
    _activity_attributes,
    _AppVitalsActivityInterceptor,
    _AppVitalsWorkflowInterceptor,
    _common_attributes,
    _emit_metric,
    _workflow_attributes,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _noop_next_activity():
    n = mock.AsyncMock()
    n.execute_activity = mock.AsyncMock(return_value="activity_result")
    return n


def _failing_next_activity(exc: BaseException | None = None):
    n = mock.AsyncMock()
    n.execute_activity = mock.AsyncMock(side_effect=exc or ValueError("test error"))
    return n


def _noop_next_workflow():
    n = mock.AsyncMock()
    n.execute_workflow = mock.AsyncMock(return_value="workflow_result")
    return n


def _failing_next_workflow(exc: BaseException | None = None):
    n = mock.AsyncMock()
    n.execute_workflow = mock.AsyncMock(
        side_effect=exc or RuntimeError("workflow boom")
    )
    return n


def _mock_activity_info(**overrides):
    defaults = {
        "workflow_id": "wf-test-123",
        "workflow_run_id": "run-abc",
        "workflow_type": "TestWorkflow",
        "activity_id": "act-1",
        "activity_type": "fetch_data",
        "task_queue": "test-queue",
        "namespace": "default",
        "attempt": 1,
        "current_attempt_scheduled_time": datetime(
            2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc
        ),
        "schedule_to_close_timeout": None,
        "start_to_close_timeout": None,
        "heartbeat_timeout": None,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _mock_workflow_info(**overrides):
    defaults = {
        "workflow_id": "wf-test-123",
        "run_id": "run-abc",
        "workflow_type": "TestWorkflow",
        "namespace": "default",
        "task_queue": "test-queue",
        "attempt": 1,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


@pytest.fixture
def mock_env(monkeypatch):
    monkeypatch.setenv("ATLAN_APPLICATION_NAME", "test-connector")
    monkeypatch.setenv("ATLAN_TENANT_ID", "tenant-xyz")
    monkeypatch.setenv("ATLAN_APP_VERSION", "2.1.0")
    monkeypatch.setenv("HOSTNAME", "pod-abc-123")
    monkeypatch.setenv("ATLAN_COMMIT_SHA", "deadbeef")


@pytest.fixture
def captured_metrics():
    """Capture all _emit_metric calls for assertion."""
    calls: list[dict] = []

    def fake_emit(name, value, metric_type, labels, *, description="", unit=""):
        calls.append(
            {
                "name": name,
                "value": value,
                "type": metric_type,
                "labels": dict(labels),
                "description": description,
                "unit": unit,
            }
        )

    with mock.patch(
        "application_sdk.execution._temporal.interceptors.app_vitals._emit_metric",
        side_effect=fake_emit,
    ):
        yield calls


# ---------------------------------------------------------------------------
# Common attributes
# ---------------------------------------------------------------------------


class TestCommonAttributes:
    def test_includes_identity_fields(self, mock_env):
        with (
            mock.patch(
                "application_sdk.execution._temporal.interceptors.app_vitals.APPLICATION_NAME",
                "test-connector",
            ),
            mock.patch(
                "application_sdk.execution._temporal.interceptors.app_vitals.APP_TENANT_ID",
                "tenant-xyz",
            ),
            mock.patch(
                "application_sdk.execution._temporal.interceptors.app_vitals.correlation_context"
            ) as mock_corr,
        ):
            mock_corr.get.return_value = None
            attrs = _common_attributes()

        assert attrs["app_name"] == "test-connector"
        assert attrs["app_version"] == "2.1.0"
        assert attrs["tenant_id"] == "tenant-xyz"
        assert attrs["worker_id"] == "pod-abc-123"
        assert attrs["commit_hash"] == "deadbeef"

    def test_correlation_context_overrides_tenant(self, mock_env):
        with mock.patch(
            "application_sdk.execution._temporal.interceptors.app_vitals.correlation_context"
        ) as mock_corr:
            mock_corr.get.return_value = {"atlan-tenant-id": "corr-tenant"}
            attrs = _common_attributes()

        assert attrs["tenant_id"] == "corr-tenant"


class TestActivityAttributes:
    def test_includes_temporal_context(self, mock_env):
        info = _mock_activity_info()
        with (
            mock.patch(
                "application_sdk.execution._temporal.interceptors.app_vitals.activity.info",
                return_value=info,
            ),
            mock.patch(
                "application_sdk.execution._temporal.interceptors.app_vitals.correlation_context"
            ) as mock_corr,
        ):
            mock_corr.get.return_value = None
            attrs = _activity_attributes()

        assert attrs["workflow_id"] == "wf-test-123"
        assert attrs["workflow_run_id"] == "run-abc"
        assert attrs["activity_id"] == "act-1"
        assert attrs["activity_type"] == "fetch_data"
        assert attrs["attempt"] == "1"


class TestWorkflowAttributes:
    def test_includes_temporal_context(self, mock_env):
        info = _mock_workflow_info()
        with (
            mock.patch(
                "application_sdk.execution._temporal.interceptors.app_vitals.workflow.info",
                return_value=info,
            ),
            mock.patch(
                "application_sdk.execution._temporal.interceptors.app_vitals.correlation_context"
            ) as mock_corr,
        ):
            mock_corr.get.return_value = None
            attrs = _workflow_attributes()

        assert attrs["workflow_id"] == "wf-test-123"
        assert attrs["workflow_run_id"] == "run-abc"
        assert attrs["workflow_type"] == "TestWorkflow"


# ---------------------------------------------------------------------------
# Activity interceptor
# ---------------------------------------------------------------------------


class TestActivityInterceptorSuccess:
    @pytest.mark.asyncio
    async def test_emits_completion_on_success(self, mock_env, captured_metrics):
        next_handler = _noop_next_activity()
        interceptor = _AppVitalsActivityInterceptor(next_handler)
        mock_input = mock.MagicMock()
        mock_input.args = [b"hello"]

        with (
            mock.patch(
                "application_sdk.execution._temporal.interceptors.app_vitals.activity.info",
                return_value=_mock_activity_info(),
            ),
            mock.patch(
                "application_sdk.execution._temporal.interceptors.app_vitals.correlation_context"
            ) as mock_corr,
        ):
            mock_corr.get.return_value = None
            result = await interceptor.execute_activity(mock_input)

        assert result == "activity_result"

        # Find the completion metric
        completions = [
            m for m in captured_metrics if m["name"] == "app_vitals.activity.completion"
        ]
        assert len(completions) == 1
        assert completions[0]["labels"]["status"] == "succeeded"
        assert "error_type" not in completions[0]["labels"]

    @pytest.mark.asyncio
    async def test_emits_duration(self, mock_env, captured_metrics):
        next_handler = _noop_next_activity()
        interceptor = _AppVitalsActivityInterceptor(next_handler)
        mock_input = mock.MagicMock()
        mock_input.args = []

        with (
            mock.patch(
                "application_sdk.execution._temporal.interceptors.app_vitals.activity.info",
                return_value=_mock_activity_info(),
            ),
            mock.patch(
                "application_sdk.execution._temporal.interceptors.app_vitals.correlation_context"
            ) as mock_corr,
        ):
            mock_corr.get.return_value = None
            await interceptor.execute_activity(mock_input)

        durations = [
            m
            for m in captured_metrics
            if m["name"] == "app_vitals.activity.duration_ms"
        ]
        assert len(durations) == 1
        assert durations[0]["value"] >= 0
        assert durations[0]["type"] == "histogram"
        assert durations[0]["unit"] == "ms"

    @pytest.mark.asyncio
    async def test_emits_schedule_to_start(self, mock_env, captured_metrics):
        next_handler = _noop_next_activity()
        interceptor = _AppVitalsActivityInterceptor(next_handler)
        mock_input = mock.MagicMock()
        mock_input.args = []

        with (
            mock.patch(
                "application_sdk.execution._temporal.interceptors.app_vitals.activity.info",
                return_value=_mock_activity_info(),
            ),
            mock.patch(
                "application_sdk.execution._temporal.interceptors.app_vitals.correlation_context"
            ) as mock_corr,
        ):
            mock_corr.get.return_value = None
            await interceptor.execute_activity(mock_input)

        queue_waits = [
            m
            for m in captured_metrics
            if m["name"] == "app_vitals.activity.schedule_to_start_ms"
        ]
        assert len(queue_waits) == 1
        assert queue_waits[0]["value"] >= 0
        assert queue_waits[0]["unit"] == "ms"

    @pytest.mark.asyncio
    async def test_emits_input_size(self, mock_env, captured_metrics):
        next_handler = _noop_next_activity()
        interceptor = _AppVitalsActivityInterceptor(next_handler)
        mock_input = mock.MagicMock()
        mock_input.args = ["some_input_data"]

        with (
            mock.patch(
                "application_sdk.execution._temporal.interceptors.app_vitals.activity.info",
                return_value=_mock_activity_info(),
            ),
            mock.patch(
                "application_sdk.execution._temporal.interceptors.app_vitals.correlation_context"
            ) as mock_corr,
        ):
            mock_corr.get.return_value = None
            await interceptor.execute_activity(mock_input)

        input_sizes = [
            m
            for m in captured_metrics
            if m["name"] == "app_vitals.activity.input_size_bytes"
        ]
        assert len(input_sizes) == 1
        assert input_sizes[0]["value"] > 0

    @pytest.mark.asyncio
    async def test_emits_output_size_on_success(self, mock_env, captured_metrics):
        next_handler = _noop_next_activity()
        interceptor = _AppVitalsActivityInterceptor(next_handler)
        mock_input = mock.MagicMock()
        mock_input.args = []

        with (
            mock.patch(
                "application_sdk.execution._temporal.interceptors.app_vitals.activity.info",
                return_value=_mock_activity_info(),
            ),
            mock.patch(
                "application_sdk.execution._temporal.interceptors.app_vitals.correlation_context"
            ) as mock_corr,
        ):
            mock_corr.get.return_value = None
            await interceptor.execute_activity(mock_input)

        output_sizes = [
            m
            for m in captured_metrics
            if m["name"] == "app_vitals.activity.output_size_bytes"
        ]
        assert len(output_sizes) == 1
        assert output_sizes[0]["value"] > 0

    @pytest.mark.asyncio
    async def test_emits_memory_peak(self, mock_env, captured_metrics):
        next_handler = _noop_next_activity()
        interceptor = _AppVitalsActivityInterceptor(next_handler)
        mock_input = mock.MagicMock()
        mock_input.args = []

        with (
            mock.patch(
                "application_sdk.execution._temporal.interceptors.app_vitals.activity.info",
                return_value=_mock_activity_info(),
            ),
            mock.patch(
                "application_sdk.execution._temporal.interceptors.app_vitals.correlation_context"
            ) as mock_corr,
        ):
            mock_corr.get.return_value = None
            await interceptor.execute_activity(mock_input)

        mem_peaks = [
            m
            for m in captured_metrics
            if m["name"] == "app_vitals.activity.memory_peak_bytes"
        ]
        assert len(mem_peaks) == 1
        assert mem_peaks[0]["value"] > 0


class TestActivityInterceptorFailure:
    @pytest.mark.asyncio
    async def test_emits_failed_status_with_error_type(
        self, mock_env, captured_metrics
    ):
        next_handler = _failing_next_activity(ValueError("bad config credential"))
        interceptor = _AppVitalsActivityInterceptor(next_handler)
        mock_input = mock.MagicMock()
        mock_input.args = []

        with (
            mock.patch(
                "application_sdk.execution._temporal.interceptors.app_vitals.activity.info",
                return_value=_mock_activity_info(),
            ),
            mock.patch(
                "application_sdk.execution._temporal.interceptors.app_vitals.correlation_context"
            ) as mock_corr,
        ):
            mock_corr.get.return_value = None
            with pytest.raises(ValueError, match="bad config credential"):
                await interceptor.execute_activity(mock_input)

        completions = [
            m for m in captured_metrics if m["name"] == "app_vitals.activity.completion"
        ]
        assert len(completions) == 1
        assert completions[0]["labels"]["status"] == "failed"
        # ValueError with "credential" in message → "config" error type
        assert completions[0]["labels"]["error_type"] == "config"

    @pytest.mark.asyncio
    async def test_classifies_app_bug(self, mock_env, captured_metrics):
        next_handler = _failing_next_activity(TypeError("NoneType has no len"))
        interceptor = _AppVitalsActivityInterceptor(next_handler)
        mock_input = mock.MagicMock()
        mock_input.args = []

        with (
            mock.patch(
                "application_sdk.execution._temporal.interceptors.app_vitals.activity.info",
                return_value=_mock_activity_info(),
            ),
            mock.patch(
                "application_sdk.execution._temporal.interceptors.app_vitals.correlation_context"
            ) as mock_corr,
        ):
            mock_corr.get.return_value = None
            with pytest.raises(TypeError):
                await interceptor.execute_activity(mock_input)

        completions = [
            m for m in captured_metrics if m["name"] == "app_vitals.activity.completion"
        ]
        assert completions[0]["labels"]["error_type"] == "app_bug"

    @pytest.mark.asyncio
    async def test_classifies_transient(self, mock_env, captured_metrics):
        next_handler = _failing_next_activity(ConnectionError("connection refused"))
        interceptor = _AppVitalsActivityInterceptor(next_handler)
        mock_input = mock.MagicMock()
        mock_input.args = []

        with (
            mock.patch(
                "application_sdk.execution._temporal.interceptors.app_vitals.activity.info",
                return_value=_mock_activity_info(),
            ),
            mock.patch(
                "application_sdk.execution._temporal.interceptors.app_vitals.correlation_context"
            ) as mock_corr,
        ):
            mock_corr.get.return_value = None
            with pytest.raises(ConnectionError):
                await interceptor.execute_activity(mock_input)

        completions = [
            m for m in captured_metrics if m["name"] == "app_vitals.activity.completion"
        ]
        assert completions[0]["labels"]["error_type"] == "transient"

    @pytest.mark.asyncio
    async def test_classifies_timeout(self, mock_env, captured_metrics):
        next_handler = _failing_next_activity(asyncio.TimeoutError())
        interceptor = _AppVitalsActivityInterceptor(next_handler)
        mock_input = mock.MagicMock()
        mock_input.args = []

        with (
            mock.patch(
                "application_sdk.execution._temporal.interceptors.app_vitals.activity.info",
                return_value=_mock_activity_info(),
            ),
            mock.patch(
                "application_sdk.execution._temporal.interceptors.app_vitals.correlation_context"
            ) as mock_corr,
        ):
            mock_corr.get.return_value = None
            with pytest.raises(asyncio.TimeoutError):
                await interceptor.execute_activity(mock_input)

        completions = [
            m for m in captured_metrics if m["name"] == "app_vitals.activity.completion"
        ]
        assert completions[0]["labels"]["error_type"] == "timeout"

    @pytest.mark.asyncio
    async def test_no_output_size_on_failure(self, mock_env, captured_metrics):
        next_handler = _failing_next_activity(RuntimeError("boom"))
        interceptor = _AppVitalsActivityInterceptor(next_handler)
        mock_input = mock.MagicMock()
        mock_input.args = []

        with (
            mock.patch(
                "application_sdk.execution._temporal.interceptors.app_vitals.activity.info",
                return_value=_mock_activity_info(),
            ),
            mock.patch(
                "application_sdk.execution._temporal.interceptors.app_vitals.correlation_context"
            ) as mock_corr,
        ):
            mock_corr.get.return_value = None
            with pytest.raises(RuntimeError):
                await interceptor.execute_activity(mock_input)

        output_sizes = [
            m
            for m in captured_metrics
            if m["name"] == "app_vitals.activity.output_size_bytes"
        ]
        assert len(output_sizes) == 0


class TestActivityRetryDetection:
    @pytest.mark.asyncio
    async def test_retry_emitted_when_attempt_gt_1(self, mock_env, captured_metrics):
        next_handler = _noop_next_activity()
        interceptor = _AppVitalsActivityInterceptor(next_handler)
        mock_input = mock.MagicMock()
        mock_input.args = []

        with (
            mock.patch(
                "application_sdk.execution._temporal.interceptors.app_vitals.activity.info",
                return_value=_mock_activity_info(attempt=3),
            ),
            mock.patch(
                "application_sdk.execution._temporal.interceptors.app_vitals.correlation_context"
            ) as mock_corr,
        ):
            mock_corr.get.return_value = None
            await interceptor.execute_activity(mock_input)

        retries = [
            m for m in captured_metrics if m["name"] == "app_vitals.activity.retry"
        ]
        assert len(retries) == 1

    @pytest.mark.asyncio
    async def test_no_retry_on_first_attempt(self, mock_env, captured_metrics):
        next_handler = _noop_next_activity()
        interceptor = _AppVitalsActivityInterceptor(next_handler)
        mock_input = mock.MagicMock()
        mock_input.args = []

        with (
            mock.patch(
                "application_sdk.execution._temporal.interceptors.app_vitals.activity.info",
                return_value=_mock_activity_info(attempt=1),
            ),
            mock.patch(
                "application_sdk.execution._temporal.interceptors.app_vitals.correlation_context"
            ) as mock_corr,
        ):
            mock_corr.get.return_value = None
            await interceptor.execute_activity(mock_input)

        retries = [
            m for m in captured_metrics if m["name"] == "app_vitals.activity.retry"
        ]
        assert len(retries) == 0


# ---------------------------------------------------------------------------
# Workflow interceptor
# ---------------------------------------------------------------------------


class TestWorkflowInterceptorSuccess:
    @pytest.mark.asyncio
    async def test_emits_completion_on_success(self, mock_env, captured_metrics):
        next_handler = _noop_next_workflow()
        interceptor = _AppVitalsWorkflowInterceptor(next_handler)
        mock_input = mock.MagicMock()

        _time_counter = [0.0]

        def fake_time():
            _time_counter[0] += 1.5
            return _time_counter[0]

        with (
            mock.patch(
                "application_sdk.execution._temporal.interceptors.app_vitals.workflow.info",
                return_value=_mock_workflow_info(),
            ),
            mock.patch(
                "application_sdk.execution._temporal.interceptors.app_vitals.workflow.time",
                side_effect=fake_time,
            ),
            mock.patch(
                "application_sdk.execution._temporal.interceptors.app_vitals.correlation_context"
            ) as mock_corr,
        ):
            mock_corr.get.return_value = None
            result = await interceptor.execute_workflow(mock_input)

        assert result == "workflow_result"

        completions = [
            m for m in captured_metrics if m["name"] == "app_vitals.workflow.completion"
        ]
        assert len(completions) == 1
        assert completions[0]["labels"]["status"] == "succeeded"
        assert "error_type" not in completions[0]["labels"]

    @pytest.mark.asyncio
    async def test_emits_duration(self, mock_env, captured_metrics):
        next_handler = _noop_next_workflow()
        interceptor = _AppVitalsWorkflowInterceptor(next_handler)
        mock_input = mock.MagicMock()

        _time_counter = [0.0]

        def fake_time():
            _time_counter[0] += 2.0
            return _time_counter[0]

        with (
            mock.patch(
                "application_sdk.execution._temporal.interceptors.app_vitals.workflow.info",
                return_value=_mock_workflow_info(),
            ),
            mock.patch(
                "application_sdk.execution._temporal.interceptors.app_vitals.workflow.time",
                side_effect=fake_time,
            ),
            mock.patch(
                "application_sdk.execution._temporal.interceptors.app_vitals.correlation_context"
            ) as mock_corr,
        ):
            mock_corr.get.return_value = None
            await interceptor.execute_workflow(mock_input)

        durations = [
            m
            for m in captured_metrics
            if m["name"] == "app_vitals.workflow.duration_ms"
        ]
        assert len(durations) == 1
        # Two calls to workflow.time(): start=2.0, end=4.0 → duration=2000ms
        assert durations[0]["value"] == 2000.0
        assert durations[0]["type"] == "histogram"


class TestWorkflowInterceptorFailure:
    @pytest.mark.asyncio
    async def test_emits_failed_with_error_type(self, mock_env, captured_metrics):
        next_handler = _failing_next_workflow(MemoryError("oomkilled"))
        interceptor = _AppVitalsWorkflowInterceptor(next_handler)
        mock_input = mock.MagicMock()

        _time_counter = [0.0]

        def fake_time():
            _time_counter[0] += 1.0
            return _time_counter[0]

        with (
            mock.patch(
                "application_sdk.execution._temporal.interceptors.app_vitals.workflow.info",
                return_value=_mock_workflow_info(),
            ),
            mock.patch(
                "application_sdk.execution._temporal.interceptors.app_vitals.workflow.time",
                side_effect=fake_time,
            ),
            mock.patch(
                "application_sdk.execution._temporal.interceptors.app_vitals.correlation_context"
            ) as mock_corr,
        ):
            mock_corr.get.return_value = None
            with pytest.raises(MemoryError):
                await interceptor.execute_workflow(mock_input)

        completions = [
            m for m in captured_metrics if m["name"] == "app_vitals.workflow.completion"
        ]
        assert len(completions) == 1
        assert completions[0]["labels"]["status"] == "failed"
        assert completions[0]["labels"]["error_type"] == "oom"

    @pytest.mark.asyncio
    async def test_still_emits_duration_on_failure(self, mock_env, captured_metrics):
        next_handler = _failing_next_workflow()
        interceptor = _AppVitalsWorkflowInterceptor(next_handler)
        mock_input = mock.MagicMock()

        _time_counter = [0.0]

        def fake_time():
            _time_counter[0] += 0.5
            return _time_counter[0]

        with (
            mock.patch(
                "application_sdk.execution._temporal.interceptors.app_vitals.workflow.info",
                return_value=_mock_workflow_info(),
            ),
            mock.patch(
                "application_sdk.execution._temporal.interceptors.app_vitals.workflow.time",
                side_effect=fake_time,
            ),
            mock.patch(
                "application_sdk.execution._temporal.interceptors.app_vitals.correlation_context"
            ) as mock_corr,
        ):
            mock_corr.get.return_value = None
            with pytest.raises(RuntimeError):
                await interceptor.execute_workflow(mock_input)

        durations = [
            m
            for m in captured_metrics
            if m["name"] == "app_vitals.workflow.duration_ms"
        ]
        assert len(durations) == 1
        assert durations[0]["value"] > 0


# ---------------------------------------------------------------------------
# Top-level interceptor class
# ---------------------------------------------------------------------------


class TestAppVitalsInterceptor:
    def test_returns_workflow_interceptor_class(self):
        interceptor = AppVitalsInterceptor()
        cls = interceptor.workflow_interceptor_class(mock.MagicMock())
        assert cls is _AppVitalsWorkflowInterceptor

    def test_wraps_activity_interceptor(self):
        interceptor = AppVitalsInterceptor()
        next_handler = mock.MagicMock()
        wrapped = interceptor.intercept_activity(next_handler)
        assert isinstance(wrapped, _AppVitalsActivityInterceptor)


# ---------------------------------------------------------------------------
# _emit_metric (fire-and-forget)
# ---------------------------------------------------------------------------


class TestEmitMetric:
    def test_swallows_errors(self):
        """_emit_metric never raises, even if get_metrics() fails."""
        with mock.patch(
            "application_sdk.observability.metrics_adaptor.get_metrics",
            side_effect=RuntimeError("metrics init failed"),
        ):
            # Should not raise
            _emit_metric("test.metric", 1.0, "counter", {})

    def test_calls_record_metric(self):
        mock_metrics = mock.MagicMock()
        with mock.patch(
            "application_sdk.observability.metrics_adaptor.get_metrics",
            return_value=mock_metrics,
        ):
            _emit_metric("test.metric", 42.0, "histogram", {"key": "val"}, unit="ms")

        mock_metrics.record_metric.assert_called_once()
        call_kwargs = mock_metrics.record_metric.call_args
        assert call_kwargs.kwargs["name"] == "test.metric"
        assert call_kwargs.kwargs["value"] == 42.0


# ---------------------------------------------------------------------------
# Error classifier integration
# ---------------------------------------------------------------------------


class TestErrorClassifierIntegration:
    @pytest.mark.asyncio
    async def test_upstream_error(self, mock_env, captured_metrics):
        next_handler = _failing_next_activity(
            RuntimeError("upstream 502 bad gateway from source system")
        )
        interceptor = _AppVitalsActivityInterceptor(next_handler)
        mock_input = mock.MagicMock()
        mock_input.args = []

        with (
            mock.patch(
                "application_sdk.execution._temporal.interceptors.app_vitals.activity.info",
                return_value=_mock_activity_info(),
            ),
            mock.patch(
                "application_sdk.execution._temporal.interceptors.app_vitals.correlation_context"
            ) as mock_corr,
        ):
            mock_corr.get.return_value = None
            with pytest.raises(RuntimeError):
                await interceptor.execute_activity(mock_input)

        completions = [
            m for m in captured_metrics if m["name"] == "app_vitals.activity.completion"
        ]
        assert completions[0]["labels"]["error_type"] == "upstream"

    @pytest.mark.asyncio
    async def test_oom_error(self, mock_env, captured_metrics):
        next_handler = _failing_next_activity(MemoryError("out of memory"))
        interceptor = _AppVitalsActivityInterceptor(next_handler)
        mock_input = mock.MagicMock()
        mock_input.args = []

        with (
            mock.patch(
                "application_sdk.execution._temporal.interceptors.app_vitals.activity.info",
                return_value=_mock_activity_info(),
            ),
            mock.patch(
                "application_sdk.execution._temporal.interceptors.app_vitals.correlation_context"
            ) as mock_corr,
        ):
            mock_corr.get.return_value = None
            with pytest.raises(MemoryError):
                await interceptor.execute_activity(mock_input)

        completions = [
            m for m in captured_metrics if m["name"] == "app_vitals.activity.completion"
        ]
        assert completions[0]["labels"]["error_type"] == "oom"

    @pytest.mark.asyncio
    async def test_unknown_error(self, mock_env, captured_metrics):
        next_handler = _failing_next_activity(
            RuntimeError("something completely unexpected")
        )
        interceptor = _AppVitalsActivityInterceptor(next_handler)
        mock_input = mock.MagicMock()
        mock_input.args = []

        with (
            mock.patch(
                "application_sdk.execution._temporal.interceptors.app_vitals.activity.info",
                return_value=_mock_activity_info(),
            ),
            mock.patch(
                "application_sdk.execution._temporal.interceptors.app_vitals.correlation_context"
            ) as mock_corr,
        ):
            mock_corr.get.return_value = None
            with pytest.raises(RuntimeError):
                await interceptor.execute_activity(mock_input)

        completions = [
            m for m in captured_metrics if m["name"] == "app_vitals.activity.completion"
        ]
        assert completions[0]["labels"]["error_type"] == "unknown"
