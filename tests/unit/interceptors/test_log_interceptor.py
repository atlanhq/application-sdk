"""Unit tests for the LogInterceptor."""

from __future__ import annotations

from dataclasses import dataclass, field
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from temporalio.converter import default as default_converter
from temporalio.exceptions import ApplicationError

from application_sdk.errors.leaves import AuthError, InvalidInputError
from application_sdk.execution._temporal.interceptors.log import (
    _APP_NAME_MAX_CHARS,
    _HEADER_APP_NAME,
    LogInterceptor,
    _correlation_id_or_empty,
    _extract_failure_attrs,
    _LogActivityInboundInterceptor,
    _LogWorkflowInboundInterceptor,
    _LogWorkflowOutboundInterceptor,
)
from application_sdk.observability.context import (
    ExecutionContext,
    _execution_ctx,
    _replay_predicate,
    get_execution_context,
)
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
    _replay_predicate.set(None)
    yield
    _correlation_ctx.set(None)
    _execution_ctx.set(ExecutionContext())
    _replay_predicate.set(None)


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
# TestExtractFailureAttrs
# ---------------------------------------------------------------------------


class TestExtractFailureAttrs:
    def test_none_returns_empty(self):
        assert _extract_failure_attrs(None) == {}

    def test_non_app_exception_returns_empty(self):
        assert _extract_failure_attrs(ValueError("oops")) == {}

    def test_direct_apperror(self):
        attrs = _extract_failure_attrs(AuthError(message="bad creds"))
        assert attrs == {
            "failure.category": "AUTH",
            "failure.audience": "USER",
            "failure.code": "AUTH",
        }

    def test_unwraps_cause_chain(self):
        # Common shape: outer wrapper raised "from" the SDK error.
        leaf = InvalidInputError(message="missing field")
        outer = RuntimeError("wrapped")
        outer.__cause__ = leaf
        attrs = _extract_failure_attrs(outer)
        assert attrs["failure.category"] == "INVALID_INPUT"

    def test_extracts_from_application_error_details(self):
        leaf = AuthError(message="bad creds")
        app_err = ApplicationError(
            "bad creds",
            leaf.to_failure_details(),
            type="AuthError",
            non_retryable=True,
        )
        attrs = _extract_failure_attrs(app_err)
        assert attrs == {
            "failure.category": "AUTH",
            "failure.audience": "USER",
            "failure.code": "AUTH",
        }

    def test_handles_self_cycle(self):
        # Pathological case — exception that points to itself via __context__.
        # Helper must not loop forever.
        exc = RuntimeError("loop")
        exc.__context__ = exc
        assert _extract_failure_attrs(exc) == {}

    def test_extracts_from_dict_details_post_serde(self):
        # Workflow-side shape: after activity → workflow boundary, Temporal's
        # pydantic_data_converter reconstructs ApplicationError.details from
        # JSON without a target type, so the entry is a plain dict — not a
        # FailureDetails model. The classifier must still recover the labels.
        leaf = AuthError(message="bad creds")
        wire = leaf.to_failure_details().model_dump(mode="json")
        app_err = ApplicationError(
            "bad creds",
            wire,
            type="AuthError",
            non_retryable=True,
        )
        attrs = _extract_failure_attrs(app_err)
        assert attrs == {
            "failure.category": "AUTH",
            "failure.audience": "USER",
            "failure.code": "AUTH",
        }

    def test_extracts_from_dict_details_through_cause_chain(self):
        # ActivityError-style wrapping: workflow catches ActivityError whose
        # __cause__ is the rehydrated ApplicationError with dict-shaped details.
        leaf = InvalidInputError(message="missing field", field="account")
        wire = leaf.to_failure_details().model_dump(mode="json")
        inner = ApplicationError(
            "missing field", wire, type="InvalidInputError", non_retryable=True
        )
        outer = RuntimeError("Activity task failed")
        outer.__cause__ = inner
        attrs = _extract_failure_attrs(outer)
        assert attrs["failure.category"] == "INVALID_INPUT"
        assert attrs["failure.audience"] == "USER"

    def test_ignores_unrelated_dict_details(self):
        # ApplicationError.details may carry arbitrary user-supplied payloads.
        # A dict that doesn't match the FailureDetails shape must not be
        # mistaken for a typed envelope.
        app_err = ApplicationError(
            "oops",
            {"unrelated": "payload"},
            type="Custom",
        )
        assert _extract_failure_attrs(app_err) == {}


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

    async def test_skips_log_emission_on_replay(self, interceptor, mock_next):
        # On replay the lifecycle log lines must NOT be emitted (they would
        # double-count workflow.started / workflow.ended across attempts).
        with patch(
            "application_sdk.execution._temporal.interceptors.log.workflow"
        ) as mock_wf:
            mock_wf.unsafe.is_replaying.return_value = True
            mock_wf.info.return_value = MockWorkflowInfo()
            mock_wf.memo.return_value = {}
            with patch(
                "application_sdk.execution._temporal.interceptors.log.logger"
            ) as mock_logger:
                await interceptor.execute_workflow(MockExecuteWorkflowInput())

        mock_logger.info.assert_not_called()
        mock_logger.error.assert_not_called()
        # …but ``next.execute_workflow`` must still run so the wrapped workflow
        # can replay its commands.
        mock_next.execute_workflow.assert_awaited_once()

    async def test_preflight_block_logs_terse_not_error(self, interceptor, mock_next):
        # A deliberate preflight-gate block (type="PreflightFailed") is an
        # expected, typed outcome — workflow.ended logs at WARNING with no stack,
        # not ERROR + exc_info.
        mock_next.execute_workflow = AsyncMock(
            side_effect=ApplicationError(
                "Preflight check failed", type="PreflightFailed", non_retryable=True
            )
        )
        with patch(
            "application_sdk.execution._temporal.interceptors.log.workflow"
        ) as mock_wf:
            mock_wf.unsafe.is_replaying.return_value = False
            mock_wf.info.return_value = MockWorkflowInfo()
            mock_wf.memo.return_value = {}
            with patch(
                "application_sdk.execution._temporal.interceptors.log.logger"
            ) as mock_logger:
                with pytest.raises(ApplicationError):
                    await interceptor.execute_workflow(MockExecuteWorkflowInput())

        ended_warn = [
            c
            for c in mock_logger.warning.call_args_list
            if c.args and c.args[0].startswith("workflow.ended")
        ]
        ended_error = [
            c
            for c in mock_logger.error.call_args_list
            if c.args and c.args[0].startswith("workflow.ended")
        ]
        assert ended_warn, "preflight block should log workflow.ended at warning"
        assert not ended_error, "preflight block must not log workflow.ended at error"
        assert "exc_info" not in ended_warn[0].kwargs

    async def test_cause_wrapped_preflight_block_logs_terse(
        self, interceptor, mock_next
    ):
        # Temporal wraps the activity's ApplicationError in an ActivityError, so
        # the PreflightFailed marker sits on a cause, not the top-level error.
        inner = ApplicationError(
            "Preflight check failed", type="PreflightFailed", non_retryable=True
        )
        wrapper = RuntimeError("Activity task failed")
        wrapper.__cause__ = inner
        mock_next.execute_workflow = AsyncMock(side_effect=wrapper)
        with patch(
            "application_sdk.execution._temporal.interceptors.log.workflow"
        ) as mock_wf:
            mock_wf.unsafe.is_replaying.return_value = False
            mock_wf.info.return_value = MockWorkflowInfo()
            mock_wf.memo.return_value = {}
            with patch(
                "application_sdk.execution._temporal.interceptors.log.logger"
            ) as mock_logger:
                with pytest.raises(RuntimeError):
                    await interceptor.execute_workflow(MockExecuteWorkflowInput())

        ended_warn = [
            c
            for c in mock_logger.warning.call_args_list
            if c.args and c.args[0].startswith("workflow.ended")
        ]
        ended_error = [
            c
            for c in mock_logger.error.call_args_list
            if c.args and c.args[0].startswith("workflow.ended")
        ]
        assert ended_warn, "cause-wrapped preflight block should log at warning"
        assert not ended_error, "cause-wrapped preflight block must not log at error"
        assert "exc_info" not in ended_warn[0].kwargs

    async def test_unexpected_failure_logs_error_with_stack(
        self, interceptor, mock_next
    ):
        # A non-preflight failure keeps the full ERROR traceback (exc_info=True).
        mock_next.execute_workflow = AsyncMock(side_effect=ValueError("boom"))
        with patch(
            "application_sdk.execution._temporal.interceptors.log.workflow"
        ) as mock_wf:
            mock_wf.unsafe.is_replaying.return_value = False
            mock_wf.info.return_value = MockWorkflowInfo()
            mock_wf.memo.return_value = {}
            with patch(
                "application_sdk.execution._temporal.interceptors.log.logger"
            ) as mock_logger:
                with pytest.raises(ValueError):
                    await interceptor.execute_workflow(MockExecuteWorkflowInput())

        ended_error = [
            c
            for c in mock_logger.error.call_args_list
            if c.args and c.args[0].startswith("workflow.ended")
        ]
        assert ended_error, "unexpected failure should log workflow.ended at error"
        assert ended_error[0].kwargs.get("exc_info") is True

    async def test_sets_correlation_id_on_replay_from_header(
        self, interceptor, mock_next
    ):
        # Regression: a worker that picks up an in-flight workflow rebuilds
        # state under is_replaying() == True. The interceptor must still
        # resolve correlation_id (here from the header injected by the parent)
        # and stash it on self so the outbound interceptor can re-inject it
        # on workflow-issued commands during/after replay.
        payload = _encode_header("inherited-corr-id")
        headers = {"x-correlation-id": payload}

        with patch(
            "application_sdk.execution._temporal.interceptors.log.workflow"
        ) as mock_wf:
            mock_wf.unsafe.is_replaying.return_value = True
            mock_wf.info.return_value = MockWorkflowInfo()
            mock_wf.memo.return_value = {}
            await interceptor.execute_workflow(
                MockExecuteWorkflowInput(headers=headers)
            )

        assert interceptor._correlation_id == "inherited-corr-id"
        ctx = get_correlation_context()
        assert ctx is not None
        assert ctx.correlation_id == "inherited-corr-id"

    async def test_sets_correlation_id_on_replay_from_memo(
        self, interceptor, mock_next
    ):
        # Continue-as-new path under replay: correlation_id comes from the
        # workflow memo and must still land on self / the ContextVar.
        with patch(
            "application_sdk.execution._temporal.interceptors.log.workflow"
        ) as mock_wf:
            mock_wf.unsafe.is_replaying.return_value = True
            mock_wf.info.return_value = MockWorkflowInfo()
            mock_wf.memo.return_value = {"correlation_id": "memo-corr-id"}
            await interceptor.execute_workflow(MockExecuteWorkflowInput())

        assert interceptor._correlation_id == "memo-corr-id"

    async def test_sets_parent_identity_on_replay(self, interceptor, mock_next):
        # Outbound activity-header injection relies on self._parent_*; these
        # must be populated on replay too.
        with patch(
            "application_sdk.execution._temporal.interceptors.log.workflow"
        ) as mock_wf:
            mock_wf.unsafe.is_replaying.return_value = True
            mock_wf.info.return_value = MockWorkflowInfo(
                parent=MockParentInfo(
                    workflow_id="parent-wf-42", run_id="parent-run-42"
                )
            )
            mock_wf.memo.return_value = {}
            await interceptor.execute_workflow(MockExecuteWorkflowInput())

        assert interceptor._parent_workflow_id == "parent-wf-42"
        assert interceptor._parent_run_id == "parent-run-42"

    async def test_outbound_injects_header_after_replay_setup(
        self, interceptor, mock_next
    ):
        # End-to-end shape of the bug: after the inbound runs under replay,
        # the outbound interceptor must still be able to inject the
        # ``x-correlation-id`` header on a workflow-issued command.
        payload = _encode_header("inherited-corr-id")
        headers = {"x-correlation-id": payload}

        with patch(
            "application_sdk.execution._temporal.interceptors.log.workflow"
        ) as mock_wf:
            mock_wf.unsafe.is_replaying.return_value = True
            mock_wf.info.return_value = MockWorkflowInfo()
            mock_wf.memo.return_value = {}
            await interceptor.execute_workflow(
                MockExecuteWorkflowInput(headers=headers)
            )

        outbound = _LogWorkflowOutboundInterceptor(MagicMock(), interceptor)
        injected = outbound._inject({})
        assert "x-correlation-id" in injected
        decoded = default_converter().payload_converter.from_payload(
            injected["x-correlation-id"], type_hint=str
        )
        assert decoded == "inherited-corr-id"

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
            c
            for c in mock_logger.info.call_args_list
            if c[0][0].startswith("workflow.started")
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
            c
            for c in mock_logger.info.call_args_list
            if c[0][0].startswith("workflow.ended")
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
            c
            for c in mock_logger.error.call_args_list
            if c[0][0].startswith("workflow.ended")
        ]
        assert len(ended_calls) == 1
        kwargs = ended_calls[0][1]
        assert kwargs["otel.status_code"] == "ERROR"
        assert kwargs["exc_info"] is True
        # Raw ValueError has no SDK classification — failure.* keys absent so
        # downstream consumers can tell "uncategorised" from a real category.
        assert "failure.category" not in kwargs

    async def test_workflow_ended_flattens_failure_attrs_for_apperror(self, mock_next):
        # AppError raised directly inside the workflow → interceptor extracts
        # category/audience/code from the class-level ClassVars onto the
        # workflow.ended ERROR log.
        mock_next.execute_workflow = AsyncMock(
            side_effect=AuthError(message="bad creds")
        )
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
                pytest.raises(AuthError),
            ):
                await interceptor.execute_workflow(MockExecuteWorkflowInput())

        ended_calls = [
            c
            for c in mock_logger.error.call_args_list
            if c[0][0].startswith("workflow.ended")
        ]
        assert len(ended_calls) == 1
        kwargs = ended_calls[0][1]
        assert kwargs["failure.category"] == "AUTH"
        assert kwargs["failure.audience"] == "USER"
        assert kwargs["failure.code"] == "AUTH"

    async def test_workflow_ended_flattens_failure_attrs_from_application_error(
        self, mock_next
    ):
        # Activity wrappers re-raise as ApplicationError(..., FailureDetails) —
        # workflow-side propagation must still surface the original category.
        leaf = InvalidInputError(message="missing field", field="hostname")
        app_err = ApplicationError(
            "missing field",
            leaf.to_failure_details(),
            type="InvalidInputError",
            non_retryable=True,
        )
        mock_next.execute_workflow = AsyncMock(side_effect=app_err)
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
                pytest.raises(ApplicationError),
            ):
                await interceptor.execute_workflow(MockExecuteWorkflowInput())

        ended_calls = [
            c
            for c in mock_logger.error.call_args_list
            if c[0][0].startswith("workflow.ended")
        ]
        kwargs = ended_calls[0][1]
        assert kwargs["failure.category"] == "INVALID_INPUT"
        assert kwargs["failure.audience"] == "USER"
        assert kwargs["failure.code"] == "INVALID_INPUT"

    async def test_defaults_correlation_id_to_run_id_when_no_headers_no_memo(
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
        # CNCT-104: no memo/header/args → correlation defaults to the run's
        # own Temporal run_id (a discoverable identity), not a random uuid4.
        assert interceptor._correlation_id == "run-id"

    async def test_restores_correlation_id_from_memo(self, interceptor):
        with patch(
            "application_sdk.execution._temporal.interceptors.log.workflow"
        ) as mock_wf:
            mock_wf.unsafe.is_replaying.return_value = False
            mock_wf.info.return_value = MockWorkflowInfo()
            mock_wf.memo.return_value = {"correlation_id": "memo-id-123"}
            await interceptor.execute_workflow(MockExecuteWorkflowInput(headers={}))

        assert interceptor._correlation_id == "memo-id-123"

    async def test_reads_correlation_id_from_args_when_no_memo_no_headers(
        self, interceptor
    ):
        # Legacy args-based propagation: pre-v3 CorrelationContextInterceptor
        # (still in use on SDK 2.8.7 callers like the automation-engine) puts
        # correlation_id in the first arg dict. Priority 3 must surface it so
        # the chain stays intact without forcing every caller to migrate to
        # memo / header on start_workflow.
        with patch(
            "application_sdk.execution._temporal.interceptors.log.workflow"
        ) as mock_wf:
            mock_wf.unsafe.is_replaying.return_value = False
            mock_wf.info.return_value = MockWorkflowInfo()
            mock_wf.memo.return_value = {}
            await interceptor.execute_workflow(
                MockExecuteWorkflowInput(
                    headers={}, args=[{"correlation_id": "from-args-id"}]
                )
            )

        assert interceptor._correlation_id == "from-args-id"

    async def test_args_correlation_id_is_lower_priority_than_memo(self, interceptor):
        # Memo wins over args — memo is the explicit / preferred channel.
        with patch(
            "application_sdk.execution._temporal.interceptors.log.workflow"
        ) as mock_wf:
            mock_wf.unsafe.is_replaying.return_value = False
            mock_wf.info.return_value = MockWorkflowInfo()
            mock_wf.memo.return_value = {"correlation_id": "memo-wins"}
            await interceptor.execute_workflow(
                MockExecuteWorkflowInput(
                    headers={}, args=[{"correlation_id": "args-loses"}]
                )
            )

        assert interceptor._correlation_id == "memo-wins"

    async def test_args_correlation_id_is_lower_priority_than_header(self, interceptor):
        # Header wins over args — header is the explicit / preferred channel
        # for child-workflow inheritance.
        payload = _encode_header("header-wins")
        with patch(
            "application_sdk.execution._temporal.interceptors.log.workflow"
        ) as mock_wf:
            mock_wf.unsafe.is_replaying.return_value = False
            mock_wf.info.return_value = MockWorkflowInfo()
            mock_wf.memo.return_value = {}
            await interceptor.execute_workflow(
                MockExecuteWorkflowInput(
                    headers={"x-correlation-id": payload},
                    args=[{"correlation_id": "args-loses"}],
                )
            )

        assert interceptor._correlation_id == "header-wins"

    async def test_falls_through_args_when_first_arg_is_not_a_dict(self, interceptor):
        # Typed args (Pydantic model, dataclass, primitive) are skipped
        # silently — those callers should use memo / header. Verifies we
        # fall through to the run_id fallback instead of crashing.
        with patch(
            "application_sdk.execution._temporal.interceptors.log.workflow"
        ) as mock_wf:
            mock_wf.unsafe.is_replaying.return_value = False
            mock_wf.info.return_value = MockWorkflowInfo()
            mock_wf.memo.return_value = {}
            await interceptor.execute_workflow(
                MockExecuteWorkflowInput(headers={}, args=["just-a-string"])
            )

        assert interceptor._correlation_id != "just-a-string"
        # Falls back to the priority-4 run_id path (CNCT-104): the run's own
        # Temporal run_id, never a random uuid the run page can't query.
        assert interceptor._correlation_id == "run-id"

    async def test_falls_through_args_when_dict_lacks_correlation_id_key(
        self, interceptor
    ):
        # Dict args without the magic key fall through cleanly to the run_id
        # fallback rather than raising or returning an empty string.
        with patch(
            "application_sdk.execution._temporal.interceptors.log.workflow"
        ) as mock_wf:
            mock_wf.unsafe.is_replaying.return_value = False
            mock_wf.info.return_value = MockWorkflowInfo()
            mock_wf.memo.return_value = {}
            await interceptor.execute_workflow(
                MockExecuteWorkflowInput(
                    headers={}, args=[{"workflow_id": "wf-1", "other": "field"}]
                )
            )

        assert interceptor._correlation_id == "run-id"

    async def test_reads_correlation_id_from_typed_object_with_attr(self, interceptor):
        # Real-world v3 case: the SDK-generated workflow wrapper takes a
        # typed ``Input`` instance (Pydantic model / dataclass / namespace).
        # args[0] reaches the interceptor as that typed object, not a dict.
        # We must still find correlation_id via attribute access.
        @dataclass
        class TypedInput:
            workflow_id: str = "wf-1"
            correlation_id: str = "from-typed-input"

        with patch(
            "application_sdk.execution._temporal.interceptors.log.workflow"
        ) as mock_wf:
            mock_wf.unsafe.is_replaying.return_value = False
            mock_wf.info.return_value = MockWorkflowInfo()
            mock_wf.memo.return_value = {}
            await interceptor.execute_workflow(
                MockExecuteWorkflowInput(headers={}, args=[TypedInput()])
            )

        assert interceptor._correlation_id == "from-typed-input"

    async def test_reads_correlation_id_from_pydantic_extra_bag(self, interceptor):
        # Pydantic v2 models with ``model_config = ConfigDict(extra='allow')``
        # stash undeclared fields on ``__pydantic_extra__``. The caller-supplied
        # ``correlation_id`` lives there when the model doesn't declare it as
        # a field. Cover that bag too.
        class FakeExtraBagModel:
            def __init__(self):
                # No declared correlation_id attribute — only on the extras
                # dict. ``getattr(self, 'correlation_id', None)`` returns None.
                self.__pydantic_extra__ = {"correlation_id": "from-extras-bag"}

        with patch(
            "application_sdk.execution._temporal.interceptors.log.workflow"
        ) as mock_wf:
            mock_wf.unsafe.is_replaying.return_value = False
            mock_wf.info.return_value = MockWorkflowInfo()
            mock_wf.memo.return_value = {}
            await interceptor.execute_workflow(
                MockExecuteWorkflowInput(headers={}, args=[FakeExtraBagModel()])
            )

        assert interceptor._correlation_id == "from-extras-bag"

    async def test_falls_through_typed_object_without_correlation_id(self, interceptor):
        # Typed object that genuinely has no correlation_id (no attribute
        # declared, no pydantic extras bag) falls through to priority 4.
        # Common case: v3 workflows whose Input contract doesn't carry the
        # correlation field at all — those callers should use memo / header.
        @dataclass
        class TypedInputWithoutCorrId:
            workflow_id: str = "wf-1"
            some_other_field: int = 42

        with patch(
            "application_sdk.execution._temporal.interceptors.log.workflow"
        ) as mock_wf:
            mock_wf.unsafe.is_replaying.return_value = False
            mock_wf.info.return_value = MockWorkflowInfo()
            mock_wf.memo.return_value = {}
            await interceptor.execute_workflow(
                MockExecuteWorkflowInput(headers={}, args=[TypedInputWithoutCorrId()])
            )

        # run_id fallback (CNCT-104) — correlation defaults to the run's own
        # Temporal run_id so the identity is always discoverable.
        assert interceptor._correlation_id == "run-id"

    async def test_priority_4_defaults_to_workflow_run_id(self, interceptor):
        # CNCT-104: a top-level workflow with no memo / header / args
        # correlation gets its own Temporal run_id — never a random uuid4.
        # A uuid4 here exists nowhere else in the platform, so the run page
        # (which queries by correlation_id) rendered such runs as "no logs".
        with patch(
            "application_sdk.execution._temporal.interceptors.log.workflow"
        ) as mock_wf:
            mock_wf.unsafe.is_replaying.return_value = False
            mock_wf.info.return_value = MockWorkflowInfo(run_id="the-run-id")
            mock_wf.memo.return_value = {}
            await interceptor.execute_workflow(
                MockExecuteWorkflowInput(headers={}, args=[])
            )

        assert interceptor._correlation_id == "the-run-id"

    async def test_priority_4_uuid_last_resort_when_run_id_empty(self, interceptor):
        # Defensive last resort: an empty run_id (should be unreachable in a
        # real workflow) still mints a uuid4 rather than returning an empty
        # correlation — an empty string would silently break header injection.
        with patch(
            "application_sdk.execution._temporal.interceptors.log.workflow"
        ) as mock_wf:
            mock_wf.unsafe.is_replaying.return_value = False
            mock_wf.info.return_value = MockWorkflowInfo(run_id="")
            mock_wf.memo.return_value = {}
            await interceptor.execute_workflow(
                MockExecuteWorkflowInput(headers={}, args=[])
            )

        # Empty run_id → falls to the uuid4 last resort (36 chars, hyphens).
        assert len(interceptor._correlation_id) == 36

    def test_priority_4_uuid_last_resort_when_info_raises(self, interceptor):
        # The third fallback branch: workflow.info() itself raising inside
        # _resolve_correlation_id must land on the uuid4 last resort, not
        # propagate — logging identity must never break workflow execution.
        with patch(
            "application_sdk.execution._temporal.interceptors.log.workflow"
        ) as mock_wf:
            mock_wf.memo.return_value = {}
            mock_wf.info.side_effect = RuntimeError("info unavailable")
            cid = interceptor._resolve_correlation_id(
                MockExecuteWorkflowInput(headers={}, args=[])
            )

        assert len(cid) == 36

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
# TestAppNameResolution (CNCT-93 — per-entrypoint app_name)
# ---------------------------------------------------------------------------


class TestAppNameResolution:
    """The workflow resolves its own ``app_name`` from its input args (never
    from memo or an inherited header), stores it on the shared ExecutionContext
    (read by the logger; metric labels stay connector-level by design), and
    propagates it to activities. Guards per-entrypoint log attribution and
    backward compatibility."""

    @pytest.fixture
    def mock_next(self):
        n = AsyncMock()
        n.execute_workflow = AsyncMock(return_value="wf-result")
        return n

    @pytest.fixture
    def interceptor(self, mock_next):
        return _LogWorkflowInboundInterceptor(mock_next)

    async def _run(self, interceptor, wf_input):
        with patch(
            "application_sdk.execution._temporal.interceptors.log.workflow"
        ) as mock_wf:
            mock_wf.unsafe.is_replaying.return_value = False
            mock_wf.info.return_value = MockWorkflowInfo()
            mock_wf.memo.return_value = {}
            await interceptor.execute_workflow(wf_input)

    async def test_reads_app_name_from_workflow_args(self, interceptor):
        # The toolkit stamps the node's own app_name into inputs.args; the
        # workflow reads it and puts it on the correlation context so log lines
        # attribute to the per-entrypoint app (e.g. powerbi-crawler).
        await self._run(
            interceptor,
            MockExecuteWorkflowInput(
                headers={}, args=[{"app_name": "powerbi-crawler"}]
            ),
        )
        assert interceptor._app_name == "powerbi-crawler"
        assert get_execution_context().app_name == "powerbi-crawler"

    async def test_app_name_absent_falls_back_to_empty(self, interceptor):
        # Older / not-yet-regenerated apps carry no app_name in args → context
        # app_name stays "" so the logger falls back to
        # ATLAN_APPLICATION_NAME (backward compatible).
        await self._run(
            interceptor,
            MockExecuteWorkflowInput(headers={}, args=[{"workflow_id": "w1"}]),
        )
        assert interceptor._app_name == ""
        assert get_execution_context().app_name == ""

    async def test_reads_app_name_from_typed_object_attr(self, interceptor):
        class TypedInput:
            app_name = "sql-server-crawler"

        await self._run(
            interceptor, MockExecuteWorkflowInput(headers={}, args=[TypedInput()])
        )
        assert interceptor._app_name == "sql-server-crawler"

    async def test_reads_app_name_from_pydantic_extra_bag(self, interceptor):
        class FakeExtraBagModel:
            # Pydantic v2 extra='allow' stows unknown fields here; no attr.
            app_name = None
            __pydantic_extra__ = {"app_name": "bundle-miner"}

        await self._run(
            interceptor,
            MockExecuteWorkflowInput(headers={}, args=[FakeExtraBagModel()]),
        )
        assert interceptor._app_name == "bundle-miner"

    async def test_child_workflow_does_not_inherit_parent_app_name(self, interceptor):
        # A child receives the parent's x-app-name header (the outbound injects
        # it), but the workflow inbound resolves app_name ONLY from its own args
        # — so a header without a matching arg yields "". Each bundle entrypoint
        # keeps its own identity; no parent -> child inheritance.
        headers = {_HEADER_APP_NAME: _encode_header("powerbi-crawler")}
        await self._run(
            interceptor, MockExecuteWorkflowInput(headers=headers, args=[{}])
        )
        assert interceptor._app_name == ""
        assert get_execution_context().app_name == ""

    async def test_outbound_injects_app_name_header(self, interceptor):
        # After the workflow resolves its app_name, the outbound interceptor
        # must inject it as x-app-name so the activity inbound can inherit it.
        await self._run(
            interceptor,
            MockExecuteWorkflowInput(
                headers={}, args=[{"app_name": "powerbi-crawler"}]
            ),
        )
        outbound = _LogWorkflowOutboundInterceptor(MagicMock(), interceptor)
        injected = outbound._inject({})
        assert _HEADER_APP_NAME in injected
        decoded = default_converter().payload_converter.from_payload(
            injected[_HEADER_APP_NAME], type_hint=str
        )
        assert decoded == "powerbi-crawler"

    async def test_outbound_omits_app_name_header_when_absent(self, interceptor):
        # No app_name resolved → no x-app-name header (nothing to propagate).
        await self._run(interceptor, MockExecuteWorkflowInput(headers={}, args=[{}]))
        outbound = _LogWorkflowOutboundInterceptor(MagicMock(), interceptor)
        injected = outbound._inject({})
        assert _HEADER_APP_NAME not in injected

    async def test_sets_app_name_on_replay(self, interceptor):
        # app_name resolution sits in the must-run-on-every-replay block (like
        # correlation_id / parent identity); a worker picking up an in-flight
        # workflow must still stamp the per-entrypoint app_name, not revert to
        # the connector-level env default.
        with patch(
            "application_sdk.execution._temporal.interceptors.log.workflow"
        ) as mock_wf:
            mock_wf.unsafe.is_replaying.return_value = True
            mock_wf.info.return_value = MockWorkflowInfo()
            mock_wf.memo.return_value = {}
            await interceptor.execute_workflow(
                MockExecuteWorkflowInput(
                    headers={}, args=[{"app_name": "powerbi-crawler"}]
                )
            )
        assert interceptor._app_name == "powerbi-crawler"
        assert get_execution_context().app_name == "powerbi-crawler"
        # The resumed worker must still PROPAGATE it: a subsequent outbound inject
        # must carry x-app-name, or activities started after replay would silently
        # lose per-entrypoint attribution mid-run.
        outbound = _LogWorkflowOutboundInterceptor(MagicMock(), interceptor)
        assert _HEADER_APP_NAME in outbound._inject({})

    async def test_resolve_app_name_swallows_exception(self, interceptor):
        # A non-AttributeError raised while probing args[0] must be swallowed
        # (returns "" → env fallback), never propagated — mirrors the sibling
        # _resolve_correlation_id forced-failure guarantee.
        class RaisingInput:
            @property
            def app_name(self):
                raise RuntimeError("boom")

        await self._run(
            interceptor,
            MockExecuteWorkflowInput(headers={}, args=[RaisingInput()]),
        )
        assert interceptor._app_name == ""
        assert get_execution_context().app_name == ""

    async def test_non_str_app_name_rejected(self, interceptor):
        # A non-string args["app_name"] (e.g. a run id / int / dict) must NOT be
        # str()-coerced into the log attribution field — it is rejected → "" →
        # env fallback, so an arbitrary object's repr never becomes app_name.
        await self._run(
            interceptor,
            MockExecuteWorkflowInput(headers={}, args=[{"app_name": 12345}]),
        )
        assert interceptor._app_name == ""
        assert get_execution_context().app_name == ""

    async def test_oversized_app_name_truncated(self, interceptor):
        # An over-long value is capped at the SDK boundary (_APP_NAME_MAX_CHARS)
        # before it can reach the log fields.
        await self._run(
            interceptor,
            MockExecuteWorkflowInput(headers={}, args=[{"app_name": "x" * 200}]),
        )
        assert interceptor._app_name == "x" * _APP_NAME_MAX_CHARS
        assert len(interceptor._app_name) == 64


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

    async def test_inherits_app_name_from_workflow_header(self, interceptor):
        # activity.Info exposes no app_name; the activity inherits the parent
        # workflow's via the x-app-name header so activity + @task logs stamp
        # the same per-entrypoint app_name (CNCT-93).
        headers = {_HEADER_APP_NAME: _encode_header("powerbi-crawler")}
        with patch(
            "application_sdk.execution._temporal.interceptors.log.activity"
        ) as mock_act:
            mock_act.info.return_value = MockActivityInfo()
            await interceptor.execute_activity(
                MockExecuteActivityInput(headers=headers)
            )
        assert get_execution_context().app_name == "powerbi-crawler"

    async def test_no_app_name_header_leaves_context_without_app_name(
        self, interceptor
    ):
        # Older apps propagate no x-app-name header → the activity's execution
        # context carries no app_name (the logger falls back to
        # ATLAN_APPLICATION_NAME), while correlation still resolves. Backward
        # compatible.
        payload = _encode_header("corr-1")
        with patch(
            "application_sdk.execution._temporal.interceptors.log.activity"
        ) as mock_act:
            mock_act.info.return_value = MockActivityInfo()
            await interceptor.execute_activity(
                MockExecuteActivityInput(headers={"x-correlation-id": payload})
            )
        assert get_correlation_context().correlation_id == "corr-1"
        assert get_execution_context().app_name == ""

    async def test_app_name_header_decode_swallows_exception(self, interceptor):
        # A malformed x-app-name header payload must not blow up the activity;
        # the decode failure is swallowed and app_name stays "" (env fallback).
        with patch(
            "application_sdk.execution._temporal.interceptors.log.activity"
        ) as mock_act:
            mock_act.info.return_value = MockActivityInfo()
            await interceptor.execute_activity(
                MockExecuteActivityInput(headers={_HEADER_APP_NAME: object()})
            )
        assert get_execution_context().app_name == ""

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
            c
            for c in mock_logger.info.call_args_list
            if c[0][0].startswith("activity.started")
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
            c
            for c in mock_logger.info.call_args_list
            if c[0][0].startswith("activity.ended")
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
            c
            for c in mock_logger.error.call_args_list
            if c[0][0].startswith("activity.ended")
        ]
        assert len(ended_calls) == 1
        kwargs = ended_calls[0][1]
        assert kwargs["otel.status_code"] == "ERROR"
        assert kwargs["exc_info"] is True

    async def test_preflight_block_activity_ended_terse(self, mock_next):
        # A deliberate preflight-gate block is an expected, typed outcome —
        # activity.ended logs at WARNING with no stack (mirrors workflow.ended).
        mock_next.execute_activity = AsyncMock(
            side_effect=ApplicationError(
                "Preflight failed", type="PreflightFailed", non_retryable=True
            )
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
                pytest.raises(ApplicationError),
            ):
                await interceptor.execute_activity(MockExecuteActivityInput())
        warn = [
            c
            for c in mock_logger.warning.call_args_list
            if c[0][0].startswith("activity.ended")
        ]
        err = [
            c
            for c in mock_logger.error.call_args_list
            if c[0][0].startswith("activity.ended")
        ]
        assert warn, "preflight block should log activity.ended at warning"
        assert not err, "preflight block must not log activity.ended at error"
        assert "exc_info" not in warn[0][1]

    async def test_cause_wrapped_preflight_block_activity_ended_terse(self, mock_next):
        # Temporal may wrap the raised error; the marker sits on a cause.
        inner = ApplicationError(
            "Preflight failed", type="PreflightFailed", non_retryable=True
        )
        wrapper = RuntimeError("activity task failed")
        wrapper.__cause__ = inner
        mock_next.execute_activity = AsyncMock(side_effect=wrapper)
        interceptor = _LogActivityInboundInterceptor(mock_next)
        with patch(
            "application_sdk.execution._temporal.interceptors.log.activity"
        ) as mock_act:
            mock_act.info.return_value = MockActivityInfo()
            with (
                patch(
                    "application_sdk.execution._temporal.interceptors.log.logger"
                ) as mock_logger,
                pytest.raises(RuntimeError),
            ):
                await interceptor.execute_activity(MockExecuteActivityInput())
        warn = [
            c
            for c in mock_logger.warning.call_args_list
            if c[0][0].startswith("activity.ended")
        ]
        err = [
            c
            for c in mock_logger.error.call_args_list
            if c[0][0].startswith("activity.ended")
        ]
        assert (
            warn
        ), "cause-wrapped preflight block should log activity.ended at warning"
        assert not err
        assert "exc_info" not in warn[0][1]

    async def test_activity_ended_flattens_failure_attrs_for_apperror(self, mock_next):
        mock_next.execute_activity = AsyncMock(
            side_effect=AuthError(message="bad creds")
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
                pytest.raises(AuthError),
            ):
                await interceptor.execute_activity(MockExecuteActivityInput())

        ended_calls = [
            c
            for c in mock_logger.error.call_args_list
            if c[0][0].startswith("activity.ended")
        ]
        kwargs = ended_calls[0][1]
        assert kwargs["failure.category"] == "AUTH"
        assert kwargs["failure.audience"] == "USER"
        assert kwargs["failure.code"] == "AUTH"

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
            c
            for c in mock_logger.info.call_args_list
            if c[0][0].startswith("activity.started")
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


# ---------------------------------------------------------------------------
# TestReplayPredicateInjection
# ---------------------------------------------------------------------------


class TestReplayPredicateInjection:
    """The workflow log interceptor must inject the replay predicate into the
    replay_predicate ContextVar so the SDK logger can suppress workflow-body
    logs during replay without importing temporalio."""

    @pytest.fixture(autouse=True)
    def reset_replay_predicate(self):
        _replay_predicate.set(None)
        yield
        _replay_predicate.set(None)

    @pytest.fixture
    def mock_next(self):
        n = AsyncMock()
        n.execute_workflow = AsyncMock(return_value="wf-result")
        return n

    @pytest.fixture
    def interceptor(self, mock_next):
        return _LogWorkflowInboundInterceptor(mock_next)

    async def test_injects_predicate_on_live_execution(self, interceptor, mock_next):
        """On a live (non-replay) execution the predicate must be set to the
        ``workflow.unsafe.is_replaying_history_events`` callable."""
        with patch(
            "application_sdk.execution._temporal.interceptors.log.workflow"
        ) as mock_wf:
            mock_wf.unsafe.is_replaying.return_value = False
            mock_wf.unsafe.is_replaying_history_events = lambda: False
            mock_wf.info.return_value = MockWorkflowInfo()
            mock_wf.memo.return_value = {}
            await interceptor.execute_workflow(MockExecuteWorkflowInput())

        pred = _replay_predicate.get()
        assert pred is not None, "Replay predicate must be set after execute_workflow"
        assert callable(pred)

    async def test_injects_predicate_on_replay(self, interceptor, mock_next):
        """On replay the predicate must also be set (before the is_replaying()
        early-return gate) so the logger can check live state for workflow-tail
        log calls once replay finishes."""
        with patch(
            "application_sdk.execution._temporal.interceptors.log.workflow"
        ) as mock_wf:
            mock_wf.unsafe.is_replaying.return_value = True
            mock_wf.unsafe.is_replaying_history_events = lambda: True
            mock_wf.info.return_value = MockWorkflowInfo()
            mock_wf.memo.return_value = {}
            await interceptor.execute_workflow(MockExecuteWorkflowInput())

        pred = _replay_predicate.get()
        assert pred is not None, (
            "Replay predicate must be set even during replay so logger can "
            "check live state once the replay tail completes"
        )
        assert callable(pred)

    async def test_predicate_is_injected_before_replay_gate(
        self, interceptor, mock_next
    ):
        """set_replay_predicate must be called before is_replaying() is checked
        so the predicate is always present when user workflow code runs.

        Patches the module-level ``set_replay_predicate`` name in the
        interceptor module (not the ContextVar.set method, which is read-only)
        to track call order vs the ``is_replaying()`` gate.
        """
        injection_order: list[str] = []

        from application_sdk.observability.context import (
            set_replay_predicate as _orig_srp,
        )

        def _tracking_srp(pred: object) -> None:
            injection_order.append("predicate_set")
            _orig_srp(pred)  # type: ignore[arg-type]

        with (
            patch(
                "application_sdk.execution._temporal.interceptors.log.workflow"
            ) as mock_wf,
            patch(
                "application_sdk.execution._temporal.interceptors.log.set_replay_predicate",
                side_effect=_tracking_srp,
            ),
        ):
            mock_wf.unsafe.is_replaying.side_effect = lambda: (
                injection_order.append("is_replaying_checked") or True
            )
            mock_wf.unsafe.is_replaying_history_events = lambda: True
            mock_wf.info.return_value = MockWorkflowInfo()
            mock_wf.memo.return_value = {}
            await interceptor.execute_workflow(MockExecuteWorkflowInput())

        assert (
            "predicate_set" in injection_order
        ), "set_replay_predicate was never called"
        pred_idx = injection_order.index("predicate_set")
        gate_idx = injection_order.index("is_replaying_checked")
        assert pred_idx < gate_idx, (
            f"Predicate must be injected before is_replaying() gate; "
            f"got order {injection_order}"
        )


class TestLifecycleMessageBodies:
    """CNCT-105: lifecycle messages are self-describing.

    The event token stays the exact message PREFIX (compat contract for
    dashboards/alerts matching on it); the subject makes the line readable
    without the structured attributes (which many renderers drop).
    """

    @pytest.fixture
    def wf_next(self):
        n = AsyncMock()
        n.execute_workflow = AsyncMock(return_value="wf-result")
        return n

    @pytest.fixture
    def act_next(self):
        n = AsyncMock()
        n.execute_activity = AsyncMock(return_value="act-result")
        return n

    async def test_activity_started_message_names_the_activity(self, act_next):
        interceptor = _LogActivityInboundInterceptor(act_next)
        with patch(
            "application_sdk.execution._temporal.interceptors.log.activity"
        ) as mock_act:
            mock_act.info.return_value = MockActivityInfo()
            with patch(
                "application_sdk.execution._temporal.interceptors.log.logger"
            ) as mock_logger:
                await interceptor.execute_activity(MockExecuteActivityInput())
        started = [
            c[0][0]
            for c in mock_logger.info.call_args_list
            if c[0][0].startswith("activity.started")
        ]
        assert started == ["activity.started TestActivity"]

    async def test_activity_ended_ok_message_has_type_and_duration(self, act_next):
        interceptor = _LogActivityInboundInterceptor(act_next)
        with patch(
            "application_sdk.execution._temporal.interceptors.log.activity"
        ) as mock_act:
            mock_act.info.return_value = MockActivityInfo()
            with patch(
                "application_sdk.execution._temporal.interceptors.log.logger"
            ) as mock_logger:
                await interceptor.execute_activity(MockExecuteActivityInput())
        ended = [
            c[0][0]
            for c in mock_logger.info.call_args_list
            if c[0][0].startswith("activity.ended")
        ]
        assert len(ended) == 1
        assert ended[0].startswith("activity.ended TestActivity OK (")
        assert ended[0].endswith("ms)")

    async def test_activity_ended_error_message_carries_reason_and_frame(
        self, act_next
    ):
        act_next.execute_activity = AsyncMock(
            side_effect=ValueError("could not connect: timeout after 30s")
        )
        interceptor = _LogActivityInboundInterceptor(act_next)
        with patch(
            "application_sdk.execution._temporal.interceptors.log.activity"
        ) as mock_act:
            mock_act.info.return_value = MockActivityInfo()
            with patch(
                "application_sdk.execution._temporal.interceptors.log.logger"
            ) as mock_logger:
                with pytest.raises(ValueError):
                    await interceptor.execute_activity(MockExecuteActivityInput())
        errors = [
            c[0][0]
            for c in mock_logger.error.call_args_list
            if c[0][0].startswith("activity.ended")
        ]
        assert len(errors) == 1
        msg = errors[0]
        # activity.ended TestActivity FAILED (ValueError): could not connect… — at file:line in fn
        assert msg.startswith("activity.ended TestActivity FAILED (ValueError):")
        assert "could not connect: timeout after 30s" in msg
        assert " — at " in msg and " in " in msg

    async def test_workflow_started_and_ended_messages_name_the_workflow(self, wf_next):
        interceptor = _LogWorkflowInboundInterceptor(wf_next)
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
        infos = [c[0][0] for c in mock_logger.info.call_args_list]
        assert "workflow.started TestWorkflow" in infos
        assert any(m.startswith("workflow.ended TestWorkflow OK (") for m in infos)

    async def test_failure_suffix_prefers_typed_failure_code(self):
        from application_sdk.execution._temporal.interceptors.log import _failure_suffix

        exc = ValueError("boom")
        suffix = _failure_suffix(exc, {"failure.code": "auth_error"})
        assert suffix.startswith("FAILED (auth_error): boom")

    async def test_failure_suffix_without_exception_reports_unknown(self):
        # The ended log is built in a `finally`, so a status of ERROR with no
        # captured exception is reachable; the body must still name a reason.
        from application_sdk.execution._temporal.interceptors.log import _failure_suffix

        assert _failure_suffix(None, {}) == "FAILED (unknown)"

    async def test_failure_suffix_truncates_long_message(self):
        # Full text still ships via exc_info -> exception.message; the body is
        # capped so one huge message cannot dominate the log line.
        from application_sdk.execution._temporal.interceptors.log import (
            _FAILURE_MSG_MAX_CHARS,
            _failure_suffix,
        )

        suffix = _failure_suffix(ValueError("x" * 500), {})
        assert "x" * _FAILURE_MSG_MAX_CHARS in suffix
        assert "x" * (_FAILURE_MSG_MAX_CHARS + 1) not in suffix

    async def test_failure_suffix_omits_frame_when_traceback_missing(self):
        # An exception that never propagated (or whose traceback was cleared)
        # has no frame to name — degrade to code+message, not a crash.
        from application_sdk.execution._temporal.interceptors.log import _failure_suffix

        exc = ValueError("no frames here")
        exc.__traceback__ = None
        suffix = _failure_suffix(exc, {})
        assert suffix == "FAILED (ValueError): no frames here"
        assert " — at " not in suffix

    async def test_failure_suffix_survives_whitespace_only_message(self):
        # str(exc) is truthy but strips to empty, so splitlines() yields [] —
        # indexing that would raise IndexError, and the caller's best-effort
        # guard would swallow it and drop the entire ended log.
        from application_sdk.execution._temporal.interceptors.log import _failure_suffix

        suffix = _failure_suffix(ValueError("\n  \n"), {"failure.code": "blank"})
        assert suffix.startswith("FAILED (blank)")
        assert "FAILED (blank):" not in suffix  # no empty ": " tail
