"""Regression coverage for global preflight posture override precedence."""

from __future__ import annotations

from dataclasses import dataclass
from unittest import mock

import pytest
from temporalio.exceptions import ApplicationError

from application_sdk.app.base import App
from application_sdk.app.registry import AppRegistry, TaskRegistry
from application_sdk.contracts.base import Input, Output
from application_sdk.execution._temporal.preflight_gate import (
    PreflightGateInput,
    build_preflight_gate_activity,
)
from application_sdk.execution._temporal.worker import create_worker
from application_sdk.handler.base import DefaultHandler
from application_sdk.handler.contracts import (
    PreflightCheck,
    PreflightInput,
    PreflightOutput,
    PreflightStatus,
)


class _NotReadyHandler(DefaultHandler):
    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        return PreflightOutput(
            status=PreflightStatus.NOT_READY,
            checks=[PreflightCheck(name="stl_connection_log", passed=False)],
        )


@dataclass
class _Input(Input, allow_unbounded_fields=True):
    pass


@dataclass
class _Output(Output, allow_unbounded_fields=True):
    pass


def _client() -> mock.MagicMock:
    client = mock.MagicMock()
    client.namespace = "default"
    return client


@pytest.mark.parametrize(
    ("global_mode", "blocks"),
    [("soft", False), ("hard", True)],
)
async def test_global_override_applies_to_every_entrypoint(
    monkeypatch, global_mode: str, blocks: bool
) -> None:
    """A non-empty global mode overrides a miner-only hard declaration."""
    AppRegistry.reset()
    TaskRegistry.reset()
    monkeypatch.setenv("ATLAN_PREFLIGHT_GATE_MODE", global_mode)

    class _MinerHardApp(App):
        preflight_gate_entrypoint_modes = {"miner": "hard"}  # noqa: RUF012

        async def run(self, input: _Input) -> _Output:
            return _Output()

    captured: dict = {}
    gates = []

    def build_gate(*args, **kwargs):
        captured.update(kwargs)
        gate = build_preflight_gate_activity(*args, **kwargs)
        gates.append(gate)
        return gate

    with (
        mock.patch(
            "application_sdk.execution._temporal.worker.Worker",
            return_value=mock.MagicMock(),
        ),
        mock.patch(
            "application_sdk.execution._temporal.preflight_gate.build_preflight_gate_activity",
            side_effect=build_gate,
        ),
    ):
        create_worker(_client(), handler=_NotReadyHandler())

    assert captured["enforce"] is blocks

    gate = gates[0]
    for entrypoint in ("miner", "crawler"):
        if blocks:
            with pytest.raises(ApplicationError, match="Preflight failed"):
                await gate(PreflightGateInput(entrypoint=entrypoint))
        else:
            result = await gate(PreflightGateInput(entrypoint=entrypoint))
            assert result.status is PreflightStatus.NOT_READY
