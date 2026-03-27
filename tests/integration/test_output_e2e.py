"""E2E test: Prove that get_outputs().add_metric() in activities
automatically appears in the workflow's return dict.

Uses WorkflowEnvironment.start_local() — spins up a local Temporal server,
no external dependencies needed.
"""

from datetime import timedelta

import pytest
from temporalio import activity, workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from application_sdk.interceptors.outputs import OutputInterceptor
from application_sdk.workflows.outputs import Artifact, Metric, get_outputs


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


@pytest.mark.asyncio
async def test_metrics_flow_through_interceptor():
    """Test that metrics from activities are merged into workflow result."""
    async with await WorkflowEnvironment.start_local() as env:
        async with Worker(
            env.client,
            task_queue="test-outputs-queue",
            workflows=[SampleMetricsWorkflow],
            activities=[fetch_tables, fetch_columns],
            interceptors=[OutputInterceptor()],
        ):
            result = await env.client.execute_workflow(
                SampleMetricsWorkflow.run,
                {"test": True},
                id="test-outputs-workflow",
                task_queue="test-outputs-queue",
            )

    assert result["connection_qualified_name"] == "default/test/123"
    assert result["transformed_data_prefix"] == "artifacts/test/transformed"

    assert "metrics" in result
    assert result["metrics"]["tables-extracted"] == 155
    assert result["metrics"]["schemas-extracted"] == 20
    assert result["metrics"]["columns-extracted"] == 3000

    assert "artifacts" in result
    assert result["artifacts"]["debug-log"] == "s3://bucket/debug.tgz"


@pytest.mark.asyncio
async def test_workflow_without_metrics_returns_unchanged():
    """Test that workflows without metrics return their original result."""
    async with await WorkflowEnvironment.start_local() as env:
        async with Worker(
            env.client,
            task_queue="test-simple-queue",
            workflows=[SimpleWorkflow],
            activities=[simple_activity],
            interceptors=[OutputInterceptor()],
        ):
            result = await env.client.execute_workflow(
                SimpleWorkflow.run,
                {},
                id="test-simple-workflow",
                task_queue="test-simple-queue",
            )

    assert result == {"status": "completed", "data": "unchanged"}
    assert "metrics" not in result
    assert "artifacts" not in result


@pytest.mark.asyncio
async def test_multiple_activities_same_metric_sums():
    """Test that same-named numeric metrics from multiple activities sum correctly."""
    async with await WorkflowEnvironment.start_local() as env:
        async with Worker(
            env.client,
            task_queue="test-batch-queue",
            workflows=[BatchCountWorkflow],
            activities=[count_batch_1, count_batch_2, count_batch_3],
            interceptors=[OutputInterceptor()],
        ):
            result = await env.client.execute_workflow(
                BatchCountWorkflow.run,
                {},
                id="test-batch-workflow",
                task_queue="test-batch-queue",
            )

    assert result["batch_count"] == 3
    assert result["metrics"]["rows-processed"] == 4000
