"""Unit tests for the injected pre-extraction preflight gate (HYP-1883).

Exercises ``_run_preflight_gate`` directly with ``workflow.patched`` and
``workflow.execute_activity`` mocked. The activity holds the verdict and raises
the deliberate ``PreflightFailed`` block, which the workflow re-raises unchanged.
When the activity returns no verdict the workflow classifies the failure chain:
surviving evidence or a lost frame is subject to the mode, and only the gate's
own plumbing fails open.
"""

from __future__ import annotations

from unittest import mock

import pytest

from application_sdk.app.base import _run_preflight_gate
from application_sdk.errors.categories import Audience, FailureCategory
from application_sdk.errors.leaves import SourceUnavailableError
from application_sdk.execution._temporal.preflight_gate import (
    FAILURE_AUDIENCE_KEY,
    GATE_OUTCOME_ROW_KEYS,
    GATE_TIMEOUT_DEFAULT_SECONDS,
    PREFLIGHT_FAILED_ERROR_TYPE,
    PREFLIGHT_NO_VERDICT_ERROR_TYPE,
    PreflightClassification,
)
from application_sdk.execution.errors import ApplicationError
from application_sdk.handler.contracts import (
    PreflightGateMode,
    PreflightOutput,
    PreflightStatus,
)
from application_sdk.observability.logger_adaptor import (
    CHECK_MATRIX_KEY,
    GATE_ATTEMPTS_KEY,
    GATE_CLASSIFICATION_KEY,
    GATE_DURATION_KEY,
    GATE_MODE_KEY,
    GATE_TIMEOUT_KEY,
)


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


def _rows(safe_log) -> list[dict]:
    return [c.kwargs for c in safe_log.call_args_list if "outcome" in c.kwargs]


def _row(safe_log) -> dict:
    (row,) = _rows(safe_log)
    return row


def _outcomes(safe_log) -> list[str]:
    return [row["outcome"] for row in _rows(safe_log)]


@pytest.fixture
def safe_log():
    with mock.patch("application_sdk.app.base._safe_log") as m:
        yield m


@pytest.fixture(autouse=True)
def _workflow_now():
    from datetime import datetime, timezone

    with mock.patch(
        "application_sdk.app.base.workflow.now",
        return_value=datetime(2026, 1, 1, tzinfo=timezone.utc),
    ):
        yield


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
            == PreflightClassification.GATE_BROKEN
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
            == PreflightClassification.GATE_BROKEN
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
        row = _rows(safe_log)[0]
        assert row["outcome"] == "skipped"
        assert row[CHECK_MATRIX_KEY] == "[]"

    async def test_skipped_on_non_resolvable_input(self, safe_log) -> None:
        _, exec_patch = _exec()
        with _patched(True), exec_patch:
            await _run_preflight_gate(_NonResolvableInput(), "myapp", "crawl")
        row = _rows(safe_log)[0]
        assert row["outcome"] == "skipped"
        assert row[CHECK_MATRIX_KEY] == "[]"

    async def test_no_verdict_on_fail_open(self, safe_log) -> None:
        _, exec_patch = _exec(side_effect=RuntimeError("worker gone"))
        with _patched(True), exec_patch:
            await _run_preflight_gate(_ResolvableInput(), "myapp", "crawl")
        row = _rows(safe_log)[0]
        assert row["outcome"] == "no_verdict"
        # Empty, not synthetic: the activity never returned, so the workflow has
        # no checks to report and must not invent one.
        assert row[CHECK_MATRIX_KEY] == "[]"
        assert row["gate_classification"] == PreflightClassification.GATE_BROKEN


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


class TestGateActivityHeartbeat:
    """CONNECT-1170 gap 3: the gate is scheduled without a heartbeat.

    With ``heartbeat_timeout`` unset, Temporal cannot detect a stalled gate
    before ``start_to_close`` (it burns the full budget before retrying), and
    cancellation is never delivered to the worker — which is what lets an
    abandoned attempt keep running and emit an orphan verdict row (gap 1).
    """

    async def test_gate_activity_is_scheduled_with_a_heartbeat_timeout(self) -> None:
        exec_mock, exec_patch = _exec(
            PreflightOutput(status=PreflightStatus.READY, checks=[])
        )
        with _patched(True), exec_patch:
            await _run_preflight_gate(_ResolvableInput(), "myapp", "crawl")
        heartbeat = exec_mock.call_args.kwargs.get("heartbeat_timeout")
        assert heartbeat is not None
        assert heartbeat.total_seconds() > 0


def _workflow_clock(*seconds: float):
    from datetime import datetime, timedelta, timezone

    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    ticks = [base + timedelta(seconds=s) for s in seconds]
    return mock.patch("application_sdk.app.base.workflow.now", side_effect=ticks)


class TestEveryWorkflowRowCarriesTheFullShape:
    """The rows the workflow emits carry the same keys the activity's rows do.

    A consumer that filters on ``gate_mode`` or ``gate_duration_ms`` must not
    drop exactly the rows that prove a gate never ran or never returned. Every
    outcome is parsed the same way, so every outcome carries every key.
    """

    async def test_skipped_on_replay_carries_every_key(self, safe_log) -> None:
        with _patched(False):
            await _run_preflight_gate(_ResolvableInput(), "myapp", "crawl")
        row = _row(safe_log)
        assert row["outcome"] == "skipped"
        assert set(GATE_OUTCOME_ROW_KEYS) <= row.keys()
        assert row[GATE_CLASSIFICATION_KEY] == PreflightClassification.NOT_RUN
        assert row[GATE_DURATION_KEY] == 0.0
        assert row[GATE_ATTEMPTS_KEY] == 0

    async def test_skipped_on_non_resolvable_input_carries_every_key(
        self, safe_log
    ) -> None:
        with _patched(True):
            await _run_preflight_gate(_NonResolvableInput(), "myapp", "crawl")
        row = _row(safe_log)
        assert row["outcome"] == "skipped"
        assert set(GATE_OUTCOME_ROW_KEYS) <= row.keys()
        assert row[GATE_CLASSIFICATION_KEY] == PreflightClassification.NOT_RUN

    async def test_no_verdict_carries_every_key_and_a_measured_duration(
        self, safe_log
    ) -> None:
        from temporalio.exceptions import ApplicationError as TemporalApplicationError

        _, exec_patch = _exec(side_effect=TemporalApplicationError("secret store down"))
        with _patched(True), exec_patch, _workflow_clock(0, 7.5):
            await _run_preflight_gate(
                _ResolvableInput(), "myapp", "crawl", budget_seconds=200
            )
        row = _row(safe_log)
        assert row["outcome"] == "no_verdict"
        assert set(GATE_OUTCOME_ROW_KEYS) <= row.keys()
        assert row[GATE_CLASSIFICATION_KEY] == PreflightClassification.GATE_BROKEN
        assert row[GATE_DURATION_KEY] == 7500.0
        assert row[GATE_TIMEOUT_KEY] == 200
        assert row[GATE_ATTEMPTS_KEY] == 0

    async def test_rows_report_the_declared_mode(self, safe_log) -> None:
        with _patched(False):
            await _run_preflight_gate(
                _ResolvableInput(), "myapp", "crawl", gate_mode="hard"
            )
        assert _row(safe_log)[GATE_MODE_KEY] == "hard"

    async def test_rows_default_to_soft_when_no_mode_is_declared(
        self, safe_log
    ) -> None:
        with _patched(False):
            await _run_preflight_gate(_ResolvableInput(), "myapp", "crawl")
        assert _row(safe_log)[GATE_MODE_KEY] == "soft"

    async def test_rows_report_the_default_budget_when_none_is_declared(
        self, safe_log
    ) -> None:
        with _patched(False):
            await _run_preflight_gate(_ResolvableInput(), "myapp", "crawl")
        assert _row(safe_log)[GATE_TIMEOUT_KEY] == GATE_TIMEOUT_DEFAULT_SECONDS

    async def test_malformed_declared_mode_reads_as_soft(self, safe_log) -> None:
        with _patched(False):
            await _run_preflight_gate(
                _ResolvableInput(), "myapp", "crawl", gate_mode="on"
            )
        assert _row(safe_log)[GATE_MODE_KEY] == "soft"


def _temporal_timeout(timeout_type):
    from temporalio.exceptions import TimeoutError as TemporalTimeoutError

    return TemporalTimeoutError(
        "deadline exceeded", type=timeout_type, last_heartbeat_details=[]
    )


def _no_verdict_marker(*, as_dict: bool = False) -> ApplicationError:
    """The retry marker a non-final attempt raises, with a source fault as evidence."""
    details = SourceUnavailableError(
        message="The SQL Server did not answer in time"
    ).to_failure_details()
    checks = [
        {
            "name": "preflightVerdict",
            "passed": False,
            "error": details.model_dump(mode="json"),
        }
    ]
    payload = details.model_dump(mode="json") if as_dict else details
    return ApplicationError(
        "Preflight could not reach a verdict",
        payload,
        {"checks": checks},
        type=PREFLIGHT_NO_VERDICT_ERROR_TYPE,
    )


def _killed_attempt_after_marker(as_dict: bool = False):
    """Event shape from production: START_TO_CLOSE wrapping the previous attempt's marker."""
    from temporalio.exceptions import TimeoutType

    timeout = _temporal_timeout(TimeoutType.START_TO_CLOSE)
    timeout.__cause__ = _no_verdict_marker(as_dict=as_dict)
    return _real_activity_error(timeout)


class TestWorkflowAppliesTheModeToADeadFrame:
    """A gate attempt Temporal killed is not a silent proceed.

    The activity holds the mode, but a probe that stalls the loop outlives the
    activity's own cancel and Temporal ends the frame. The workflow then reads
    the chain: the previous attempt's typed evidence, or the bare fact that a
    running attempt was killed, are both statements about the source or the
    handler, and the mode applies. Only the gate's own plumbing still proceeds.
    """

    async def test_hard_mode_blocks_from_the_previous_attempts_evidence(
        self, safe_log
    ) -> None:
        _, exec_patch = _exec(side_effect=_killed_attempt_after_marker())
        with _patched(True), exec_patch:
            with pytest.raises(ApplicationError) as excinfo:
                await _run_preflight_gate(
                    _ResolvableInput(), "mssql", "crawler", gate_mode="hard"
                )
        err = excinfo.value
        assert err.type == PREFLIGHT_FAILED_ERROR_TYPE
        assert err.non_retryable is True
        assert err.details[0].category is FailureCategory.SOURCE_UNAVAILABLE
        assert err.details[0].audience is Audience.USER
        assert err.details[0].app_name == "mssql"
        assert err.details[1]["status"] == "not_ready"
        assert err.details[1]["checks"][0]["passed"] is False
        (row,) = _rows(safe_log)
        assert row["outcome"] == "blocked"
        assert (
            row[GATE_CLASSIFICATION_KEY] == PreflightClassification.SOURCE_UNVERIFIABLE
        )
        assert row[FAILURE_AUDIENCE_KEY] == "USER"
        assert row["reason"] == err.details[0].code
        assert "preflightVerdict" in row[CHECK_MATRIX_KEY]

    async def test_soft_mode_reports_would_block_and_proceeds(self, safe_log) -> None:
        _, exec_patch = _exec(side_effect=_killed_attempt_after_marker())
        with _patched(True), exec_patch:
            result = await _run_preflight_gate(
                _ResolvableInput(), "mssql", "crawler", gate_mode="soft"
            )
        assert result is None
        (row,) = _rows(safe_log)
        assert row["outcome"] == "would_block"
        assert (
            row[GATE_CLASSIFICATION_KEY] == PreflightClassification.SOURCE_UNVERIFIABLE
        )

    async def test_evidence_that_crossed_the_converter_as_a_dict_is_accepted(
        self, safe_log
    ) -> None:
        _, exec_patch = _exec(side_effect=_killed_attempt_after_marker(as_dict=True))
        with _patched(True), exec_patch:
            with pytest.raises(ApplicationError) as excinfo:
                await _run_preflight_gate(
                    _ResolvableInput(), "mssql", "crawler", gate_mode="hard"
                )
        assert excinfo.value.details[0].category is FailureCategory.SOURCE_UNAVAILABLE

    async def test_marker_as_the_final_failure_is_also_a_verdict(
        self, safe_log
    ) -> None:
        _, exec_patch = _exec(side_effect=_real_activity_error(_no_verdict_marker()))
        with _patched(True), exec_patch:
            with pytest.raises(ApplicationError) as excinfo:
                await _run_preflight_gate(
                    _ResolvableInput(), "mssql", "crawler", gate_mode="hard"
                )
        assert excinfo.value.type == PREFLIGHT_FAILED_ERROR_TYPE

    @pytest.mark.parametrize("timeout_name", ["START_TO_CLOSE", "HEARTBEAT"])
    async def test_killed_frame_without_evidence_is_frame_lost_and_hard_blocks(
        self, safe_log, timeout_name: str
    ) -> None:
        from temporalio.exceptions import TimeoutType

        killed = _real_activity_error(_temporal_timeout(TimeoutType[timeout_name]))
        _, exec_patch = _exec(side_effect=killed)
        with _patched(True), exec_patch:
            with pytest.raises(ApplicationError) as excinfo:
                await _run_preflight_gate(
                    _ResolvableInput(),
                    "mssql",
                    "crawler",
                    budget_seconds=300,
                    gate_mode="hard",
                )
        details = excinfo.value.details[0]
        assert details.category is FailureCategory.TIMEOUT
        assert details.audience is Audience.APP_OWNER
        assert details.app_name == "mssql"
        assert "300" in details.message
        assert "lost worker" in details.message
        assert excinfo.value.details[1]["status"] == "not_ready"
        (row,) = _rows(safe_log)
        assert row["outcome"] == "blocked"
        assert row[GATE_CLASSIFICATION_KEY] == PreflightClassification.FRAME_LOST
        assert row[FAILURE_AUDIENCE_KEY] == "APP_OWNER"
        assert row[CHECK_MATRIX_KEY] == "[]"

    async def test_killed_frame_without_evidence_is_frame_lost_and_soft_proceeds(
        self, safe_log
    ) -> None:
        from temporalio.exceptions import TimeoutType

        killed = _real_activity_error(_temporal_timeout(TimeoutType.HEARTBEAT))
        _, exec_patch = _exec(side_effect=killed)
        with _patched(True), exec_patch:
            result = await _run_preflight_gate(
                _ResolvableInput(), "mssql", "crawler", gate_mode="soft"
            )
        assert result is None
        (row,) = _rows(safe_log)
        assert row["outcome"] == "would_block"
        assert row[GATE_CLASSIFICATION_KEY] == PreflightClassification.FRAME_LOST

    async def test_the_mode_may_be_declared_as_the_enum(self, safe_log) -> None:
        _, exec_patch = _exec(side_effect=_killed_attempt_after_marker())
        with _patched(True), exec_patch:
            with pytest.raises(ApplicationError):
                await _run_preflight_gate(
                    _ResolvableInput(),
                    "mssql",
                    "crawler",
                    gate_mode=PreflightGateMode.HARD,
                )
        assert _rows(safe_log)[0][GATE_MODE_KEY] == "hard"

    async def test_no_worker_ever_ran_the_attempt_still_fails_open(
        self, safe_log
    ) -> None:
        from temporalio.exceptions import TimeoutType

        never_started = _real_activity_error(
            _temporal_timeout(TimeoutType.SCHEDULE_TO_START)
        )
        _, exec_patch = _exec(side_effect=never_started)
        with _patched(True), exec_patch:
            result = await _run_preflight_gate(
                _ResolvableInput(), "mssql", "crawler", gate_mode="hard"
            )
        assert result is None
        (row,) = _rows(safe_log)
        assert row["outcome"] == "no_verdict"
        assert row[GATE_CLASSIFICATION_KEY] == PreflightClassification.GATE_BROKEN
        assert row["reason"] == "Timeout:SCHEDULE_TO_START"

    async def test_plumbing_failure_with_details_still_fails_open(
        self, safe_log
    ) -> None:
        from application_sdk.errors.leaves import DependencyUnavailableError

        plumbing = ApplicationError(
            "vault down",
            DependencyUnavailableError(
                message="vault down", service="secret_store"
            ).to_failure_details(),
            type="DependencyUnavailableError",
        )
        _, exec_patch = _exec(side_effect=_real_activity_error(plumbing))
        with _patched(True), exec_patch:
            result = await _run_preflight_gate(
                _ResolvableInput(), "mssql", "crawler", gate_mode="hard"
            )
        assert result is None
        (row,) = _rows(safe_log)
        assert row["outcome"] == "no_verdict"
        assert row[GATE_CLASSIFICATION_KEY] == PreflightClassification.GATE_BROKEN
        assert row["reason"] == "DependencyUnavailableError"

    async def test_the_block_is_still_reraised_unchanged(self, safe_log) -> None:
        block = _preflight_failed_error()
        _, exec_patch = _exec(side_effect=_real_activity_error(block))
        with _patched(True), exec_patch:
            with pytest.raises(Exception) as excinfo:
                await _run_preflight_gate(
                    _ResolvableInput(), "mssql", "crawler", gate_mode="hard"
                )
        assert excinfo.value.__cause__ is block
        assert _rows(safe_log) == []


def _marker_with_payload(*details) -> ApplicationError:
    return ApplicationError(
        "Preflight could not reach a verdict",
        *details,
        type=PREFLIGHT_NO_VERDICT_ERROR_TYPE,
    )


class TestAMalformedMarkerPayloadFailsOpen:
    """A payload this frame cannot read is the gate's problem, never the run's.

    The workflow parses the marker inside its own ``except``; anything escaping
    there is a workflow task failure Temporal retries forever, and a newer pod's
    ``FailureDetails`` reaching an older pod during a rollout is exactly how
    that happens. An unreadable payload therefore reads as ``gate_broken`` and
    the run proceeds, in hard mode too.
    """

    _good = SourceUnavailableError(message="no answer").to_failure_details()

    @pytest.fixture(
        params=[
            "extra_field_on_details",
            "envelope_is_a_list",
            "check_is_not_a_dict",
            "details_is_a_string",
        ]
    )
    def malformed(self, request) -> ApplicationError:
        good = self._good.model_dump(mode="json")
        return {
            "extra_field_on_details": _marker_with_payload(
                {**good, "added_in_a_newer_sdk": 1}, {"checks": []}
            ),
            "envelope_is_a_list": _marker_with_payload(good, ["not", "a", "dict"]),
            "check_is_not_a_dict": _marker_with_payload(good, {"checks": ["x"]}),
            "details_is_a_string": _marker_with_payload("just text", {"checks": []}),
        }[request.param]

    @pytest.mark.parametrize("mode", ["hard", "soft"])
    async def test_unreadable_evidence_fails_open_as_gate_broken(
        self, safe_log, malformed, mode: str
    ) -> None:
        _, exec_patch = _exec(side_effect=_real_activity_error(malformed))
        with _patched(True), exec_patch:
            result = await _run_preflight_gate(
                _ResolvableInput(), "myapp", "crawl", gate_mode=mode
            )
        assert result is None
        row = _row(safe_log)
        assert row["outcome"] == "no_verdict"
        assert row[GATE_CLASSIFICATION_KEY] == PreflightClassification.GATE_BROKEN


class TestWorkflowRowsCarryTheRealAttempt:
    """``gate_attempt`` is the attempt that ran, and ``0`` only when none did."""

    async def test_skipped_rows_report_zero(self, safe_log) -> None:
        with _patched(False):
            await _run_preflight_gate(_ResolvableInput(), "myapp", "crawl")
        assert _row(safe_log)[GATE_ATTEMPTS_KEY] == 0

    async def test_an_attempt_no_worker_started_reports_zero(self, safe_log) -> None:
        from temporalio.exceptions import TimeoutType

        never = _real_activity_error(_temporal_timeout(TimeoutType.SCHEDULE_TO_START))
        _, exec_patch = _exec(side_effect=never)
        with _patched(True), exec_patch:
            await _run_preflight_gate(_ResolvableInput(), "myapp", "crawl")
        assert _row(safe_log)[GATE_ATTEMPTS_KEY] == 0

    async def test_a_markers_attempt_is_carried(self, safe_log) -> None:
        details = self._good = SourceUnavailableError(
            message="no answer"
        ).to_failure_details()
        marker = _marker_with_payload(details, {"checks": [], "attempt": 2})
        _, exec_patch = _exec(side_effect=_real_activity_error(marker))
        with _patched(True), exec_patch:
            await _run_preflight_gate(_ResolvableInput(), "myapp", "crawl")
        assert _row(safe_log)[GATE_ATTEMPTS_KEY] == 2

    async def test_a_killed_frames_last_heartbeat_names_the_attempt(
        self, safe_log
    ) -> None:
        from temporalio.exceptions import TimeoutError as TemporalTimeoutError
        from temporalio.exceptions import TimeoutType

        killed = _real_activity_error(
            TemporalTimeoutError(
                "deadline exceeded",
                type=TimeoutType.HEARTBEAT,
                last_heartbeat_details=[2],
            )
        )
        _, exec_patch = _exec(side_effect=killed)
        with _patched(True), exec_patch:
            await _run_preflight_gate(_ResolvableInput(), "myapp", "crawl")
        assert _row(safe_log)[GATE_ATTEMPTS_KEY] == 2

    async def test_a_plumbing_errors_attempt_is_carried(self, safe_log) -> None:
        from application_sdk.errors.leaves import DependencyUnavailableError

        plumbing = ApplicationError(
            "vault down",
            DependencyUnavailableError(
                message="vault down", service="secret_store"
            ).to_failure_details(),
            {"status": None, "checks": [], "attempt": 1},
            type="DependencyUnavailableError",
        )
        _, exec_patch = _exec(side_effect=_real_activity_error(plumbing))
        with _patched(True), exec_patch:
            await _run_preflight_gate(_ResolvableInput(), "myapp", "crawl")
        assert _row(safe_log)[GATE_ATTEMPTS_KEY] == 1


class TestWorkflowRowsAreLevelledLikeTheActivitys:
    """Same level policy as the activity: blocks and lost gates are ERROR records."""

    @staticmethod
    def _level(safe_log) -> str:
        (call,) = [c for c in safe_log.call_args_list if "outcome" in c.kwargs]
        return call.args[0]

    async def test_skipped_is_info(self, safe_log) -> None:
        with _patched(False):
            await _run_preflight_gate(_ResolvableInput(), "myapp", "crawl")
        assert self._level(safe_log) == "info"

    async def test_no_verdict_is_error(self, safe_log) -> None:
        from temporalio.exceptions import ApplicationError as TemporalApplicationError

        _, exec_patch = _exec(side_effect=TemporalApplicationError("secret store down"))
        with _patched(True), exec_patch:
            await _run_preflight_gate(_ResolvableInput(), "myapp", "crawl")
        assert self._level(safe_log) == "error"

    async def test_would_block_from_a_dead_frame_is_error(self, safe_log) -> None:
        _, exec_patch = _exec(side_effect=_killed_attempt_after_marker())
        with _patched(True), exec_patch:
            await _run_preflight_gate(
                _ResolvableInput(), "myapp", "crawl", gate_mode="soft"
            )
        assert self._level(safe_log) == "error"
