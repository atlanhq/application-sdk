"""Unit tests for the injected pre-extraction preflight gate (HYP-1883).

Exercises ``_run_preflight_gate`` directly with ``workflow.patched`` and
``workflow.execute_activity`` mocked, so the gate's guard + abort logic is
verified without a full Temporal workflow environment.
"""

from __future__ import annotations

from unittest import mock

import pytest

from application_sdk.app.base import _run_preflight_gate
from application_sdk.execution.errors import ApplicationError
from application_sdk.handler.contracts import (
    PreflightCheck,
    PreflightOutput,
    PreflightStatus,
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


def _blocking() -> PreflightOutput:
    """A failed blocking check → should_block is True."""
    return PreflightOutput(
        status=PreflightStatus.NOT_READY,
        checks=[PreflightCheck(name="auth", passed=False, blocking=True)],
    )


def _proceeding() -> PreflightOutput:
    """No blocking failure → should_block is False."""
    return PreflightOutput(status=PreflightStatus.READY, checks=[])


def _advisory_failure() -> PreflightOutput:
    """A failed NON-blocking check → should_block is False (advisory only)."""
    return PreflightOutput(
        status=PreflightStatus.NOT_READY,
        checks=[PreflightCheck(name="version", passed=False, blocking=False)],
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

    async def test_handler_raise_propagates(self) -> None:
        # A handler that raises its own typed error surfaces as a FailureError
        # and propagates straight through to fail the run.
        from temporalio.exceptions import ApplicationError as TemporalApplicationError

        exec_mock = mock.AsyncMock(side_effect=TemporalApplicationError("boom"))
        with (
            _patched(True),
            mock.patch("application_sdk.app.base.workflow.execute_activity", exec_mock),
        ):
            with pytest.raises(TemporalApplicationError):
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
