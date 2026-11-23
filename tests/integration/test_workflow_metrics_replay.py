"""Workflow-level metrics stay correct when a run spans several instance lifetimes.

A workflow run is not tied to one in-memory workflow instance. A cache eviction
tears the instance down between tasks, and a worker restart hands the run to
another worker, which rebuilds it by replaying history. ``MetricsInterceptor``
must record exactly one ``OK`` execution per successful run in every case, with
a duration measured on the run's history clock:

* default cache: one worker keeps the instance for the whole run (control);
* ``max_cached_workflows=0``: the instance is evicted after every task;
* worker change: worker 1 shuts down mid-run, worker 2 rebuilds and finishes it.

Before the fix the second scenario recorded one ``ERROR`` with a ~0 s duration
and a classified failure (eviction teardown cancels the coroutine), and the
third recorded nothing (the rehydrated worker entered ``execute_workflow``
while replaying and skipped instrumentation for the rest of the run).

Requires the embedded Temporal dev server (see conftest.py).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from application_sdk.execution._temporal.interceptors import metrics as metrics_mod
from application_sdk.execution._temporal.interceptors.metrics import (
    MetricsInterceptor,
)

# The workflow sleeps this long on its own clock, so every run's recorded
# duration has a known floor.
_TIMER_S = 1.0


@activity.defn(name="fnd5217_step")
async def _step(n: int) -> int:
    return n + 1


@workflow.defn(name="Fnd5217MetricsReplayWorkflow", sandboxed=False)
class _TwoStepWorkflow:
    def __init__(self) -> None:
        self._go = False

    @workflow.signal
    def go(self) -> None:
        self._go = True

    @workflow.run
    async def run(self, wait_for_signal: bool) -> int:
        n = await workflow.execute_activity(
            _step, 0, start_to_close_timeout=timedelta(seconds=30)
        )
        await workflow.sleep(_TIMER_S)
        if wait_for_signal:
            await workflow.wait_condition(lambda: self._go)
        return await workflow.execute_activity(
            _step, n, start_to_close_timeout=timedelta(seconds=30)
        )


@pytest.fixture
def metric_reader(monkeypatch: pytest.MonkeyPatch) -> Iterator[InMemoryMetricReader]:
    """Route the interceptor's instruments to a private in-memory reader."""
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    monkeypatch.setattr(
        metrics_mod._otel_metrics,
        "get_meter",
        lambda name, *a, **kw: provider.get_meter(name),
    )
    metrics_mod._INSTRUMENTS.clear()
    yield reader
    metrics_mod._INSTRUMENTS.clear()
    provider.shutdown()


def _points(reader: InMemoryMetricReader, name: str) -> list[Any]:
    data = reader.get_metrics_data()
    if data is None:
        return []
    return [
        point
        for rm in data.resource_metrics
        for sm in rm.scope_metrics
        for metric in sm.metrics
        if metric.name == name
        for point in metric.data.data_points
    ]


def _worker(client: Client, task_queue: str, **kwargs: Any) -> Worker:
    return Worker(
        client,
        task_queue=task_queue,
        workflows=[_TwoStepWorkflow],
        activities=[_step],
        interceptors=[MetricsInterceptor()],
        workflow_runner=UnsandboxedWorkflowRunner(),
        **kwargs,
    )


def _assert_one_ok_run(reader: InMemoryMetricReader, wall_s: float) -> None:
    executions = _points(reader, "temporal.workflow.executions")
    by_status = {p.attributes["otel.status_code"]: p.value for p in executions}
    assert by_status == {"OK": 1}, by_status

    assert _points(reader, "temporal.workflow.failures.classified") == []

    durations = _points(reader, "temporal.workflow.duration")
    assert len(durations) == 1
    (duration,) = durations
    assert duration.count == 1
    # Floor: the run slept _TIMER_S on its own clock. Ceiling: the client saw
    # the whole run, start RPC to result, so the run cannot have taken longer.
    assert _TIMER_S <= duration.sum <= wall_s, (duration.sum, wall_s)


async def _run_to_completion(
    client: Client, task_queue: str, worker_kwargs: dict[str, Any]
) -> float:
    started = time.monotonic()
    async with _worker(client, task_queue, **worker_kwargs):
        result = await client.execute_workflow(
            _TwoStepWorkflow.run,
            False,
            id=f"fnd5217-{uuid4().hex[:8]}",
            task_queue=task_queue,
        )
    assert result == 2
    return time.monotonic() - started


async def test_default_cache_records_one_ok(
    temporal_client: Client, task_queue: str, metric_reader: InMemoryMetricReader
) -> None:
    wall_s = await _run_to_completion(temporal_client, task_queue, {})
    _assert_one_ok_run(metric_reader, wall_s)


async def test_evicted_after_every_task_records_one_ok(
    temporal_client: Client, task_queue: str, metric_reader: InMemoryMetricReader
) -> None:
    wall_s = await _run_to_completion(
        temporal_client, task_queue, {"max_cached_workflows": 0}
    )
    _assert_one_ok_run(metric_reader, wall_s)


async def test_run_finished_by_another_worker_records_one_ok(
    temporal_client: Client, task_queue: str, metric_reader: InMemoryMetricReader
) -> None:
    started = time.monotonic()
    async with _worker(temporal_client, task_queue):
        handle = await temporal_client.start_workflow(
            _TwoStepWorkflow.run,
            True,
            id=f"fnd5217-{uuid4().hex[:8]}",
            task_queue=task_queue,
        )
        # Worker 1 runs the first activity and the timer, then parks the run
        # on the signal wait. Shut it down there, mid-run.
        async with asyncio.timeout(30):
            while True:
                history = [e async for e in handle.fetch_history_events()]
                timer_fired = any(
                    e.HasField("timer_fired_event_attributes") for e in history
                )
                if timer_fired and history[-1].HasField(
                    "workflow_task_completed_event_attributes"
                ):
                    break
                await asyncio.sleep(0.1)

    # Nothing completed on worker 1: the run is still open.
    assert _points(metric_reader, "temporal.workflow.executions") == []

    async with _worker(temporal_client, task_queue):
        await handle.signal(_TwoStepWorkflow.go)
        assert await handle.result() == 2
    wall_s = time.monotonic() - started

    _assert_one_ok_run(metric_reader, wall_s)
