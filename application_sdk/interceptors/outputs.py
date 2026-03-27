"""Output interceptor for structured workflow outputs.

This module provides a Temporal interceptor that automatically collects
metrics and artifacts from activities and merges them into the workflow's
return dict. This enables the Automation Engine CUE UI to display metrics
like "Total Assets Extracted: 232,008".

Architecture:
    - OutputActivityInboundInterceptor: Creates fresh collector per activity,
      stashes non-empty collectors for later merge.
    - OutputWorkflowInboundInterceptor: At workflow exit, merges all activity
      collectors into the return dict.
    - OutputInterceptor: Top-level interceptor registering both components.

Usage:
    Activities call get_outputs().add_metric(...) during execution.
    The interceptor handles the rest automatically.
"""

from typing import Any, Optional, Type

from temporalio import activity, workflow
from temporalio.worker import (
    ActivityInboundInterceptor,
    ExecuteActivityInput,
    ExecuteWorkflowInput,
    Interceptor,
    WorkflowInboundInterceptor,
    WorkflowInterceptorClassInput,
)

from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.workflows.outputs import (
    _collected_outputs,
    _current_outputs,
    _lock,
)
from application_sdk.workflows.outputs.collector import OutputCollector

logger = get_logger(__name__)


class OutputActivityInboundInterceptor(ActivityInboundInterceptor):
    """Interceptor for collecting activity outputs.

    Creates a fresh OutputCollector for each activity execution and stashes
    non-empty collectors in process-level storage keyed by workflow_run_id.
    """

    async def execute_activity(self, input: ExecuteActivityInput) -> Any:
        """Execute an activity with output collection.

        Args:
            input: The activity execution input.

        Returns:
            The original activity return value, unchanged.
        """
        collector = OutputCollector()
        _current_outputs.set(collector)

        result = await super().execute_activity(input)

        if collector.has_data():
            workflow_run_id = activity.info().workflow_run_id
            with _lock:
                _collected_outputs[workflow_run_id].append(collector)
            logger.debug(f"Stashed output collector for workflow run {workflow_run_id}")

        return result


class OutputWorkflowInboundInterceptor(WorkflowInboundInterceptor):
    """Interceptor for merging outputs at workflow completion.

    After the workflow's run() method completes, merges all collected
    activity outputs into the return dict under "metrics" and "artifacts"
    namespaces.
    """

    async def execute_workflow(self, input: ExecuteWorkflowInput) -> Any:
        """Execute a workflow with output merging.

        Args:
            input: The workflow execution input.

        Returns:
            The workflow result, enriched with collected outputs if any.
        """
        collector = OutputCollector()
        _current_outputs.set(collector)

        result = await super().execute_workflow(input)

        workflow_run_id = workflow.info().run_id
        with _lock:
            activity_collectors = _collected_outputs.pop(workflow_run_id, [])

        for activity_collector in activity_collectors:
            collector.merge(activity_collector)

        if collector.has_data():
            if isinstance(result, dict):
                return collector.merge_with(result)
            else:
                return collector.to_dict()

        return result


class OutputInterceptor(Interceptor):
    """Temporal interceptor for automatic output collection.

    This interceptor provides structured output collection for both
    workflow and activity executions, enabling metrics and artifacts
    to flow from activities to the workflow's return dict.

    Example:
        >>> # Register the interceptor with Temporal worker
        >>> worker = Worker(
        ...     client,
        ...     task_queue="my-task-queue",
        ...     workflows=[MyWorkflow],
        ...     activities=[my_activity],
        ...     interceptors=[OutputInterceptor()]
        ... )
    """

    def intercept_activity(
        self, next: ActivityInboundInterceptor
    ) -> ActivityInboundInterceptor:
        """Intercept activity executions for output collection.

        Args:
            next: The next interceptor in the chain.

        Returns:
            The output activity interceptor wrapping the chain.
        """
        return OutputActivityInboundInterceptor(super().intercept_activity(next))

    def workflow_interceptor_class(
        self, input: WorkflowInterceptorClassInput
    ) -> Optional[Type[WorkflowInboundInterceptor]]:
        """Get the workflow interceptor class for output merging.

        Args:
            input: The interceptor input.

        Returns:
            The workflow interceptor class.
        """
        return OutputWorkflowInboundInterceptor
