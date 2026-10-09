"""The gate rejects a Connection that carries no qualified name (CONNECT-1738).

Every fixture is the snapshot a real ``ExtractionInput`` dumps to, built the
way the workflow builds it, so the default ``ConnectionRef`` of an app with no
connection widget is exercised rather than a hand-written dict.
"""

from __future__ import annotations

from contextlib import ExitStack
from typing import Any
from unittest import mock

import pytest

from application_sdk.execution._temporal.preflight_gate import (
    PreflightGateInput,
    PreflightGateMode,
    build_preflight_gate_activity,
)
from application_sdk.execution.errors import ApplicationError
from application_sdk.handler.base import DefaultHandler
from application_sdk.handler.contracts import PreflightOutput, PreflightStatus
from application_sdk.templates.contracts.sql_metadata import ExtractionInput
from application_sdk.testing.preflight import first_outcome_or_none

_GATE = "application_sdk.execution._temporal.preflight_gate"
_QN = "default/snowflake/1700000000"
_INCIDENT_CONNECTION = {
    "typeName": "Connection",
    "attributes": {"defaultCredentialGuid": "example-credential-guid"},
}


def _gate_input(**fields: Any) -> PreflightGateInput:
    return PreflightGateInput.from_extraction_input(
        ExtractionInput.model_validate(fields), "extract"
    )


def _with_snapshot_keys(
    gate_input: PreflightGateInput, **keys: Any
) -> PreflightGateInput:
    snapshot = {**gate_input.extraction_snapshot, **keys}
    return gate_input.model_copy(update={"extraction_snapshot": snapshot})


def _handler() -> DefaultHandler:
    handler = DefaultHandler()
    handler.preflight_check = mock.AsyncMock(  # type: ignore[method-assign]
        return_value=PreflightOutput(status=PreflightStatus.READY)
    )
    return handler


def _patches() -> ExitStack:
    stack = ExitStack()
    stack.enter_context(
        mock.patch(f"{_GATE}.get_infrastructure", return_value=mock.MagicMock())
    )
    return stack


@pytest.fixture(params=[PreflightGateMode.HARD, PreflightGateMode.SOFT])
def mode(request: pytest.FixtureRequest) -> PreflightGateMode:
    return request.param


class TestUnidentifiableConnectionBlocks:
    async def test_incident_payload_blocks_in_every_mode(
        self, mode: PreflightGateMode
    ) -> None:
        handler = _handler()
        gate = build_preflight_gate_activity(handler, app_name="myapp", mode=mode)
        with _patches(), mock.patch(f"{_GATE}.logger") as ml:
            with pytest.raises(ApplicationError) as exc_info:
                await gate(_gate_input(connection=_INCIDENT_CONNECTION))

        assert exc_info.value.type == "PreflightFailed"
        assert exc_info.value.non_retryable
        row = first_outcome_or_none(ml)
        assert row["outcome"] == "blocked"
        assert row["failure.check"] == "connection_qualified_name"
        assert "no qualifiedName" in row["failure.message"]
        handler.preflight_check.assert_not_called()

    async def test_explicit_empty_qualified_name_blocks(self) -> None:
        gate = build_preflight_gate_activity(
            _handler(), app_name="myapp", mode=PreflightGateMode.SOFT
        )
        connection = {"typeName": "Connection", "attributes": {"qualifiedName": " "}}
        with _patches(), pytest.raises(ApplicationError):
            await gate(_gate_input(connection=connection))

    @pytest.mark.parametrize(
        "qualified_name",
        ["default/mongodb", "default/mongodb/", "default//1700000000"],
    )
    async def test_malformed_qualified_name_blocks(
        self, qualified_name: str, mode: PreflightGateMode
    ) -> None:
        handler = _handler()
        gate = build_preflight_gate_activity(handler, app_name="myapp", mode=mode)
        connection = {
            "typeName": "Connection",
            "attributes": {"qualifiedName": qualified_name, "name": "example"},
        }
        with _patches(), mock.patch(f"{_GATE}.logger") as ml:
            with pytest.raises(ApplicationError):
                await gate(_gate_input(connection=connection))

        row = first_outcome_or_none(ml)
        assert row["outcome"] == "blocked"
        assert "not well-formed" in row["failure.message"]
        handler.preflight_check.assert_not_called()

    async def test_malformed_bare_qualified_name_blocks(
        self, mode: PreflightGateMode
    ) -> None:
        handler = _handler()
        gate = build_preflight_gate_activity(handler, app_name="myapp", mode=mode)
        gate_input = _with_snapshot_keys(
            _gate_input(connection=_INCIDENT_CONNECTION),
            connection_qualified_name="default/mongodb",
        )
        with _patches(), pytest.raises(ApplicationError):
            await gate(gate_input)
        handler.preflight_check.assert_not_called()


class TestIdentifiableOrAbsentConnectionProceeds:
    async def _assert_proceeds(
        self, gate_input: PreflightGateInput, mode: PreflightGateMode
    ) -> None:
        handler = _handler()
        gate = build_preflight_gate_activity(handler, app_name="myapp", mode=mode)
        with _patches():
            result = await gate(gate_input)
        assert result.status is PreflightStatus.READY
        handler.preflight_check.assert_awaited_once()

    async def test_app_without_a_connection_widget(
        self, mode: PreflightGateMode
    ) -> None:
        await self._assert_proceeds(_gate_input(), mode)

    async def test_connection_with_qualified_name(
        self, mode: PreflightGateMode
    ) -> None:
        connection = {
            "typeName": "Connection",
            "attributes": {"qualifiedName": _QN, "name": "example"},
        }
        await self._assert_proceeds(_gate_input(connection=connection), mode)

    @pytest.mark.parametrize(
        "keys",
        [
            {"connection_qualified_name": _QN},
            {"connection_qualified_name": [_QN]},
            {"connection-qualified-name": _QN},
        ],
        ids=["bare-name", "list-of-one", "kebab-key"],
    )
    async def test_bare_qualified_name_identifies_the_connection(
        self, keys: dict[str, Any], mode: PreflightGateMode
    ) -> None:
        gate_input = _with_snapshot_keys(
            _gate_input(connection=_INCIDENT_CONNECTION), **keys
        )
        await self._assert_proceeds(gate_input, mode)
