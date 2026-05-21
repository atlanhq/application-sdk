"""End-to-end test: @workflow.update / @workflow.signal / @workflow.query on App
classes route to per-run App-instance state through a live Temporal worker.

Pre-BLDX-1283, ``generate_workflow_class`` synthesized a wf_cls carrying only
``run``; any handlers declared on the App subclass were silently dropped.
``handle.execute_update(...)`` failed with "unknown update name."

This test stands up an embedded Temporal server (``WorkflowEnvironment``),
runs a worker against an App whose lifecycle spans a long workflow body,
fires updates/signals/queries against the running execution, and asserts
that state mutations are visible to the App's ``run`` method on the same
instance.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from temporalio import workflow
from temporalio.client import WorkflowUpdateFailedError
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner

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


class _HandlerInput(Input, allow_unbounded_fields=True):
    timeout_seconds: float = 5.0


class _HandlerOutput(Output, allow_unbounded_fields=True):
    final_state: str = ""
    signals_received: int = 0


# ---------------------------------------------------------------------------
# App under test
# ---------------------------------------------------------------------------


class _HandlerApp(App):
    """Long-running App with one of each handler type. ``run`` polls state
    until paused, allowing handlers to fire in-flight against the same instance."""

    def __init__(self) -> None:
        self.state: str = "running"
        self.signals_received: int = 0

    async def run(self, input: _HandlerInput) -> _HandlerOutput:
        # Block on a state flip from any handler. ``workflow.wait_condition``
        # is the deterministic primitive — it suspends until ``self.state``
        # changes or the timeout elapses.
        await workflow.wait_condition(
            lambda: self.state != "running",
            timeout=timedelta(seconds=input.timeout_seconds),
        )
        return _HandlerOutput(
            final_state=self.state, signals_received=self.signals_received
        )

    @workflow.signal
    async def ping(self) -> None:
        self.signals_received += 1

    @workflow.query
    def get_state(self) -> str:
        return self.state

    @workflow.update
    async def stop(self, reason: str) -> str:
        self.state = f"stopped:{reason}"
        return self.state

    @stop.validator
    def _validate_stop(self, reason: str) -> None:
        if not reason:
            raise ValueError("reason must be non-empty")


# Build the wf_cls via the SDK path under test (not a hand-rolled @workflow.defn).
_WF_CLS = generate_workflow_class(
    _HandlerApp,
    EntryPointMetadata(
        name="run",
        input_type=_HandlerInput,
        output_type=_HandlerOutput,
        method_name="run",
        implicit=True,
    ),
)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_handler_mutates_run_state() -> None:
    """Update fired mid-run reaches the same App instance ``run`` observes."""
    async with await WorkflowEnvironment.start_local(
        data_converter=pydantic_data_converter
    ) as env:
        async with Worker(
            env.client,
            task_queue="handler-relay-queue",
            workflows=[_WF_CLS],
            workflow_runner=_SANDBOX_RUNNER,
        ):
            handle = await env.client.start_workflow(
                _WF_CLS.run,
                _HandlerInput(timeout_seconds=5.0),
                id="handler-relay-update",
                task_queue="handler-relay-queue",
                result_type=_HandlerOutput,
            )

            # Give run() a moment to enter the poll loop, then issue the update.
            await asyncio.sleep(0.2)
            stopped = await handle.execute_update("stop", "operator-request")
            assert stopped == "stopped:operator-request"

            result = await handle.result()
    assert result.final_state == "stopped:operator-request"


@pytest.mark.asyncio
async def test_signal_handler_increments_state() -> None:
    """Multiple signals accumulate on the same per-run App instance."""
    async with await WorkflowEnvironment.start_local(
        data_converter=pydantic_data_converter
    ) as env:
        async with Worker(
            env.client,
            task_queue="handler-relay-queue-2",
            workflows=[_WF_CLS],
            workflow_runner=_SANDBOX_RUNNER,
        ):
            handle = await env.client.start_workflow(
                _WF_CLS.run,
                _HandlerInput(timeout_seconds=5.0),
                id="handler-relay-signal",
                task_queue="handler-relay-queue-2",
                result_type=_HandlerOutput,
            )
            await asyncio.sleep(0.2)
            await handle.signal("ping")
            await handle.signal("ping")
            await handle.signal("ping")
            await handle.execute_update("stop", "done")

            result = await handle.result()
    assert result.signals_received == 3


@pytest.mark.asyncio
async def test_query_handler_returns_live_state() -> None:
    """Query reads state from the same instance ``run`` is mutating."""
    async with await WorkflowEnvironment.start_local(
        data_converter=pydantic_data_converter
    ) as env:
        async with Worker(
            env.client,
            task_queue="handler-relay-queue-3",
            workflows=[_WF_CLS],
            workflow_runner=_SANDBOX_RUNNER,
        ):
            handle = await env.client.start_workflow(
                _WF_CLS.run,
                _HandlerInput(timeout_seconds=5.0),
                id="handler-relay-query",
                task_queue="handler-relay-queue-3",
                result_type=_HandlerOutput,
            )
            await asyncio.sleep(0.2)
            assert await handle.query("get_state") == "running"
            await handle.execute_update("stop", "done")
            await handle.result()


@pytest.mark.asyncio
async def test_validator_rejects_invalid_update() -> None:
    """Validator decorated on the App method propagates through the relay,
    causing Temporal to reject the update before it reaches the handler body."""
    async with await WorkflowEnvironment.start_local(
        data_converter=pydantic_data_converter
    ) as env:
        async with Worker(
            env.client,
            task_queue="handler-relay-queue-4",
            workflows=[_WF_CLS],
            workflow_runner=_SANDBOX_RUNNER,
        ):
            handle = await env.client.start_workflow(
                _WF_CLS.run,
                _HandlerInput(timeout_seconds=5.0),
                id="handler-relay-validator",
                task_queue="handler-relay-queue-4",
                result_type=_HandlerOutput,
            )
            await asyncio.sleep(0.2)

            with pytest.raises(WorkflowUpdateFailedError) as exc_info:
                await handle.execute_update("stop", "")
            # The validator's ValueError travels back as the cause chain.
            assert "non-empty" in str(exc_info.value.cause)

            # Workflow is still running; a valid update unblocks it.
            await handle.execute_update("stop", "after-rejection")
            result = await handle.result()
    assert result.final_state == "stopped:after-rejection"
