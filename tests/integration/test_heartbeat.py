"""P3: Heartbeat tests.

Verifies that auto_heartbeat keeps long-running tasks alive past the heartbeat
timeout, and that manual heartbeating also works.

Requires a running Temporal dev server (see conftest.py).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from application_sdk.app.base import App
from application_sdk.app.context import AppContext
from application_sdk.app.task import task
from application_sdk.contracts.base import Input, Output
from application_sdk.execution.retry import NO_RETRY


@pytest.mark.integration
async def test_auto_heartbeat_keeps_task_alive(run_worker, executor):
    """P3.1: auto_heartbeat_seconds=2 keeps an 8s task alive past 5s timeout."""

    @dataclass
    class HbInput(Input):
        pass

    @dataclass
    class HbOutput(Output):
        done: bool = False

    class AutoHeartbeatApp(App):
        @task(auto_heartbeat_seconds=2, heartbeat_timeout_seconds=5)
        async def long_task(self, input: HbInput) -> HbOutput:
            # Sleep 8s — exceeds heartbeat_timeout_seconds (5s) but
            # auto_heartbeat fires every 2s keeping the task alive.
            await asyncio.sleep(8)
            return HbOutput(done=True)

        async def run(self, input: HbInput) -> HbOutput:
            return await self.long_task(input)

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
async def test_manual_heartbeat_extends_timeout(run_worker, executor):
    """P3.2: Manual self.heartbeat() calls extend the heartbeat timeout."""

    @dataclass
    class ManualHbInput(Input):
        pass

    @dataclass
    class ManualHbOutput(Output):
        done: bool = False

    class ManualHeartbeatApp(App):
        @task(auto_heartbeat_seconds=None, heartbeat_timeout_seconds=5)
        async def heartbeat_task(self, input: ManualHbInput) -> ManualHbOutput:
            # Manually heartbeat every 2s for a 6s total duration.
            for _ in range(3):
                await asyncio.sleep(2)
                self.heartbeat()
            return ManualHbOutput(done=True)

        async def run(self, input: ManualHbInput) -> ManualHbOutput:
            return await self.heartbeat_task(input)

    async with run_worker():
        context = AppContext(app_name=ManualHeartbeatApp._app_name, app_version="1.0.0")
        result = await executor.execute(
            ManualHeartbeatApp,
            ManualHbInput(),
            context=context,
            retry_policy=NO_RETRY,
        )
    assert result.done is True
