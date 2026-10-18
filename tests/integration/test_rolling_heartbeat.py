"""The SDK heartbeat reaches Temporal when called from a worker thread.

``RollingFileWriter`` runs a plain-function ``on_chunk_complete`` in the SDK's
blocking pool (like ``flush_fn``) so a slow callback cannot stall the event
loop. The documented callback heartbeats, and Temporal's ``activity.heartbeat``
schedules its send with ``asyncio.create_task`` — it only works on the loop's
thread. The SDK heartbeat (``TemporalHeartbeatController``, behind
``self.heartbeat`` / ``task_context.heartbeat``) must therefore hand itself back
to the loop when called off it.

Each test runs inside a real worker against a local Temporal server and reads
the recorded heartbeat back on the retry attempt, which is the only place a
heartbeat is observable from inside the activity.
"""

from __future__ import annotations

import tempfile
import uuid
from datetime import timedelta

import pytest
from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

with workflow.unsafe.imports_passed_through():
    from application_sdk._runtime.offload import run_in_thread
    from application_sdk.execution.heartbeat import TemporalHeartbeatController
    from application_sdk.storage.rolling import RollingFileWriter

pytestmark = pytest.mark.integration


def _flush_lines(batches: list[list[str]], path: str) -> None:
    with open(path, "w") as fh:
        for batch in batches:
            fh.writelines(f"{line}\n" for line in batch)


def _recorded_details() -> list[str] | None:
    """On a retry, the details Temporal recorded for the failed attempt."""
    if activity.info().attempt > 1:
        return [str(d) for d in activity.info().heartbeat_details]
    return None


@activity.defn
async def rolling_writer_sync_callback() -> list[str]:
    """Attempt 1 heartbeats per chunk from a sync callback, then fails."""
    if (details := _recorded_details()) is not None:
        return details

    controller = TemporalHeartbeatController()
    with tempfile.TemporaryDirectory() as base:
        async with RollingFileWriter[list[str]](
            base,
            ".txt",
            _flush_lines,
            max_buffer_records=1,
            on_chunk_complete=lambda idx, _path: controller.heartbeat(f"chunk {idx}"),
        ) as writer:
            await writer.append(["a"])
        assert writer.chunk_count == 1
    raise RuntimeError("fail attempt 1 so attempt 2 can read the heartbeat")


@activity.defn
async def heartbeat_from_run_in_thread() -> list[str]:
    """Attempt 1 heartbeats from inside ``run_in_thread``, then fails."""
    if (details := _recorded_details()) is not None:
        return details

    controller = TemporalHeartbeatController()
    await run_in_thread(controller.heartbeat, "from a thread")
    raise RuntimeError("fail attempt 1 so attempt 2 can read the heartbeat")


@workflow.defn
class HeartbeatProbeWorkflow:
    @workflow.run
    async def run(self, activity_name: str) -> list[str]:
        return await workflow.execute_activity(
            activity_name,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(
                maximum_attempts=2, initial_interval=timedelta(milliseconds=100)
            ),
            result_type=list[str],
        )


@pytest.mark.parametrize(
    ("activity_fn", "expected"),
    [
        pytest.param(rolling_writer_sync_callback, ["chunk 0"], id="rolling-writer"),
        pytest.param(
            heartbeat_from_run_in_thread, ["from a thread"], id="run-in-thread"
        ),
    ],
)
async def test_sdk_heartbeat_from_a_thread_reaches_temporal(
    activity_fn: object, expected: list[str]
) -> None:
    async with await WorkflowEnvironment.start_local() as env:
        task_queue = f"heartbeat-probe-{uuid.uuid4().hex[:8]}"
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[HeartbeatProbeWorkflow],
            activities=[rolling_writer_sync_callback, heartbeat_from_run_in_thread],
        ):
            details = await env.client.execute_workflow(
                HeartbeatProbeWorkflow.run,
                activity_fn.__name__,  # type: ignore[attr-defined]
                id=f"heartbeat-probe-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )
    assert details == expected
