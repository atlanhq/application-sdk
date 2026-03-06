"""OpenTelemetry enrichment interceptor for Temporal activities.

This interceptor enriches OTel spans created by Temporal's TracingInterceptor with
additional context that Temporal doesn't capture by default:
- Activity retry attempt number
- Task queue name
- Workflow ID and run ID
- Timeout configurations
- Tenant ID from correlation context
- Exception details (type, message, stack trace) on failure

IMPORTANT: This interceptor must be LAST in the interceptor chain. TracingInterceptor
(from temporalio.contrib.opentelemetry) must run first to create the span, then this
interceptor enriches that same span.

This is an activity-only interceptor. Workflow interceptors cannot use this module
because Temporal's workflow sandbox blocks importing opentelemetry and traceback.
"""

import traceback
from typing import Any, Optional, Type

from temporalio import activity
from temporalio.worker import (
    ActivityInboundInterceptor,
    ExecuteActivityInput,
    Interceptor,
    WorkflowInboundInterceptor,
    WorkflowInterceptorClassInput,
)

from application_sdk.observability.context import correlation_context
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


class OTelEnrichmentActivityInboundInterceptor(ActivityInboundInterceptor):
    """Activity interceptor that enriches OTel spans with Temporal context.

    This interceptor reads the current span (created by TracingInterceptor),
    adds activity metadata, tenant ID, and timeout configurations. On failure,
    it adds exception details including the full stack trace.
    """

    async def execute_activity(self, input: ExecuteActivityInput) -> Any:
        """Execute the activity and enrich the OTel span with context.

        Args:
            input: The activity execution input from Temporal.

        Returns:
            The result of the activity execution.

        Raises:
            Any exception raised by the activity is re-raised after span enrichment.
        """
        try:
            from opentelemetry import trace
        except ImportError:
            return await super().execute_activity(input)

        span = trace.get_current_span()

        if span is None or not span.is_recording():
            return await super().execute_activity(input)

        try:
            info = activity.info()

            span.set_attribute("temporal.activity.attempt", info.attempt)
            span.set_attribute("temporal.activity.task_queue", info.task_queue)
            span.set_attribute("temporal.workflow.id", info.workflow_id)
            span.set_attribute("temporal.workflow.run_id", info.workflow_run_id)

            if info.schedule_to_close_timeout is not None:
                span.set_attribute(
                    "temporal.activity.schedule_to_close_timeout",
                    str(info.schedule_to_close_timeout),
                )
            if info.start_to_close_timeout is not None:
                span.set_attribute(
                    "temporal.activity.start_to_close_timeout",
                    str(info.start_to_close_timeout),
                )
            if info.heartbeat_timeout is not None:
                span.set_attribute(
                    "temporal.activity.heartbeat_timeout",
                    str(info.heartbeat_timeout),
                )

            ctx = correlation_context.get()
            if ctx is not None:
                tenant_id = ctx.get("atlan-tenant-id", "")
                if tenant_id:
                    span.set_attribute("tenant.id", tenant_id)

        except Exception as e:
            logger.warning(f"Failed to enrich span with activity context: {e}")

        try:
            return await super().execute_activity(input)
        except Exception as e:
            try:
                span.set_attribute("exception.type", type(e).__name__)
                span.set_attribute("exception.message", str(e))
                span.set_attribute("exception.stacktrace", traceback.format_exc())
                span.record_exception(e)
            except Exception as enrich_error:
                logger.warning(
                    f"Failed to enrich span with exception details: {enrich_error}"
                )
            raise


class OTelEnrichmentInterceptor(Interceptor):
    """Interceptor that enriches OTel spans with Temporal activity context.

    This interceptor should be placed LAST in the interceptor chain so that
    TracingInterceptor creates the span first, and this interceptor enriches it.

    This is an activity-only interceptor. The workflow_interceptor_class method
    returns None because Temporal's workflow sandbox blocks importing the
    opentelemetry and traceback modules required for span enrichment.
    """

    def intercept_activity(
        self, next: ActivityInboundInterceptor
    ) -> ActivityInboundInterceptor:
        """Wrap the activity interceptor chain with OTel enrichment.

        Args:
            next: The next interceptor in the chain.

        Returns:
            The OTelEnrichmentActivityInboundInterceptor wrapping the next interceptor.
        """
        return OTelEnrichmentActivityInboundInterceptor(next)

    def workflow_interceptor_class(
        self, input: WorkflowInterceptorClassInput
    ) -> Optional[Type[WorkflowInboundInterceptor]]:
        """Return None since this is an activity-only interceptor.

        Workflow interceptors cannot use this module because Temporal's workflow
        sandbox blocks importing opentelemetry and traceback.

        Args:
            input: The workflow interceptor class input.

        Returns:
            None, indicating no workflow interceptor is provided.
        """
        return None
