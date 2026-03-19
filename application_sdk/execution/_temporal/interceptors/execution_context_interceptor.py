"""Temporal interceptor that sets the ExecutionContext ContextVar.

Registered unconditionally on every worker so that observability code
(logging, metrics) can read Temporal context via a plain ContextVar read
instead of calling ``temporalio.workflow.info()`` / ``temporalio.activity.info()``
inside try/except blocks on every log line.

Registration order: this interceptor must come FIRST so the ContextVar is
populated before any other interceptor (e.g. EventInterceptor) or user code runs.
"""

from __future__ import annotations

from typing import Any

from temporalio import activity, workflow
from temporalio.worker import (
    ActivityInboundInterceptor,
    ExecuteActivityInput,
    ExecuteWorkflowInput,
    Interceptor,
    WorkflowInboundInterceptor,
    WorkflowInterceptorClassInput,
)

from application_sdk.observability.context import (
    ExecutionContext,
    set_execution_context,
)


class _ExecutionContextWorkflowInboundInterceptor(WorkflowInboundInterceptor):
    """Sets the ExecutionContext ContextVar at the start of each workflow execution."""

    async def execute_workflow(self, input: ExecuteWorkflowInput) -> Any:
        info = workflow.info()
        set_execution_context(
            ExecutionContext(
                execution_type="workflow",
                workflow_id=info.workflow_id or "",
                workflow_run_id=info.run_id or "",
                workflow_type=info.workflow_type or "",
                namespace=info.namespace or "",
                task_queue=info.task_queue or "",
                attempt=info.attempt or 0,
            )
        )
        return await self.next.execute_workflow(input)


class _ExecutionContextActivityInboundInterceptor(ActivityInboundInterceptor):
    """Sets the ExecutionContext ContextVar at the start of each activity execution."""

    async def execute_activity(self, input: ExecuteActivityInput) -> Any:
        info = activity.info()
        set_execution_context(
            ExecutionContext(
                execution_type="activity",
                workflow_id=info.workflow_id or "",
                workflow_run_id=info.workflow_run_id or "",
                activity_id=info.activity_id or "",
                activity_type=info.activity_type or "",
                task_queue=info.task_queue or "",
                attempt=info.attempt or 0,
            )
        )
        return await self.next.execute_activity(input)


class ExecutionContextInterceptor(Interceptor):
    """Temporal interceptor that populates the ``ExecutionContext`` ContextVar.

    Must be registered before all other interceptors so that the ContextVar
    is available to downstream interceptors and all user code.
    """

    def workflow_interceptor_class(
        self,
        input: WorkflowInterceptorClassInput,  # noqa: ARG002
    ) -> type[WorkflowInboundInterceptor] | None:
        return _ExecutionContextWorkflowInboundInterceptor

    def intercept_activity(
        self, next: ActivityInboundInterceptor
    ) -> ActivityInboundInterceptor:
        return _ExecutionContextActivityInboundInterceptor(next)
