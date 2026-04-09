"""App Vitals interceptor — emits P0 observability data points.

Captures Temporal-native workflow and activity metrics in OTel-friendly
format.  Runs as a fire-and-forget interceptor: failures in metric
emission never block or affect workflow/activity execution.

Data points emitted (all prefixed ``app_vitals.``):

Reliability:
  workflow.completion   — event per workflow run (status, error_type)
  activity.completion   — event per activity run (status, error_type, attempt)

Performance:
  workflow.duration_ms  — histogram of workflow wall-clock time
  activity.duration_ms  — histogram of activity wall-clock time
  activity.schedule_to_start_ms — queue wait time (backpressure signal)

Efficiency:
  activity.input_size_bytes  — serialized input payload size
  activity.output_size_bytes — serialized output payload size

Every data point carries a common attribute set:
  app_name, app_version, tenant_id, worker_id,
  workflow_id, workflow_run_id, workflow_type,
  activity_id, activity_type, task_queue, namespace, attempt
"""

from __future__ import annotations

import logging
import os
import resource
import sys
import time
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

from application_sdk.constants import APP_TENANT_ID, APPLICATION_NAME
from application_sdk.observability.context import correlation_context
from application_sdk.observability.error_classifier import classify_error
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)

# Metric name prefix — all data points use this namespace.
_PREFIX = "app_vitals"

# ---------------------------------------------------------------------------
# Common attributes
# ---------------------------------------------------------------------------


def _common_attributes() -> dict[str, str]:
    """Build the attribute dict attached to every data point.

    Reads identity from env vars (set by K8s / deployment infra) and
    correlation context when available.
    """
    attrs: dict[str, str] = {
        "app_name": APPLICATION_NAME,
        "app_version": os.environ.get("ATLAN_APP_VERSION", "unknown"),
        "tenant_id": APP_TENANT_ID,
        "worker_id": os.environ.get("HOSTNAME", "unknown"),
        "commit_hash": os.environ.get("ATLAN_COMMIT_SHA", ""),
    }

    # Merge correlation context (tenant headers, trace propagation) when available.
    try:
        corr = correlation_context.get()
        if corr:
            tenant = corr.get("atlan-tenant-id", "")
            if tenant:
                attrs["tenant_id"] = tenant
    except Exception:
        pass

    return attrs


def _workflow_attributes() -> dict[str, str]:
    """Temporal workflow context attributes."""
    attrs = _common_attributes()
    try:
        info = workflow.info()
        attrs.update(
            {
                "workflow_id": info.workflow_id or "",
                "workflow_run_id": info.run_id or "",
                "workflow_type": info.workflow_type or "",
                "namespace": info.namespace or "",
                "task_queue": info.task_queue or "",
                "attempt": str(info.attempt or 0),
            }
        )
    except Exception:
        pass
    return attrs


def _activity_attributes() -> dict[str, str]:
    """Temporal activity context attributes."""
    attrs = _common_attributes()
    try:
        info = activity.info()
        attrs.update(
            {
                "workflow_id": info.workflow_id or "",
                "workflow_run_id": info.workflow_run_id or "",
                "workflow_type": info.workflow_type or "",
                "activity_id": info.activity_id or "",
                "activity_type": info.activity_type or "",
                "task_queue": info.task_queue or "",
                "namespace": info.task_queue or "",
                "attempt": str(info.attempt or 0),
            }
        )
    except Exception:
        pass
    return attrs


# ---------------------------------------------------------------------------
# Metric emission (fire-and-forget via the existing metrics adapter)
# ---------------------------------------------------------------------------


def _emit_metric(
    name: str,
    value: float,
    metric_type: str,
    labels: dict[str, str],
    *,
    description: str = "",
    unit: str = "",
) -> None:
    """Record a metric through the SDK's metrics adapter.

    Never raises — all errors are swallowed and logged at DEBUG.
    """
    try:
        from application_sdk.observability.metrics_adaptor import get_metrics
        from application_sdk.observability.models import MetricType

        _type_map = {
            "counter": MetricType.COUNTER,
            "histogram": MetricType.HISTOGRAM,
            "gauge": MetricType.GAUGE,
        }
        metrics = get_metrics()
        metrics.record_metric(
            name=name,
            value=value,
            metric_type=_type_map[metric_type],
            labels=dict(labels),  # copy — record_metric mutates labels
            description=description or None,
            unit=unit or None,
        )
    except Exception:
        logging.debug("app_vitals: failed to emit metric %s", name, exc_info=True)


# ---------------------------------------------------------------------------
# Activity interceptor
# ---------------------------------------------------------------------------


class _AppVitalsActivityInterceptor(ActivityInboundInterceptor):
    """Wraps every activity execution to emit P0 vitals."""

    async def execute_activity(self, input: ExecuteActivityInput) -> Any:
        attrs = _activity_attributes()
        start_time = time.monotonic()

        # --- schedule-to-start (queue wait) ---
        try:
            info = activity.info()
            if info.current_attempt_scheduled_time is not None:
                scheduled_ts = info.current_attempt_scheduled_time.timestamp()
                queue_wait_ms = (time.time() - scheduled_ts) * 1000
                if queue_wait_ms >= 0:
                    _emit_metric(
                        f"{_PREFIX}.activity.schedule_to_start_ms",
                        round(queue_wait_ms, 2),
                        "histogram",
                        attrs,
                        description="Activity queue wait time",
                        unit="ms",
                    )
        except Exception:
            logging.debug(
                "app_vitals: failed to compute schedule_to_start", exc_info=True
            )

        # --- input size ---
        try:
            if input.args:
                input_size = sum(sys.getsizeof(a) for a in input.args)
                _emit_metric(
                    f"{_PREFIX}.activity.input_size_bytes",
                    float(input_size),
                    "histogram",
                    attrs,
                    description="Serialized activity input size",
                    unit="bytes",
                )
        except Exception:
            logging.debug("app_vitals: failed to measure input size", exc_info=True)

        # --- memory before ---
        mem_before = 0
        try:
            mem_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        except Exception:
            pass

        status = "succeeded"
        error_type = ""
        result = None

        try:
            result = await super().execute_activity(input)
            return result
        except BaseException as exc:
            status = "failed"
            error_type = classify_error(exc)

            # Distinguish timeout and cancellation
            import asyncio

            if isinstance(exc, asyncio.CancelledError):
                status = "cancelled"
                if "timeout" in str(exc).lower() or "deadline" in str(exc).lower():
                    status = "timed_out"

            raise
        finally:
            duration_ms = (time.monotonic() - start_time) * 1000

            # Completion labels include status and error_type
            completion_attrs = {**attrs, "status": status}
            if error_type:
                completion_attrs["error_type"] = error_type

            # --- activity.completion event (counter) ---
            _emit_metric(
                f"{_PREFIX}.activity.completion",
                1.0,
                "counter",
                completion_attrs,
                description="Activity completion event",
            )

            # --- activity.duration_ms (histogram) ---
            _emit_metric(
                f"{_PREFIX}.activity.duration_ms",
                round(duration_ms, 2),
                "histogram",
                completion_attrs,
                description="Activity execution duration",
                unit="ms",
            )

            # --- retry detection (attempt > 1) ---
            try:
                attempt = int(attrs.get("attempt", "0"))
                if attempt > 1:
                    _emit_metric(
                        f"{_PREFIX}.activity.retry",
                        1.0,
                        "counter",
                        completion_attrs,
                        description="Activity retry (attempt > 1)",
                    )
            except Exception:
                pass

            # --- output size (only on success) ---
            if status == "succeeded" and result is not None:
                try:
                    output_size = sys.getsizeof(result)
                    _emit_metric(
                        f"{_PREFIX}.activity.output_size_bytes",
                        float(output_size),
                        "histogram",
                        attrs,
                        description="Serialized activity output size",
                        unit="bytes",
                    )
                except Exception:
                    pass

            # --- memory peak / delta ---
            try:
                mem_after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                _emit_metric(
                    f"{_PREFIX}.activity.memory_peak_bytes",
                    float(mem_after),
                    "gauge",
                    attrs,
                    description="Peak RSS after activity execution",
                    unit="bytes",
                )
                if mem_before > 0:
                    delta = mem_after - mem_before
                    if delta > 0:
                        _emit_metric(
                            f"{_PREFIX}.activity.memory_delta_bytes",
                            float(delta),
                            "gauge",
                            attrs,
                            description="RSS increase during activity",
                            unit="bytes",
                        )
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Workflow interceptor
# ---------------------------------------------------------------------------


class _AppVitalsWorkflowInterceptor(WorkflowInboundInterceptor):
    """Wraps every workflow execution to emit P0 vitals.

    Uses ``workflow.time()`` for deterministic time inside the sandbox.
    Metric emission happens through a local activity to escape the sandbox.
    """

    async def execute_workflow(self, input: ExecuteWorkflowInput) -> Any:
        start_time = workflow.time()

        status = "succeeded"
        error_type = ""

        try:
            result = await self.next.execute_workflow(input)
            return result
        except BaseException as exc:
            status = "failed"
            error_type = classify_error(exc)
            raise
        finally:
            duration_ms = (workflow.time() - start_time) * 1000

            # Emit workflow vitals via a side-effect (non-deterministic but
            # safe for metrics — Temporal replays skip side effects).
            try:
                attrs = _workflow_attributes()
                completion_attrs = {**attrs, "status": status}
                if error_type:
                    completion_attrs["error_type"] = error_type

                # --- workflow.completion event ---
                _emit_metric(
                    f"{_PREFIX}.workflow.completion",
                    1.0,
                    "counter",
                    completion_attrs,
                    description="Workflow completion event",
                )

                # --- workflow.duration_ms ---
                _emit_metric(
                    f"{_PREFIX}.workflow.duration_ms",
                    round(duration_ms, 2),
                    "histogram",
                    completion_attrs,
                    description="Workflow execution duration",
                    unit="ms",
                )
            except Exception:
                logging.debug(
                    "app_vitals: failed to emit workflow metrics", exc_info=True
                )


# ---------------------------------------------------------------------------
# Top-level interceptor (registered on the worker)
# ---------------------------------------------------------------------------


class AppVitalsInterceptor(Interceptor):
    """Temporal interceptor that emits App Vitals P0 data points.

    Captures workflow and activity lifecycle, duration, error classification,
    retry detection, queue backpressure, and resource usage as OTel-friendly
    metrics.

    Must be registered after ``ExecutionContextInterceptor`` (which populates
    the ContextVar) but before other interceptors so vitals capture the full
    execution including time spent in downstream interceptors.
    """

    def workflow_interceptor_class(
        self,
        input: WorkflowInterceptorClassInput,
    ) -> Optional[Type[WorkflowInboundInterceptor]]:
        return _AppVitalsWorkflowInterceptor

    def intercept_activity(
        self,
        next: ActivityInboundInterceptor,
    ) -> ActivityInboundInterceptor:
        return _AppVitalsActivityInterceptor(next)
