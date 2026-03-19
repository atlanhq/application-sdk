"""Unit tests for ExecutionContextInterceptor field-mapping logic.

These tests verify that the interceptors correctly read from
``workflow.info()`` / ``activity.info()`` and populate the
``ExecutionContext`` ContextVar with the right field values.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

import pytest

from application_sdk.execution._temporal.interceptors.execution_context_interceptor import (
    _ExecutionContextActivityInboundInterceptor,
    _ExecutionContextWorkflowInboundInterceptor,
)
from application_sdk.observability.context import (
    ExecutionContext,
    get_execution_context,
    set_execution_context,
)


@pytest.fixture(autouse=True)
def reset_execution_context():
    set_execution_context(ExecutionContext())
    yield
    set_execution_context(ExecutionContext())


def _noop_next_workflow():
    """Minimal WorkflowInboundInterceptor next that returns None."""
    n = mock.AsyncMock()
    n.execute_workflow = mock.AsyncMock(return_value=None)
    return n


def _noop_next_activity():
    """Minimal ActivityInboundInterceptor next that returns None."""
    n = mock.AsyncMock()
    n.execute_activity = mock.AsyncMock(return_value=None)
    return n


# ---------------------------------------------------------------------------
# Workflow interceptor
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_workflow_interceptor_sets_execution_context():
    """Workflow interceptor maps workflow.info() fields to ExecutionContext."""
    mock_info = SimpleNamespace(
        workflow_id="wf-abc",
        run_id="run-123",
        workflow_type="MyWorkflow",
        namespace="default",
        task_queue="my-queue",
        attempt=1,
    )
    interceptor = _ExecutionContextWorkflowInboundInterceptor(
        next=_noop_next_workflow()
    )

    with mock.patch(
        "application_sdk.execution._temporal.interceptors.execution_context_interceptor.workflow.info",
        return_value=mock_info,
    ):
        await interceptor.execute_workflow(mock.MagicMock())

    ctx = get_execution_context()
    assert ctx.execution_type == "workflow"
    assert ctx.workflow_id == "wf-abc"
    assert ctx.workflow_run_id == "run-123"
    assert ctx.workflow_type == "MyWorkflow"
    assert ctx.namespace == "default"
    assert ctx.task_queue == "my-queue"
    assert ctx.attempt == 1


@pytest.mark.asyncio
async def test_workflow_interceptor_handles_none_fields():
    """Workflow interceptor coerces None info fields to empty strings / 0."""
    mock_info = SimpleNamespace(
        workflow_id=None,
        run_id=None,
        workflow_type=None,
        namespace=None,
        task_queue=None,
        attempt=None,
    )
    interceptor = _ExecutionContextWorkflowInboundInterceptor(
        next=_noop_next_workflow()
    )

    with mock.patch(
        "application_sdk.execution._temporal.interceptors.execution_context_interceptor.workflow.info",
        return_value=mock_info,
    ):
        await interceptor.execute_workflow(mock.MagicMock())

    ctx = get_execution_context()
    assert ctx.execution_type == "workflow"
    assert ctx.workflow_id == ""
    assert ctx.workflow_run_id == ""
    assert ctx.workflow_type == ""
    assert ctx.namespace == ""
    assert ctx.task_queue == ""
    assert ctx.attempt == 0


@pytest.mark.asyncio
async def test_workflow_interceptor_calls_next():
    """Workflow interceptor always delegates to next.execute_workflow."""
    mock_info = SimpleNamespace(
        workflow_id="wf-1",
        run_id="run-1",
        workflow_type="T",
        namespace="ns",
        task_queue="q",
        attempt=1,
    )
    next_handler = _noop_next_workflow()
    interceptor = _ExecutionContextWorkflowInboundInterceptor(next=next_handler)
    mock_input = mock.MagicMock()

    with mock.patch(
        "application_sdk.execution._temporal.interceptors.execution_context_interceptor.workflow.info",
        return_value=mock_info,
    ):
        await interceptor.execute_workflow(mock_input)

    next_handler.execute_workflow.assert_awaited_once_with(mock_input)


# ---------------------------------------------------------------------------
# Activity interceptor
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_activity_interceptor_sets_execution_context():
    """Activity interceptor maps activity.info() fields to ExecutionContext."""
    mock_info = SimpleNamespace(
        workflow_id="wf-abc",
        workflow_run_id="run-123",
        activity_id="act-1",
        activity_type="fetch_databases",
        task_queue="my-queue",
        attempt=2,
    )
    interceptor = _ExecutionContextActivityInboundInterceptor(
        next=_noop_next_activity()
    )

    with mock.patch(
        "application_sdk.execution._temporal.interceptors.execution_context_interceptor.activity.info",
        return_value=mock_info,
    ):
        await interceptor.execute_activity(mock.MagicMock())

    ctx = get_execution_context()
    assert ctx.execution_type == "activity"
    assert ctx.workflow_id == "wf-abc"
    assert ctx.workflow_run_id == "run-123"
    assert ctx.activity_id == "act-1"
    assert ctx.activity_type == "fetch_databases"
    assert ctx.task_queue == "my-queue"
    assert ctx.attempt == 2


@pytest.mark.asyncio
async def test_activity_interceptor_handles_none_fields():
    """Activity interceptor coerces None info fields to empty strings / 0."""
    mock_info = SimpleNamespace(
        workflow_id=None,
        workflow_run_id=None,
        activity_id=None,
        activity_type=None,
        task_queue=None,
        attempt=None,
    )
    interceptor = _ExecutionContextActivityInboundInterceptor(
        next=_noop_next_activity()
    )

    with mock.patch(
        "application_sdk.execution._temporal.interceptors.execution_context_interceptor.activity.info",
        return_value=mock_info,
    ):
        await interceptor.execute_activity(mock.MagicMock())

    ctx = get_execution_context()
    assert ctx.execution_type == "activity"
    assert ctx.workflow_id == ""
    assert ctx.workflow_run_id == ""
    assert ctx.activity_id == ""
    assert ctx.activity_type == ""
    assert ctx.task_queue == ""
    assert ctx.attempt == 0


@pytest.mark.asyncio
async def test_activity_interceptor_calls_next():
    """Activity interceptor always delegates to next.execute_activity."""
    mock_info = SimpleNamespace(
        workflow_id="wf-1",
        workflow_run_id="run-1",
        activity_id="a",
        activity_type="T",
        task_queue="q",
        attempt=1,
    )
    next_handler = _noop_next_activity()
    interceptor = _ExecutionContextActivityInboundInterceptor(next=next_handler)
    mock_input = mock.MagicMock()

    with mock.patch(
        "application_sdk.execution._temporal.interceptors.execution_context_interceptor.activity.info",
        return_value=mock_info,
    ):
        await interceptor.execute_activity(mock_input)

    next_handler.execute_activity.assert_awaited_once_with(mock_input)
