"""Output interceptor for structured workflow outputs (v3).

This module provides a Temporal interceptor that automatically collects
metrics and artifacts from activities and merges them into the workflow's
return value. This enables the Automation Engine CUE UI to display metrics
like "Total Assets Extracted: 232,008".

v3 adaptation:
    In v3 App.run() returns a Pydantic Output model, not a dict.  The workflow
    interceptor detects this and merges collected outputs into the model via
    model_dump() / model_validate() so the return type stays correct for
    Temporal's pydantic_data_converter.

Architecture:
    - OutputActivityInboundInterceptor: Creates fresh collector per activity,
      stashes non-empty collectors for later merge.
    - OutputWorkflowInboundInterceptor: At workflow exit, merges all activity
      collectors into the return value.
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

        try:
            result = await super().execute_activity(input)
        finally:
            # Always reset so a failed/cancelled activity doesn't leak its
            # collector into the next task that runs on this thread.
            _current_outputs.set(None)

        try:
            if collector.has_data():
                workflow_run_id = activity.info().workflow_run_id
                with _lock:
                    _collected_outputs[workflow_run_id].append(collector)
                logger.debug(
                    "Stashed output collector for workflow run %s",
                    workflow_run_id,
                )
        except Exception:
            # Output collection is non-critical — never let it fail the activity.
            logger.warning(
                "Failed to stash output collector; outputs for this activity will be lost.",
                exc_info=True,
            )

        return result


class OutputWorkflowInboundInterceptor(WorkflowInboundInterceptor):
    """Interceptor for merging outputs at workflow completion.

    After the workflow's run() method completes, merges all collected
    activity outputs into the return value under "metrics" and "artifacts"
    namespaces.

    v3 adaptation: when the result is a Pydantic BaseModel (e.g. an Output
    subclass), the merge round-trips through model_dump() / model_validate()
    so Temporal's pydantic_data_converter receives the correct typed model back.
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
        workflow_run_id = workflow.info().run_id
        # Populated in finally so cleanup always happens; the list is reused
        # on the success path below.
        activity_collectors: list = []

        try:
            result = await super().execute_workflow(input)
        finally:
            # Always pop the per-run bucket (even on failure/cancellation) to
            # prevent unbounded memory growth.
            activity_collectors = _collected_outputs.pop(workflow_run_id, [])
            _current_outputs.set(None)

        # Only reached when super().execute_workflow() returned without raising.
        try:
            for ac in activity_collectors:
                collector.merge(ac)

            if collector.has_data():
                result = _merge_outputs_into_result(result, collector)
        except Exception:
            # Output enrichment is non-critical — never let it fail the workflow.
            logger.warning(
                "Failed to enrich workflow result with collected outputs; "
                "workflow result returned unchanged.",
                exc_info=True,
            )

        return result


def _merge_outputs_into_result(result: Any, collector: OutputCollector) -> Any:
    """Merge collected outputs into a workflow result.

    Handles three cases:

    - dict result: merge directly (v2 / legacy path).
    - Pydantic BaseModel result: round-trip via model_dump/model_validate.
      Output base class declares metrics/artifacts fields so they are
      accepted without any runtime class manipulation.
    - Other: return just the collector dict (fallback).
    """
    from pydantic import BaseModel

    output_data = collector.to_dict()

    if isinstance(result, dict):
        return collector.merge_with(result)

    if isinstance(result, BaseModel):
        model_cls = type(result)
        merged = {**result.model_dump(), **output_data}
        try:
            return model_cls.model_validate(merged)
        except Exception:
            logger.warning(
                "Could not merge outputs into %s model; returning outputs as separate dict",
                model_cls.__name__,
                exc_info=True,
            )
            return output_data

    return output_data


class OutputInterceptor(Interceptor):
    """Temporal interceptor for automatic output collection.

    This interceptor provides structured output collection for both
    workflow and activity executions, enabling metrics and artifacts
    to flow from activities to the workflow's return value.
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
