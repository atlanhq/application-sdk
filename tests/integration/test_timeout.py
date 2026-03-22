"""P5: Task timeout tests.

Verifies that @task(timeout_seconds=N) causes the activity to fail when
execution exceeds the timeout.

Requires a running Temporal dev server (see conftest.py).
"""

import asyncio
from dataclasses import dataclass

import pytest

from application_sdk.app.base import App
from application_sdk.app.context import AppContext
from application_sdk.app.task import task
from application_sdk.contracts.base import Input, Output
from application_sdk.execution.retry import NO_RETRY

# ---------------------------------------------------------------------------
# App/Input/Output classes at module level (see test_core_execution.py header).
# ---------------------------------------------------------------------------


@dataclass
class SlowInput(Input):
    pass


@dataclass
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
    async with run_worker():
        context = AppContext(app_name=TimeoutApp._app_name, app_version="1.0.0")
        with pytest.raises(Exception):
            await executor.execute(
                TimeoutApp,
                SlowInput(),
                context=context,
                retry_policy=NO_RETRY,
            )
