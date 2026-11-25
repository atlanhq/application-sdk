"""E2E test: Prove that get_outputs().add_metric() in activities
automatically appears in the workflow's return dict.

Runs against the session's shared embedded Temporal server (the
``temporal_client`` / ``task_queue`` fixtures in ``conftest.py``) rather than
booting a server per test.
"""

import asyncio
import os
from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest
from temporalio import activity, workflow
from temporalio.worker import (
    ActivityInboundInterceptor,
    ExecuteActivityInput,
    Interceptor,
    Worker,
)

from application_sdk.app.base import App
from application_sdk.app.entrypoint import entrypoint
from application_sdk.app.task import task
from application_sdk.contracts.base import Input, Output
from application_sdk.contracts.types import FileReference
from application_sdk.execution._temporal.interceptors.outputs import OutputInterceptor
from application_sdk.outputs import Artifact, Metric, get_outputs

pytestmark = pytest.mark.integration


@activity.defn
async def fetch_tables(args: dict) -> dict:
    """Simulates table extraction — emits metrics."""
    get_outputs().add_metric(Metric(name="tables-extracted", value=150))
    get_outputs().add_metric(Metric(name="schemas-extracted", value=20))
    return {"status": "done", "path": "/tmp/raw/tables"}


@activity.defn
async def fetch_columns(args: dict) -> dict:
    """Simulates column extraction — emits metrics + artifact."""
    get_outputs().add_metric(Metric(name="columns-extracted", value=3000))
    get_outputs().add_metric(Metric(name="tables-extracted", value=5))
    get_outputs().add_artifact(Artifact(name="debug-log", path="s3://bucket/debug.tgz"))
    return {"status": "done"}


@activity.defn
async def simple_activity(args: dict) -> dict:
    """Simple activity without any outputs."""
    return {"processed": True}


@activity.defn
async def count_batch_1(args: dict) -> dict:
    """Batch counting activity 1."""
    get_outputs().add_metric(Metric(name="rows-processed", value=1000))
    return {}


@activity.defn
async def count_batch_2(args: dict) -> dict:
    """Batch counting activity 2."""
    get_outputs().add_metric(Metric(name="rows-processed", value=2500))
    return {}


@activity.defn
async def count_batch_3(args: dict) -> dict:
    """Batch counting activity 3."""
    get_outputs().add_metric(Metric(name="rows-processed", value=500))
    return {}


class CountOutput(Output):
    """Typed task-style result, as a v3 ``@task`` returns."""

    rows: int


@activity.defn
async def count_rows(args: dict) -> CountOutput:
    """Typed activity that emits a metric and an artifact."""
    get_outputs().add_metric(Metric(name="rows-counted", value=7))
    get_outputs().add_artifact(Artifact(name="row-dump", path="s3://bucket/rows"))
    return CountOutput(rows=7)


@workflow.defn(sandboxed=False)
class PoolQueueWorkflow:
    """Dispatches its activity to another task queue, like a task with a pool."""

    @workflow.run
    async def run(self, pool_queue: str) -> dict:
        result = await workflow.execute_activity(
            count_rows,
            {},
            task_queue=pool_queue,
            result_type=CountOutput,
            start_to_close_timeout=timedelta(seconds=30),
        )
        # Echo what crossed the worker boundary on the activity result.
        return {"rows": result.rows, "carried": result.metrics}


@workflow.defn(sandboxed=False)
class SampleMetricsWorkflow:
    """Sample workflow that executes two activities emitting metrics."""

    @workflow.run
    async def run(self, config: dict) -> dict:
        """Run the workflow with two activities."""
        await workflow.execute_activity(
            fetch_tables,
            config,
            start_to_close_timeout=timedelta(seconds=30),
        )
        await workflow.execute_activity(
            fetch_columns,
            config,
            start_to_close_timeout=timedelta(seconds=30),
        )
        return {
            "connection_qualified_name": "default/test/123",
            "transformed_data_prefix": "artifacts/test/transformed",
        }


@workflow.defn(sandboxed=False)
class SimpleWorkflow:
    """Simple workflow without any outputs."""

    @workflow.run
    async def run(self, config: dict) -> dict:
        """Run a simple workflow."""
        await workflow.execute_activity(
            simple_activity,
            config,
            start_to_close_timeout=timedelta(seconds=30),
        )
        return {"status": "completed", "data": "unchanged"}


@workflow.defn(sandboxed=False)
class BatchCountWorkflow:
    """Workflow that runs multiple batch counting activities."""

    @workflow.run
    async def run(self, config: dict) -> dict:
        """Run multiple batch activities."""
        await workflow.execute_activity(
            count_batch_1,
            config,
            start_to_close_timeout=timedelta(seconds=30),
        )
        await workflow.execute_activity(
            count_batch_2,
            config,
            start_to_close_timeout=timedelta(seconds=30),
        )
        await workflow.execute_activity(
            count_batch_3,
            config,
            start_to_close_timeout=timedelta(seconds=30),
        )
        return {"batch_count": 3}


async def test_metrics_flow_through_interceptor(temporal_client, task_queue):
    """Test that metrics from activities are merged into workflow result."""
    async with Worker(
        temporal_client,
        task_queue=task_queue,
        workflows=[SampleMetricsWorkflow],
        activities=[fetch_tables, fetch_columns],
        interceptors=[OutputInterceptor()],
    ):
        result = await temporal_client.execute_workflow(
            SampleMetricsWorkflow.run,
            {"test": True},
            id=f"test-outputs-workflow-{uuid4().hex[:8]}",
            task_queue=task_queue,
        )

    assert result["connection_qualified_name"] == "default/test/123"
    assert result["transformed_data_prefix"] == "artifacts/test/transformed"

    assert "metrics" in result
    assert result["metrics"]["tables-extracted"] == 155
    assert result["metrics"]["schemas-extracted"] == 20
    assert result["metrics"]["columns-extracted"] == 3000

    assert "artifacts" in result
    assert result["artifacts"]["debug-log"] == "s3://bucket/debug.tgz"


async def test_workflow_without_metrics_returns_unchanged(temporal_client, task_queue):
    """Test that workflows without metrics return their original result."""
    async with Worker(
        temporal_client,
        task_queue=task_queue,
        workflows=[SimpleWorkflow],
        activities=[simple_activity],
        interceptors=[OutputInterceptor()],
    ):
        result = await temporal_client.execute_workflow(
            SimpleWorkflow.run,
            {},
            id=f"test-simple-workflow-{uuid4().hex[:8]}",
            task_queue=task_queue,
        )

    assert result == {"status": "completed", "data": "unchanged"}
    assert "metrics" not in result
    assert "artifacts" not in result


async def test_multiple_activities_same_metric_sums(temporal_client, task_queue):
    """Test that same-named numeric metrics from multiple activities sum correctly."""
    async with Worker(
        temporal_client,
        task_queue=task_queue,
        workflows=[BatchCountWorkflow],
        activities=[count_batch_1, count_batch_2, count_batch_3],
        interceptors=[OutputInterceptor()],
    ):
        result = await temporal_client.execute_workflow(
            BatchCountWorkflow.run,
            {},
            id=f"test-batch-workflow-{uuid4().hex[:8]}",
            task_queue=task_queue,
        )

    assert result["batch_count"] == 3
    assert result["metrics"]["rows-processed"] == 4000


async def test_activity_on_another_worker_carries_outputs_to_workflow(
    temporal_client, task_queue
):
    """An activity run by a different worker than its workflow still reports.

    The outputs must travel on the activity result through Temporal history:
    with more than one replica, or a task on its own pool queue, the activity
    and the workflow do not share process memory.
    """
    pool_queue = f"{task_queue}-pool"
    async with (
        Worker(
            temporal_client,
            task_queue=task_queue,
            workflows=[PoolQueueWorkflow],
            interceptors=[OutputInterceptor()],
        ),
        Worker(
            temporal_client,
            task_queue=pool_queue,
            activities=[count_rows],
            interceptors=[OutputInterceptor()],
        ),
    ):
        result = await temporal_client.execute_workflow(
            PoolQueueWorkflow.run,
            pool_queue,
            id=f"test-pool-queue-workflow-{uuid4().hex[:8]}",
            task_queue=task_queue,
        )

    assert result["rows"] == 7
    assert result["carried"] == {"rows-counted": 7}
    assert result["metrics"] == {"rows-counted": 7}
    assert result["artifacts"] == {"row-dump": "s3://bucket/rows"}


async def test_outputs_survive_workflow_cache_eviction(temporal_client, task_queue):
    """Metrics survive the workflow being evicted and replayed between tasks.

    ``max_cached_workflows=0`` evicts after every workflow task, so each
    activity completion is applied to a fresh replay — the same thing a worker
    restart or a move to another replica does mid-run.
    """
    async with Worker(
        temporal_client,
        task_queue=task_queue,
        workflows=[BatchCountWorkflow],
        activities=[count_batch_1, count_batch_2, count_batch_3],
        interceptors=[OutputInterceptor()],
        max_cached_workflows=0,
    ):
        result = await temporal_client.execute_workflow(
            BatchCountWorkflow.run,
            {},
            id=f"test-evicted-batch-workflow-{uuid4().hex[:8]}",
            task_queue=task_queue,
        )

    assert result["batch_count"] == 3
    assert result["metrics"]["rows-processed"] == 4000


# ---------------------------------------------------------------------------
# FND-5220: refs tracked by a task on one worker are cleaned up by a
# cleanup_storage that runs on another, and every worker releases the run's
# app-state slot once the run has closed.
# ---------------------------------------------------------------------------


class ExtractInput(Input):
    rows: int = 0


class ExtractOutput(Output):
    raw: FileReference | None = None


class RunSummary(Output):
    """The entry point's own output: a key, not a ref, so no schema is due."""

    raw_key: str = ""


class CrossWorkerCleanupApp(App):
    """Extract writes a TRANSIENT file the interceptor persists to the store."""

    name = "cross-worker-cleanup-app"

    @task
    async def extract(self, input: ExtractInput) -> ExtractOutput:
        from application_sdk.execution._temporal.activity_utils import (
            run_temp_dir,
        )

        directory = run_temp_dir()
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, "raw.json")
        with open(path, "w") as f:
            f.write('{"rows": %d}\n' % input.rows)
        return ExtractOutput(raw=FileReference(local_path=path))

    @entrypoint
    async def ingest(self, input: ExtractInput) -> RunSummary:
        extracted = await self.extract(input)
        raw = extracted.raw
        return RunSummary(raw_key=(raw.storage_path or "") if raw else "")


class _ForgetSlotsOfOtherTasks(Interceptor):
    """Makes the extract's worker a different process from the cleanup's.

    Both workers of a test share one process, so after every task but the two
    cleanups this drops the run's ``_app_state`` slot, as a separate worker's
    memory would be invisible to the cleanup's. Records the run it saw.
    """

    def __init__(self) -> None:
        self.runs: set[tuple[str, str]] = set()

    def intercept_activity(
        self, next: ActivityInboundInterceptor
    ) -> ActivityInboundInterceptor:
        outer = self

        class _Inbound(ActivityInboundInterceptor):
            async def execute_activity(self, input: ExecuteActivityInput) -> Any:
                result = await super().execute_activity(input)
                info = activity.info()
                run = (info.workflow_id, info.workflow_run_id)
                outer.runs.add(run)
                if not info.activity_type.endswith(
                    (":cleanup_storage", ":cleanup_files")
                ):
                    from application_sdk.app.base import (
                        _app_state,
                        _app_state_lock,
                    )

                    with _app_state_lock:
                        _app_state.pop(run, None)
                return result

        return _Inbound(next)


async def test_transient_ref_from_another_worker_is_cleaned_and_slots_released(
    temporal_client, task_queue, executor, reregister_app, monkeypatch, tmp_path
):
    from application_sdk.app.base import _runs_with_app_state
    from application_sdk.app.context import AppContext
    from application_sdk.execution._temporal.credential_sweep import sweep_closed_runs
    from application_sdk.execution._temporal.worker import create_worker
    from application_sdk.execution.retry import NO_RETRY
    from application_sdk.infrastructure import (
        InfrastructureContext,
        clear_infrastructure,
        set_infrastructure,
    )
    from application_sdk.storage.factory import create_local_store
    from application_sdk.testing.fixtures import _redirect_temporary_path

    # The integration conftest turns end-of-run cleanup off; this test is about it.
    monkeypatch.setenv("APPLICATION_SDK_ENABLE_CLEANUP_INTERCEPTOR", "true")
    _redirect_temporary_path(monkeypatch, str(tmp_path / "sdk-tmp"))
    store_root = tmp_path / "store"
    set_infrastructure(InfrastructureContext(storage=create_local_store(store_root)))
    reregister_app(CrossWorkerCleanupApp)
    forget = _ForgetSlotsOfOtherTasks()
    try:
        async with create_worker(
            temporal_client,
            task_queue,
            interceptors=[forget],
            shutdown_drain_delay_seconds=0,
        ):
            result = await asyncio.wait_for(
                executor.execute(
                    CrossWorkerCleanupApp,
                    ExtractInput(rows=3),
                    context=AppContext(
                        app_name=CrossWorkerCleanupApp._app_name,
                        app_version="1.0.0",
                    ),
                    retry_policy=NO_RETRY,
                    entry_point="ingest",
                ),
                timeout=60,
            )
            # The run is closed: the sweeper each worker runs releases its slot.
            await sweep_closed_runs(temporal_client)
            remaining = set(_runs_with_app_state()) & forget.runs
    finally:
        clear_infrastructure()

    # Auto-persisted TRANSIENT refs land under the run's own file_refs/.
    assert "/file_refs/" in result.raw_key, result.raw_key
    left = sorted(
        str(p.relative_to(store_root)) for p in store_root.rglob("*") if p.is_file()
    )
    assert not [k for k in left if "/file_refs/" in k], left
    assert forget.runs and not remaining
