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


class _ActivityErrorStub(Exception):
    """Stand-in for Temporal's ActivityError: wraps a cause exception."""

    def __init__(self, cause: BaseException) -> None:
        super().__init__("activity failed")
        self.cause = cause
        self.__cause__ = cause


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

    async def test_proceeds_on_ready(self, safe_log) -> None:
        exec_mock, exec_patch = _exec(
            PreflightOutput(status=PreflightStatus.READY, checks=[])
        )
        with _patched(True), exec_patch:
            result = await _run_preflight_gate(_ResolvableInput(), "myapp", "crawl")
        assert result is None
        exec_mock.assert_awaited_once()
        assert _outcomes(safe_log) == ["proceeded"]

    async def test_proceeds_on_partial(self, safe_log) -> None:
        exec_mock, exec_patch = _exec(
            PreflightOutput(status=PreflightStatus.PARTIAL, checks=[])
        )
        with _patched(True), exec_patch:
            result = await _run_preflight_gate(_ResolvableInput(), "myapp", "crawl")
        assert result is None
        assert _outcomes(safe_log) == ["proceeded"]

    async def test_reraises_on_preflight_failed(self, safe_log) -> None:
        _, exec_patch = _exec(side_effect=_preflight_failed_error())
        with _patched(True), exec_patch:
            with pytest.raises(ApplicationError) as excinfo:
                await _run_preflight_gate(_ResolvableInput(), "myapp", "crawl")
        assert excinfo.value.type == "PreflightFailed"
        assert "blocked" in _outcomes(safe_log)

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
        assert "blocked" in _outcomes(safe_log)

    async def test_fail_open_on_other_activity_error(self, safe_log) -> None:
        # Any failure that is NOT a deliberate PreflightFailed block → proceed,
        # log ERROR loudly, and emit a no_verdict outcome with the error type.
        from temporalio.exceptions import ApplicationError as TemporalApplicationError

        exec_mock, exec_patch = _exec(
            side_effect=TemporalApplicationError("secret store down")
        )
        with _patched(True), exec_patch:
            result = await _run_preflight_gate(_ResolvableInput(), "myapp", "crawl")
        assert result is None
        exec_mock.assert_awaited_once()
        levels = [c.args[0] for c in safe_log.call_args_list]
        assert "error" in levels
        assert "no_verdict" in _outcomes(safe_log)
        no_verdict_call = next(
            c
            for c in safe_log.call_args_list
            if c.kwargs.get("outcome") == "no_verdict"
        )
        assert no_verdict_call.kwargs.get("error_type")

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
