"""Integration test: @auto_heartbeater with blocking sync calls in async activities.

Verifies DBBI-310: heartbeats continue even when an async activity blocks the
event loop with synchronous calls (e.g. time.sleep), because the heartbeat
is sent from a background thread rather than an asyncio task.

Requires a running Temporal dev server:
    temporal server start-dev
"""

import os
import time
import warnings
from datetime import timedelta
from typing import Any, Dict

import pytest
from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.common import RetryPolicy
from temporalio.worker import Worker
from temporalio.worker.workflow_sandbox import (
    SandboxedWorkflowRunner,
    SandboxRestrictions,
)

# ---------------------------------------------------------------------------
# Activities — defined at module level with @auto_heartbeater applied
# ---------------------------------------------------------------------------

with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    from application_sdk.execution._temporal.activity_utils import auto_heartbeater

    @activity.defn(name="heartbeated_blocking_activity")
    @auto_heartbeater
    async def heartbeated_blocking_activity(args: Dict[str, Any]) -> str:
        """Async activity with @auto_heartbeater that blocks the event loop."""
        block_seconds = args.get("block_seconds", 6)
        time.sleep(block_seconds)
        return "completed"

    @activity.defn(name="heartbeated_failing_activity")
    @auto_heartbeater
    async def heartbeated_failing_activity(args: Dict[str, Any]) -> str:
        """Async activity with @auto_heartbeater that blocks then fails."""
        block_seconds = args.get("block_seconds", 2)
        time.sleep(block_seconds)
        raise RuntimeError("intentional failure")


# ---------------------------------------------------------------------------
# Workflow
# ---------------------------------------------------------------------------


@workflow.defn
class BlockingHeartbeatWorkflow:
    @workflow.run
    async def run(self, args: Dict[str, Any]) -> str:
        activity_name = args.get("activity_name", "heartbeated_blocking_activity")
        return await workflow.execute_activity(
            activity_name,
            args,
            start_to_close_timeout=timedelta(seconds=30),
            heartbeat_timeout=timedelta(seconds=4),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

# Sandbox passthrough for SDK and test modules
_PASSTHROUGH_MODULES = frozenset(
    {"application_sdk", "tests", "loguru", "pydantic", "opentelemetry"}
)


def _make_worker(client: Client, task_queue: str, activities: list) -> Worker:
    """Create a Worker with sandbox passthrough for application_sdk modules."""
    restrictions = SandboxRestrictions.default.with_passthrough_modules(
        *_PASSTHROUGH_MODULES
    )
    return Worker(
        client,
        task_queue=task_queue,
        workflows=[BlockingHeartbeatWorkflow],
        activities=activities,
        workflow_runner=SandboxedWorkflowRunner(restrictions=restrictions),
    )


@pytest.mark.integration
async def test_auto_heartbeater_survives_blocking_sync_call():
    """Async activity with time.sleep() survives past heartbeat timeout.

    The activity blocks the event loop for 6 seconds. The heartbeat timeout
    is 4 seconds. Without thread-based heartbeating, Temporal would kill the
    activity after 4 seconds of no heartbeats. With the fix, the background
    thread keeps sending heartbeats and the activity completes.
    """
    host = os.environ.get("TEMPORAL_HOST", "localhost:7233")
    client = await Client.connect(host)
    task_queue = "test-auto-heartbeater-blocking"

    async with _make_worker(client, task_queue, [heartbeated_blocking_activity]):
        result = await client.execute_workflow(
            BlockingHeartbeatWorkflow.run,
            {"block_seconds": 6, "activity_name": "heartbeated_blocking_activity"},
            id=f"test-blocking-hb-{int(time.time())}",
            task_queue=task_queue,
        )

    assert result == "completed"


@pytest.mark.integration
async def test_auto_heartbeater_stops_on_activity_failure():
    """Heartbeat thread stops cleanly when a blocking async activity fails.

    The activity blocks for 2 seconds then raises. The workflow should see
    the failure (not a heartbeat timeout), confirming that the heartbeat
    thread didn't mask the error and stopped properly.
    """
    from temporalio.client import WorkflowFailureError

    host = os.environ.get("TEMPORAL_HOST", "localhost:7233")
    client = await Client.connect(host)
    task_queue = "test-auto-heartbeater-failure"

    async with _make_worker(client, task_queue, [heartbeated_failing_activity]):
        with pytest.raises(WorkflowFailureError) as exc_info:
            await client.execute_workflow(
                BlockingHeartbeatWorkflow.run,
                {
                    "block_seconds": 2,
                    "activity_name": "heartbeated_failing_activity",
                },
                id=f"test-failing-hb-{int(time.time())}",
                task_queue=task_queue,
            )

        # The error should be from the activity (RuntimeError), not a heartbeat timeout.
        # Temporal wraps: WorkflowFailureError -> ActivityError -> ApplicationError
        cause = exc_info.value.__cause__  # ActivityError
        assert cause is not None
        root = cause.__cause__  # ApplicationError with the original message
        assert root is not None
        assert "intentional failure" in str(root)
