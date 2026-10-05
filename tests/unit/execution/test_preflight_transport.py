"""The gate reaches the handler only through ``PreflightTransport`` (FND-3280).

The transport here is a plain object — not a ``Handler`` — so a gate that still
reached around the seam (``transport.handler``, an ``isinstance`` check, a
``Handler``-only method) would fail these tests rather than pass by accident.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest import mock

from application_sdk.app.base import App
from application_sdk.app.registry import AppRegistry, TaskRegistry
from application_sdk.contracts.base import Input, Output
from application_sdk.execution._temporal import preflight_gate
from application_sdk.execution._temporal.preflight_gate import (
    PreflightGateInput,
    build_preflight_gate_activity,
    build_preflight_warmup_activities,
)
from application_sdk.execution._temporal.preflight_transport import (
    InProcessPreflightTransport,
    PreflightTransport,
)
from application_sdk.execution._temporal.worker import create_worker
from application_sdk.handler.base import DefaultHandler
from application_sdk.handler.contracts import (
    PreflightCheck,
    PreflightInput,
    PreflightOutput,
    PreflightStatus,
    WarmupState,
    WarmupStatus,
)


class _RecordingTransport:
    """A transport with no handler behind it: records each call, answers fixed."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, PreflightInput]] = []

    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        self.calls.append(("preflight_check", input))
        return PreflightOutput(
            status=PreflightStatus.READY,
            checks=[PreflightCheck(name="reachable", passed=True)],
        )

    async def warmup_start(self, input: PreflightInput) -> WarmupState:
        self.calls.append(("warmup_start", input))
        return WarmupState(status=WarmupStatus.RUNNING)

    async def warmup_state(self, input: PreflightInput) -> WarmupState:
        self.calls.append(("warmup_state", input))
        return WarmupState(status=WarmupStatus.READY)


def _as_transport(transport: PreflightTransport) -> PreflightTransport:
    """Typed identity: pyright checks each argument satisfies the protocol."""
    return transport


class TestTheGateCallsOnlyTheTransport:
    async def test_the_check_activity_returns_the_transports_verdict(self) -> None:
        transport = _RecordingTransport()
        gate = build_preflight_gate_activity(_as_transport(transport), "myapp")
        with mock.patch.object(preflight_gate, "logger"):
            result = await gate(PreflightGateInput(entrypoint="crawl"))
        assert result.status is PreflightStatus.READY
        assert [c.name for c in result.checks] == ["reachable"]
        ((operation, seen),) = transport.calls
        assert operation == "preflight_check"
        assert seen.entrypoint == "crawl"

    async def test_the_warmup_activities_call_their_own_operation(self) -> None:
        transport = _RecordingTransport()
        start, state = build_preflight_warmup_activities(
            _as_transport(transport), "myapp"
        )
        started = await start(PreflightGateInput(entrypoint="crawl"))
        polled = await state(PreflightGateInput(entrypoint="crawl"))
        assert started.status is WarmupStatus.RUNNING
        assert polled.status is WarmupStatus.READY
        assert [op for op, _ in transport.calls] == ["warmup_start", "warmup_state"]


class _Handler(DefaultHandler):
    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        return PreflightOutput(
            status=PreflightStatus.NOT_READY, message=input.entrypoint
        )

    async def warmup_start(self, input: PreflightInput) -> WarmupState:
        return WarmupState(status=WarmupStatus.RUNNING, message="start")

    async def warmup_state(self, input: PreflightInput) -> WarmupState:
        return WarmupState(status=WarmupStatus.READY, message="state")


class TestTheInProcessTransport:
    async def test_each_call_is_the_handlers_own_answer(self) -> None:
        transport = InProcessPreflightTransport(_Handler())
        input = PreflightInput(entrypoint="crawl")
        assert (await transport.preflight_check(input)).message == "crawl"
        assert (await transport.warmup_start(input)).message == "start"
        assert (await transport.warmup_state(input)).message == "state"

    def test_a_handler_satisfies_the_protocol(self) -> None:
        # Structural: existing callers passing a Handler keep type-checking.
        assert _as_transport(DefaultHandler()) is not None


@dataclass
class _In(Input, allow_unbounded_fields=True):
    x: str = ""


@dataclass
class _Out(Output, allow_unbounded_fields=True):
    y: str = ""


class TestTheWorkerWiring:
    def setup_method(self) -> None:
        AppRegistry.reset()
        TaskRegistry.reset()

    def teardown_method(self) -> None:
        AppRegistry.reset()
        TaskRegistry.reset()

    def test_the_worker_builds_every_gate_activity_on_the_in_process_transport(
        self,
    ) -> None:
        class _WarmApp(App):
            preflight_warmup_ceiling_seconds = 120

            async def run(self, input: _In) -> _Out:
                return _Out()

        seen: list[Any] = []

        def _gate(transport: Any, *args: Any, **kwargs: Any) -> Any:
            seen.append(transport)
            return real_gate(transport, *args, **kwargs)

        def _warmups(transport: Any, *args: Any, **kwargs: Any) -> Any:
            seen.append(transport)
            return real_warmups(transport, *args, **kwargs)

        real_gate = preflight_gate.build_preflight_gate_activity
        real_warmups = preflight_gate.build_preflight_warmup_activities
        client = mock.MagicMock()
        client.namespace = "default"
        client.service_client.config.target_host = "localhost:7233"
        with (
            mock.patch.object(preflight_gate, "build_preflight_gate_activity", _gate),
            mock.patch.object(
                preflight_gate, "build_preflight_warmup_activities", _warmups
            ),
            mock.patch("application_sdk.execution._temporal.worker.Worker"),
        ):
            create_worker(client, enable_sdr=False)
        assert len(seen) == 2
        assert all(isinstance(t, InProcessPreflightTransport) for t in seen)
