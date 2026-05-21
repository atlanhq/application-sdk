"""Runnable verification of the workflow handler cookbook
(`docs/guides/workflow-handlers.md`).

Each test mirrors one cookbook example — the App class is as close to the
prose example as practical, with the I/O bits replaced by deterministic
stand-ins so the test runs under the Temporal workflow sandbox.

If any of these break, the cookbook is wrong and must be updated in lockstep.
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

_SANDBOX_RUNNER = SandboxedWorkflowRunner(
    restrictions=SandboxConfig().to_temporal_restrictions()
)


def _ep(input_type: type, output_type: type) -> EntryPointMetadata:
    return EntryPointMetadata(
        name="run",
        input_type=input_type,
        output_type=output_type,
        method_name="run",
        implicit=True,
    )


# ---------------------------------------------------------------------------
# Cookbook 1 — Pause / Resume / Graceful Cancel
# ---------------------------------------------------------------------------


class _ExtractInput(Input, allow_unbounded_fields=True):
    total_batches: int = 5


class _ExtractOutput(Output, allow_unbounded_fields=True):
    rows_extracted: int = 0
    final_state: str = ""


class _PauseResumeApp(App):
    def __init__(self) -> None:
        self.state: str = "running"
        self.rows_extracted: int = 0

    async def run(self, input: _ExtractInput) -> _ExtractOutput:
        for _ in range(input.total_batches):
            await workflow.wait_condition(lambda: self.state != "paused")
            if self.state == "cancelled":
                break
            self.rows_extracted += 100
            # Yield so updates fired mid-run can interleave between batches.
            # Real apps do I/O here; the test stand-in needs an explicit await.
            await workflow.sleep(timedelta(milliseconds=100))
        return _ExtractOutput(
            rows_extracted=self.rows_extracted, final_state=self.state
        )

    @workflow.update
    async def pause(self, reason: str) -> str:
        self.state = "paused"
        return f"paused: {reason}"

    @workflow.update
    async def resume(self) -> str:
        self.state = "running"
        return "resumed"

    @workflow.update
    async def graceful_cancel(self, reason: str) -> str:
        self.state = "cancelled"
        return f"cancelling: {reason}"


_PAUSE_RESUME_WF = generate_workflow_class(
    _PauseResumeApp, _ep(_ExtractInput, _ExtractOutput)
)


@pytest.mark.asyncio
async def test_cookbook_pause_resume_cycle() -> None:
    """Customer pauses mid-extract, then resumes; run completes with full progress."""
    async with await WorkflowEnvironment.start_local(
        data_converter=pydantic_data_converter
    ) as env:
        async with Worker(
            env.client,
            task_queue="cookbook-pause-resume",
            workflows=[_PAUSE_RESUME_WF],
            workflow_runner=_SANDBOX_RUNNER,
        ):
            handle = await env.client.start_workflow(
                _PAUSE_RESUME_WF.run,
                _ExtractInput(total_batches=3),
                id="cookbook-pause-resume",
                task_queue="cookbook-pause-resume",
                result_type=_ExtractOutput,
            )
            await asyncio.sleep(0.2)
            assert (
                await handle.execute_update("pause", "maintenance")
                == "paused: maintenance"
            )
            assert await handle.execute_update("resume") == "resumed"
            result = await handle.result()
    assert result.final_state == "running"
    assert result.rows_extracted == 300  # 3 batches × 100 rows


@pytest.mark.asyncio
async def test_cookbook_graceful_cancel_short_circuits_run() -> None:
    """graceful_cancel flips state; run() exits its loop without processing
    remaining batches, but with partial progress preserved."""
    async with await WorkflowEnvironment.start_local(
        data_converter=pydantic_data_converter
    ) as env:
        async with Worker(
            env.client,
            task_queue="cookbook-graceful-cancel",
            workflows=[_PAUSE_RESUME_WF],
            workflow_runner=_SANDBOX_RUNNER,
        ):
            handle = await env.client.start_workflow(
                _PAUSE_RESUME_WF.run,
                _ExtractInput(total_batches=100),
                id="cookbook-graceful-cancel",
                task_queue="cookbook-graceful-cancel",
                result_type=_ExtractOutput,
            )
            await asyncio.sleep(0.2)
            msg = await handle.execute_update("graceful_cancel", "user-request")
            assert msg == "cancelling: user-request"
            result = await handle.result()
    assert result.final_state == "cancelled"
    assert result.rows_extracted < 100 * 100  # short-circuited before all batches


# ---------------------------------------------------------------------------
# Cookbook 2 — Live progress / ETA query
# ---------------------------------------------------------------------------


class _MetricsApp(App):
    def __init__(self) -> None:
        self.tables_done: int = 0
        self.tables_total: int = 5
        self.unblock: bool = False

    async def run(self, input: _ExtractInput) -> _ExtractOutput:
        self.tables_total = input.total_batches
        # Block until the client has had a chance to issue a query.
        await workflow.wait_condition(lambda: self.unblock)
        for _ in range(self.tables_total):
            self.tables_done += 1
        return _ExtractOutput(rows_extracted=self.tables_done)

    @workflow.query
    def progress(self) -> dict:
        return {"tables_done": self.tables_done, "tables_total": self.tables_total}

    @workflow.signal
    async def go(self) -> None:
        self.unblock = True


_METRICS_WF = generate_workflow_class(_MetricsApp, _ep(_ExtractInput, _ExtractOutput))


@pytest.mark.asyncio
async def test_cookbook_progress_query_returns_live_state() -> None:
    """Operator queries get_progress mid-run; query reads live App state."""
    async with await WorkflowEnvironment.start_local(
        data_converter=pydantic_data_converter
    ) as env:
        async with Worker(
            env.client,
            task_queue="cookbook-progress",
            workflows=[_METRICS_WF],
            workflow_runner=_SANDBOX_RUNNER,
        ):
            handle = await env.client.start_workflow(
                _METRICS_WF.run,
                _ExtractInput(total_batches=7),
                id="cookbook-progress",
                task_queue="cookbook-progress",
                result_type=_ExtractOutput,
            )
            await asyncio.sleep(0.2)
            mid_run = await handle.query("progress")
            assert mid_run == {"tables_done": 0, "tables_total": 7}
            await handle.signal("go")
            await handle.result()


# ---------------------------------------------------------------------------
# Cookbook 3 — In-flight rate-limit + validator
# ---------------------------------------------------------------------------


class _RateLimitedApp(App):
    def __init__(self) -> None:
        self.rps_limit: int = 100
        self.unblock: bool = False

    async def run(self, input: _ExtractInput) -> _ExtractOutput:
        await workflow.wait_condition(lambda: self.unblock)
        return _ExtractOutput(rows_extracted=self.rps_limit)

    @workflow.update
    async def set_rate_limit(self, rps: int) -> int:
        self.rps_limit = rps
        return self.rps_limit

    @set_rate_limit.validator
    def _validate_rps(self, rps: int) -> None:
        if rps < 1 or rps > 10_000:
            raise ValueError("rps must be in [1, 10_000]")

    @workflow.signal
    async def go(self) -> None:
        self.unblock = True


_RATE_LIMITED_WF = generate_workflow_class(
    _RateLimitedApp, _ep(_ExtractInput, _ExtractOutput)
)


@pytest.mark.asyncio
async def test_cookbook_rate_limit_validator_rejects_out_of_range() -> None:
    """Out-of-range rate limit is rejected by the validator before the
    handler body runs; in-range update succeeds and mutates state."""
    async with await WorkflowEnvironment.start_local(
        data_converter=pydantic_data_converter
    ) as env:
        async with Worker(
            env.client,
            task_queue="cookbook-rate-limit",
            workflows=[_RATE_LIMITED_WF],
            workflow_runner=_SANDBOX_RUNNER,
        ):
            handle = await env.client.start_workflow(
                _RATE_LIMITED_WF.run,
                _ExtractInput(),
                id="cookbook-rate-limit",
                task_queue="cookbook-rate-limit",
                result_type=_ExtractOutput,
            )
            await asyncio.sleep(0.2)

            with pytest.raises(WorkflowUpdateFailedError) as exc_info:
                await handle.execute_update("set_rate_limit", 99_999)
            assert "rps must be in" in str(exc_info.value.cause)

            new_limit = await handle.execute_update("set_rate_limit", 250)
            assert new_limit == 250

            await handle.signal("go")
            result = await handle.result()
    assert result.rows_extracted == 250  # state was actually mutated


# ---------------------------------------------------------------------------
# Cookbook 4 — External signal advances a watermark
# ---------------------------------------------------------------------------


class _IncrementalApp(App):
    def __init__(self) -> None:
        self.watermark: str = ""
        self.advance_signal: bool = False
        self.stop: bool = False

    async def run(self, input: _ExtractInput) -> _ExtractOutput:
        # One advance cycle for the test; production code would loop.
        await workflow.wait_condition(lambda: self.advance_signal)
        return _ExtractOutput(rows_extracted=0, final_state=self.watermark)

    @workflow.signal
    async def new_data_available(self, new_watermark: str) -> None:
        self.watermark = new_watermark
        self.advance_signal = True


_INCREMENTAL_WF = generate_workflow_class(
    _IncrementalApp, _ep(_ExtractInput, _ExtractOutput)
)


@pytest.mark.asyncio
async def test_cookbook_external_signal_advances_watermark() -> None:
    """An external service signals a new watermark; run() observes it on the same instance."""
    async with await WorkflowEnvironment.start_local(
        data_converter=pydantic_data_converter
    ) as env:
        async with Worker(
            env.client,
            task_queue="cookbook-watermark",
            workflows=[_INCREMENTAL_WF],
            workflow_runner=_SANDBOX_RUNNER,
        ):
            handle = await env.client.start_workflow(
                _INCREMENTAL_WF.run,
                _ExtractInput(),
                id="cookbook-watermark",
                task_queue="cookbook-watermark",
                result_type=_ExtractOutput,
            )
            await asyncio.sleep(0.2)
            await handle.signal("new_data_available", "2026-05-21T08:00:00Z")
            result = await handle.result()
    assert result.final_state == "2026-05-21T08:00:00Z"


# ---------------------------------------------------------------------------
# Cookbook 5 — Operator probe via query for incident response
# ---------------------------------------------------------------------------


class _CheckpointedApp(App):
    def __init__(self) -> None:
        self.last_checkpoint: dict = {"partition": None, "offset": 0}
        self.unblock: bool = False

    async def run(self, input: _ExtractInput) -> _ExtractOutput:
        # Simulate work that updates the checkpoint as it goes.
        self.last_checkpoint = {"partition": "shard-7", "offset": 1842394}
        await workflow.wait_condition(lambda: self.unblock)
        return _ExtractOutput(rows_extracted=0)

    @workflow.query
    def get_checkpoint(self) -> dict:
        return self.last_checkpoint

    @workflow.signal
    async def finish(self) -> None:
        self.unblock = True


_CHECKPOINTED_WF = generate_workflow_class(
    _CheckpointedApp, _ep(_ExtractInput, _ExtractOutput)
)


@pytest.mark.asyncio
async def test_cookbook_checkpoint_query_returns_position() -> None:
    """Oncall queries the live checkpoint without restarting the run."""
    async with await WorkflowEnvironment.start_local(
        data_converter=pydantic_data_converter
    ) as env:
        async with Worker(
            env.client,
            task_queue="cookbook-checkpoint",
            workflows=[_CHECKPOINTED_WF],
            workflow_runner=_SANDBOX_RUNNER,
        ):
            handle = await env.client.start_workflow(
                _CHECKPOINTED_WF.run,
                _ExtractInput(),
                id="cookbook-checkpoint",
                task_queue="cookbook-checkpoint",
                result_type=_ExtractOutput,
            )
            await asyncio.sleep(0.2)
            checkpoint = await handle.query("get_checkpoint")
            assert checkpoint == {"partition": "shard-7", "offset": 1842394}
            await handle.signal("finish")
            await handle.result()
