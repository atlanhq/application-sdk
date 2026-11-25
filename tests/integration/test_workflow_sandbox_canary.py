"""Canary: the workflow sandbox must check app workflow code.

``create_worker`` used to pass every app module (and all of
``application_sdk``) through Temporal's sandbox. A passed-through module is
never re-imported, so ``run()`` executed as a plain host function and a
``datetime.now()`` in it completed without complaint. Non-determinism then
surfaced only on replay in production.

The worker here is built by ``create_worker`` with no extra passthrough
modules (the shared ``run_worker`` fixture passes all of ``tests``, which
would hide exactly what this test checks).

Requires a running Temporal dev server (see conftest.py).
"""

from __future__ import annotations

import asyncio
import contextlib
from uuid import uuid4

import pytest
from temporalio.api.enums.v1 import EventType
from temporalio.service import RPCError

from application_sdk.app.context import AppContext
from application_sdk.execution._temporal.worker import create_worker
from application_sdk.execution.retry import NO_RETRY
from tests.integration._sandbox_canary_apps import (
    CanaryInput,
    SandboxCanaryApp,
    SandboxCleanApp,
)

# Long enough for a cold sandbox import of the app module on a slow runner.
_DEADLINE_SECONDS = 60


async def _first_workflow_task_failure(handle) -> str | None:
    """Return the message of the run's first workflow-task failure, if any."""
    async for event in handle.fetch_history_events():
        if event.event_type == EventType.EVENT_TYPE_WORKFLOW_TASK_FAILED:
            return event.workflow_task_failed_event_attributes.failure.message
    return None


@pytest.mark.integration
async def test_nondeterministic_run_fails_its_workflow_task(
    temporal_client, task_queue, executor, reregister_app
):
    """A ``datetime.now()`` in ``run()`` must fail the workflow task.

    Temporal retries a failed workflow task indefinitely, so the run never
    reaches a terminal state; the test reads the failure from history and
    terminates the run.
    """
    reregister_app(SandboxCanaryApp)
    worker = create_worker(temporal_client, task_queue, shutdown_drain_delay_seconds=0)
    async with worker:
        workflow_id = await executor.start(
            SandboxCanaryApp,
            CanaryInput(value=1, workflow_id=f"sandbox-canary-{uuid4().hex[:8]}"),
            context=AppContext(
                app_name=SandboxCanaryApp._app_name, app_version="1.0.0"
            ),
            retry_policy=NO_RETRY,
        )
        handle = temporal_client.get_workflow_handle(workflow_id)
        try:
            message = None
            loop = asyncio.get_running_loop()
            deadline = loop.time() + _DEADLINE_SECONDS
            while message is None and loop.time() < deadline:
                desc = await handle.describe()
                if desc.status is not None and desc.status.name != "RUNNING":
                    pytest.fail(
                        f"Workflow reached {desc.status.name}: the sandbox let "
                        "datetime.now() in run() through. App workflow code is "
                        "not being checked."
                    )
                message = await _first_workflow_task_failure(handle)
                if message is None:
                    await asyncio.sleep(0.5)
        finally:
            # Already closed when the sandbox let the run through.
            with contextlib.suppress(RPCError):
                await handle.terminate("sandbox canary cleanup")

    assert message is not None, "no workflow-task failure recorded before the deadline"
    assert "datetime.datetime.now" in message, message


@pytest.mark.integration
async def test_deterministic_app_completes_under_the_sandbox(
    temporal_client, task_queue, executor, reregister_app
):
    """An app module that is NOT passed through still runs end to end."""
    reregister_app(SandboxCleanApp)
    worker = create_worker(temporal_client, task_queue, shutdown_drain_delay_seconds=0)
    async with worker:
        result = await asyncio.wait_for(
            executor.execute(
                SandboxCleanApp,
                CanaryInput(value=41),
                context=AppContext(
                    app_name=SandboxCleanApp._app_name, app_version="1.0.0"
                ),
                retry_policy=NO_RETRY,
            ),
            timeout=_DEADLINE_SECONDS,
        )
    assert result.result == 42
