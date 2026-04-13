"""Activity failure logging interceptor for Temporal activities.

Catches activity failures and emits structured logs with full Temporal context
(activity type, attempt, workflow ID, timeouts, tenant ID) AND App Vitals
identity (app_name, tenant_id, app_version, error_type, trace_id, span_id) for
queryable observability in ClickHouse via the OTEL logging pipeline.

This completes RFC P0.4 (07-implementation.md) — "Structured Logs for AI
Consumption" — by ensuring every activity failure is queryable by the same
attributes used across App Vitals.
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

from application_sdk.constants import APP_BUILD_ID, APP_TENANT_ID, APPLICATION_NAME
from application_sdk.observability.context import correlation_context
from application_sdk.observability.error_classifier import (
    classify_error,
    extract_cause_chain,
    is_retriable,
)
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.observability.trace_context import get_trace_context

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
        """Log activity failure with structured Temporal + App Vitals context.

        Extracts context from activity.info() and correlation_context, classifies
        the error, and emits a structured error log. The log flows through the
        existing OTEL pipeline where exception attributes are extracted via
        exc_info=True.

        The structured attributes are the same ones App Vitals uses everywhere,
        so failure logs can be filtered/joined with the rest of App Vitals data
        in ClickHouse by (app_name, tenant_id, workflow_id, error_type, ...).

        Args:
            exception: The exception that caused the activity failure.
        """
        try:
            log_attrs = self._collect_temporal_context()
            log_attrs.update(self._collect_app_vitals_context(exception))
            # kwargs are temporal.*/tenant.*/app_vitals.* keys promoted to indexed OTEL fields
            logger.error("Temporal activity failed", exc_info=True, **log_attrs)
        except Exception:
            logger.warning("Failed to log activity failure context", exc_info=True)

    def _collect_app_vitals_context(self, exception: BaseException) -> dict[str, Any]:
        """Collect App Vitals identity + error classification for structured logging.

        Returns the same attributes that the AppVitalsInterceptor emits, so a
        single ClickHouse query can filter failures by app_name, tenant_id,
        error_type, etc. without joining across schemas.
        """
        trace_id, span_id = get_trace_context()
        error_type = classify_error(exception)
        cause_chain = extract_cause_chain(exception)

        attrs: dict[str, Any] = {
            "app_name": APPLICATION_NAME,
            "app_version": APP_BUILD_ID or "",
            "error_type": error_type,
            "error_class": type(exception).__name__,
            "is_retriable": is_retriable(exception, error_type),
            "dimension": "reliability",
            "source": "temporal",
            "metric_name": "app_vitals.reliability.activity_completed",
            "status": "failed",
        }

        if cause_chain:
            attrs["error_cause_chain"] = cause_chain

        if trace_id:
            attrs["trace_id"] = trace_id
        if span_id:
            attrs["span_id"] = span_id

        # tenant_id from correlation context, env var fallback
        try:
            corr_ctx = correlation_context.get()
            if corr_ctx:
                tenant_from_corr = corr_ctx.get("atlan-tenant-id", "")
                if tenant_from_corr:
                    attrs["tenant_id"] = tenant_from_corr
        except Exception:
            pass
        if "tenant_id" not in attrs:
            attrs["tenant_id"] = APP_TENANT_ID

        # correlation_id from v3 CorrelationContext
        try:
            from application_sdk.observability.correlation import (
                get_correlation_context,
            )

            ctx = get_correlation_context()
            if ctx and ctx.correlation_id:
                attrs["correlation_id"] = ctx.correlation_id
        except Exception:
            pass

        return attrs

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
        except Exception:
            logger.warning("Failed to extract activity info", exc_info=True)

        try:
            corr_ctx = correlation_context.get()
            if corr_ctx:
                tenant_id = corr_ctx.get("atlan-tenant-id", "")
                if tenant_id:
                    attrs["tenant.id"] = tenant_id
        except Exception:
            logger.warning("Failed to extract correlation context", exc_info=True)

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
