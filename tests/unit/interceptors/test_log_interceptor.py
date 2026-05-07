"""Unit tests for the LogInterceptor."""

from __future__ import annotations

from dataclasses import dataclass, field
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from temporalio.converter import default as default_converter

from application_sdk.execution._temporal.interceptors.log import (
    LogInterceptor,
    _correlation_id_or_empty,
    _LogActivityInboundInterceptor,
    _LogWorkflowInboundInterceptor,
    _LogWorkflowOutboundInterceptor,
)
from application_sdk.observability.context import ExecutionContext, _execution_ctx
from application_sdk.observability.correlation import (
    CorrelationContext,
    _correlation_ctx,
    get_correlation_context,
    set_correlation_context,
)

# ---------------------------------------------------------------------------
# Shared mock dataclasses
# ---------------------------------------------------------------------------


@dataclass
class MockParentInfo:
    workflow_id: str = "parent-wf-id"
    run_id: str = "parent-run-id"


@dataclass
class MockWorkflowInfo:
    workflow_id: str = "wf-id"
    run_id: str = "run-id"
    workflow_type: str = "TestWorkflow"
    task_queue: str = "default"
    namespace: str = "ns"
    attempt: int = 1
    parent: MockParentInfo | None = None


@dataclass
class MockActivityInfo:
    activity_id: str = "act-id"
    activity_type: str = "TestActivity"
    task_queue: str = "default"
    workflow_id: str = "wf-id"
    workflow_run_id: str = "run-id"
    workflow_type: str = "TestWorkflow"
    attempt: int = 1
    namespace: str = "ns"


@dataclass
class MockExecuteWorkflowInput:
    headers: dict = field(default_factory=dict)
    args: list = field(default_factory=list)


@dataclass
class MockExecuteActivityInput:
    headers: dict = field(default_factory=dict)
    args: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# Reset ContextVars before/after every test
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_context():
    _correlation_ctx.set(None)
    _execution_ctx.set(ExecutionContext())
    yield
    _correlation_ctx.set(None)
    _execution_ctx.set(ExecutionContext())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _encode_header(value: str):
    """Encode a string as a Temporal payload for use in headers."""
    return default_converter().payload_converter.to_payload(value)


# ---------------------------------------------------------------------------
# TestCorrelationIdOrEmpty
# ---------------------------------------------------------------------------


class TestCorrelationIdOrEmpty:
    def test_returns_empty_when_no_context(self):
        assert _correlation_id_or_empty() == ""

    def test_returns_correlation_id_when_set(self):
        set_correlation_context(CorrelationContext(correlation_id="abc-123"))
        assert _correlation_id_or_empty() == "abc-123"

    def test_returns_empty_when_context_has_empty_id(self):
        set_correlation_context(CorrelationContext(correlation_id=""))
        assert _correlation_id_or_empty() == ""


# ---------------------------------------------------------------------------
# TestLogWorkflowInboundInterceptor
# ---------------------------------------------------------------------------


class TestLogWorkflowInboundInterceptor:
    @pytest.fixture
    def mock_next(self):
        n = AsyncMock()
        n.execute_workflow = AsyncMock(return_value="wf-result")
        return n

    @pytest.fixture
    def interceptor(self, mock_next):
        return _LogWorkflowInboundInterceptor(mock_next)

    async def test_skips_on_replay(self, interceptor, mock_next):
        with patch(
            "application_sdk.execution._temporal.interceptors.log.workflow"
        ) as mock_wf:
            mock_wf.unsafe.is_replaying.return_value = True
            with patch(
                "application_sdk.execution._temporal.interceptors.log.logger"
            ) as mock_logger:
                await interceptor.execute_workflow(MockExecuteWorkflowInput())

        mock_logger.info.assert_not_called()
        mock_logger.error.assert_not_called()

    async def test_emits_workflow_started_log(self, interceptor):
        with patch(
            "application_sdk.execution._temporal.interceptors.log.workflow"
        ) as mock_wf:
            mock_wf.unsafe.is_replaying.return_value = False
            mock_wf.info.return_value = MockWorkflowInfo()
            mock_wf.memo.return_value = {}
            with patch(
                "application_sdk.execution._temporal.interceptors.log.logger"
            ) as mock_logger:
                await interceptor.execute_workflow(MockExecuteWorkflowInput())

        started_calls = [
            c for c in mock_logger.info.call_args_list if c[0][0] == "workflow.started"
        ]
        assert len(started_calls) == 1
        kwargs = started_calls[0][1]
        assert kwargs["temporal.workflow.type"] == "TestWorkflow"
        assert "atlan.correlation_id" in kwargs

    async def test_emits_workflow_ended_ok(self, interceptor):
        with patch(
            "application_sdk.execution._temporal.interceptors.log.workflow"
        ) as mock_wf:
            mock_wf.unsafe.is_replaying.return_value = False
            mock_wf.info.return_value = MockWorkflowInfo()
            mock_wf.memo.return_value = {}
            with patch(
                "application_sdk.execution._temporal.interceptors.log.logger"
            ) as mock_logger:
                await interceptor.execute_workflow(MockExecuteWorkflowInput())

        ended_calls = [
            c for c in mock_logger.info.call_args_list if c[0][0] == "workflow.ended"
        ]
        assert len(ended_calls) == 1
        kwargs = ended_calls[0][1]
        assert kwargs["otel.status_code"] == "OK"
        assert kwargs["temporal.workflow.duration_ms"] >= 0

    async def test_emits_workflow_ended_error_on_exception(self, mock_next):
        mock_next.execute_workflow = AsyncMock(side_effect=ValueError("fail"))
        interceptor = _LogWorkflowInboundInterceptor(mock_next)

        with patch(
            "application_sdk.execution._temporal.interceptors.log.workflow"
        ) as mock_wf:
            mock_wf.unsafe.is_replaying.return_value = False
            mock_wf.info.return_value = MockWorkflowInfo()
            mock_wf.memo.return_value = {}
            with (
                patch(
                    "application_sdk.execution._temporal.interceptors.log.logger"
                ) as mock_logger,
                pytest.raises(ValueError, match="fail"),
            ):
                await interceptor.execute_workflow(MockExecuteWorkflowInput())

        ended_calls = [
            c for c in mock_logger.error.call_args_list if c[0][0] == "workflow.ended"
        ]
        assert len(ended_calls) == 1
        kwargs = ended_calls[0][1]
        assert kwargs["otel.status_code"] == "ERROR"
        assert kwargs["exc_info"] is True

    async def test_generates_new_correlation_id_when_no_headers_no_memo(
        self, interceptor
    ):
        with patch(
            "application_sdk.execution._temporal.interceptors.log.workflow"
        ) as mock_wf:
            mock_wf.unsafe.is_replaying.return_value = False
            mock_wf.info.return_value = MockWorkflowInfo()
            mock_wf.memo.return_value = {}
            await interceptor.execute_workflow(MockExecuteWorkflowInput(headers={}))

        assert interceptor._correlation_id != ""
        # Should look like a UUID (36 chars with hyphens)
        assert len(interceptor._correlation_id) == 36

    async def test_restores_correlation_id_from_memo(self, interceptor):
        with patch(
            "application_sdk.execution._temporal.interceptors.log.workflow"
        ) as mock_wf:
            mock_wf.unsafe.is_replaying.return_value = False
            mock_wf.info.return_value = MockWorkflowInfo()
            mock_wf.memo.return_value = {"correlation_id": "memo-id-123"}
            await interceptor.execute_workflow(MockExecuteWorkflowInput(headers={}))

        assert interceptor._correlation_id == "memo-id-123"

    async def test_reads_correlation_id_from_header(self, interceptor):
        payload = _encode_header("header-corr-id")
        headers = {"x-correlation-id": payload}

        with patch(
            "application_sdk.execution._temporal.interceptors.log.workflow"
        ) as mock_wf:
            mock_wf.unsafe.is_replaying.return_value = False
            mock_wf.info.return_value = MockWorkflowInfo()
            mock_wf.memo.return_value = {}
            await interceptor.execute_workflow(
                MockExecuteWorkflowInput(headers=headers)
            )

        assert interceptor._correlation_id == "header-corr-id"


# ---------------------------------------------------------------------------
# TestLogWorkflowOutboundInject
# ---------------------------------------------------------------------------


class TestLogWorkflowOutboundInject:
    def _make_outbound(self, correlation_id: str = "test-id"):
        inbound = _LogWorkflowInboundInterceptor(MagicMock())
        inbound._correlation_id = correlation_id
        outbound = _LogWorkflowOutboundInterceptor(MagicMock(), inbound)
        return outbound

    def test_inject_adds_correlation_header(self):
        outbound = self._make_outbound("test-id")
        result = outbound._inject({})

        assert "x-correlation-id" in result
        decoded = default_converter().payload_converter.from_payload(
            result["x-correlation-id"], type_hint=str
        )
        assert decoded == "test-id"

    def test_inject_returns_unchanged_when_empty_correlation_id(self):
        outbound = self._make_outbound("")
        result = outbound._inject({})
        assert "x-correlation-id" not in result

    def test_inject_preserves_existing_headers(self):
        existing_payload = _encode_header("other-value")
        outbound = self._make_outbound("corr-id")
        result = outbound._inject({"other-header": existing_payload})

        assert "other-header" in result
        assert "x-correlation-id" in result


# ---------------------------------------------------------------------------
# TestLogActivityInboundInterceptor
# ---------------------------------------------------------------------------


class TestLogActivityInboundInterceptor:
    @pytest.fixture
    def mock_next(self):
        n = AsyncMock()
        n.execute_activity = AsyncMock(return_value="act-result")
        return n

    @pytest.fixture
    def interceptor(self, mock_next):
        return _LogActivityInboundInterceptor(mock_next)

    async def test_emits_activity_started_log(self, interceptor):
        with patch(
            "application_sdk.execution._temporal.interceptors.log.activity"
        ) as mock_act:
            mock_act.info.return_value = MockActivityInfo()
            with patch(
                "application_sdk.execution._temporal.interceptors.log.logger"
            ) as mock_logger:
                await interceptor.execute_activity(MockExecuteActivityInput())

        started_calls = [
            c for c in mock_logger.info.call_args_list if c[0][0] == "activity.started"
        ]
        assert len(started_calls) == 1
        kwargs = started_calls[0][1]
        assert kwargs["temporal.activity.type"] == "TestActivity"
        assert kwargs["temporal.workflow.id"] == "wf-id"
        assert "atlan.correlation_id" in kwargs

    async def test_emits_activity_ended_ok(self, interceptor):
        with patch(
            "application_sdk.execution._temporal.interceptors.log.activity"
        ) as mock_act:
            mock_act.info.return_value = MockActivityInfo()
            with patch(
                "application_sdk.execution._temporal.interceptors.log.logger"
            ) as mock_logger:
                await interceptor.execute_activity(MockExecuteActivityInput())

        ended_calls = [
            c for c in mock_logger.info.call_args_list if c[0][0] == "activity.ended"
        ]
        assert len(ended_calls) == 1
        kwargs = ended_calls[0][1]
        assert kwargs["otel.status_code"] == "OK"
        assert kwargs["temporal.activity.duration_ms"] >= 0

    async def test_emits_activity_ended_error(self, mock_next):
        mock_next.execute_activity = AsyncMock(
            side_effect=RuntimeError("activity fail")
        )
        interceptor = _LogActivityInboundInterceptor(mock_next)

        with patch(
            "application_sdk.execution._temporal.interceptors.log.activity"
        ) as mock_act:
            mock_act.info.return_value = MockActivityInfo()
            with (
                patch(
                    "application_sdk.execution._temporal.interceptors.log.logger"
                ) as mock_logger,
                pytest.raises(RuntimeError, match="activity fail"),
            ):
                await interceptor.execute_activity(MockExecuteActivityInput())

        ended_calls = [
            c for c in mock_logger.error.call_args_list if c[0][0] == "activity.ended"
        ]
        assert len(ended_calls) == 1
        kwargs = ended_calls[0][1]
        assert kwargs["otel.status_code"] == "ERROR"
        assert kwargs["exc_info"] is True

    async def test_reads_correlation_id_from_header(self, interceptor):
        payload = _encode_header("from-header")
        headers = {"x-correlation-id": payload}

        with patch(
            "application_sdk.execution._temporal.interceptors.log.activity"
        ) as mock_act:
            mock_act.info.return_value = MockActivityInfo()
            await interceptor.execute_activity(
                MockExecuteActivityInput(headers=headers)
            )

        ctx = get_correlation_context()
        assert ctx is not None
        assert ctx.correlation_id == "from-header"

    async def test_falls_back_to_context_var_when_no_header(self, interceptor):
        set_correlation_context(CorrelationContext(correlation_id="ctx-id"))

        with patch(
            "application_sdk.execution._temporal.interceptors.log.activity"
        ) as mock_act:
            mock_act.info.return_value = MockActivityInfo()
            with patch(
                "application_sdk.execution._temporal.interceptors.log.logger"
            ) as mock_logger:
                await interceptor.execute_activity(MockExecuteActivityInput(headers={}))

        started_calls = [
            c for c in mock_logger.info.call_args_list if c[0][0] == "activity.started"
        ]
        assert len(started_calls) == 1
        assert started_calls[0][1]["atlan.correlation_id"] == "ctx-id"


# ---------------------------------------------------------------------------
# TestLogInterceptor
# ---------------------------------------------------------------------------


class TestLogInterceptor:
    def test_workflow_interceptor_class_returns_log_inbound_type(self):
        interceptor = LogInterceptor()
        result = interceptor.workflow_interceptor_class(MagicMock())
        assert result is _LogWorkflowInboundInterceptor

    def test_intercept_activity_wraps_in_log_inbound_interceptor(self):
        interceptor = LogInterceptor()
        mock_next = MagicMock()
        result = interceptor.intercept_activity(mock_next)
        assert isinstance(result, _LogActivityInboundInterceptor)


# ---------------------------------------------------------------------------
# Parent identity propagation (workflow → activity)
# ---------------------------------------------------------------------------


class TestWorkflowInboundCachesParentIdentity:
    """``info.parent`` is read once on entry and cached on the inbound instance
    so the outbound interceptor can inject it without a ContextVar read."""

    @pytest.fixture
    def mock_next(self):
        n = AsyncMock()
        n.execute_workflow = AsyncMock(return_value=None)
        return n

    @pytest.fixture
    def interceptor(self, mock_next):
        return _LogWorkflowInboundInterceptor(mock_next)

    async def test_top_level_workflow_caches_empty_parent(self, interceptor):
        with patch(
            "application_sdk.execution._temporal.interceptors.log.workflow"
        ) as mock_wf:
            mock_wf.unsafe.is_replaying.return_value = False
            mock_wf.info.return_value = MockWorkflowInfo(parent=None)
            mock_wf.memo.return_value = {}
            await interceptor.execute_workflow(MockExecuteWorkflowInput())

        assert interceptor._parent_workflow_id == ""
        assert interceptor._parent_run_id == ""

    async def test_child_workflow_caches_parent_identity(self, interceptor):
        with patch(
            "application_sdk.execution._temporal.interceptors.log.workflow"
        ) as mock_wf:
            mock_wf.unsafe.is_replaying.return_value = False
            mock_wf.info.return_value = MockWorkflowInfo(
                parent=MockParentInfo(workflow_id="A", run_id="A_run")
            )
            mock_wf.memo.return_value = {}
            await interceptor.execute_workflow(MockExecuteWorkflowInput())

        assert interceptor._parent_workflow_id == "A"
        assert interceptor._parent_run_id == "A_run"

    async def test_parent_identity_propagates_to_execution_context(self, interceptor):
        with patch(
            "application_sdk.execution._temporal.interceptors.log.workflow"
        ) as mock_wf:
            mock_wf.unsafe.is_replaying.return_value = False
            mock_wf.info.return_value = MockWorkflowInfo(
                parent=MockParentInfo(workflow_id="A", run_id="A_run")
            )
            mock_wf.memo.return_value = {}
            await interceptor.execute_workflow(MockExecuteWorkflowInput())

        ctx = _execution_ctx.get()
        assert ctx.parent_workflow_id == "A"
        assert ctx.parent_run_id == "A_run"


class TestWorkflowOutboundInjectsParentHeaders:
    """The outbound interceptor reads parent identity from the inbound instance
    and injects it as Temporal headers so activities inherit it."""

    def _make_outbound(
        self,
        *,
        correlation_id: str = "",
        parent_workflow_id: str = "",
        parent_run_id: str = "",
    ):
        inbound = _LogWorkflowInboundInterceptor(MagicMock())
        inbound._correlation_id = correlation_id
        inbound._parent_workflow_id = parent_workflow_id
        inbound._parent_run_id = parent_run_id
        return _LogWorkflowOutboundInterceptor(MagicMock(), inbound)

    def test_omits_parent_headers_when_empty(self):
        outbound = self._make_outbound(correlation_id="cid")
        result = outbound._inject({})

        assert "atlan-parent-workflow-id" not in result
        assert "atlan-parent-run-id" not in result
        assert "x-correlation-id" in result

    def test_returns_unchanged_when_all_empty(self):
        outbound = self._make_outbound()
        result = outbound._inject({})
        assert result == {}

    def test_injects_parent_headers_when_present(self):
        outbound = self._make_outbound(
            correlation_id="cid",
            parent_workflow_id="A",
            parent_run_id="A_run",
        )
        result = outbound._inject({})

        converter = default_converter().payload_converter
        assert (
            converter.from_payload(result["atlan-parent-workflow-id"], type_hint=str)
            == "A"
        )
        assert (
            converter.from_payload(result["atlan-parent-run-id"], type_hint=str)
            == "A_run"
        )

    def test_injects_only_parent_workflow_id_when_run_id_missing(self):
        outbound = self._make_outbound(parent_workflow_id="A")
        result = outbound._inject({})

        assert "atlan-parent-workflow-id" in result
        assert "atlan-parent-run-id" not in result


class TestActivityInboundReadsParentHeaders:
    """Activity inbound reads ``atlan-parent-*`` headers and stores them on
    the activity's ExecutionContext."""

    @pytest.fixture
    def mock_next(self):
        n = AsyncMock()
        n.execute_activity = AsyncMock(return_value=None)
        return n

    @pytest.fixture
    def interceptor(self, mock_next):
        return _LogActivityInboundInterceptor(mock_next)

    async def test_no_parent_headers_leaves_execution_context_empty(self, interceptor):
        with patch(
            "application_sdk.execution._temporal.interceptors.log.activity"
        ) as mock_act:
            mock_act.info.return_value = MockActivityInfo()
            await interceptor.execute_activity(MockExecuteActivityInput(headers={}))

        ctx = _execution_ctx.get()
        assert ctx.parent_workflow_id == ""
        assert ctx.parent_run_id == ""

    async def test_parent_headers_populate_execution_context(self, interceptor):
        headers = {
            "atlan-parent-workflow-id": _encode_header("A"),
            "atlan-parent-run-id": _encode_header("A_run"),
        }
        with patch(
            "application_sdk.execution._temporal.interceptors.log.activity"
        ) as mock_act:
            mock_act.info.return_value = MockActivityInfo()
            await interceptor.execute_activity(
                MockExecuteActivityInput(headers=headers)
            )

        ctx = _execution_ctx.get()
        assert ctx.parent_workflow_id == "A"
        assert ctx.parent_run_id == "A_run"
