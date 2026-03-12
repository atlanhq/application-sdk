"""Correlation context interceptor for Temporal workflows.

Propagates atlan-* correlation context fields, trace_id, and correlation_id from
workflow arguments to activities via Temporal headers, ensuring all activity logs
include correlation identifiers for end-to-end observability.
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
    """Outbound interceptor that injects correlation context into activity headers."""

    def __init__(
        self,
        next: WorkflowOutboundInterceptor,
        inbound: "CorrelationContextWorkflowInboundInterceptor",
    ):
        """Initialize the outbound interceptor."""
        super().__init__(next)
        self.inbound = inbound

    def start_activity(self, input: StartActivityInput) -> workflow.ActivityHandle[Any]:
        """Inject atlan-* headers and trace_id into activity calls."""
        try:
            # Merge interceptor-captured data with correlation_context ContextVar.
            # The ContextVar is updated by the workflow after deriving correlation_id
            # (e.g. for scheduled runs where it's only known after WorkflowRun creation).
            merged: Dict[str, str] = {}
            ctx_data = correlation_context.get()
            if ctx_data:
                for k, v in ctx_data.items():
                    if (
                        k.startswith(ATLAN_HEADER_PREFIX)
                        or k in ("trace_id", "correlation_id")
                    ) and v:
                        merged[k] = str(v)
            # Interceptor-captured data takes precedence
            if self.inbound.correlation_data:
                merged.update(self.inbound.correlation_data)

            if merged:
                new_headers: Dict[str, Payload] = dict(input.headers)
                payload_converter = default_converter().payload_converter

                for key, value in merged.items():
                    if value:
                        payload = payload_converter.to_payload(value)
                        new_headers[key] = payload

                input = replace(input, headers=new_headers)
        except Exception as e:
            logger.warning(f"Failed to inject correlation context headers: {e}")

        return self.next.start_activity(input)


class CorrelationContextWorkflowInboundInterceptor(WorkflowInboundInterceptor):
    """Inbound workflow interceptor that extracts correlation context from workflow args."""

    def __init__(self, next: WorkflowInboundInterceptor):
        """Initialize the inbound interceptor."""
        super().__init__(next)
        self.correlation_data: Dict[str, str] = {}

    def init(self, outbound: WorkflowOutboundInterceptor) -> None:
        """Initialize with correlation context outbound interceptor."""
        context_outbound = CorrelationContextOutboundInterceptor(outbound, self)
        super().init(context_outbound)

    async def execute_workflow(self, input: ExecuteWorkflowInput) -> Any:
        """Execute workflow and extract atlan-* fields and trace_id from arguments."""
        try:
            if input.args and len(input.args) > 0:
                workflow_config = input.args[0]
                if isinstance(workflow_config, dict):
                    # Extract atlan-* prefixed fields
                    self.correlation_data = {
                        k: str(v)
                        for k, v in workflow_config.items()
                        if k.startswith(ATLAN_HEADER_PREFIX) and v
                    }
                    # Extract trace_id and correlation_id separately (not atlan- prefixed)
                    for field_name in ("trace_id", "correlation_id"):
                        field_value = workflow_config.get(field_name, "")
                        if field_value:
                            self.correlation_data[field_name] = str(field_value)
                    if self.correlation_data:
                        correlation_context.set(self.correlation_data)
        except Exception as e:
            logger.warning(f"Failed to extract correlation context from args: {e}")

        return await super().execute_workflow(input)


class CorrelationContextActivityInboundInterceptor(ActivityInboundInterceptor):
    """Activity interceptor that reads correlation headers and sets correlation_context."""

    async def execute_activity(self, input: ExecuteActivityInput) -> Any:
        """Execute activity after extracting atlan-* headers and trace_id."""
        try:
            atlan_fields: Dict[str, str] = {}
            payload_converter = default_converter().payload_converter

            for key, payload in input.headers.items():
                # Extract atlan-* prefixed headers, trace_id, and correlation_id
                if key.startswith(ATLAN_HEADER_PREFIX) or key in (
                    "trace_id",
                    "correlation_id",
                ):
                    value = payload_converter.from_payload(payload, type_hint=str)
                    atlan_fields[key] = value

            if atlan_fields:
                correlation_context.set(atlan_fields)

        except Exception as e:
            logger.warning(f"Failed to extract correlation context from headers: {e}")

        return await super().execute_activity(input)


class CorrelationContextInterceptor(Interceptor):
    """Main interceptor for propagating atlan-* correlation context.

    Ensures atlan-* fields are propagated from workflow arguments to all activities via Temporal headers.
    """

    def workflow_interceptor_class(
        self, input: WorkflowInterceptorClassInput
    ) -> Optional[Type[WorkflowInboundInterceptor]]:
        """Get the workflow interceptor class."""
        return CorrelationContextWorkflowInboundInterceptor

    def intercept_activity(
        self, next: ActivityInboundInterceptor
    ) -> ActivityInboundInterceptor:
        """Intercept activity executions to read correlation context."""
        return CorrelationContextActivityInboundInterceptor(next)
