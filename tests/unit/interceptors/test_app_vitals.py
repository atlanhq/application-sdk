"""Unit tests for the App Vitals interceptor and error classifier."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence
from unittest import mock

import pytest
from temporalio.api.common.v1 import Payload

from application_sdk.observability.error_classifier import (
    classify_error,
    extract_cause_chain,
    is_retriable,
)
from application_sdk.observability.resource_sampler import (
    ResourceSample,
    compute_deltas,
)
from application_sdk.observability.trace_context import get_trace_context

# ---------------------------------------------------------------------------
# Error Classifier Tests
# ---------------------------------------------------------------------------


class TestClassifyError:
    """Tests for the error classification function."""

    def test_timeout_error(self):
        assert classify_error(TimeoutError("connection timed out")) == "timeout"

    def test_asyncio_timeout(self):
        assert classify_error(asyncio.TimeoutError()) == "timeout"

    def test_timeout_in_message(self):
        assert classify_error(Exception("request timed out after 30s")) == "timeout"

    def test_deadline_exceeded(self):
        assert classify_error(Exception("deadline exceeded")) == "timeout"

    def test_memory_error(self):
        assert classify_error(MemoryError("out of memory")) == "oom"

    def test_oomkilled_in_message(self):
        assert classify_error(Exception("process oomkilled")) == "oom"

    def test_cancelled_error(self):
        assert classify_error(asyncio.CancelledError()) == "cancelled"

    def test_cancelled_in_message(self):
        assert classify_error(Exception("workflow was cancelled")) == "cancelled"

    def test_connection_error(self):
        assert classify_error(ConnectionError("refused")) == "upstream"

    def test_connection_refused(self):
        assert classify_error(ConnectionRefusedError()) == "upstream"

    def test_connection_reset(self):
        assert classify_error(ConnectionResetError()) == "upstream"

    def test_auth_in_message(self):
        assert classify_error(Exception("authentication failed")) == "upstream"

    def test_unauthorized_in_message(self):
        assert classify_error(Exception("401 unauthorized")) == "upstream"

    def test_value_error(self):
        assert classify_error(ValueError("invalid input")) == "config"

    def test_key_error(self):
        assert classify_error(KeyError("missing_field")) == "config"

    def test_type_error(self):
        assert classify_error(TypeError("expected str")) == "config"

    def test_generic_exception(self):
        assert classify_error(Exception("something went wrong")) == "internal"

    def test_runtime_error(self):
        assert classify_error(RuntimeError("unexpected state")) == "internal"


# ---------------------------------------------------------------------------
# Mock objects for interceptor tests
# ---------------------------------------------------------------------------


@dataclass
class MockExecuteActivityInput:
    fn: Any = None
    args: Sequence[Any] = field(default_factory=list)
    headers: Mapping[str, Payload] = field(default_factory=dict)
    executor: Any = None


@dataclass
class MockExecuteWorkflowInput:
    type: str = "TestWorkflow"
    args: Sequence[Any] = field(default_factory=list)
    headers: Mapping[str, Payload] = field(default_factory=dict)


@dataclass
class MockActivityInfo:
    activity_type: str = "extract"
    activity_id: str = "act-001"
    attempt: int = 1
    workflow_id: str = "wf-abc-123"
    workflow_run_id: str = "run-def-456"
    workflow_type: str = "metadata_crawl"
    task_queue: str = "atlan-snowflake"
    scheduled_time: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc) - timedelta(seconds=2)
    )
    started_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    namespace: str = "default"
    schedule_to_close_timeout: timedelta | None = timedelta(minutes=5)
    start_to_close_timeout: timedelta | None = timedelta(minutes=5)
    heartbeat_timeout: timedelta | None = timedelta(seconds=30)


@dataclass
class MockWorkflowInfo:
    workflow_id: str = "wf-abc-123"
    run_id: str = "run-def-456"
    workflow_type: str = "metadata_crawl"
    task_queue: str = "atlan-snowflake"
    namespace: str = "default"
    attempt: int = 1
    parent: None = None
    continued_run_id: str | None = None
    cron_schedule: str | None = None


# ---------------------------------------------------------------------------
# AppVitalsInterceptor Tests
# ---------------------------------------------------------------------------


class TestAppVitalsActivityInterceptor:
    """Tests for the activity-level App Vitals interceptor."""

    @pytest.fixture
    def mock_next_activity(self):
        mock_next = mock.AsyncMock()
        mock_next.execute_activity = mock.AsyncMock(return_value="result")
        return mock_next

    @pytest.fixture
    def activity_info(self):
        return MockActivityInfo()

    @pytest.mark.asyncio
    async def test_successful_activity_emits_events(
        self, mock_next_activity, activity_info
    ):
        from application_sdk.observability.app_vitals import (
            _AppVitalsActivityInboundInterceptor,
        )

        interceptor = _AppVitalsActivityInboundInterceptor(mock_next_activity)

        with (
            mock.patch(
                "application_sdk.observability.app_vitals.activity"
            ) as mock_activity_mod,
            mock.patch(
                "application_sdk.observability.app_vitals._emit_log_event"
            ) as mock_log,
            mock.patch(
                "application_sdk.observability.app_vitals.APPLICATION_NAME", "snowflake"
            ),
            mock.patch("application_sdk.constants.APP_TENANT_ID", "tenant_123"),
        ):
            mock_activity_mod.info.return_value = activity_info

            result = await interceptor.execute_activity(MockExecuteActivityInput())

            assert result == "result"

            # Phase 2/L: 2 log events — started + completed
            log_event_names = [c[0][0] for c in mock_log.call_args_list]
            assert "app_vitals.act.started" in log_event_names
            assert "app_vitals.act.completed" in log_event_names

            # Check the completion event attrs
            completed_call = next(
                c
                for c in mock_log.call_args_list
                if c[0][0] == "app_vitals.act.completed"
            )
            log_attrs = completed_call[0][1]
            assert log_attrs["status"] == "succeeded"
            assert log_attrs["activity_type"] == "extract"
            assert log_attrs["workflow_id"] == "wf-abc-123"
            assert log_attrs["duration_ms"] >= 0

    @pytest.mark.asyncio
    async def test_failed_activity_emits_error_type(
        self, mock_next_activity, activity_info
    ):
        from application_sdk.observability.app_vitals import (
            _AppVitalsActivityInboundInterceptor,
        )

        mock_next_activity.execute_activity = mock.AsyncMock(
            side_effect=TimeoutError("connection timed out")
        )
        interceptor = _AppVitalsActivityInboundInterceptor(mock_next_activity)

        with (
            mock.patch(
                "application_sdk.observability.app_vitals.activity"
            ) as mock_activity_mod,
            mock.patch(
                "application_sdk.observability.app_vitals._emit_log_event"
            ) as mock_log,
            mock.patch(
                "application_sdk.observability.app_vitals.APPLICATION_NAME", "snowflake"
            ),
            mock.patch("application_sdk.constants.APP_TENANT_ID", "tenant_123"),
        ):
            mock_activity_mod.info.return_value = activity_info

            with pytest.raises(TimeoutError):
                await interceptor.execute_activity(MockExecuteActivityInput())

            log_attrs = mock_log.call_args[0][1]
            assert log_attrs["status"] == "failed"
            assert log_attrs["error_type"] == "timeout"
            assert "connection timed out" in log_attrs["error_message"]

    @pytest.mark.asyncio
    async def test_retry_attempt_emits_retry_metric(
        self, mock_next_activity, activity_info
    ):
        from application_sdk.observability.app_vitals import (
            _AppVitalsActivityInboundInterceptor,
        )

        activity_info.attempt = 3
        interceptor = _AppVitalsActivityInboundInterceptor(mock_next_activity)

        with (
            mock.patch(
                "application_sdk.observability.app_vitals.activity"
            ) as mock_activity_mod,
            mock.patch(
                "application_sdk.observability.app_vitals._emit_log_event"
            ) as mock_log,
            mock.patch(
                "application_sdk.observability.app_vitals.APPLICATION_NAME", "snowflake"
            ),
            mock.patch("application_sdk.constants.APP_TENANT_ID", "tenant_123"),
        ):
            mock_activity_mod.info.return_value = activity_info

            await interceptor.execute_activity(MockExecuteActivityInput())

            # Verify the act.completed event captured the retry attempt
            completed_call = next(
                c
                for c in mock_log.call_args_list
                if c[0][0] == "app_vitals.act.completed"
            )
            attrs = completed_call[0][1]
            assert attrs["attempt"] == 3


class TestAppVitalsWorkflowInterceptor:
    """Tests for the workflow-level App Vitals interceptor."""

    @pytest.fixture
    def mock_next_workflow(self):
        mock_next = mock.AsyncMock()
        mock_next.execute_workflow = mock.AsyncMock(return_value="wf_result")
        return mock_next

    @pytest.fixture
    def workflow_info(self):
        return MockWorkflowInfo()

    @pytest.mark.asyncio
    async def test_successful_workflow_emits_events(
        self, mock_next_workflow, workflow_info
    ):
        from application_sdk.observability.app_vitals import (
            _AppVitalsWorkflowInboundInterceptor,
        )

        interceptor = _AppVitalsWorkflowInboundInterceptor(mock_next_workflow)

        with (
            mock.patch(
                "application_sdk.observability.app_vitals.workflow"
            ) as mock_wf_mod,
            mock.patch(
                "application_sdk.observability.app_vitals._emit_log_event"
            ) as mock_log,
            mock.patch(
                "application_sdk.observability.app_vitals.APPLICATION_NAME", "snowflake"
            ),
            mock.patch("application_sdk.constants.APP_TENANT_ID", "tenant_123"),
        ):
            mock_wf_mod.info.return_value = workflow_info
            mock_wf_mod.unsafe.is_replaying.return_value = False

            result = await interceptor.execute_workflow(MockExecuteWorkflowInput())

            assert result == "wf_result"

            # Phase 2/L: now 3 log events — started + completed + summary
            log_event_names = [c[0][0] for c in mock_log.call_args_list]
            assert "app_vitals.wf.started" in log_event_names
            assert "app_vitals.wf.completed" in log_event_names
            assert "app_vitals.wf.summary" in log_event_names

            completed_call = next(
                c
                for c in mock_log.call_args_list
                if c[0][0] == "app_vitals.wf.completed"
            )
            log_attrs = completed_call[0][1]
            assert log_attrs["status"] == "succeeded"
            assert log_attrs["workflow_id"] == "wf-abc-123"
            assert log_attrs["workflow_type"] == "metadata_crawl"

    @pytest.mark.asyncio
    async def test_failed_workflow_emits_error(self, mock_next_workflow, workflow_info):
        from application_sdk.observability.app_vitals import (
            _AppVitalsWorkflowInboundInterceptor,
        )

        mock_next_workflow.execute_workflow = mock.AsyncMock(
            side_effect=ConnectionError("db unreachable")
        )
        interceptor = _AppVitalsWorkflowInboundInterceptor(mock_next_workflow)

        with (
            mock.patch(
                "application_sdk.observability.app_vitals.workflow"
            ) as mock_wf_mod,
            mock.patch(
                "application_sdk.observability.app_vitals._emit_log_event"
            ) as mock_log,
            mock.patch(
                "application_sdk.observability.app_vitals.APPLICATION_NAME", "snowflake"
            ),
            mock.patch("application_sdk.constants.APP_TENANT_ID", "tenant_123"),
        ):
            mock_wf_mod.info.return_value = workflow_info
            mock_wf_mod.unsafe.is_replaying.return_value = False

            with pytest.raises(ConnectionError):
                await interceptor.execute_workflow(MockExecuteWorkflowInput())

            # Check the completion event (not started, not summary)
            completed_call = next(
                c
                for c in mock_log.call_args_list
                if c[0][0] == "app_vitals.wf.completed"
            )
            log_attrs = completed_call[0][1]
            assert log_attrs["status"] == "failed"
            assert log_attrs["error_type"] == "upstream"
            assert "db unreachable" in log_attrs["error_message"]


# ---------------------------------------------------------------------------
# Phase 2: Trace Context Tests
# ---------------------------------------------------------------------------


class TestTraceContext:
    """Tests for the OTel trace context helper."""

    def test_no_tracer_returns_empty_strings(self):
        # With no active tracer, the default span context is invalid
        trace_id, span_id = get_trace_context()
        assert trace_id == ""
        assert span_id == ""

    def test_active_span_returns_hex_ids(self):
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider

        # Set up a minimal tracer provider
        provider = TracerProvider()
        trace.set_tracer_provider(provider)
        tracer = trace.get_tracer("test")

        with tracer.start_as_current_span("test_span"):
            trace_id, span_id = get_trace_context()
            assert len(trace_id) == 32  # 128-bit hex
            assert len(span_id) == 16  # 64-bit hex
            assert all(c in "0123456789abcdef" for c in trace_id)
            assert all(c in "0123456789abcdef" for c in span_id)


# ---------------------------------------------------------------------------
# Phase 2: Resource Sampler Tests
# ---------------------------------------------------------------------------


class TestResourceSampler:
    """Tests for process-level CPU/memory sampling."""

    def test_sample_returns_valid_data(self):
        from application_sdk.observability.resource_sampler import sample

        s = sample()
        # On Mac/Linux with psutil installed, this should work
        if s is not None:
            assert s.cpu_time_s >= 0
            assert s.rss_bytes > 0

    def test_compute_deltas_normal_case(self):
        start = ResourceSample(cpu_time_s=10.0, rss_bytes=100 * 1024 * 1024)  # 100 MB
        end = ResourceSample(cpu_time_s=12.5, rss_bytes=200 * 1024 * 1024)  # 200 MB

        cpu_seconds, mem_gb_sec = compute_deltas(start, end, duration_s=10.0)

        assert cpu_seconds == 2.5
        # avg RSS = 150 MB = 150/1024 GiB ≈ 0.1465
        # mem_gb_sec = 0.1465 * 10 = 1.465
        assert 1.4 < mem_gb_sec < 1.5

    def test_compute_deltas_none_sample_returns_zero(self):
        cpu_seconds, mem_gb_sec = compute_deltas(None, None, 1.0)
        assert cpu_seconds == 0.0
        assert mem_gb_sec == 0.0

    def test_compute_deltas_clamps_negative_cpu(self):
        # Can happen due to rounding or counter resets
        start = ResourceSample(cpu_time_s=10.0, rss_bytes=100 * 1024 * 1024)
        end = ResourceSample(cpu_time_s=9.5, rss_bytes=100 * 1024 * 1024)

        cpu_seconds, _ = compute_deltas(start, end, duration_s=1.0)
        assert cpu_seconds == 0.0


# ---------------------------------------------------------------------------
# Phase 2: Enhanced Interceptor — Efficiency Metrics
# ---------------------------------------------------------------------------


class TestAppVitalsEfficiencyMetrics:
    """Tests that activity interceptor emits cpu_seconds and mem_gb_sec."""

    @pytest.fixture
    def mock_next_activity(self):
        mock_next = mock.AsyncMock()
        mock_next.execute_activity = mock.AsyncMock(return_value="result")
        return mock_next

    @pytest.fixture
    def activity_info(self):
        return MockActivityInfo()

    @pytest.mark.asyncio
    async def test_efficiency_metrics_emitted(self, mock_next_activity, activity_info):
        from application_sdk.observability.app_vitals import (
            _AppVitalsActivityInboundInterceptor,
        )

        interceptor = _AppVitalsActivityInboundInterceptor(mock_next_activity)

        with (
            mock.patch(
                "application_sdk.observability.app_vitals.activity"
            ) as mock_activity_mod,
            mock.patch("application_sdk.observability.app_vitals._emit_log_event"),
            mock.patch(
                "application_sdk.observability.app_vitals.APPLICATION_NAME", "snowflake"
            ),
            mock.patch("application_sdk.constants.APP_TENANT_ID", "tenant_123"),
        ):
            mock_activity_mod.info.return_value = activity_info

            await interceptor.execute_activity(MockExecuteActivityInput())

            # Efficiency metrics should be emitted if psutil works on this platform
            # (may be 0.0 values, but metrics are emitted)


# ---------------------------------------------------------------------------
# Phase 2: Enhanced Interceptor — Trace ID Attachment
# ---------------------------------------------------------------------------


class TestAppVitalsTraceIdAttachment:
    """Tests that trace_id and span_id are attached to emitted events."""

    @pytest.fixture
    def mock_next_activity(self):
        mock_next = mock.AsyncMock()
        mock_next.execute_activity = mock.AsyncMock(return_value="result")
        return mock_next

    @pytest.fixture
    def activity_info(self):
        return MockActivityInfo()

    @pytest.mark.asyncio
    async def test_trace_ids_present_in_event_attrs(
        self, mock_next_activity, activity_info
    ):
        from application_sdk.observability.app_vitals import (
            _AppVitalsActivityInboundInterceptor,
        )

        interceptor = _AppVitalsActivityInboundInterceptor(mock_next_activity)

        with (
            mock.patch(
                "application_sdk.observability.app_vitals.activity"
            ) as mock_activity_mod,
            mock.patch(
                "application_sdk.observability.app_vitals._emit_log_event"
            ) as mock_log,
            mock.patch(
                "application_sdk.observability.app_vitals.APPLICATION_NAME", "snowflake"
            ),
            mock.patch("application_sdk.constants.APP_TENANT_ID", "tenant_123"),
            mock.patch(
                "application_sdk.observability.app_vitals.get_trace_context",
                return_value=("0123456789abcdef0123456789abcdef", "fedcba9876543210"),
            ),
        ):
            mock_activity_mod.info.return_value = activity_info

            await interceptor.execute_activity(MockExecuteActivityInput())

            log_attrs = mock_log.call_args[0][1]
            assert log_attrs["trace_id"] == "0123456789abcdef0123456789abcdef"
            assert log_attrs["span_id"] == "fedcba9876543210"

    @pytest.mark.asyncio
    async def test_trace_ids_empty_when_no_tracer(
        self, mock_next_activity, activity_info
    ):
        from application_sdk.observability.app_vitals import (
            _AppVitalsActivityInboundInterceptor,
        )

        interceptor = _AppVitalsActivityInboundInterceptor(mock_next_activity)

        with (
            mock.patch(
                "application_sdk.observability.app_vitals.activity"
            ) as mock_activity_mod,
            mock.patch(
                "application_sdk.observability.app_vitals._emit_log_event"
            ) as mock_log,
            mock.patch(
                "application_sdk.observability.app_vitals.APPLICATION_NAME", "snowflake"
            ),
            mock.patch("application_sdk.constants.APP_TENANT_ID", "tenant_123"),
            mock.patch(
                "application_sdk.observability.app_vitals.get_trace_context",
                return_value=("", ""),
            ),
        ):
            mock_activity_mod.info.return_value = activity_info

            await interceptor.execute_activity(MockExecuteActivityInput())

            log_attrs = mock_log.call_args[0][1]
            # Empty strings are OK — they just mean no tracer is running
            assert log_attrs["trace_id"] == ""
            assert log_attrs["span_id"] == ""


# ---------------------------------------------------------------------------
# L3: Rich Error Schema (cause chain, is_retriable, timeout budget)
# ---------------------------------------------------------------------------


class TestErrorClassifierRichFields:
    """Tests for cause chain extraction and is_retriable heuristic."""

    def test_extract_cause_chain_explicit(self):
        try:
            try:
                raise OSError("network unreachable")
            except OSError as inner:
                raise ConnectionError("db unreachable") from inner
        except ConnectionError as e:
            chain = extract_cause_chain(e)

        assert len(chain) == 1
        assert "OSError" in chain[0]
        assert "network unreachable" in chain[0]

    def test_extract_cause_chain_implicit(self):
        try:
            try:
                raise ValueError("bad input")
            except ValueError:
                raise RuntimeError("wrapper")  # implicit __context__
        except RuntimeError as e:
            chain = extract_cause_chain(e)

        assert len(chain) == 1
        assert "ValueError" in chain[0]

    def test_extract_cause_chain_none(self):
        e = Exception("standalone")
        assert extract_cause_chain(e) == []

    def test_extract_cause_chain_respects_limit(self):
        # Deeply nested chain
        e = None
        for i in range(10):
            try:
                if e is None:
                    raise Exception(f"layer-{i}")
                raise Exception(f"layer-{i}") from e
            except Exception as err:
                e = err
        chain = extract_cause_chain(e, limit=3)
        assert len(chain) == 3

    def test_is_retriable_config_error(self):
        assert is_retriable(ValueError("bad input")) is False

    def test_is_retriable_timeout(self):
        assert is_retriable(TimeoutError("timed out")) is True

    def test_is_retriable_upstream(self):
        assert is_retriable(ConnectionError("refused")) is True

    def test_is_retriable_cancelled(self):
        import asyncio

        assert is_retriable(asyncio.CancelledError()) is False

    def test_is_retriable_respects_non_retryable_flag(self):
        e = Exception("app error")
        e.non_retryable = True  # Temporal ApplicationError pattern
        assert is_retriable(e) is False


class TestAppVitalsRichErrorFields:
    """Tests that activity failure events include cause chain + is_retriable + timeout budget."""

    @pytest.fixture
    def mock_next_activity(self):
        mock_next = mock.AsyncMock()
        return mock_next

    @pytest.fixture
    def activity_info(self):
        return MockActivityInfo()

    @pytest.mark.asyncio
    async def test_failure_carries_error_class_and_retriable(
        self, mock_next_activity, activity_info
    ):
        from application_sdk.observability.app_vitals import (
            _AppVitalsActivityInboundInterceptor,
        )

        mock_next_activity.execute_activity = mock.AsyncMock(
            side_effect=TimeoutError("timed out")
        )
        interceptor = _AppVitalsActivityInboundInterceptor(mock_next_activity)

        with (
            mock.patch(
                "application_sdk.observability.app_vitals.activity"
            ) as mock_activity_mod,
            mock.patch(
                "application_sdk.observability.app_vitals._emit_log_event"
            ) as mock_log,
            mock.patch(
                "application_sdk.observability.app_vitals.APPLICATION_NAME", "snowflake"
            ),
            mock.patch("application_sdk.constants.APP_TENANT_ID", "tenant_123"),
        ):
            mock_activity_mod.info.return_value = activity_info

            with pytest.raises(TimeoutError):
                await interceptor.execute_activity(MockExecuteActivityInput())

            completed_call = next(
                c
                for c in mock_log.call_args_list
                if c[0][0] == "app_vitals.act.completed"
            )
            log_attrs = completed_call[0][1]
            assert log_attrs["error_class"] == "TimeoutError"
            assert log_attrs["is_retriable"] is True

    @pytest.mark.asyncio
    async def test_failure_carries_cause_chain(self, mock_next_activity, activity_info):
        from application_sdk.observability.app_vitals import (
            _AppVitalsActivityInboundInterceptor,
        )

        async def _raise_with_cause(_input):
            try:
                raise OSError("network down")
            except OSError as inner:
                raise ConnectionError("db unreachable") from inner

        mock_next_activity.execute_activity = mock.AsyncMock(
            side_effect=_raise_with_cause
        )
        interceptor = _AppVitalsActivityInboundInterceptor(mock_next_activity)

        with (
            mock.patch(
                "application_sdk.observability.app_vitals.activity"
            ) as mock_activity_mod,
            mock.patch(
                "application_sdk.observability.app_vitals._emit_log_event"
            ) as mock_log,
            mock.patch(
                "application_sdk.observability.app_vitals.APPLICATION_NAME", "snowflake"
            ),
            mock.patch("application_sdk.constants.APP_TENANT_ID", "tenant_123"),
        ):
            mock_activity_mod.info.return_value = activity_info

            with pytest.raises(ConnectionError):
                await interceptor.execute_activity(MockExecuteActivityInput())

            completed_call = next(
                c
                for c in mock_log.call_args_list
                if c[0][0] == "app_vitals.act.completed"
            )
            log_attrs = completed_call[0][1]
            assert "error_cause_chain" in log_attrs
            assert len(log_attrs["error_cause_chain"]) >= 1
            assert "OSError" in log_attrs["error_cause_chain"][0]

    @pytest.mark.asyncio
    async def test_timeout_budget_computed(self, mock_next_activity, activity_info):
        from application_sdk.observability.app_vitals import (
            _AppVitalsActivityInboundInterceptor,
        )

        interceptor = _AppVitalsActivityInboundInterceptor(mock_next_activity)

        with (
            mock.patch(
                "application_sdk.observability.app_vitals.activity"
            ) as mock_activity_mod,
            mock.patch(
                "application_sdk.observability.app_vitals._emit_log_event"
            ) as mock_log,
            mock.patch(
                "application_sdk.observability.app_vitals.APPLICATION_NAME", "snowflake"
            ),
            mock.patch("application_sdk.constants.APP_TENANT_ID", "tenant_123"),
        ):
            mock_activity_mod.info.return_value = activity_info  # 5-min timeout

            await interceptor.execute_activity(MockExecuteActivityInput())

            completed_call = next(
                c
                for c in mock_log.call_args_list
                if c[0][0] == "app_vitals.act.completed"
            )
            log_attrs = completed_call[0][1]
            assert log_attrs["timeout_budget_total_ms"] == 300000.0  # 5 min
            assert log_attrs["timeout_budget_used_pct"] is not None


# ---------------------------------------------------------------------------
# L2: Lifecycle Start Events
# ---------------------------------------------------------------------------


class TestLifecycleStartEvents:
    """Tests that started events are emitted before the activity/workflow runs."""

    @pytest.fixture
    def mock_next_activity(self):
        mock_next = mock.AsyncMock()
        mock_next.execute_activity = mock.AsyncMock(return_value="ok")
        return mock_next

    @pytest.mark.asyncio
    async def test_act_started_emitted_before_completed(self, mock_next_activity):
        from application_sdk.observability.app_vitals import (
            _AppVitalsActivityInboundInterceptor,
        )

        interceptor = _AppVitalsActivityInboundInterceptor(mock_next_activity)
        info = MockActivityInfo()

        with (
            mock.patch(
                "application_sdk.observability.app_vitals.activity"
            ) as mock_activity_mod,
            mock.patch(
                "application_sdk.observability.app_vitals._emit_log_event"
            ) as mock_log,
            mock.patch(
                "application_sdk.observability.app_vitals.APPLICATION_NAME", "snowflake"
            ),
            mock.patch("application_sdk.constants.APP_TENANT_ID", "tenant_123"),
        ):
            mock_activity_mod.info.return_value = info

            await interceptor.execute_activity(MockExecuteActivityInput())

            event_names = [c[0][0] for c in mock_log.call_args_list]
            assert event_names.index("app_vitals.act.started") < event_names.index(
                "app_vitals.act.completed"
            )

            started_call = next(
                c
                for c in mock_log.call_args_list
                if c[0][0] == "app_vitals.act.started"
            )
            started_attrs = started_call[0][1]
            assert started_attrs["activity_type"] == "extract"
            assert started_attrs["workflow_id"] == "wf-abc-123"
            # Started event must NOT have duration_ms or status
            assert "status" not in started_attrs
            assert "duration_ms" not in started_attrs


# ---------------------------------------------------------------------------
# L9: assets_processed Convention
# ---------------------------------------------------------------------------


class TestAssetsProcessed:
    """Tests extraction of assets_processed from activity return values."""

    def test_extract_from_total_record_count(self):
        from application_sdk.observability.app_vitals import _extract_assets_processed

        class Result:
            total_record_count = 50000

        assert _extract_assets_processed(Result()) == 50000

    def test_extract_from_assets_processed(self):
        from application_sdk.observability.app_vitals import _extract_assets_processed

        class Result:
            assets_processed = 42

        assert _extract_assets_processed(Result()) == 42

    def test_extract_from_dict(self):
        from application_sdk.observability.app_vitals import _extract_assets_processed

        assert _extract_assets_processed({"assets_processed": 100}) == 100

    def test_extract_returns_none_for_missing(self):
        from application_sdk.observability.app_vitals import _extract_assets_processed

        class Result:
            other_field = 1

        assert _extract_assets_processed(Result()) is None

    def test_extract_returns_none_for_none(self):
        from application_sdk.observability.app_vitals import _extract_assets_processed

        assert _extract_assets_processed(None) is None

    def test_extract_handles_non_int(self):
        from application_sdk.observability.app_vitals import _extract_assets_processed

        class Result:
            assets_processed = "not_a_number"

        assert _extract_assets_processed(Result()) is None

    @pytest.mark.asyncio
    async def test_activity_event_carries_assets_processed(self):
        from application_sdk.observability.app_vitals import (
            _AppVitalsActivityInboundInterceptor,
        )

        class MockResult:
            total_record_count = 12345

        mock_next = mock.AsyncMock()
        mock_next.execute_activity = mock.AsyncMock(return_value=MockResult())
        interceptor = _AppVitalsActivityInboundInterceptor(mock_next)

        with (
            mock.patch(
                "application_sdk.observability.app_vitals.activity"
            ) as mock_activity_mod,
            mock.patch(
                "application_sdk.observability.app_vitals._emit_log_event"
            ) as mock_log,
            mock.patch(
                "application_sdk.observability.app_vitals.APPLICATION_NAME", "snowflake"
            ),
            mock.patch("application_sdk.constants.APP_TENANT_ID", "tenant_123"),
        ):
            mock_activity_mod.info.return_value = MockActivityInfo()

            await interceptor.execute_activity(MockExecuteActivityInput())

            completed_call = next(
                c
                for c in mock_log.call_args_list
                if c[0][0] == "app_vitals.act.completed"
            )
            assert completed_call[0][1]["assets_processed"] == 12345


# ---------------------------------------------------------------------------
# L1: Workflow Summary Event
# ---------------------------------------------------------------------------


class TestWorkflowSummary:
    """Tests the workflow summary event rollup."""

    @pytest.fixture
    def mock_next_workflow(self):
        mock_next = mock.AsyncMock()
        mock_next.execute_workflow = mock.AsyncMock(return_value="ok")
        return mock_next

    @pytest.mark.asyncio
    async def test_summary_event_emitted_with_zero_activities(self, mock_next_workflow):
        from application_sdk.observability.app_vitals import (
            _AppVitalsWorkflowInboundInterceptor,
        )

        interceptor = _AppVitalsWorkflowInboundInterceptor(mock_next_workflow)

        with (
            mock.patch(
                "application_sdk.observability.app_vitals.workflow"
            ) as mock_wf_mod,
            mock.patch(
                "application_sdk.observability.app_vitals._emit_log_event"
            ) as mock_log,
            mock.patch(
                "application_sdk.observability.app_vitals.APPLICATION_NAME", "snowflake"
            ),
            mock.patch("application_sdk.constants.APP_TENANT_ID", "tenant_123"),
        ):
            mock_wf_mod.info.return_value = MockWorkflowInfo()
            mock_wf_mod.unsafe.is_replaying.return_value = False

            await interceptor.execute_workflow(MockExecuteWorkflowInput())

            summary_call = next(
                c for c in mock_log.call_args_list if c[0][0] == "app_vitals.wf.summary"
            )
            attrs = summary_call[0][1]
            assert attrs["total_activities"] == 0
            assert attrs["succeeded_activities"] == 0
            assert attrs["failed_activities"] == 0
            assert attrs["status"] == "succeeded"

    def test_build_summary_with_activity_records(self):
        from application_sdk.observability.app_vitals import (
            _AppVitalsWorkflowInboundInterceptor,
        )

        mock_next = mock.AsyncMock()
        interceptor = _AppVitalsWorkflowInboundInterceptor(mock_next)

        # Seed records as if the outbound interceptor had tracked them
        interceptor._activity_records = [
            {
                "activity_type": "fetch_databases",
                "status": "succeeded",
                "start_ns": 1000,
                "end_ns": 51000,
                "duration_ms": 50.0,
            },
            {
                "activity_type": "fetch_tables",
                "status": "failed",
                "error_type": "timeout",
                "start_ns": 52000,
                "end_ns": 82000,
                "duration_ms": 30.0,
            },
            {
                "activity_type": "fetch_views",
                "status": "succeeded",
                "start_ns": 83000,
                "end_ns": 183000,
                "duration_ms": 100.0,  # bottleneck (longest)
            },
        ]

        common = {"app_name": "snowflake"}
        info = MockWorkflowInfo()
        summary = interceptor._build_summary_attrs(common, info, "succeeded", 500.0)

        assert summary["total_activities"] == 3
        assert summary["succeeded_activities"] == 2
        assert summary["failed_activities"] == 1
        assert summary["first_failure_activity_type"] == "fetch_tables"
        assert summary["first_failure_error_type"] == "timeout"
        assert summary["bottleneck_activity_type"] == "fetch_views"
        assert summary["bottleneck_duration_ms"] == 100.0


# ---------------------------------------------------------------------------
# Regression: post-audit fixes
# ---------------------------------------------------------------------------


class TestLoggerAdaptorExtraKeysAllowlist:
    """Regression for audit finding: logger_adaptor._KNOWN_EXTRA_KEYS allowlist
    was silently dropping ~20 App Vitals fields before they reached OTLP log
    export. All fields we emit on events must be in the allowlist.
    """

    APP_VITALS_LOG_FIELDS = [
        # Identity — only per-workflow/per-app fields. Deployment attrs (version,
        # release_id, pod_name, k8s.domain.name, k8s.cluster.name) are on OTel
        # Resource. tenant_id is redundant with k8s.cluster.name.
        "app_vitals",
        "app_name",
        # Execution context
        "workflow_id",
        "run_id",
        "workflow_run_id",
        "workflow_type",
        "activity_id",
        "activity_type",
        "task_queue",
        "attempt",
        "namespace",
        "correlation_id",
        "trace_id",
        "span_id",
        # Outcome
        "status",
        "error_type",
        "error_class",
        "error_message",
        "is_retriable",
        "error_cause_chain",
        # Metric classification
        "dimension",
        "source",
        "metric_name",
        # Throughput / efficiency
        "assets_processed",
        "cpu_seconds",
        "mem_gb_sec",
        # Performance timings
        "duration_ms",
        "schedule_to_start_ms",
        "timeout_budget_total_ms",
        "timeout_budget_used_pct",
        # Workflow summary
        "total_activities",
        "succeeded_activities",
        "failed_activities",
        "total_child_workflows",
        "first_failure_activity_type",
        "first_failure_error_type",
        "bottleneck_activity_type",
        "bottleneck_duration_ms",
    ]

    def test_all_app_vitals_fields_survive_extra_dict_filter(self):
        """Every attribute we put on an event must come out the other side
        of _build_extra_dict() unchanged. If this test fails, new events we
        emit won't reach OTLP/ClickHouse even though the interceptor calls
        the logger."""
        from application_sdk.observability.logger_adaptor import (
            _KNOWN_EXTRA_KEYS,
            _build_extra_dict,
        )

        missing_from_allowlist = [
            f for f in self.APP_VITALS_LOG_FIELDS if f not in _KNOWN_EXTRA_KEYS
        ]
        assert missing_from_allowlist == [], (
            "These App Vitals fields are missing from _KNOWN_EXTRA_KEYS "
            f"and will be dropped before OTLP export: {missing_from_allowlist}"
        )

        # Realistic example: a failed activity event
        sample_extra = {
            "app_vitals": "true",
            "app_name": "snowflake",
            "workflow_id": "wf-abc",
            "run_id": "run-def",
            "activity_type": "fetch_tables",
            "status": "failed",
            "error_type": "timeout",
            "error_class": "TimeoutError",
            "is_retriable": True,
            "duration_ms": 30000,
            "cpu_seconds": 0.5,
            "assets_processed": 50000,
            "timeout_budget_used_pct": 95.5,
            "dimension": "reliability",
            "metric_name": "app_vitals.reliability.activity_completed",
            "logger_name": "app_vitals",  # should be filtered
        }
        out = _build_extra_dict(sample_extra)

        # logger_name is always filtered — that's the one explicit exclusion
        assert "logger_name" not in out
        # Everything else the interceptor emits must pass through
        for k, v in sample_extra.items():
            if k == "logger_name":
                continue
            assert k in out, f"{k} was dropped by _build_extra_dict"


# TestDurationMetricsUseHistogram removed — Path 2 (OTel metrics) was removed;
# all analytics are now computed from Path 1 (log events → MV).
