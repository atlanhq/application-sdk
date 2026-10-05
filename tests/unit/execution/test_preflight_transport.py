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
    build_preflight_warmup_activity,
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
    WarmupInput,
    WarmupObservation,
    WarmupState,
)


class _RecordingTransport:
    """A transport with no handler behind it: records each call, answers fixed."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, PreflightInput | WarmupInput]] = []

    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        self.calls.append(("preflight_check", input))
        return PreflightOutput(
            status=PreflightStatus.READY,
            checks=[PreflightCheck(name="reachable", passed=True)],
        )

    async def warmup(self, input: WarmupInput) -> WarmupObservation:
        self.calls.append(("warmup", input))
        return WarmupObservation(state=WarmupState.READY)


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
        # The first dispatch probes the warmup, then runs the checks.
        assert [op for op, _ in transport.calls] == ["warmup", "preflight_check"]
        assert all(seen.entrypoint == "crawl" for _, seen in transport.calls)

    async def test_the_warmup_activity_calls_the_warmup_operation(self) -> None:
        transport = _RecordingTransport()
        poll = build_preflight_warmup_activity(_as_transport(transport), "myapp")
        polled = await poll(PreflightGateInput(entrypoint="crawl"))
        assert polled.observation.state is WarmupState.READY
        assert [op for op, _ in transport.calls] == ["warmup"]


class _Handler(DefaultHandler):
    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        return PreflightOutput(
            status=PreflightStatus.NOT_READY, message=input.entrypoint
        )

    async def warmup(self, input: WarmupInput) -> WarmupObservation:
        return WarmupObservation(state=WarmupState.WARMING, source_state="probe")


class TestTheInProcessTransport:
    async def test_each_call_is_the_handlers_own_answer(self) -> None:
        transport = InProcessPreflightTransport(_Handler())
        input = PreflightInput(entrypoint="crawl")
        assert (await transport.preflight_check(input)).message == "crawl"
        warmup = await transport.warmup(WarmupInput(entrypoint="crawl"))
        assert warmup.source_state == "probe"

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
        real_warmups = preflight_gate.build_preflight_warmup_activity
        client = mock.MagicMock()
        client.namespace = "default"
        client.service_client.config.target_host = "localhost:7233"
        with (
            mock.patch.object(preflight_gate, "build_preflight_gate_activity", _gate),
            mock.patch.object(
                preflight_gate, "build_preflight_warmup_activity", _warmups
            ),
            mock.patch("application_sdk.execution._temporal.worker.Worker"),
        ):
            create_worker(client, enable_sdr=False)
        assert len(seen) == 2
        assert all(isinstance(t, InProcessPreflightTransport) for t in seen)
