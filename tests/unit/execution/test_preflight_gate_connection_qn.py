"""Tests for connection_qualified_name validation in the preflight gate.

Validates the platform-level check that rejects a broken connection snapshot
(missing qualifiedName) before any extraction work begins — both at the
helper level and at the gate activity level (end-to-end).
"""

from __future__ import annotations

from contextlib import ExitStack
from typing import Any
from unittest import mock

import pytest

from application_sdk.execution._temporal.preflight_gate import (
    PreflightGateInput,
    _check_connection_qualified_name,
    build_preflight_gate_activity,
)
from application_sdk.execution.errors import ApplicationError
from application_sdk.handler.base import DefaultHandler
from application_sdk.handler.contracts import PreflightOutput, PreflightStatus

# ---------------------------------------------------------------------------
# Test infrastructure
# ---------------------------------------------------------------------------

_GATE = "application_sdk.execution._temporal.preflight_gate"
_UNSET = object()


def _gate(*, mode: str = "hard"):
    """Build a gate activity with a DefaultHandler (always READY)."""
    from application_sdk.execution._temporal.preflight_gate import PreflightGateMode

    gate_mode = PreflightGateMode.HARD if mode == "hard" else PreflightGateMode.SOFT
    return build_preflight_gate_activity(
        DefaultHandler(), app_name="myapp", mode=gate_mode
    )


def _infra_patches(secret_store: Any = _UNSET):
    """Patch infrastructure so the gate doesn't need a real secret store."""
    fake_infra = mock.MagicMock()
    fake_infra.secret_store = (
        mock.MagicMock(name="SecretStore") if secret_store is _UNSET else secret_store
    )
    stack = ExitStack()
    stack.enter_context(
        mock.patch(f"{_GATE}.get_infrastructure", return_value=fake_infra)
    )
    return stack


def _connect_1738_snapshot() -> dict[str, Any]:
    """The exact broken snapshot from the CONNECT-1738 incident."""
    return {
        "connection_qualified_name": "",
        "credential_guid": "cfd33802-a2ac-4031-a7d3-240d8d4717b0",
        "connection": {
            "typeName": "Connection",
            "attributes": {
                "defaultCredentialGuid": "cfd33802-a2ac-4031-a7d3-240d8d4717b0",
            },
        },
    }


def _healthy_snapshot() -> dict[str, Any]:
    """A healthy snapshot with a valid qualifiedName."""
    return {
        "connection_qualified_name": "default/looker/1789393939",
        "credential_guid": "cfd33802-a2ac-4031-a7d3-240d8d4717b0",
        "connection": {
            "typeName": "Connection",
            "attributes": {
                "qualifiedName": "default/looker/1789393939",
                "defaultCredentialGuid": "cfd33802-a2ac-4031-a7d3-240d8d4717b0",
                "connectorName": "looker",
                "name": "looker-prod",
            },
        },
    }


# ---------------------------------------------------------------------------
# Gate activity behavioral tests (end-to-end through the gate)
# ---------------------------------------------------------------------------


class TestGateRejectsEmptyConnectionQN:
    """The gate activity must reject a broken connection snapshot
    BEFORE credential resolution or handler invocation."""

    async def test_hard_mode_blocks_empty_cqn(self) -> None:
        """Hard-mode gate raises PreflightFailed on the CONNECT-1738 snapshot."""
        gate = _gate(mode="hard")
        gate_input = PreflightGateInput(
            extraction_snapshot=_connect_1738_snapshot(),
        )
        with _infra_patches():
            with pytest.raises(ApplicationError) as exc_info:
                await gate(gate_input)
        assert exc_info.value.type == "PreflightFailed"

    async def test_soft_mode_returns_not_ready_for_empty_cqn(self) -> None:
        """Soft-mode gate returns NOT_READY (does not raise) on the broken snapshot."""
        gate = _gate(mode="soft")
        gate_input = PreflightGateInput(
            extraction_snapshot=_connect_1738_snapshot(),
        )
        with _infra_patches():
            result = await gate(gate_input)
        assert result.status is PreflightStatus.NOT_READY
        assert any(
            c.name == "connection_qualified_name" and not c.passed
            for c in result.checks
        )

    async def test_healthy_snapshot_proceeds(self) -> None:
        """A healthy snapshot with a valid qualifiedName proceeds normally."""
        gate = _gate(mode="hard")
        gate_input = PreflightGateInput(
            extraction_snapshot=_healthy_snapshot(),
        )
        with _infra_patches():
            result = await gate(gate_input)
        assert result.status is PreflightStatus.READY

    async def test_no_connection_in_snapshot_proceeds(self) -> None:
        """A snapshot without any connection data proceeds (check skipped)."""
        gate = _gate(mode="hard")
        gate_input = PreflightGateInput(
            extraction_snapshot={"some_other_field": "value"},
        )
        with _infra_patches():
            result = await gate(gate_input)
        assert result.status is PreflightStatus.READY

    async def test_handler_never_called_on_empty_cqn(self) -> None:
        """The handler's preflight_check must NOT be called when the CQN is empty —
        the gate should short-circuit before credential resolution."""
        handler = DefaultHandler()
        handler.preflight_check = mock.AsyncMock(  # type: ignore[method-assign]
            return_value=PreflightOutput(status=PreflightStatus.READY)
        )
        from application_sdk.execution._temporal.preflight_gate import PreflightGateMode

        gate = build_preflight_gate_activity(
            handler, app_name="myapp", mode=PreflightGateMode.SOFT
        )
        gate_input = PreflightGateInput(
            extraction_snapshot=_connect_1738_snapshot(),
        )
        with _infra_patches():
            result = await gate(gate_input)
        assert result.status is PreflightStatus.NOT_READY
        handler.preflight_check.assert_not_called()


# ---------------------------------------------------------------------------
# Helper function unit tests (_check_connection_qualified_name)
# ---------------------------------------------------------------------------


class TestCheckConnectionQualifiedName:
    """Unit tests for the _check_connection_qualified_name helper."""

    def test_no_connection_data_returns_none(self) -> None:
        """Snapshot without connection or connection_qualified_name — skip."""
        assert _check_connection_qualified_name({"some_other_field": "value"}) is None

    def test_empty_snapshot_returns_none(self) -> None:
        assert _check_connection_qualified_name({}) is None

    def test_valid_top_level_cqn_returns_none(self) -> None:
        result = _check_connection_qualified_name(
            {"connection_qualified_name": "default/looker/1789393939"}
        )
        assert result is None

    def test_valid_attribute_qn_returns_none(self) -> None:
        result = _check_connection_qualified_name(
            {
                "connection_qualified_name": "",
                "connection": {
                    "typeName": "Connection",
                    "attributes": {
                        "qualifiedName": "default/looker/1789393939",
                        "defaultCredentialGuid": "abc-123",
                    },
                },
            }
        )
        assert result is None

    def test_connect_1738_snapshot_fails(self) -> None:
        """The exact CONNECT-1738 scenario: connection exists, no qualifiedName."""
        result = _check_connection_qualified_name(_connect_1738_snapshot())
        assert result is not None
        assert result.passed is False
        assert result.name == "connection_qualified_name"
        assert result.error is not None

    def test_empty_cqn_no_connection_object_fails(self) -> None:
        result = _check_connection_qualified_name({"connection_qualified_name": ""})
        assert result is not None
        assert result.passed is False

    def test_whitespace_only_cqn_fails(self) -> None:
        result = _check_connection_qualified_name(
            {
                "connection_qualified_name": "   ",
                "connection": {"typeName": "Connection", "attributes": {}},
            }
        )
        assert result is not None
        assert result.passed is False

    def test_snake_case_qualified_name_passes(self) -> None:
        result = _check_connection_qualified_name(
            {
                "connection_qualified_name": "",
                "connection": {
                    "typeName": "Connection",
                    "attributes": {"qualified_name": "default/databricks/1789000000"},
                },
            }
        )
        assert result is None

    def test_none_attributes_fails(self) -> None:
        result = _check_connection_qualified_name(
            {
                "connection_qualified_name": "",
                "connection": {"typeName": "Connection", "attributes": None},
            }
        )
        assert result is not None
        assert result.passed is False

    def test_valid_cqn_overrides_missing_attribute_qn(self) -> None:
        result = _check_connection_qualified_name(
            {
                "connection_qualified_name": "default/looker/1789393939",
                "connection": {"typeName": "Connection", "attributes": {}},
            }
        )
        assert result is None

    def test_error_has_invalid_input_category(self) -> None:
        result = _check_connection_qualified_name(_connect_1738_snapshot())
        assert result is not None
        assert result.error is not None
        assert result.error.category.value == "INVALID_INPUT"
