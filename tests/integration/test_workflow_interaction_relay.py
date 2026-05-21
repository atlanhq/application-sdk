"""End-to-end test: @update / @signal / @query runtime interactions on App
classes route to per-run App-instance state through a live worker.

Pre-BLDX-1283, ``generate_workflow_class`` synthesized a wf_cls carrying only
``run``; any interactions declared on the App subclass were silently dropped.
``handle.execute_update(...)`` failed with "unknown update name."

This test stands up an embedded server (``WorkflowEnvironment``),
runs a worker against an App whose lifecycle spans a long run body,
fires updates/signals/queries against the running execution, and asserts
that state mutations are visible to the App's ``run`` method on the same
instance.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from temporalio.client import WorkflowUpdateFailedError
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner

from application_sdk.app import query, signal, update, wait_condition
from application_sdk.app.base import App, generate_workflow_class
from application_sdk.app.entrypoint import EntryPointMetadata
from application_sdk.contracts.base import Input, Output
from application_sdk.execution.sandbox import SandboxConfig

# Production sandbox passthrough config — same one the SDK worker uses, so
# loguru / application_sdk imports work inside the workflow sandbox.
_SANDBOX_RUNNER = SandboxedWorkflowRunner(
    restrictions=SandboxConfig().to_temporal_restrictions()
)


# ---------------------------------------------------------------------------
# Test contracts
# ---------------------------------------------------------------------------


class _InteractionInput(Input, allow_unbounded_fields=True):
    timeout_seconds: float = 5.0


class _InteractionOutput(Output, allow_unbounded_fields=True):
    final_state: str = ""
    signals_received: int = 0


class _StopInput(Input):
    reason: str = ""


class _StopOutput(Output):
    state: str = ""


class _StateOutput(Output):
    state: str = ""


# ---------------------------------------------------------------------------
# App under test
# ---------------------------------------------------------------------------


class _InteractionApp(App):
    """Long-running App with one of each interaction type. ``run`` polls state
    until paused, allowing interactions to fire in-flight against the same instance."""

    def __init__(self) -> None:
        self.state: str = "running"
        self.signals_received: int = 0

    async def run(self, input: _InteractionInput) -> _InteractionOutput:
        # Block on a state flip from any interaction. ``workflow.wait_condition``
        # is the deterministic primitive — it suspends until ``self.state``
        # changes or the timeout elapses.
        await wait_condition(
            lambda: self.state != "running",
            timeout=timedelta(seconds=input.timeout_seconds),
        )
        return _InteractionOutput(
            final_state=self.state, signals_received=self.signals_received
        )

    @signal
    async def ping(self) -> None:
        self.signals_received += 1

    @query
    def get_state(self) -> _StateOutput:
        return _StateOutput(state=self.state)

    @update
    async def stop(self, input: _StopInput) -> _StopOutput:
        self.state = f"stopped:{input.reason}"
        return _StopOutput(state=self.state)

    @stop.validator
    def _validate_stop(self, input: _StopInput) -> None:
        if not input.reason:
            raise ValueError("reason must be non-empty")


# Build the wf_cls via the SDK path under test.
_WF_CLS = generate_workflow_class(
    _InteractionApp,
    EntryPointMetadata(
        name="run",
        input_type=_InteractionInput,
        output_type=_InteractionOutput,
        method_name="run",
        implicit=True,
    ),
)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_update_interaction_mutates_run_state() -> None:
    """Update fired mid-run reaches the same App instance ``run`` observes."""
    async with (
        await WorkflowEnvironment.start_local(
            data_converter=pydantic_data_converter
        ) as env,
        Worker(
            env.client,
            task_queue="interaction-relay-queue",
            workflows=[_WF_CLS],
            workflow_runner=_SANDBOX_RUNNER,
        ),
    ):
        handle = await env.client.start_workflow(
            _WF_CLS.run,
            _InteractionInput(timeout_seconds=5.0),
            id="interaction-relay-update",
            task_queue="interaction-relay-queue",
            result_type=_InteractionOutput,
        )

        # Give run() a moment to enter the poll loop, then issue the update.
        await asyncio.sleep(0.2)
        stopped = await handle.execute_update(
            "stop", _StopInput(reason="operator-request")
        )
        assert stopped["state"] == "stopped:operator-request"

        result = await handle.result()
    assert result.final_state == "stopped:operator-request"


async def test_signal_interaction_increments_state() -> None:
    """Multiple signals accumulate on the same per-run App instance."""
    async with (
        await WorkflowEnvironment.start_local(
            data_converter=pydantic_data_converter
        ) as env,
        Worker(
            env.client,
            task_queue="interaction-relay-queue-2",
            workflows=[_WF_CLS],
            workflow_runner=_SANDBOX_RUNNER,
        ),
    ):
        handle = await env.client.start_workflow(
            _WF_CLS.run,
            _InteractionInput(timeout_seconds=5.0),
            id="interaction-relay-signal",
            task_queue="interaction-relay-queue-2",
            result_type=_InteractionOutput,
        )
        await asyncio.sleep(0.2)
        await handle.signal("ping")
        await handle.signal("ping")
        await handle.signal("ping")
        await handle.execute_update("stop", _StopInput(reason="done"))

        result = await handle.result()
    assert result.signals_received == 3


async def test_query_interaction_returns_live_state() -> None:
    """Query reads state from the same instance ``run`` is mutating."""
    async with (
        await WorkflowEnvironment.start_local(
            data_converter=pydantic_data_converter
        ) as env,
        Worker(
            env.client,
            task_queue="interaction-relay-queue-3",
            workflows=[_WF_CLS],
            workflow_runner=_SANDBOX_RUNNER,
        ),
    ):
        handle = await env.client.start_workflow(
            _WF_CLS.run,
            _InteractionInput(timeout_seconds=5.0),
            id="interaction-relay-query",
            task_queue="interaction-relay-queue-3",
            result_type=_InteractionOutput,
        )
        await asyncio.sleep(0.2)
        assert (await handle.query("get_state"))["state"] == "running"
        await handle.execute_update("stop", _StopInput(reason="done"))
        await handle.result()


async def test_validator_rejects_invalid_update() -> None:
    """Validator decorated on the App method propagates through the relay,
    causing the runtime to reject the update before it reaches the interaction body."""
    async with (
        await WorkflowEnvironment.start_local(
            data_converter=pydantic_data_converter
        ) as env,
        Worker(
            env.client,
            task_queue="interaction-relay-queue-4",
            workflows=[_WF_CLS],
            workflow_runner=_SANDBOX_RUNNER,
        ),
    ):
        handle = await env.client.start_workflow(
            _WF_CLS.run,
            _InteractionInput(timeout_seconds=5.0),
            id="interaction-relay-validator",
            task_queue="interaction-relay-queue-4",
            result_type=_InteractionOutput,
        )
        await asyncio.sleep(0.2)

        with pytest.raises(WorkflowUpdateFailedError) as exc_info:
            await handle.execute_update("stop", _StopInput(reason=""))
        # The validator's ValueError travels back as the cause chain.
        assert "non-empty" in str(exc_info.value.cause)

        # Workflow is still running; a valid update unblocks it.
        await handle.execute_update("stop", _StopInput(reason="after-rejection"))
        result = await handle.result()
    assert result.final_state == "stopped:after-rejection"
