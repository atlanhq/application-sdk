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
from application_sdk.errors.leaves import AuthError, PreconditionError
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
    """A failed blocking check → should_block is True."""
    return PreflightOutput(
        checks=[
            PreflightCheck(
                name="auth",
                passed=False,
                required=True,
                message="Auth failed: bad creds",
            )
        ],
    )


def _proceeding() -> PreflightOutput:
    """No blocking failure → should_block is False."""
    return PreflightOutput(checks=[])


def _advisory_failure() -> PreflightOutput:
    """A failed NON-blocking check → should_block is False (advisory only)."""
    return PreflightOutput(
        checks=[PreflightCheck(name="version", passed=False, required=False)],
    )


def _patched(value: bool):
    return mock.patch("application_sdk.app.base.workflow.patched", return_value=value)


def _exec(return_value=None):
    m = mock.AsyncMock(return_value=return_value)
    return m, mock.patch("application_sdk.app.base.workflow.execute_activity", m)


class TestRunPreflightGate:
    async def test_skipped_when_not_patched(self) -> None:
        exec_mock, exec_patch = _exec()
        with _patched(False), exec_patch:
            await _run_preflight_gate(_ResolvableInput(), "myapp", "crawl")
        exec_mock.assert_not_called()

    async def test_skipped_for_non_resolvable_input(self) -> None:
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
        # The failed blocking check's reason is surfaced on the abort, not the
        # generic fallback — so an automated run's failure says why.
        assert "Auth failed: bad creds" in excinfo.value.message

    async def test_abort_reason_excludes_advisory_messages(self) -> None:
        # Only blocking-and-failed checks contribute to the surfaced reason;
        # a failed advisory check must not leak into the abort message.
        verdict = PreflightOutput(
            checks=[
                PreflightCheck(
                    name="auth", passed=False, required=True, message="auth blocked"
                ),
                PreflightCheck(
                    name="version", passed=False, required=False, message="old version"
                ),
            ],
        )
        exec_mock, exec_patch = _exec(verdict)
        with _patched(True), exec_patch:
            with pytest.raises(ApplicationError) as excinfo:
                await _run_preflight_gate(_ResolvableInput(), "myapp", "crawl")
        assert "auth blocked" in excinfo.value.message
        assert "old version" not in excinfo.value.message

    async def test_abort_reason_falls_back_when_no_check_message(self) -> None:
        # A blocking failure with no message still aborts with the generic reason.
        verdict = PreflightOutput(
            checks=[PreflightCheck(name="auth", passed=False, required=True)],
        )
        exec_mock, exec_patch = _exec(verdict)
        with _patched(True), exec_patch:
            with pytest.raises(ApplicationError) as excinfo:
                await _run_preflight_gate(_ResolvableInput(), "myapp", "crawl")
        assert "aborting before extraction" in excinfo.value.message

    async def test_abort_reason_joins_multiple_blocking_failures(self) -> None:
        # Multiple blocking failures are joined with "; " — pin the separator so
        # a switch to newline/comma output is caught.
        verdict = PreflightOutput(
            checks=[
                PreflightCheck(
                    name="auth", passed=False, required=True, message="auth down"
                ),
                PreflightCheck(
                    name="net", passed=False, required=True, message="host unreachable"
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
                    name="auth", passed=False, required=True, message="auth down"
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
        # A blocking check that declares a category must surface that category
        # (and its canonical code/audience) to AE via structured FailureDetails,
        # while the "PreflightFailed" type marker is preserved.
        verdict = PreflightOutput(
            checks=[
                PreflightCheck(
                    name="auth",
                    passed=False,
                    required=True,
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

    async def test_abort_uses_first_classified_blocking_failure(self) -> None:
        # When multiple blocking checks fail, the first classified one wins.
        verdict = PreflightOutput(
            checks=[
                PreflightCheck(
                    name="a",
                    passed=False,
                    required=True,
                    category=FailureCategory.SOURCE_UNAVAILABLE,
                ),
                PreflightCheck(
                    name="b",
                    passed=False,
                    required=True,
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
                    required=True,
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
        # A failed non-blocking check must not abort the run.
        exec_mock, exec_patch = _exec(_advisory_failure())
        with _patched(True), exec_patch:
            result = await _run_preflight_gate(_ResolvableInput(), "myapp", "crawl")
        assert result is None

    async def test_infra_error_fails_open(self) -> None:
        # Fail-open (HYP-1883): if the gate activity can't produce a verdict —
        # timeout, transport/secret-store failure, or an unexpected handler crash
        # (all FailureError subclasses) — the run PROCEEDS rather than dies. A
        # handler that raises no longer blocks; blocking is only via should_block.
        from temporalio.exceptions import ApplicationError as TemporalApplicationError

        exec_mock = mock.AsyncMock(side_effect=TemporalApplicationError("boom"))
        with (
            _patched(True),
            mock.patch("application_sdk.app.base.workflow.execute_activity", exec_mock),
            mock.patch("application_sdk.app.base._safe_log") as safe_log,
        ):
            result = await _run_preflight_gate(_ResolvableInput(), "myapp", "crawl")
        assert result is None  # proceeded, did not raise
        exec_mock.assert_awaited_once()
        safe_log.assert_called_once()
        assert safe_log.call_args.args[0] == "error"  # logged loudly

    async def test_non_failure_error_still_propagates(self) -> None:
        # Fail-open catches only FailureError (gate plumbing). Control-flow
        # exceptions like cancellation must NOT be swallowed.
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
