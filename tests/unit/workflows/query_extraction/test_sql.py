from typing import Any, Dict, List
from unittest.mock import AsyncMock, Mock

import pytest

from application_sdk.activities.query_extraction.sql import SQLQueryExtractionActivities
from application_sdk.workflows.query_extraction.sql import SQLQueryExtractionWorkflow


@pytest.fixture()
def workflow() -> SQLQueryExtractionWorkflow:  # type: ignore[return-type]
    """Fixture that returns a fresh workflow instance for each test."""
    return SQLQueryExtractionWorkflow()


# ---------------------------------------------------------------------------
# Basic object validation
# ---------------------------------------------------------------------------


def test_workflow_initialization(workflow: SQLQueryExtractionWorkflow):
    """Validate default attributes are correctly set on initialization."""
    assert workflow.application_name == "default"
    assert workflow.activities_cls == SQLQueryExtractionActivities


def test_get_activities():
    """Ensure get_activities returns the expected ordered activity sequence."""
    activities = Mock(spec=SQLQueryExtractionActivities)

    activity_sequence = SQLQueryExtractionWorkflow.get_activities(activities)  # type: ignore[arg-type]

    assert activity_sequence == [
        activities.get_query_batches,
        activities.fetch_queries,
        activities.preflight_check,
        activities.get_workflow_args,
    ]
    # Defensive check – the workflow should expose exactly four activities
    assert len(activity_sequence) == 4


# ---------------------------------------------------------------------------
# run() method behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_success(monkeypatch: pytest.MonkeyPatch):
    """Simulate a happy-path execution of the workflow run method."""

    wf = SQLQueryExtractionWorkflow()
    workflow_config: Dict[str, Any] = {"workflow_id": "wf-123"}

    fake_workflow_args: Dict[str, Any] = {
        "output_prefix": "s3://bucket/prefix",
        "miner_args": {"chunk_size": 1000},
    }

    query_batches: List[Dict[str, Any]] = [
        {"sql": "SELECT 1", "start": "0", "end": "100"},
        {"sql": "SELECT 2", "start": "100", "end": "200"},
    ]

    # Patch workflow.info() so that a deterministic run_id is returned
    monkeypatch.setattr(
        "temporalio.workflow.info", lambda: Mock(run_id="run-123"), raising=False
    )

    # Patch execute_activity_method to return data based on which activity is invoked
    async def exec_activity_method_side_effect(activity_fn, *args, **kwargs):  # type: ignore[unused-argument]
        if activity_fn == wf.activities_cls.get_workflow_args:
            return fake_workflow_args
        if activity_fn == wf.activities_cls.preflight_check:
            return None
        if activity_fn == wf.activities_cls.get_query_batches:
            return query_batches
        # Default fall-through
        return None

    mock_exec_activity_method = AsyncMock(side_effect=exec_activity_method_side_effect)
    monkeypatch.setattr(
        "temporalio.workflow.execute_activity_method", mock_exec_activity_method
    )

    # Patch execute_activity so the created coroutine resolves immediately
    mock_exec_activity = AsyncMock(return_value=None)
    monkeypatch.setattr("temporalio.workflow.execute_activity", mock_exec_activity)

    # Run the workflow
    await wf.run(workflow_config)

    # ---------------------------------------------------------------------
    # Assertions – verify that the expected Temporal primitives were invoked
    # ---------------------------------------------------------------------
    # Two calls to get_workflow_args (once in super().run + once again),
    # one to preflight_check and one to get_query_batches ==> 4 calls total
    assert mock_exec_activity_method.call_count == 4
    # Number of fetch_queries invocations must match the number of batches
    assert mock_exec_activity.call_count == len(query_batches)

    # Validate the arguments passed to fetch_queries contain the computed output_path
    expected_output_path = (
        f"{fake_workflow_args['output_prefix']}/"
        f"{workflow_config['workflow_id']}/run-123"
    )

    for call, expected_batch in zip(mock_exec_activity.call_args_list, query_batches):
        # First positional arg should be the activity function reference
        assert call.args[0] == wf.activities_cls.fetch_queries

        # The workflow packs the activity parameters inside the keyword arg "args"
        activity_kwargs = call.kwargs["args"][0]
        assert activity_kwargs["sql_query"] == expected_batch["sql"]
        assert activity_kwargs["start_marker"] == expected_batch["start"]
        assert activity_kwargs["end_marker"] == expected_batch["end"]
        assert activity_kwargs["output_path"] == expected_output_path


@pytest.mark.asyncio
async def test_run_error_handling(monkeypatch: pytest.MonkeyPatch):
    """Ensure exceptions raised during query batching propagate out of run()."""

    wf = SQLQueryExtractionWorkflow()
    workflow_config: Dict[str, Any] = {"workflow_id": "wf-error"}

    fake_workflow_args: Dict[str, Any] = {
        "output_prefix": "s3://bucket/prefix",
        "miner_args": {"chunk_size": 1000},
    }

    # Patching
    monkeypatch.setattr(
        "temporalio.workflow.info", lambda: Mock(run_id="run-error"), raising=False
    )

    async def exec_activity_method_side_effect(activity_fn, *args, **kwargs):  # type: ignore[unused-argument]
        if activity_fn == wf.activities_cls.get_workflow_args:
            return fake_workflow_args
        if activity_fn == wf.activities_cls.preflight_check:
            return None
        if activity_fn == wf.activities_cls.get_query_batches:
            raise RuntimeError("Failed to get batches")
        return None

    mock_exec_activity_method = AsyncMock(side_effect=exec_activity_method_side_effect)
    monkeypatch.setattr(
        "temporalio.workflow.execute_activity_method", mock_exec_activity_method
    )

    monkeypatch.setattr(
        "temporalio.workflow.execute_activity", AsyncMock(return_value=None)
    )

    with pytest.raises(RuntimeError, match="Failed to get batches"):
        await wf.run(workflow_config)
