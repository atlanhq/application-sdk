"""Correlation context interceptor for Temporal workflows.

Propagates atlan-* correlation context fields from workflow arguments to activities
via Temporal headers, ensuring all activity logs include correlation identifiers
like atlan-ignore, atlan-argo-workflow-id, and atlan-argo-workflow-node.
"""

from dataclasses import replace
from typing import Any, Dict, Optional, Type

from temporalio import workflow
from temporalio.api.common.v1 import Payload
from temporalio.converter import default as default_converter
from temporalio.worker import (
    ActivityInboundInterceptor,
    ExecuteActivityInput,
    ExecuteWorkflowInput,
    Interceptor,
    StartActivityInput,
    WorkflowInboundInterceptor,
    WorkflowInterceptorClassInput,
    WorkflowOutboundInterceptor,
)

from application_sdk.observability.context import correlation_context
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)

ATLAN_HEADER_PREFIX = "atlan-"


class CorrelationContextOutboundInterceptor(WorkflowOutboundInterceptor):
    """Outbound interceptor that injects atlan-* context into activity headers."""

    def __init__(
        self,
        next: WorkflowOutboundInterceptor,
        inbound: "CorrelationContextWorkflowInboundInterceptor",
    ):
        """Initialize the outbound interceptor.

        Args:
            next: The next interceptor in the chain.
            inbound: Reference to the inbound interceptor to access correlation data.
        """
        super().__init__(next)
        self.inbound = inbound

    def start_activity(self, input: StartActivityInput) -> workflow.ActivityHandle[Any]:
        """Inject atlan-* headers into activity calls.

        Args:
            input: The activity input containing headers and other metadata.

        Returns:
            ActivityHandle for the started activity.
        """
        try:
            if self.inbound.correlation_data:
                new_headers: Dict[str, Payload] = dict(input.headers)
                payload_converter = default_converter().payload_converter

                for key, value in self.inbound.correlation_data.items():
                    if key.startswith(ATLAN_HEADER_PREFIX) and value:
                        payload = payload_converter.to_payload(value)
                        new_headers[key] = payload

                input = replace(input, headers=new_headers)
        except Exception as e:
            logger.warning(f"Failed to inject correlation context headers: {e}")

        return self.next.start_activity(input)


class CorrelationContextWorkflowInboundInterceptor(WorkflowInboundInterceptor):
    """Inbound workflow interceptor that extracts atlan-* context from workflow args."""

    def __init__(self, next: WorkflowInboundInterceptor):
        """Initialize the inbound interceptor.

        Args:
            next: The next interceptor in the chain.
        """
        super().__init__(next)
        self.correlation_data: Dict[str, str] = {}

    def init(self, outbound: WorkflowOutboundInterceptor) -> None:
        """Initialize with correlation context outbound interceptor.

        Args:
            outbound: The outbound interceptor to wrap.
        """
        context_outbound = CorrelationContextOutboundInterceptor(outbound, self)
        super().init(context_outbound)

    async def execute_workflow(self, input: ExecuteWorkflowInput) -> Any:
        """Execute workflow and extract atlan-* fields from arguments.

        Args:
            input: The workflow execution input containing args.

        Returns:
            The result of the workflow execution.
        """
        try:
            if input.args and len(input.args) > 0:
                workflow_config = input.args[0]
                if isinstance(workflow_config, dict):
                    self.correlation_data = {
                        k: str(v)
                        for k, v in workflow_config.items()
                        if k.startswith(ATLAN_HEADER_PREFIX) and v
                    }
                    if self.correlation_data:
                        correlation_context.set(self.correlation_data)
        except Exception as e:
            logger.warning(f"Failed to extract correlation context from args: {e}")

        return await super().execute_workflow(input)


class CorrelationContextActivityInboundInterceptor(ActivityInboundInterceptor):
    """Activity interceptor that reads atlan-* headers and sets correlation_context."""

    async def execute_activity(self, input: ExecuteActivityInput) -> Any:
        """Execute activity after extracting atlan-* headers.

        Args:
            input: The activity execution input containing headers.

        Returns:
            The result of the activity execution.
        """
        try:
            atlan_fields: Dict[str, str] = {}
            payload_converter = default_converter().payload_converter

            for key, payload in input.headers.items():
                if key.startswith(ATLAN_HEADER_PREFIX):
                    value = payload_converter.from_payload(payload, type_hint=str)
                    atlan_fields[key] = value

            if atlan_fields:
                correlation_context.set(atlan_fields)

        except Exception as e:
            logger.warning(f"Failed to extract correlation context from headers: {e}")

        return await super().execute_activity(input)


class CorrelationContextInterceptor(Interceptor):
    """Main interceptor for propagating atlan-* correlation context.

    This interceptor ensures that atlan-* fields (like atlan-ignore,
    atlan-argo-workflow-id, atlan-argo-workflow-node) are propagated from
    workflow arguments to all activities via Temporal headers.

    Example:
        >>> worker = Worker(
        ...     client,
        ...     task_queue="my-task-queue",
        ...     workflows=[MyWorkflow],
        ...     activities=[my_activity],
        ...     interceptors=[CorrelationContextInterceptor()]
        ... )
    """

    def workflow_interceptor_class(
        self, input: WorkflowInterceptorClassInput
    ) -> Optional[Type[WorkflowInboundInterceptor]]:
        """Get the workflow interceptor class.

        Args:
            input: The interceptor class input.

        Returns:
            The CorrelationContextWorkflowInboundInterceptor class.
        """
        return CorrelationContextWorkflowInboundInterceptor

    def intercept_activity(
        self, next: ActivityInboundInterceptor
    ) -> ActivityInboundInterceptor:
        """Intercept activity executions to read correlation context.

        Args:
            next: The next interceptor in the chain.

        Returns:
            The CorrelationContextActivityInboundInterceptor wrapping the next.
        """
        return CorrelationContextActivityInboundInterceptor(next)
