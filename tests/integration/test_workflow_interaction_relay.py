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
import time
from collections.abc import Callable
from datetime import timedelta
from unittest import mock
from uuid import uuid4

import pytest
from temporalio import workflow
from temporalio.client import WorkflowQueryFailedError, WorkflowUpdateFailedError
from temporalio.service import RPCError
from temporalio.worker import Replayer, UnsandboxedWorkflowRunner, Worker
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner
from temporalio.workflow import NondeterminismError

from application_sdk.app import query, signal, update, wait_condition
from application_sdk.app.base import (
    App,
)
from application_sdk.app.entrypoint import EntryPointMetadata
from application_sdk.app.task import task
from application_sdk.contracts.base import Input, Output
from application_sdk.execution._temporal.app_workflow import (
    _EARLY_RUN_BINDING_PATCH,
    generate_workflow_class,
)
from application_sdk.execution._temporal.worker import build_interceptors
from application_sdk.execution.sandbox import SandboxConfig
from application_sdk.execution.settings import InterceptorSettings

pytestmark = pytest.mark.integration

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


async def test_update_interaction_mutates_run_state(
    temporal_client, task_queue
) -> None:
    """Update fired mid-run reaches the same App instance ``run`` observes."""
    async with Worker(
        temporal_client,
        task_queue=task_queue,
        workflows=[_WF_CLS],
        workflow_runner=_SANDBOX_RUNNER,
    ):
        handle = await temporal_client.start_workflow(
            _WF_CLS.run,
            _InteractionInput(timeout_seconds=5.0),
            id=f"interaction-relay-update-{uuid4().hex[:8]}",
            task_queue=task_queue,
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


async def test_signal_interaction_increments_state(temporal_client, task_queue) -> None:
    """Multiple signals accumulate on the same per-run App instance."""
    async with Worker(
        temporal_client,
        task_queue=task_queue,
        workflows=[_WF_CLS],
        workflow_runner=_SANDBOX_RUNNER,
    ):
        handle = await temporal_client.start_workflow(
            _WF_CLS.run,
            _InteractionInput(timeout_seconds=5.0),
            id=f"interaction-relay-signal-{uuid4().hex[:8]}",
            task_queue=task_queue,
            result_type=_InteractionOutput,
        )
        await asyncio.sleep(0.2)
        await handle.signal("ping")
        await handle.signal("ping")
        await handle.signal("ping")
        await handle.execute_update("stop", _StopInput(reason="done"))

        result = await handle.result()
    assert result.signals_received == 3


_QUERY_DEADLINE_SECONDS = 30.0


async def test_query_interaction_returns_live_state(
    temporal_client, task_queue
) -> None:
    """Query reads state from the same instance ``run`` is mutating."""
    async with Worker(
        temporal_client,
        task_queue=task_queue,
        workflows=[_WF_CLS],
        workflow_runner=_SANDBOX_RUNNER,
    ):
        handle = await temporal_client.start_workflow(
            _WF_CLS.run,
            _InteractionInput(timeout_seconds=5.0),
            id=f"interaction-relay-query-{uuid4().hex[:8]}",
            task_queue=task_queue,
            result_type=_InteractionOutput,
        )
        # Polled with a deadline, not read once after a fixed delay: a query
        # sent before the first workflow task completes can fail, and how long
        # that takes depends on the box.
        state = None
        deadline = time.monotonic() + _QUERY_DEADLINE_SECONDS
        while time.monotonic() < deadline:
            try:
                state = (await handle.query("get_state"))["state"]
            except (RPCError, WorkflowQueryFailedError):
                state = None
            if state == "running":
                break
            await asyncio.sleep(0.1)
        assert state == "running"
        await handle.execute_update("stop", _StopInput(reason="done"))
        await handle.result()


async def test_validator_rejects_invalid_update(temporal_client, task_queue) -> None:
    """Validator decorated on the App method propagates through the relay,
    causing the runtime to reject the update before it reaches the interaction body."""
    async with Worker(
        temporal_client,
        task_queue=task_queue,
        workflows=[_WF_CLS],
        workflow_runner=_SANDBOX_RUNNER,
    ):
        handle = await temporal_client.start_workflow(
            _WF_CLS.run,
            _InteractionInput(timeout_seconds=5.0),
            id=f"interaction-relay-validator-{uuid4().hex[:8]}",
            task_queue=task_queue,
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


# ---------------------------------------------------------------------------
# First-activation delivery (signal-with-start)
# ---------------------------------------------------------------------------


class _ProbeInput(Input):
    pass


class _ProbeTaskOutput(Output):
    in_activity: bool = False
    in_workflow: bool = False


class _ProbeOutput(Output):
    task_in_activity: bool = False
    task_in_workflow: bool = False


class _SignalWithStartTaskApp(App):
    """App whose ``@signal`` awaits a ``@task``. Started with signal-with-start,
    so the handler runs in the run's first activation, before ``run``."""

    def __init__(self) -> None:
        self.probe: _ProbeTaskOutput | None = None

    @task(timeout_seconds=30)
    async def probe_task(self, input: _ProbeInput) -> _ProbeTaskOutput:
        # Where the body executes: as an activity (correct) or inline on the
        # workflow thread (the task was not wrapped yet).
        from temporalio import activity as _activity
        from temporalio import workflow as _workflow

        return _ProbeTaskOutput(
            in_activity=_activity.in_activity(), in_workflow=_workflow.in_workflow()
        )

    async def run(self, input: _ProbeInput) -> _ProbeOutput:
        await wait_condition(
            lambda: self.probe is not None, timeout=timedelta(seconds=30)
        )
        assert self.probe is not None
        return _ProbeOutput(
            task_in_activity=self.probe.in_activity,
            task_in_workflow=self.probe.in_workflow,
        )

    @signal
    async def kick(self) -> None:
        self.probe = await self.probe_task(_ProbeInput())


async def test_signal_with_start_handler_runs_task_as_activity(
    temporal_client, task_queue, run_worker, reregister_app
) -> None:
    """A @signal delivered with the start (first activation) that awaits a
    @task schedules it as an activity, not run inline in workflow code.

    Temporal applies the signal before the run coroutine starts, so the
    @task methods must already be wrapped when the workflow object is built.
    """
    reregister_app(_SignalWithStartTaskApp)
    (workflow_type,) = _SignalWithStartTaskApp._app_metadata.workflow_types
    async with run_worker():
        handle = await temporal_client.start_workflow(
            workflow_type,
            _ProbeInput(),
            id=f"interaction-relay-signal-with-start-{uuid4().hex[:8]}",
            task_queue=task_queue,
            start_signal="kick",
            result_type=_ProbeOutput,
        )
        result = await asyncio.wait_for(handle.result(), timeout=60)
        scheduled = [
            event
            async for event in handle.fetch_history_events()
            if event.HasField("activity_task_scheduled_event_attributes")
        ]
        history = await handle.fetch_history()

    # The run's own history, marker included, replays on the same code
    # through the interceptor chain create_worker registered (it records the
    # run-flags marker, so the replayer needs it too).
    ((_, ep),) = _SignalWithStartTaskApp._app_metadata.workflow_types.items()
    await Replayer(
        workflows=[generate_workflow_class(_SignalWithStartTaskApp, ep, workflow_type)],
        interceptors=build_interceptors(InterceptorSettings()),
        data_converter=temporal_client.data_converter,
        workflow_runner=_SANDBOX_RUNNER,
    ).replay_workflow(history)

    assert result.task_in_activity is True
    assert result.task_in_workflow is False
    assert len(scheduled) == 1


def _patched_without_early_binding(real_patched: Callable[[str], bool]):
    """``workflow.patched`` as code before FND-5219 saw it: the early-binding
    marker does not exist, so it is never recorded and always reads False."""

    def _patched(patch_id: str) -> bool:
        if patch_id == _EARLY_RUN_BINDING_PATCH:
            return False
        return real_patched(patch_id)

    return _patched


def _patched_forcing_early_binding(real_patched: Callable[[str], bool]):
    """``workflow.patched`` with the early-binding guard removed: it always
    reads True, as unguarded early binding would behave on replay."""

    def _patched(patch_id: str) -> bool:
        if patch_id == _EARLY_RUN_BINDING_PATCH:
            return True
        return real_patched(patch_id)

    return _patched


async def test_pre_change_signal_with_start_history_still_replays(
    temporal_client, task_queue, reregister_app
) -> None:
    """A run written before early binding (its first-activation handler ran
    the task inline, recording no activity) replays on the current code.

    The history is produced with the marker suppressed, which is the command
    sequence older SDK versions emitted. Replaying it with the current code
    must succeed; replaying it with the guard removed must not, which shows
    the guard is what keeps it replayable.
    """
    reregister_app(_SignalWithStartTaskApp)
    ((workflow_type, ep),) = (
        _SignalWithStartTaskApp._app_metadata.workflow_types.items()
    )
    wf_cls = generate_workflow_class(_SignalWithStartTaskApp, ep, workflow_type)
    real_patched = workflow.patched

    with mock.patch.object(
        workflow, "patched", _patched_without_early_binding(real_patched)
    ):
        async with Worker(
            temporal_client,
            task_queue=task_queue,
            workflows=[wf_cls],
            workflow_runner=UnsandboxedWorkflowRunner(),
        ):
            handle = await temporal_client.start_workflow(
                workflow_type,
                _ProbeInput(),
                id=f"interaction-relay-pre-change-{uuid4().hex[:8]}",
                task_queue=task_queue,
                start_signal="kick",
                result_type=_ProbeOutput,
            )
            result = await asyncio.wait_for(handle.result(), timeout=60)
    # The pre-change shape: the task body ran inline in workflow code.
    assert result.task_in_workflow is True
    history = await handle.fetch_history()

    await Replayer(
        workflows=[wf_cls],
        data_converter=temporal_client.data_converter,
        workflow_runner=_SANDBOX_RUNNER,
    ).replay_workflow(history)

    with mock.patch.object(
        workflow, "patched", _patched_forcing_early_binding(real_patched)
    ):
        unguarded = await Replayer(
            workflows=[wf_cls],
            data_converter=temporal_client.data_converter,
            workflow_runner=UnsandboxedWorkflowRunner(),
        ).replay_workflow(history, raise_on_replay_failure=False)
    assert isinstance(unguarded.replay_failure, NondeterminismError)
