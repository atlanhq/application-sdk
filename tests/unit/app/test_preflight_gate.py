"""Unit tests for the injected pre-extraction preflight gate (HYP-1883).

Exercises ``_run_preflight_gate`` directly with ``workflow.patched`` and
``workflow.execute_activity`` mocked, so the gate's guard + abort logic is
verified without a full Temporal workflow environment.
"""

from __future__ import annotations

from unittest import mock

import pytest

from application_sdk.app.base import _run_preflight_gate
from application_sdk.errors.categories import Audience, FailureCategory
from application_sdk.errors.leaves import (
    AuthError,
    PreconditionError,
    SourceUnavailableError,
)
from application_sdk.execution.errors import ApplicationError
from application_sdk.handler.contracts import PreflightCheck, PreflightOutput


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


def _blocking() -> PreflightOutput:
    """A failing blocking check → should_block is True."""
    return PreflightOutput(
        checks=[
            PreflightCheck(
                name="auth",
                passed=False,
                blocking=True,
                message="Auth failed: bad creds",
            )
        ],
    )


def _proceeding() -> PreflightOutput:
    """No failing blocking check → should_block is False."""
    return PreflightOutput(checks=[])


def _advisory_failure() -> PreflightOutput:
    """A failing check left advisory (blocking=False) → does not block."""
    return PreflightOutput(
        checks=[PreflightCheck(name="version", passed=False, blocking=False)],
    )


def _patched(value: bool):
    return mock.patch("application_sdk.app.base.workflow.patched", return_value=value)


def _exec(return_value=None):
    m = mock.AsyncMock(return_value=return_value)
    return m, mock.patch("application_sdk.app.base.workflow.execute_activity", m)


class TestRunPreflightGate:
    async def test_skipped_when_not_patched(self) -> None:
        # Gate not applicable (old workflow history) — silently returns, never
        # raises. Fail-closed applies only once the gate is engaged.
        exec_mock, exec_patch = _exec()
        with _patched(False), exec_patch:
            await _run_preflight_gate(_ResolvableInput(), "myapp", "crawl")
        exec_mock.assert_not_called()

    async def test_skipped_for_non_resolvable_input(self) -> None:
        # Gate not applicable (source-less app) — silently returns, never raises.
        exec_mock, exec_patch = _exec()
        with _patched(True), exec_patch:
            await _run_preflight_gate(_NonResolvableInput(), "myapp", "crawl")
        exec_mock.assert_not_called()

    async def test_aborts_when_should_block(self) -> None:
        exec_mock, exec_patch = _exec(_blocking())
        with _patched(True), exec_patch:
            with pytest.raises(ApplicationError) as excinfo:
                await _run_preflight_gate(_ResolvableInput(), "myapp", "crawl")
        assert excinfo.value.non_retryable is True
        assert excinfo.value.type == "PreflightFailed"
        # The failed check's reason is surfaced on the abort, not the generic
        # fallback — so an automated run's failure says why.
        assert "Auth failed: bad creds" in excinfo.value.message

    async def test_abort_reason_folds_all_blocking_failed_messages(self) -> None:
        # Every blocking-failed check contributes to the surfaced reason.
        verdict = PreflightOutput(
            checks=[
                PreflightCheck(
                    name="auth", passed=False, blocking=True, message="auth blocked"
                ),
                PreflightCheck(
                    name="version",
                    passed=False,
                    blocking=True,
                    message="old version",
                ),
            ],
        )
        exec_mock, exec_patch = _exec(verdict)
        with _patched(True), exec_patch:
            with pytest.raises(ApplicationError) as excinfo:
                await _run_preflight_gate(_ResolvableInput(), "myapp", "crawl")
        assert "auth blocked" in excinfo.value.message
        assert "old version" in excinfo.value.message

    async def test_advisory_messages_excluded_from_abort_reason(self) -> None:
        # A blocking failure aborts the run; a co-occurring advisory failure
        # (blocking=False) surfaces in the UI but its message must NOT leak into
        # the abort reason — attribution is scoped to what actually gated.
        verdict = PreflightOutput(
            checks=[
                PreflightCheck(
                    name="auth", passed=False, blocking=True, message="auth blocked"
                ),
                PreflightCheck(
                    name="version",
                    passed=False,
                    blocking=False,
                    message="advisory version drift",
                ),
            ],
        )
        exec_mock, exec_patch = _exec(verdict)
        with _patched(True), exec_patch:
            with pytest.raises(ApplicationError) as excinfo:
                await _run_preflight_gate(_ResolvableInput(), "myapp", "crawl")
        assert "auth blocked" in excinfo.value.message
        assert "advisory version drift" not in excinfo.value.message

    async def test_abort_reason_falls_back_when_no_check_message(self) -> None:
        # A blocking failure with no message still aborts with the generic reason.
        verdict = PreflightOutput(
            checks=[PreflightCheck(name="auth", passed=False, blocking=True)],
        )
        exec_mock, exec_patch = _exec(verdict)
        with _patched(True), exec_patch:
            with pytest.raises(ApplicationError) as excinfo:
                await _run_preflight_gate(_ResolvableInput(), "myapp", "crawl")
        assert "aborting before extraction" in excinfo.value.message

    async def test_abort_reason_joins_multiple_failures(self) -> None:
        # Multiple failed checks are joined with "; " — pin the separator so a
        # switch to newline/comma output is caught.
        verdict = PreflightOutput(
            checks=[
                PreflightCheck(
                    name="auth", passed=False, blocking=True, message="auth down"
                ),
                PreflightCheck(
                    name="net",
                    passed=False,
                    blocking=True,
                    message="host unreachable",
                ),
            ],
        )
        exec_mock, exec_patch = _exec(verdict)
        with _patched(True), exec_patch:
            with pytest.raises(ApplicationError) as excinfo:
                await _run_preflight_gate(_ResolvableInput(), "myapp", "crawl")
        assert "auth down" in excinfo.value.message
        assert "host unreachable" in excinfo.value.message
        assert "; " in excinfo.value.message

    async def test_explicit_output_message_wins_over_per_check_join(self) -> None:
        # Precedence: an explicit PreflightOutput.message takes priority over the
        # folded per-check reasons (result.message or reasons or generic).
        verdict = PreflightOutput(
            message="Summary: 3 of 5 checks failed",
            checks=[
                PreflightCheck(
                    name="auth", passed=False, blocking=True, message="auth down"
                ),
            ],
        )
        exec_mock, exec_patch = _exec(verdict)
        with _patched(True), exec_patch:
            with pytest.raises(ApplicationError) as excinfo:
                await _run_preflight_gate(_ResolvableInput(), "myapp", "crawl")
        assert excinfo.value.message == "Summary: 3 of 5 checks failed"
        assert "auth down" not in excinfo.value.message

    async def test_abort_details_carry_typed_category(self) -> None:
        # A failed check that declares a category must surface that category
        # (and its canonical code/audience) to AE via structured FailureDetails,
        # while the "PreflightFailed" type marker is preserved.
        verdict = PreflightOutput(
            checks=[
                PreflightCheck(
                    name="auth",
                    passed=False,
                    blocking=True,
                    message="Auth failed",
                    category=FailureCategory.AUTH,
                    suggested_action="Rotate the credential",
                )
            ],
        )
        _, exec_patch = _exec(verdict)
        with _patched(True), exec_patch:
            with pytest.raises(ApplicationError) as excinfo:
                await _run_preflight_gate(_ResolvableInput(), "myapp", "crawl")
        err = excinfo.value
        assert err.type == "PreflightFailed"  # preflight marker preserved
        details = err.details[0]
        assert details.category is FailureCategory.AUTH
        assert details.code == "AUTH"
        assert details.audience is Audience.USER
        assert details.retryable is False
        assert details.suggested_action == "Rotate the credential"

    async def test_abort_details_default_precondition_when_unclassified(self) -> None:
        # _blocking() sets no category → default to PRECONDITION so the abort
        # still carries structured details (better than an opaque PreflightFailed).
        _, exec_patch = _exec(_blocking())
        with _patched(True), exec_patch:
            with pytest.raises(ApplicationError) as excinfo:
                await _run_preflight_gate(_ResolvableInput(), "myapp", "crawl")
        assert excinfo.value.details[0].category is FailureCategory.PRECONDITION

    async def test_abort_uses_first_classified_failure(self) -> None:
        # When multiple checks fail, the first classified one wins.
        verdict = PreflightOutput(
            checks=[
                PreflightCheck(
                    name="a",
                    passed=False,
                    blocking=True,
                    category=FailureCategory.SOURCE_UNAVAILABLE,
                ),
                PreflightCheck(
                    name="b",
                    passed=False,
                    blocking=True,
                    category=FailureCategory.PERMISSION,
                ),
            ],
        )
        _, exec_patch = _exec(verdict)
        with _patched(True), exec_patch:
            with pytest.raises(ApplicationError) as excinfo:
                await _run_preflight_gate(_ResolvableInput(), "myapp", "crawl")
        assert excinfo.value.details[0].category is FailureCategory.SOURCE_UNAVAILABLE

    async def test_abort_forwards_typed_check_error_verbatim(self) -> None:
        # A check built via from_error carries FailureDetails; the gate must
        # forward the app-specific code/audience/evidence — not rebuild the
        # canonical category-level leaf — with retryable/app_name gate-owned.
        class _ViewDefinitionError(PreconditionError):
            code = "PRECONDITION_SOURCE_VIEW_DEFINITION"

        verdict = PreflightOutput(
            checks=[
                PreflightCheck.from_error(
                    "au_views",
                    _ViewDefinitionError(
                        message="A referenced source view is broken",
                        suggested_action="Repair or drop the broken view",
                    ),
                )
            ],
        )
        _, exec_patch = _exec(verdict)
        with _patched(True), exec_patch:
            with pytest.raises(ApplicationError) as excinfo:
                await _run_preflight_gate(_ResolvableInput(), "myapp", "crawl")
        err = excinfo.value
        assert err.type == "PreflightFailed"
        details = err.details[0]
        assert details.code == "PRECONDITION_SOURCE_VIEW_DEFINITION"
        assert details.category is FailureCategory.PRECONDITION
        assert details.retryable is False
        assert details.app_name == "myapp"
        assert details.message == "A referenced source view is broken"
        assert details.suggested_action == "Repair or drop the broken view"
        assert details.message in err.message

    async def test_error_bearing_check_outranks_earlier_category_only_check(
        self,
    ) -> None:
        # Typed evidence wins over a bare category, regardless of check order.
        verdict = PreflightOutput(
            checks=[
                PreflightCheck(
                    name="a",
                    passed=False,
                    blocking=True,
                    category=FailureCategory.SOURCE_UNAVAILABLE,
                ),
                PreflightCheck.from_error("b", AuthError(message="Login rejected")),
            ],
        )
        _, exec_patch = _exec(verdict)
        with _patched(True), exec_patch:
            with pytest.raises(ApplicationError) as excinfo:
                await _run_preflight_gate(_ResolvableInput(), "myapp", "crawl")
        details = excinfo.value.details[0]
        assert details.code == AuthError.code
        assert details.category is FailureCategory.AUTH

    async def test_proceeds_on_success(self) -> None:
        exec_mock, exec_patch = _exec(_proceeding())
        with _patched(True), exec_patch:
            result = await _run_preflight_gate(_ResolvableInput(), "myapp", "crawl")
        assert result is None
        exec_mock.assert_awaited_once()

    async def test_advisory_failure_does_not_block(self) -> None:
        # A failing check left blocking=False is advisory — nothing the app
        # marked blocking failed, so the gate must not abort the run.
        exec_mock, exec_patch = _exec(_advisory_failure())
        with _patched(True), exec_patch:
            result = await _run_preflight_gate(_ResolvableInput(), "myapp", "crawl")
        assert result is None

    async def test_fails_closed_when_gate_raises(self) -> None:
        # Fail-closed (HYP-1883): if the gate activity can't produce a verdict —
        # timeout, transport/secret-store failure, or an unexpected handler
        # crash — the run BLOCKS rather than proceeds. With no typed detail on
        # the cause chain, the block is attributed to DEPENDENCY_UNAVAILABLE (a
        # dead dependency, not misattributed as bad auth), and it is logged
        # loudly with the traceback.
        from temporalio.exceptions import ApplicationError as TemporalApplicationError

        exec_mock = mock.AsyncMock(side_effect=TemporalApplicationError("boom"))
        with (
            _patched(True),
            mock.patch("application_sdk.app.base.workflow.execute_activity", exec_mock),
            mock.patch("application_sdk.app.base._safe_log") as safe_log,
        ):
            with pytest.raises(ApplicationError) as excinfo:
                await _run_preflight_gate(_ResolvableInput(), "myapp", "crawl")
        err = excinfo.value
        assert err.type == "PreflightUnavailable"
        assert err.non_retryable is True
        details = err.details[0]
        assert details.category is FailureCategory.DEPENDENCY_UNAVAILABLE
        assert details.retryable is False
        assert details.app_name == "myapp"
        assert safe_log.call_args.args[0] == "error"
        assert safe_log.call_args.kwargs.get("exc_info") is True

    async def test_fails_closed_forwards_typed_cause(self) -> None:
        # When the gate failure carries a typed FailureDetails on its cause chain
        # (the SDK activity wrapper serializes an AppError into the wrapping
        # ApplicationError.details — as a dict after the activity→workflow serde
        # boundary), the fail-closed block forwards that attribution: code
        # preserved, retryable forced False, app_name stamped.
        from temporalio.exceptions import ApplicationError as TemporalApplicationError

        typed = SourceUnavailableError(message="Source is down").to_failure_details()
        cause = TemporalApplicationError(
            "source down", typed.model_dump(mode="json"), type="SourceUnavailableError"
        )
        wrapper = TemporalApplicationError("activity failed")
        wrapper.__cause__ = cause

        exec_mock = mock.AsyncMock(side_effect=wrapper)
        with (
            _patched(True),
            mock.patch("application_sdk.app.base.workflow.execute_activity", exec_mock),
            mock.patch("application_sdk.app.base._safe_log"),
        ):
            with pytest.raises(ApplicationError) as excinfo:
                await _run_preflight_gate(_ResolvableInput(), "myapp", "crawl")
        err = excinfo.value
        assert err.type == "PreflightUnavailable"
        details = err.details[0]
        assert details.category is FailureCategory.SOURCE_UNAVAILABLE
        assert details.code == "SOURCE_UNAVAILABLE"
        assert details.retryable is False
        assert details.app_name == "myapp"

    async def test_non_exception_error_still_propagates(self) -> None:
        # Fail-closed catches Exception (gate plumbing). Control-flow signals
        # like cancellation are BaseException, not Exception, so they propagate
        # untouched rather than being turned into a PreflightUnavailable block.
        import asyncio

        exec_mock = mock.AsyncMock(side_effect=asyncio.CancelledError())
        with (
            _patched(True),
            mock.patch("application_sdk.app.base.workflow.execute_activity", exec_mock),
        ):
            with pytest.raises(asyncio.CancelledError):
                await _run_preflight_gate(_ResolvableInput(), "myapp", "crawl")

    async def test_dispatches_activity_with_preflight_output_result_type(self) -> None:
        # Regression: execute_activity dispatched by name returns a raw dict
        # unless result_type is set — and the workflow calls .should_block
        # on the result, which a dict doesn't have. Caught live on mysql canary.
        exec_mock, exec_patch = _exec(_proceeding())
        with _patched(True), exec_patch:
            await _run_preflight_gate(_ResolvableInput(), "myapp", "crawl")
        _, kwargs = exec_mock.call_args
        assert kwargs.get("result_type") is PreflightOutput

    async def test_forwards_routing_fields_to_activity(self) -> None:
        exec_mock, exec_patch = _exec(_proceeding())
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
