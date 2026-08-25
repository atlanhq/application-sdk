"""Regression coverage for entrypoint-specific preflight gate posture."""

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


def test_worker_passes_entrypoint_modes_to_preflight_gate(
    monkeypatch,
) -> None:
    """A miner can hard-block without changing the crawler's soft posture."""
    AppRegistry.reset()
    TaskRegistry.reset()
    monkeypatch.delenv("ATLAN_PREFLIGHT_GATE_MODE", raising=False)

    class _ScopedGateApp(App):
        preflight_gate_entrypoint_modes = {"miner": "hard"}  # noqa: RUF012

        async def run(self, input: _Input) -> _Output:
            return _Output()

    captured: dict = {}

    def build_gate(*args, **kwargs):
        captured.update(kwargs)

        async def gate(*args, **kwargs):
            return None

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
        create_worker(_client())

    assert captured["entrypoint_modes"] == {"miner": "hard"}


async def test_hard_entrypoint_blocks_not_ready_without_blocking_crawl() -> None:
    """A miner-only required table cannot make crawler readiness fail closed."""
    gate = build_preflight_gate_activity(
        _NotReadyHandler(),
        app_name="redshift",
        entrypoint_modes={"miner": "hard"},
    )

    crawl = await gate(PreflightGateInput(entrypoint="crawl"))

    assert crawl.status is PreflightStatus.NOT_READY
    with pytest.raises(ApplicationError, match="Preflight failed"):
        await gate(PreflightGateInput(entrypoint="miner"))
