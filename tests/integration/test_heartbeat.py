"""P3: Heartbeat tests.

Verifies that auto_heartbeat keeps long-running tasks alive past the heartbeat
timeout, that manual heartbeating also works, and that a task which stops
heartbeating fails on its heartbeat timeout rather than its start-to-close.

Requires a running Temporal dev server (see conftest.py).
"""

import asyncio

import pytest
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import TimeoutError as TemporalTimeoutError
from temporalio.exceptions import TimeoutType

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


# A task that never heartbeats, with a heartbeat timeout far shorter than its
# start-to-close. Only a heartbeat timeout that reaches the activity dispatch
# can end it before the start-to-close (FND-5480). The start-to-close stays
# short enough that a regression fails the test in a minute, not hangs it.
_SILENT_HEARTBEAT_TIMEOUT_SECONDS = 3
_SILENT_START_TO_CLOSE_SECONDS = 60


class SilentInput(Input):
    pass


class SilentOutput(Output):
    done: bool = False


class SilentTaskApp(App):
    @task(
        timeout_seconds=_SILENT_START_TO_CLOSE_SECONDS,
        heartbeat_timeout_seconds=_SILENT_HEARTBEAT_TIMEOUT_SECONDS,
        auto_heartbeat_seconds=None,
        retry_max_attempts=1,
    )
    async def silent_task(self, input: SilentInput) -> SilentOutput:
        await asyncio.sleep(_SILENT_START_TO_CLOSE_SECONDS)
        return SilentOutput(done=True)

    async def run(self, input: SilentInput) -> SilentOutput:
        return await self.silent_task(input)


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


@pytest.mark.integration
async def test_missed_heartbeat_fails_task_on_heartbeat_timeout(
    run_worker, executor, reregister_app
):
    """P3.3: a task that stops heartbeating fails on its 3s heartbeat timeout.

    Its start-to-close is 60s, so a HEARTBEAT timeout in the failure chain
    proves @task(heartbeat_timeout_seconds=...) reached the activity dispatch.
    """
    reregister_app(SilentTaskApp)
    # silent_task never heartbeats, so the timeout's cancellation never reaches
    # it; a zero grace cancels it at worker exit instead of waiting out its sleep.
    async with run_worker(graceful_shutdown_timeout_seconds=0):
        context = AppContext(app_name=SilentTaskApp._app_name, app_version="1.0.0")
        with pytest.raises(WorkflowFailureError) as exc_info:
            await executor.execute(
                SilentTaskApp,
                SilentInput(),
                context=context,
                retry_policy=NO_RETRY,
            )

    timeout_types: list[TimeoutType | None] = []
    cause: BaseException | None = exc_info.value
    while cause is not None:
        if isinstance(cause, TemporalTimeoutError):
            timeout_types.append(cause.type)
        cause = getattr(cause, "cause", None) or cause.__cause__
    assert TimeoutType.HEARTBEAT in timeout_types, (
        f"expected a heartbeat timeout in the failure chain, got {timeout_types!r}"
    )
