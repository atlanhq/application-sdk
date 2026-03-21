"""Unit tests for ExecutionContext ContextVar and get_workflow_context() rewrite."""

import dataclasses

import pytest

from application_sdk.observability.context import (
    ExecutionContext,
    get_execution_context,
    set_execution_context,
)
from application_sdk.observability.utils import get_workflow_context

# ---------------------------------------------------------------------------
# ExecutionContext dataclass
# ---------------------------------------------------------------------------


def test_execution_context_defaults():
    """Default ExecutionContext has execution_type='none' and empty fields."""
    ctx = ExecutionContext()
    assert ctx.execution_type == "none"
    assert ctx.workflow_id == ""
    assert ctx.workflow_run_id == ""
    assert ctx.workflow_type == ""
    assert ctx.namespace == ""
    assert ctx.task_queue == ""
    assert ctx.attempt == 0
    assert ctx.activity_id == ""
    assert ctx.activity_type == ""


def test_execution_context_is_frozen():
    """ExecutionContext is immutable (frozen dataclass)."""
    ctx = ExecutionContext(execution_type="workflow", workflow_id="wf-123")
    with pytest.raises((dataclasses.FrozenInstanceError, AttributeError)):
        ctx.workflow_id = "other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# ContextVar get/set
# ---------------------------------------------------------------------------


def test_get_execution_context_returns_default_outside_temporal():
    """get_execution_context() returns the default (none) outside Temporal."""
    # Reset to default by setting it explicitly
    set_execution_context(ExecutionContext())
    ctx = get_execution_context()
    assert ctx.execution_type == "none"


def test_set_and_get_execution_context_workflow():
    """set_execution_context() then get_execution_context() round-trips correctly."""
    workflow_ctx = ExecutionContext(
        execution_type="workflow",
        workflow_id="wf-abc",
        workflow_run_id="run-123",
        workflow_type="MyWorkflow",
        namespace="default",
        task_queue="my-queue",
        attempt=1,
    )
    set_execution_context(workflow_ctx)
    retrieved = get_execution_context()
    assert retrieved.execution_type == "workflow"
    assert retrieved.workflow_id == "wf-abc"
    assert retrieved.workflow_run_id == "run-123"
    assert retrieved.workflow_type == "MyWorkflow"
    assert retrieved.namespace == "default"
    assert retrieved.task_queue == "my-queue"
    assert retrieved.attempt == 1


def test_set_and_get_execution_context_activity():
    """Activity ExecutionContext is retrievable after set."""
    activity_ctx = ExecutionContext(
        execution_type="activity",
        workflow_id="wf-abc",
        workflow_run_id="run-123",
        activity_id="act-1",
        activity_type="fetch_databases",
        task_queue="my-queue",
        attempt=2,
    )
    set_execution_context(activity_ctx)
    retrieved = get_execution_context()
    assert retrieved.execution_type == "activity"
    assert retrieved.activity_id == "act-1"
    assert retrieved.activity_type == "fetch_databases"
    assert retrieved.attempt == 2


# ---------------------------------------------------------------------------
# get_workflow_context() — rewritten to read ContextVar
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_execution_context():
    """Reset the execution context to 'none' before each test."""
    set_execution_context(ExecutionContext())
    yield
    set_execution_context(ExecutionContext())


def test_get_workflow_context_defaults_outside_temporal():
    """Outside Temporal: in_workflow='false', in_activity='false', fields empty."""
    ctx = get_workflow_context()
    assert isinstance(ctx, dict)
    assert ctx["in_workflow"] == "false"
    assert ctx["in_activity"] == "false"
    assert ctx["workflow_id"] == ""
    assert ctx["workflow_run_id"] == ""
    assert ctx["workflow_type"] == ""
    assert ctx["namespace"] == ""
    assert ctx["task_queue"] == ""
    assert ctx["attempt"] == "0"
    assert ctx["activity_id"] == ""
    assert ctx["activity_type"] == ""


def test_get_workflow_context_in_workflow():
    """When execution_type='workflow', in_workflow='true' and fields are populated."""
    set_execution_context(
        ExecutionContext(
            execution_type="workflow",
            workflow_id="wf-abc",
            workflow_run_id="run-123",
            workflow_type="MyWorkflow",
            namespace="default",
            task_queue="my-queue",
            attempt=1,
        )
    )
    ctx = get_workflow_context()
    assert ctx["in_workflow"] == "true"
    assert ctx["in_activity"] == "false"
    assert ctx["workflow_id"] == "wf-abc"
    assert ctx["workflow_run_id"] == "run-123"
    assert ctx["workflow_type"] == "MyWorkflow"
    assert ctx["namespace"] == "default"
    assert ctx["task_queue"] == "my-queue"
    assert ctx["attempt"] == "1"
    assert ctx["activity_id"] == ""
    assert ctx["activity_type"] == ""


def test_get_workflow_context_in_activity():
    """When execution_type='activity', in_activity='true' and fields are populated."""
    set_execution_context(
        ExecutionContext(
            execution_type="activity",
            workflow_id="wf-abc",
            workflow_run_id="run-123",
            activity_id="act-1",
            activity_type="fetch_databases",
            task_queue="my-queue",
            attempt=2,
        )
    )
    ctx = get_workflow_context()
    assert ctx["in_workflow"] == "false"
    assert ctx["in_activity"] == "true"
    assert ctx["workflow_id"] == "wf-abc"
    assert ctx["workflow_run_id"] == "run-123"
    assert ctx["activity_id"] == "act-1"
    assert ctx["activity_type"] == "fetch_databases"
    assert ctx["task_queue"] == "my-queue"
    assert ctx["attempt"] == "2"


def test_get_workflow_context_merges_correlation_context():
    """atlan- prefixed correlation context fields are merged into the workflow context dict."""
    from application_sdk.observability.context import correlation_context

    token = correlation_context.set(
        {
            "atlan-workflow-name": "test-workflow",
            "atlan-workflow-node": "node-1",
            "non-atlan-key": "ignored",
        }
    )
    try:
        set_execution_context(
            ExecutionContext(
                execution_type="workflow",
                workflow_id="wf-abc",
            )
        )
        ctx = get_workflow_context()
        assert ctx.get("atlan-workflow-name") == "test-workflow"
        assert ctx.get("atlan-workflow-node") == "node-1"
        assert "non-atlan-key" not in ctx
    finally:
        correlation_context.reset(token)
