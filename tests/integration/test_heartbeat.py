"""P3: Heartbeat tests.

Verifies that auto_heartbeat keeps long-running tasks alive past the heartbeat
timeout, and that manual heartbeating also works.

Requires a running Temporal dev server (see conftest.py).
"""

import asyncio

import pytest

from application_sdk.app.base import App
from application_sdk.app.context import AppContext
from application_sdk.app.task import task
from application_sdk.contracts.base import Input, Output
from application_sdk.execution.retry import NO_RETRY

# ---------------------------------------------------------------------------
# App/Input/Output classes at module level (see test_core_execution.py header).
#
# The worker throttles heartbeats to min(0.8 x timeout, 10s), so however often
# the task beats, the server sees one at most every throttle interval. A 5s
# timeout left 1s between that interval and the timeout for a loaded runner to
# miss; 15s leaves 5s. Each task still runs past its timeout, which is the
# subject.
# ---------------------------------------------------------------------------

_HEARTBEAT_TIMEOUT_SECONDS = 15
_RUNS_FOR_SECONDS = 18


class HbInput(Input):
    pass


class HbOutput(Output):
    done: bool = False


class AutoHeartbeatApp(App):
    @task(
        auto_heartbeat_seconds=2, heartbeat_timeout_seconds=_HEARTBEAT_TIMEOUT_SECONDS
    )
    async def long_task(self, input: HbInput) -> HbOutput:
        # Outlives heartbeat_timeout_seconds, but auto_heartbeat fires every 2s
        # keeping the task alive.
        await asyncio.sleep(_RUNS_FOR_SECONDS)
        return HbOutput(done=True)

    async def run(self, input: HbInput) -> HbOutput:
        return await self.long_task(input)


class ManualHbInput(Input):
    pass


class ManualHbOutput(Output):
    done: bool = False


class ManualHeartbeatApp(App):
    @task(
        auto_heartbeat_seconds=None,
        heartbeat_timeout_seconds=_HEARTBEAT_TIMEOUT_SECONDS,
    )
    async def heartbeat_task(self, input: ManualHbInput) -> ManualHbOutput:
        # Manually heartbeat every 2s, for longer than the heartbeat timeout.
        for _ in range(_RUNS_FOR_SECONDS // 2):
            await asyncio.sleep(2)
            self.heartbeat()
        return ManualHbOutput(done=True)

    async def run(self, input: ManualHbInput) -> ManualHbOutput:
        return await self.heartbeat_task(input)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_auto_heartbeat_keeps_task_alive(run_worker, executor, reregister_app):
    """P3.1: auto_heartbeat_seconds=2 keeps an 18s task alive past a 15s timeout."""
    reregister_app(AutoHeartbeatApp)
    async with run_worker():
        context = AppContext(app_name=AutoHeartbeatApp._app_name, app_version="1.0.0")
        result = await executor.execute(
            AutoHeartbeatApp,
            HbInput(),
            context=context,
            retry_policy=NO_RETRY,
        )
    assert result.done is True


@pytest.mark.integration
async def test_manual_heartbeat_extends_timeout(run_worker, executor, reregister_app):
    """P3.2: Manual self.heartbeat() calls extend the heartbeat timeout."""
    reregister_app(ManualHeartbeatApp)
    async with run_worker():
        context = AppContext(app_name=ManualHeartbeatApp._app_name, app_version="1.0.0")
        result = await executor.execute(
            ManualHeartbeatApp,
            ManualHbInput(),
            context=context,
            retry_policy=NO_RETRY,
        )
    assert result.done is True
