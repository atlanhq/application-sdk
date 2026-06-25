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
from application_sdk.handler.contracts import PreflightOutput, PreflightStatus


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


def _output(status: PreflightStatus) -> PreflightOutput:
    return PreflightOutput(status=status, checks=[])


def _patched(value: bool):
    return mock.patch("application_sdk.app.base.workflow.patched", return_value=value)


def _exec(return_value=None):
    m = mock.AsyncMock(return_value=return_value)
    return m, mock.patch("application_sdk.app.base.workflow.execute_activity", m)


class TestRunPreflightGate:
    async def test_skipped_when_not_patched(self) -> None:
        exec_mock, exec_patch = _exec()
        with _patched(False), exec_patch:
            await _run_preflight_gate(_ResolvableInput(), "crawl")
        exec_mock.assert_not_called()

    async def test_skipped_for_non_resolvable_input(self) -> None:
        exec_mock, exec_patch = _exec()
        with _patched(True), exec_patch:
            await _run_preflight_gate(_NonResolvableInput(), "crawl")
        exec_mock.assert_not_called()

    async def test_aborts_on_canonical_failed(self) -> None:
        exec_mock, exec_patch = _exec(_output(PreflightStatus.FAILED))
        with _patched(True), exec_patch:
            with pytest.raises(ApplicationError) as excinfo:
                await _run_preflight_gate(_ResolvableInput(), "crawl")
        assert excinfo.value.non_retryable is True
        assert excinfo.value.type == "PreflightFailed"

    async def test_proceeds_on_success(self) -> None:
        exec_mock, exec_patch = _exec(_output(PreflightStatus.SUCCESS))
        with _patched(True), exec_patch:
            result = await _run_preflight_gate(_ResolvableInput(), "crawl")
        assert result is None
        exec_mock.assert_awaited_once()

    async def test_legacy_not_ready_does_not_block(self) -> None:
        # NOT_READY folds to canonical SUCCESS — back-compat must not abort.
        exec_mock, exec_patch = _exec(_output(PreflightStatus.NOT_READY))
        with _patched(True), exec_patch:
            result = await _run_preflight_gate(_ResolvableInput(), "crawl")
        assert result is None

    async def test_forwards_routing_fields_to_activity(self) -> None:
        exec_mock, exec_patch = _exec(_output(PreflightStatus.SUCCESS))
        with _patched(True), exec_patch:
            await _run_preflight_gate(
                _ResolvableInput(guid="abc", method="agent"), "asset-export"
            )
        args, _ = exec_mock.call_args
        assert args[0] == "sdr:preflight_gate"
        gate_input = args[1]
        assert gate_input.credential_guid == "abc"
        assert gate_input.extraction_method == "agent"
        assert gate_input.entrypoint == "asset-export"
