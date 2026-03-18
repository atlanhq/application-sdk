"""Activity failure logging interceptor for Temporal activities.

Catches activity failures and emits structured logs with full Temporal context
(activity type, attempt, workflow ID, timeouts, tenant ID) for queryable
observability in ClickHouse via the OTEL logging pipeline.
"""

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


class _TaskFailureLoggingActivityInboundInterceptor(ActivityInboundInterceptor):
    """Activity interceptor that logs failures with full Temporal context."""

    async def execute_activity(self, input: ExecuteActivityInput) -> Any:
        """Execute activity and log failures with structured Temporal context.

        On success: returns result normally without logging.
        On failure: emits structured error log with Temporal context, then re-raises.
        """
        try:
            return await super().execute_activity(input)
        except BaseException as e:
            self._log_activity_failure(e)
            raise

    def _log_activity_failure(self, exception: BaseException) -> None:
        """Log activity failure with structured Temporal context.

        Extracts context from activity.info() and correlation_context, then emits
        a structured error log. The log flows through the existing OTEL pipeline
        (PR #1063) where exception attributes are extracted via exc_info=True.

        Args:
            exception: The exception that caused the activity failure.
        """
        try:
            log_attrs = self._collect_temporal_context()
            logger.error("Temporal activity failed", exc_info=True, **log_attrs)
        except Exception as log_error:
            logger.warning(f"Failed to log activity failure context: {log_error}")

    def _collect_temporal_context(self) -> dict[str, Any]:
        """Collect Temporal activity context for structured logging.

        Returns:
            Dictionary of temporal.* and tenant.* prefixed attributes.
        """
        attrs: dict[str, Any] = {}

        try:
            info = activity.info()

            attrs["temporal.activity.type"] = info.activity_type
            attrs["temporal.activity.attempt"] = info.attempt
            attrs["temporal.workflow.type"] = info.workflow_type
            attrs["temporal.workflow.id"] = info.workflow_id
            attrs["temporal.workflow.run_id"] = info.workflow_run_id
            attrs["temporal.activity.task_queue"] = info.task_queue

            if info.schedule_to_close_timeout is not None:
                attrs["temporal.activity.schedule_to_close_timeout"] = str(
                    info.schedule_to_close_timeout
                )
            if info.start_to_close_timeout is not None:
                attrs["temporal.activity.start_to_close_timeout"] = str(
                    info.start_to_close_timeout
                )
            if info.heartbeat_timeout is not None:
                attrs["temporal.activity.heartbeat_timeout"] = str(
                    info.heartbeat_timeout
                )
        except Exception as info_error:
            logger.warning(f"Failed to extract activity info: {info_error}")

        try:
            corr_ctx = correlation_context.get()
            if corr_ctx:
                tenant_id = corr_ctx.get("atlan-tenant-id", "")
                if tenant_id:
                    attrs["tenant.id"] = tenant_id
        except Exception as ctx_error:
            logger.warning(f"Failed to extract correlation context: {ctx_error}")

        return attrs


class TaskFailureLoggingInterceptor(Interceptor):
    """Temporal interceptor that logs task (activity) failures with full context.

    Activity-only interceptor that catches failures and emits structured logs
    with Temporal context (activity type, attempt, workflow ID, timeouts, tenant).
    Named "Task" because in v3 these are registered via @task decorators.
    """

    def workflow_interceptor_class(
        self, input: WorkflowInterceptorClassInput
    ) -> Optional[Type[WorkflowInboundInterceptor]]:
        """Return None - task-only interceptor.

        activity.info() is only available in activity context, and Temporal's
        workflow sandbox blocks certain imports.
        """
        return None

    def intercept_activity(
        self, next: ActivityInboundInterceptor
    ) -> ActivityInboundInterceptor:
        """Wrap task execution with failure logging interceptor."""
        return _TaskFailureLoggingActivityInboundInterceptor(next)


# Backwards-compatible alias — v2 code that imports ActivityFailureLoggingInterceptor
# by name (e.g. clients/temporal.py) continues to work unchanged.
ActivityFailureLoggingInterceptor = TaskFailureLoggingInterceptor
