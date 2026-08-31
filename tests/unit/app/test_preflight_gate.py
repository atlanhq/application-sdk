"""Unit tests for the injected pre-extraction preflight gate (HYP-1883).

Exercises ``_run_preflight_gate`` directly with ``workflow.patched`` and
``workflow.execute_activity`` mocked. The block decision lives in the activity
(it raises); the workflow only re-raises the deliberate ``PreflightFailed``
block and fails open on every other activity failure.
"""

from __future__ import annotations

from unittest import mock

import pytest

from application_sdk.app.base import _run_preflight_gate
from application_sdk.execution._temporal.preflight_gate import (
    CLASSIFICATION_GATE_BROKEN,
    FAILURE_AUDIENCE_KEY,
)
from application_sdk.execution.errors import ApplicationError
from application_sdk.handler.contracts import PreflightOutput, PreflightStatus
from application_sdk.observability.logger_adaptor import CHECK_MATRIX_KEY


class _ResolvableInput:
    """Minimal object satisfying the CredentialResolvable protocol."""

    def __init__(
        self,
        *,
        guid: str = "g-1",
        method: str = "direct",
        agent_json=None,
        credential_ref=None,
        metadata=None,
    ) -> None:
        self.extraction_method = method
        self.credential_guid = guid
        self.agent_json = agent_json
        self.credential_ref = credential_ref
        if metadata is not None:
            self.metadata = metadata


class _NonResolvableInput:
    """Carries no credential routing — must skip the gate (e.g. openapi-app)."""


class _ActivityErrorStub(Exception):
    """Stand-in for Temporal's ActivityError: wraps a cause exception."""

    def __init__(self, cause: BaseException) -> None:
        super().__init__("activity failed")
        self.cause = cause
        self.__cause__ = cause


def _real_activity_error(cause: BaseException):
    """A genuine Temporal ``ActivityError`` wrapping ``cause``.

    Built from the real class rather than ``_ActivityErrorStub`` wherever the
    behaviour under test *is* a temporalio detail — that ``ActivityError``
    exposes ``.cause``, and that ``TimeoutError.type`` is a ``TimeoutType``
    enum rather than a string. A hand-rolled double there would pin our belief
    about the dependency instead of the dependency itself, and would keep
    passing if either premise changed under us.
    """
    from temporalio.exceptions import ActivityError

    err = ActivityError(
        "Activity task failed",
        scheduled_event_id=1,
        started_event_id=2,
        identity="worker",
        activity_type="myapp:preflight",
        activity_id="1",
        retry_state=None,
    )
    err.__cause__ = cause
    return err


def _preflight_failed_error() -> ApplicationError:
    return ApplicationError(
        "Preflight failed: bad creds", type="PreflightFailed", non_retryable=True
    )


def _patched(value: bool):
    return mock.patch("application_sdk.app.base.workflow.patched", return_value=value)


def _exec(return_value=None, side_effect=None):
    m = mock.AsyncMock(return_value=return_value, side_effect=side_effect)
    return m, mock.patch("application_sdk.app.base.workflow.execute_activity", m)


def _outcomes(safe_log) -> list[str]:
    return [
        c.kwargs.get("outcome")
        for c in safe_log.call_args_list
        if "outcome" in c.kwargs
    ]


@pytest.fixture
def safe_log():
    with mock.patch("application_sdk.app.base._safe_log") as m:
        yield m


class TestRunPreflightGate:
    async def test_skipped_when_not_patched(self, safe_log) -> None:
        exec_mock, exec_patch = _exec()
        with _patched(False), exec_patch:
            await _run_preflight_gate(_ResolvableInput(), "myapp", "crawl")
        exec_mock.assert_not_called()
        # A gate that never ran must be visible, not silent — otherwise a
        # zero-verdict app is indistinguishable from a healthy one.
        assert _outcomes(safe_log) == ["skipped"]

    async def test_skipped_for_non_resolvable_input(self, safe_log) -> None:
        exec_mock, exec_patch = _exec()
        with _patched(True), exec_patch:
            await _run_preflight_gate(_NonResolvableInput(), "myapp", "crawl")
        exec_mock.assert_not_called()
        assert _outcomes(safe_log) == ["skipped"]

    async def test_proceeds_on_ready(self, safe_log) -> None:
        exec_mock, exec_patch = _exec(
            PreflightOutput(status=PreflightStatus.READY, checks=[])
        )
        with _patched(True), exec_patch:
            result = await _run_preflight_gate(_ResolvableInput(), "myapp", "crawl")
        assert result is None
        exec_mock.assert_awaited_once()
        # The activity emits the proceeded outcome event now — the workflow emits none.
        assert _outcomes(safe_log) == []

    async def test_proceeds_on_partial(self, safe_log) -> None:
        exec_mock, exec_patch = _exec(
            PreflightOutput(status=PreflightStatus.PARTIAL, checks=[])
        )
        with _patched(True), exec_patch:
            result = await _run_preflight_gate(_ResolvableInput(), "myapp", "crawl")
        assert result is None
        assert _outcomes(safe_log) == []

    async def test_reraises_on_preflight_failed(self, safe_log) -> None:
        _, exec_patch = _exec(side_effect=_preflight_failed_error())
        with _patched(True), exec_patch:
            with pytest.raises(ApplicationError) as excinfo:
                await _run_preflight_gate(_ResolvableInput(), "myapp", "crawl")
        assert excinfo.value.type == "PreflightFailed"
        # The activity emits the blocked outcome event before raising; the
        # workflow only re-raises and emits nothing.
        assert _outcomes(safe_log) == []

    async def test_reraises_when_preflight_failed_is_activity_error_cause(
        self, safe_log
    ) -> None:
        # Real Temporal wraps the activity's ApplicationError in an ActivityError;
        # the label check must walk the cause chain, not just the top-level error.
        wrapper = _ActivityErrorStub(_preflight_failed_error())
        _, exec_patch = _exec(side_effect=wrapper)
        with _patched(True), exec_patch:
            with pytest.raises(_ActivityErrorStub) as excinfo:
                await _run_preflight_gate(_ResolvableInput(), "myapp", "crawl")
        assert excinfo.value is wrapper
        assert _outcomes(safe_log) == []

    async def test_fail_open_on_other_activity_error(self, safe_log) -> None:
        # What still reaches this layer is only the gate's own plumbing breaking
        # (worker gone, schedule_to_close, lost completion) — source-attributable
        # failures are classified and enforced inside the activity. Those fail
        # open in both modes, so this path is deliberately mode-blind, and the
        # row is stamped gate_broken so the dashboard can separate the two.
        from temporalio.exceptions import ApplicationError as TemporalApplicationError

        exec_mock, exec_patch = _exec(
            side_effect=TemporalApplicationError("secret store down")
        )
        with _patched(True), exec_patch:
            result = await _run_preflight_gate(_ResolvableInput(), "myapp", "crawl")
        assert result is None
        exec_mock.assert_awaited_once()
        # One record for the whole event (FND-901): the no_verdict outcome row
        # itself is the ERROR, carrying the stack and who must act.
        error_calls = [c for c in safe_log.call_args_list if c.args[0] == "error"]
        assert len(error_calls) == 1
        no_verdict_call = error_calls[0]
        assert no_verdict_call.kwargs.get("outcome") == "no_verdict"
        assert no_verdict_call.kwargs.get("exc_info") is True
        assert no_verdict_call.kwargs.get(FAILURE_AUDIENCE_KEY) == "APP_OWNER"
        assert no_verdict_call.kwargs.get("reason") == "ApplicationError"
        assert (
            no_verdict_call.kwargs.get("gate_classification")
            == CLASSIFICATION_GATE_BROKEN
        )
        assert _outcomes(safe_log) == ["no_verdict"]

    async def test_fail_open_reason_names_the_underlying_error_not_the_wrapper(
        self, safe_log
    ) -> None:
        # Temporal wraps the activity's error in an ActivityError, so the raw
        # ``type(e).__name__`` on the no_verdict row is the useless wrapper name.
        # The row must carry the real cause so the dashboard reads
        # "DaprSidecarUnreachableError", not "ActivityError" — the whole point of
        # naming a persistent sidecar fault instead of a transient race.
        wrapped = ApplicationError(
            "Dapr sidecar unreachable: component=objectstore not reachable "
            "after 2 attempts over 120.0s",
            type="DaprSidecarUnreachableError",
        )
        _, exec_patch = _exec(side_effect=_ActivityErrorStub(wrapped))
        with _patched(True), exec_patch:
            result = await _run_preflight_gate(_ResolvableInput(), "myapp", "crawl")
        assert result is None
        no_verdict_call = next(
            c
            for c in safe_log.call_args_list
            if c.kwargs.get("outcome") == "no_verdict"
        )
        assert no_verdict_call.kwargs.get("reason") == "DaprSidecarUnreachableError"
        assert (
            no_verdict_call.kwargs.get("gate_classification")
            == CLASSIFICATION_GATE_BROKEN
        )

    async def test_activity_timeouts_derive_from_the_app_budget(self, safe_log) -> None:
        # A slow source buys budget per app; both activity timeouts must move
        # with it, or raising start_to_close past the fixed 60s schedule cap
        # would make the run fail earlier, not later.
        exec_mock, exec_patch = _exec(
            PreflightOutput(status=PreflightStatus.READY, checks=[])
        )
        with _patched(True), exec_patch:
            await _run_preflight_gate(_ResolvableInput(), "myapp", "crawl", 90)
        kwargs = exec_mock.call_args.kwargs
        assert kwargs["start_to_close_timeout"].total_seconds() > 90
        assert (
            kwargs["schedule_to_close_timeout"] >= 2 * kwargs["start_to_close_timeout"]
        )

    async def test_non_failure_error_still_propagates(self) -> None:
        # Fail-open catches only Exception; control-flow BaseExceptions like
        # cancellation must NOT be swallowed.
        import asyncio

        _, exec_patch = _exec(side_effect=asyncio.CancelledError())
        with _patched(True), exec_patch:
            with pytest.raises(asyncio.CancelledError):
                await _run_preflight_gate(_ResolvableInput(), "myapp", "crawl")

    async def test_forwards_routing_fields_to_activity(self, safe_log) -> None:
        exec_mock, exec_patch = _exec(
            PreflightOutput(status=PreflightStatus.READY, checks=[])
        )
        with _patched(True), exec_patch:
            await _run_preflight_gate(
                _ResolvableInput(guid="abc", method="agent"), "myapp", "asset-export"
            )
        args, _ = exec_mock.call_args
        assert args[0] == "myapp:preflight"
        gate_input = args[1]
        assert gate_input.credential_guid == "abc"
        assert gate_input.extraction_method == "agent"
        assert gate_input.entrypoint == "asset-export"


def _outcome_kwargs(safe_log) -> list[dict]:
    return [c.kwargs for c in safe_log.call_args_list if "outcome" in c.kwargs]


class TestEveryOutcomeCarriesTheCheckMatrix:
    """``check_matrix`` is present on all outcomes, empty where nothing ran.

    A consumer should be able to ``JSONExtractArrayRaw(check_matrix)`` on any gate
    row without first testing ``mapContains``. An absent field forces every
    consumer to branch, and that branch is easy to get wrong in the direction that
    silently drops rows — which is how a gate that never reached a verdict
    disappears from the numerator it belongs in.
    """

    async def test_skipped_on_pre_gate_replay(self, safe_log) -> None:
        _, exec_patch = _exec()
        with _patched(False), exec_patch:
            await _run_preflight_gate(_ResolvableInput(), "myapp", "crawl")
        row = _outcome_kwargs(safe_log)[0]
        assert row["outcome"] == "skipped"
        assert row[CHECK_MATRIX_KEY] == "[]"

    async def test_skipped_on_non_resolvable_input(self, safe_log) -> None:
        _, exec_patch = _exec()
        with _patched(True), exec_patch:
            await _run_preflight_gate(_NonResolvableInput(), "myapp", "crawl")
        row = _outcome_kwargs(safe_log)[0]
        assert row["outcome"] == "skipped"
        assert row[CHECK_MATRIX_KEY] == "[]"

    async def test_no_verdict_on_fail_open(self, safe_log) -> None:
        _, exec_patch = _exec(side_effect=RuntimeError("worker gone"))
        with _patched(True), exec_patch:
            await _run_preflight_gate(_ResolvableInput(), "myapp", "crawl")
        row = _outcome_kwargs(safe_log)[0]
        assert row["outcome"] == "no_verdict"
        # Empty, not synthetic: the activity never returned, so the workflow has
        # no checks to report and must not invent one.
        assert row[CHECK_MATRIX_KEY] == "[]"
        assert row["gate_classification"] == CLASSIFICATION_GATE_BROKEN


class TestUnderlyingErrorType:
    """``underlying_error_type`` sees the real fault through Temporal's wrapping."""

    def test_returns_wrapped_application_error_type(self) -> None:
        from application_sdk.execution._temporal.preflight_gate import (
            underlying_error_type,
        )

        inner = ApplicationError("boom", type="DaprSidecarUnreachableError")
        assert (
            underlying_error_type(_ActivityErrorStub(inner))
            == "DaprSidecarUnreachableError"
        )

    def test_falls_back_to_top_level_name_when_no_type_in_chain(self) -> None:
        from application_sdk.execution._temporal.preflight_gate import (
            underlying_error_type,
        )

        assert underlying_error_type(ValueError("x")) == "ValueError"

    def test_activity_timeout_names_which_deadline_fired(self) -> None:
        # A deadline overrun is the *dominant* gate_broken shape in production
        # (CONNECT-841: a 120s Dapr cold-start wait inside a narrower
        # start_to_close), and it carries no string `type` anywhere — Temporal's
        # TimeoutError puts a TimeoutType *enum* on `.type`. Reporting the
        # wrapper name ("ActivityError") for it, as this used to, is the exact
        # uninformative label this helper exists to remove. Name the deadline
        # instead, and keep it a str so `reason`'s consumers are unaffected.
        from temporalio.exceptions import TimeoutError as TemporalTimeoutError
        from temporalio.exceptions import TimeoutType

        from application_sdk.execution._temporal.preflight_gate import (
            underlying_error_type,
        )

        for timeout_type, expected in (
            (TimeoutType.START_TO_CLOSE, "Timeout:START_TO_CLOSE"),
            (TimeoutType.SCHEDULE_TO_CLOSE, "Timeout:SCHEDULE_TO_CLOSE"),
            (TimeoutType.HEARTBEAT, "Timeout:HEARTBEAT"),
        ):
            timed_out = TemporalTimeoutError(
                "deadline exceeded", type=timeout_type, last_heartbeat_details=[]
            )
            result = underlying_error_type(_real_activity_error(timed_out))
            assert result == expected
            assert isinstance(result, str)

    def test_a_real_fault_outranks_the_deadline_that_ended_it(self) -> None:
        # A schedule_to_close expiry hangs the last attempt's ApplicationError
        # off the TimeoutError, so the chain carries BOTH a timeout enum and a
        # real string type. The attempt's own fault is the better reason — the
        # deadline is what noticed, not what broke — so the string must win even
        # though the enum is encountered first.
        from temporalio.exceptions import TimeoutError as TemporalTimeoutError
        from temporalio.exceptions import TimeoutType

        from application_sdk.execution._temporal.preflight_gate import (
            underlying_error_type,
        )

        timed_out = TemporalTimeoutError(
            "deadline exceeded",
            type=TimeoutType.SCHEDULE_TO_CLOSE,
            last_heartbeat_details=[],
        )
        timed_out.__cause__ = ApplicationError(
            "Dapr sidecar unreachable", type="DaprSidecarUnreachableError"
        )
        assert (
            underlying_error_type(_real_activity_error(timed_out))
            == "DaprSidecarUnreachableError"
        )

    def test_an_unrecognised_non_string_type_still_falls_through(self) -> None:
        # The enum branch is scoped to TimeoutType specifically. Any other
        # non-string `type` must keep falling through to the class name rather
        # than being stringified into `reason` on spec.
        from application_sdk.execution._temporal.preflight_gate import (
            underlying_error_type,
        )

        class _OddType(Exception):
            def __init__(self) -> None:
                super().__init__("odd")
                self.type = object()

        result = underlying_error_type(_ActivityErrorStub(_OddType()))
        assert result == "_ActivityErrorStub"
        assert isinstance(result, str)

    def test_is_cycle_safe(self) -> None:
        from application_sdk.execution._temporal.preflight_gate import (
            underlying_error_type,
        )

        looped = _ActivityErrorStub(ValueError("x"))
        looped.cause = looped
        looped.__cause__ = looped
        assert underlying_error_type(looped) == "_ActivityErrorStub"
