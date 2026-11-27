"""P5: Task timeout tests.

Verifies that @task(timeout_seconds=N) causes the activity to fail when
execution exceeds the timeout.

Requires a running Temporal dev server (see conftest.py).
"""

import asyncio

import pytest
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ActivityError, TimeoutType
from temporalio.exceptions import TimeoutError as TemporalTimeoutError

from application_sdk.app.base import App
from application_sdk.app.context import AppContext
from application_sdk.app.task import task
from application_sdk.contracts.base import Input, Output
from application_sdk.execution.retry import NO_RETRY

# ---------------------------------------------------------------------------
# App/Input/Output classes at module level (see test_core_execution.py header).
# ---------------------------------------------------------------------------


class SlowInput(Input):
    pass


class SlowOutput(Output):
    done: bool = False


class TimeoutApp(App):
    @task(
        timeout_seconds=3,
        heartbeat_timeout_seconds=None,
        auto_heartbeat_seconds=None,
        retry_max_attempts=1,
    )
    async def slow_task(self, input: SlowInput) -> SlowOutput:
        await asyncio.sleep(10)
        return SlowOutput(done=True)

    async def run(self, input: SlowInput) -> SlowOutput:
        return await self.slow_task(input)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_task_timeout_fails_workflow(run_worker, executor, reregister_app):
    """P5.1: @task(timeout_seconds=3) fails when the activity sleeps for 10s."""
    reregister_app(TimeoutApp)
    # slow_task outlives its timeout and never heartbeats, so nothing tells it
    # to stop; a zero grace cancels it at worker exit instead of waiting out
    # the rest of its 10s sleep (FND-4305).
    async with run_worker(graceful_shutdown_timeout_seconds=0):
        context = AppContext(app_name=TimeoutApp._app_name, app_version="1.0.0")
        with pytest.raises(WorkflowFailureError) as exc_info:
            await executor.execute(
                TimeoutApp,
                SlowInput(),
                context=context,
                retry_policy=NO_RETRY,
            )

    # The run must fail because the task's start-to-close timeout fired, not
    # for any other reason (a crash in slow_task would also fail the run).
    activity_error = exc_info.value.cause
    assert isinstance(activity_error, ActivityError), repr(activity_error)
    timeout_error = activity_error.cause
    assert isinstance(timeout_error, TemporalTimeoutError), repr(timeout_error)
    assert timeout_error.type == TimeoutType.START_TO_CLOSE
