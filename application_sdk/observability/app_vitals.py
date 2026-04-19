"""App Vitals interceptor — automatic lifecycle events for workflows and activities.

Emits structured log events via AtlanLoggerAdapter on every workflow/activity
lifecycle transition. Events land in otel_logs.service_logs and are extracted
into horizon.app_vitals_events via a ClickHouse Materialized View.

All analytics (success rates, P95 durations, error breakdowns, etc.) are
computed at query time from the raw events — no pre-aggregated OTel metrics.

Registration order: AFTER ExecutionContextInterceptor and CorrelationContextInterceptor
(reads from both ContextVars).
"""

from __future__ import annotations

import hashlib
import json
import sys
import time
import traceback
from datetime import datetime, timezone
from typing import Any

from temporalio import activity, workflow
from temporalio.worker import (
    ActivityInboundInterceptor,
    ExecuteActivityInput,
    ExecuteWorkflowInput,
    Interceptor,
    StartActivityInput,
    StartChildWorkflowInput,
    WorkflowInboundInterceptor,
    WorkflowInterceptorClassInput,
    WorkflowOutboundInterceptor,
)

from application_sdk.constants import APPLICATION_NAME
from application_sdk.observability.error_classifier import (
    classify_error,
    extract_cause_chain,
    is_retriable,
)
from application_sdk.observability.resource_sampler import compute_deltas, sample
from application_sdk.observability.trace_context import get_trace_context


def _get_correlation_id() -> str:
    """Get correlation_id from the v3 CorrelationContext ContextVar."""
    try:
        from application_sdk.observability.correlation import get_correlation_context

        ctx = get_correlation_context()
        if ctx and ctx.correlation_id:
            return ctx.correlation_id
    except Exception:
        pass
    return ""


def _extract_assets_processed(result: Any) -> int | None:
    """Extract the assets_processed count from an activity return value.

    Convention (RFC DESIGN.md §4.1 - universal throughput denominator):
    apps can expose their "unit of value" as one of these attribute names
    on the return value. Recognised names checked in order:
    ``assets_processed`` (explicit, preferred),
    ``total_record_count`` (SqlMetadataExtractor subclasses),
    ``record_count``, ``records_processed`` (alternate conventions).

    Returns None if no recognised attribute is present or the value isn't
    a positive integer-like. No exceptions are raised - extraction is
    best-effort.
    """
    if result is None:
        return None

    candidates = (
        "assets_processed",
        "total_record_count",
        "record_count",
        "records_processed",
    )
    for name in candidates:
        value = None
        # Attribute access (Pydantic models, dataclasses, etc.)
        if hasattr(result, name):
            value = getattr(result, name, None)
        # Dict-like access
        elif isinstance(result, dict) and name in result:
            value = result[name]

        if value is None:
            continue
        try:
            n = int(value)
            if n >= 0:
                return n
        except (TypeError, ValueError):
            continue

    return None


def _format_stack_trace(exc: BaseException) -> str:
    """Format a full stack trace, truncated to 2000 chars."""
    try:
        lines = traceback.format_exception(type(exc), exc, exc.__traceback__)
        full = "".join(lines)
        return full[:2000]
    except Exception:
        return ""


def _compute_error_fingerprint(
    activity_type: str, error_type: str, error_class: str
) -> str:
    """Deterministic fingerprint for error deduplication.

    Same (activity_type, error_type, error_class) → same fingerprint,
    enabling GROUP BY fingerprint to count distinct failure modes.
    """
    raw = f"{activity_type}:{error_type}:{error_class}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _build_common_attrs() -> dict[str, str]:
    """Build the per-event identity attributes.

    Deployment-level attributes (app.version, release_id, sdk_version, pod_name,
    k8s.domain.name, k8s.cluster.name, etc.) are set on the OTel Resource or
    injected by the collector and appear as ResourceAttributes.* in ClickHouse
    — not duplicated here. tenant_id is intentionally omitted because
    k8s.cluster.name identifies the tenant at the deployment level.

    ``app_name`` stays in log attrs (not resource) because a single deployment
    can host multiple apps; app_name is conceptually per-event, not per-pod.

    The ``app_vitals: "true"`` flag is a deterministic marker for filtering
    the materialized view (more reliable than Body LIKE 'app_vitals.%').
    """
    trace_id, span_id = get_trace_context()
    return {
        "app_vitals": "true",
        "app_name": APPLICATION_NAME,
        "correlation_id": _get_correlation_id(),
        "trace_id": trace_id,
        "span_id": span_id,
    }


def _build_workflow_identity_attrs(info: Any) -> dict[str, Any]:
    """Extract workflow identity attributes from a Temporal WorkflowInfo object."""
    return {
        "workflow_id": info.workflow_id or "",
        "workflow_run_id": info.run_id or "",
        "workflow_type": info.workflow_type or "",
        "task_queue": info.task_queue or "",
        "namespace": info.namespace or "",
        "parent_workflow_id": info.parent.workflow_id if info.parent else "",
        "parent_run_id": info.parent.run_id if info.parent else "",
        "continued_run_id": info.continued_run_id or "",
    }


def _emit_log_event(
    event_name: str,
    attrs: dict[str, Any],
) -> None:
    """Emit a structured log event (Path 1: → otel_logs).

    Also prints a compact JSON summary to stdout so the event is visible
    in the pod console (kubectl logs) without needing the OTLP pipeline.
    The Loguru console handler strips extras from its format, so without
    this print the rich payload would be invisible in pod logs.
    """
    # Console visibility — key fields only, one line, greppable prefix.
    _CONSOLE_KEYS = (
        "app_name",
        "workflow_type",
        "activity_type",
        "status",
        "error_type",
        "duration_ms",
        "assets_processed",
        "workflow_id",
        "correlation_id",
    )
    try:
        summary = {
            k: attrs[k]
            for k in _CONSOLE_KEYS
            if k in attrs and attrs[k] not in (None, "", [])
        }
        sys.stdout.write(f"APP_VITALS | {event_name} | {json.dumps(summary)}\n")
        sys.stdout.flush()
    except Exception:
        pass

    try:
        # Deferred import: logger_adaptor may not be initialized yet during early
        # workflow lifecycle events (interceptor runs before full app setup).
        from application_sdk.observability.logger_adaptor import get_logger

        logger = get_logger("app_vitals")
        level = "error" if attrs.get("status") == "failed" else "info"
        getattr(logger, level)(event_name, **attrs)
    except Exception:
        pass


class _AppVitalsWorkflowOutboundInterceptor(WorkflowOutboundInterceptor):
    """Tracks each activity/child-workflow call so the inbound interceptor
    can emit a workflow summary on completion (L1).

    Activities can run concurrently (asyncio.gather); each call gets its own
    record appended to the list. The list lives on the paired inbound
    interceptor so its lifetime matches the workflow execution.
    """

    def __init__(
        self,
        next_: WorkflowOutboundInterceptor,
        inbound: _AppVitalsWorkflowInboundInterceptor,
    ) -> None:
        super().__init__(next_)
        self._inbound = inbound

    def start_activity(self, input: StartActivityInput) -> Any:
        record: dict[str, Any] = {
            "activity_type": input.activity,
            "start_ns": time.monotonic_ns(),
            "status": "pending",
        }
        self._inbound._activity_records.append(record)
        awaitable = self.next.start_activity(input)
        return _track_activity_completion(record, awaitable)

    async def start_child_workflow(self, input: StartChildWorkflowInput) -> Any:
        # Child workflows: track start/complete but don't deeply inspect
        record: dict[str, Any] = {
            "child_workflow_type": input.workflow,
            "start_ns": time.monotonic_ns(),
            "status": "pending",
        }
        self._inbound._child_workflow_records.append(record)
        try:
            handle = await self.next.start_child_workflow(input)
            record["status"] = "started"
            return handle
        except Exception as exc:
            record["status"] = "failed"
            record["error_type"] = classify_error(exc)
            record["end_ns"] = time.monotonic_ns()
            raise


async def _track_activity_completion(record: dict[str, Any], awaitable: Any) -> Any:
    """Await the activity and capture its outcome into the record.

    Must use monotonic_ns (not workflow.time) so it works identically in both
    workflow and any test contexts. Temporal's sandbox allows time.monotonic_ns.
    """
    try:
        result = await awaitable
        record["status"] = "succeeded"
        record["end_ns"] = time.monotonic_ns()
        record["duration_ms"] = round(
            (record["end_ns"] - record["start_ns"]) / 1_000_000, 1
        )
        return result
    except Exception as exc:
        record["status"] = "failed"
        record["error_type"] = classify_error(exc)
        record["error_class"] = type(exc).__name__
        record["end_ns"] = time.monotonic_ns()
        record["duration_ms"] = round(
            (record["end_ns"] - record["start_ns"]) / 1_000_000, 1
        )
        raise


class _AppVitalsWorkflowInboundInterceptor(WorkflowInboundInterceptor):
    """Emits lifecycle events on workflow start AND completion, plus a
    summary event that rolls up all activities seen during the workflow."""

    def __init__(self, next_: WorkflowInboundInterceptor) -> None:
        super().__init__(next_)
        self._activity_records: list[dict[str, Any]] = []
        self._child_workflow_records: list[dict[str, Any]] = []

    def init(self, outbound: WorkflowOutboundInterceptor) -> None:
        # Wrap the outbound chain so we can track each start_activity call.
        super().init(_AppVitalsWorkflowOutboundInterceptor(outbound, self))

    def _build_summary_attrs(
        self,
        common: dict[str, Any],
        info: Any,
        wf_status: str,
        wf_duration_ms: float,
    ) -> dict[str, Any]:
        """Compute aggregate stats from the tracked activity records."""
        acts = self._activity_records
        succeeded = [a for a in acts if a.get("status") == "succeeded"]
        failed = [a for a in acts if a.get("status") == "failed"]

        first_failure: dict[str, Any] | None = None
        if failed:
            # "first" by start time — earliest failure in wall-clock order
            first_failure = min(failed, key=lambda a: a.get("start_ns", 0))

        bottleneck: dict[str, Any] | None = None
        acts_with_duration = [a for a in acts if a.get("duration_ms") is not None]
        if acts_with_duration:
            bottleneck = max(acts_with_duration, key=lambda a: a["duration_ms"])

        # Sum of all activity durations — if > wf_duration, activities ran in
        # parallel; if < wf_duration, difference is orchestration overhead.
        sum_activity_duration_ms = round(
            sum(a.get("duration_ms", 0) for a in acts_with_duration), 1
        )

        summary: dict[str, Any] = {
            **common,
            **_build_workflow_identity_attrs(info),
            "status": wf_status,
            "duration_ms": round(wf_duration_ms, 1),
            "total_activities": len(acts),
            "succeeded_activities": len(succeeded),
            "failed_activities": len(failed),
            "total_child_workflows": len(self._child_workflow_records),
            "sum_activity_duration_ms": sum_activity_duration_ms,
            "dimension": "reliability",
            "source": "temporal",
            "metric_name": "app_vitals.reliability.wf_summary",
        }
        if first_failure is not None:
            summary["first_failure_activity_type"] = first_failure.get(
                "activity_type", ""
            )
            summary["first_failure_error_type"] = first_failure.get("error_type", "")
        if bottleneck is not None:
            summary["bottleneck_activity_type"] = bottleneck.get("activity_type", "")
            summary["bottleneck_duration_ms"] = bottleneck["duration_ms"]
        return summary

    async def execute_workflow(self, input: ExecuteWorkflowInput) -> Any:
        # Skip all observability during Temporal replay — replays re-execute
        # workflow code for determinism, emitting here would double-count.
        if workflow.unsafe.is_replaying():
            return await self.next.execute_workflow(input)

        start_ns = time.monotonic_ns()

        # Emit wf.started event (L2) — enables "currently running" views that
        # otherwise have to wait for completion to see any signal.
        try:
            info = workflow.info()
            common = _build_common_attrs()
            started_attrs: dict[str, Any] = {
                **common,
                **_build_workflow_identity_attrs(info),
                "cron_schedule": info.cron_schedule or "",
                "dimension": "reliability",
                "source": "temporal",
                "metric_name": "app_vitals.reliability.wf_started",
            }
            _emit_log_event("app_vitals.wf.started", started_attrs)
        except Exception:
            pass  # never block workflow on observability

        status = "succeeded"
        error_type = ""
        error_message = ""
        error_class = ""
        cause_chain: list[str] = []
        retriable = False
        stack_trace = ""

        try:
            result = await self.next.execute_workflow(input)
            return result
        except Exception as exc:
            status = "failed"
            error_type = classify_error(exc)
            error_message = str(exc)[:500]
            error_class = type(exc).__name__
            cause_chain = extract_cause_chain(exc)
            retriable = is_retriable(exc, error_type)
            stack_trace = _format_stack_trace(exc)
            raise
        finally:
            duration_ms = (time.monotonic_ns() - start_ns) / 1_000_000

            try:
                info = workflow.info()
            except Exception:
                info = None

            # Guard: skip event emission if workflow info unavailable.
            # MUST NOT use `return` in finally — that would swallow exceptions.
            if info is not None:
                self._emit_completion_events(
                    info,
                    status,
                    duration_ms,
                    error_type,
                    error_message,
                    error_class,
                    cause_chain,
                    retriable,
                    stack_trace,
                )

    def _emit_completion_events(
        self,
        info: Any,
        status: str,
        duration_ms: float,
        error_type: str,
        error_message: str,
        error_class: str,
        cause_chain: list[str],
        retriable: bool,
        stack_trace: str,
    ) -> None:
        """Emit workflow completed + summary events. Extracted to avoid return-in-finally."""
        common = _build_common_attrs()

        # Workflow-level timeout budget — same pattern as activity timeout
        wf_timeout_budget_total_ms: float | None = None
        wf_timeout_budget_used_pct: float | None = None
        execution_timeout = getattr(info, "execution_timeout", None)
        if execution_timeout is not None:
            total_ms = execution_timeout.total_seconds() * 1000
            wf_timeout_budget_total_ms = total_ms
            if total_ms > 0:
                wf_timeout_budget_used_pct = round((duration_ms / total_ms) * 100, 2)

        # History length — proxy for workflow complexity / CaN pressure
        history_length: int | None = None
        try:
            history_length = info.get_current_history_length()
        except Exception:
            pass

        event_attrs: dict[str, Any] = {
            **common,
            **_build_workflow_identity_attrs(info),
            "cron_schedule": info.cron_schedule or "",
            "status": status,
            "error_type": error_type,
            "duration_ms": round(duration_ms, 1),
            "dimension": "reliability",
            "source": "temporal",
            "metric_name": "app_vitals.reliability.wf_completed",
        }
        if wf_timeout_budget_total_ms is not None:
            event_attrs["wf_timeout_budget_total_ms"] = round(
                wf_timeout_budget_total_ms, 1
            )
            event_attrs["wf_timeout_budget_used_pct"] = wf_timeout_budget_used_pct
        if history_length is not None:
            event_attrs["history_length"] = history_length
        if error_message:
            event_attrs["error_message"] = error_message
            event_attrs["error_class"] = error_class
            event_attrs["is_retriable"] = retriable
            event_attrs["error_fingerprint"] = _compute_error_fingerprint(
                info.workflow_type or "", error_type, error_class
            )
            if stack_trace:
                event_attrs["stack_trace"] = stack_trace
            if cause_chain:
                event_attrs["error_cause_chain"] = cause_chain

        _emit_log_event("app_vitals.wf.completed", event_attrs)

        # Workflow summary event (L1) — rolls up all activities observed
        # during this workflow execution. One row per workflow run with the
        # full shape of the pipeline: counts, first failure, bottleneck.
        # Does NOT replace per-activity events — they are still the source
        # of truth (RFC D2). This is a query-time convenience.
        try:
            summary_attrs = self._build_summary_attrs(common, info, status, duration_ms)
            _emit_log_event("app_vitals.wf.summary", summary_attrs)
        except Exception:
            pass  # summary is best-effort; never block the workflow


class _AppVitalsActivityInboundInterceptor(ActivityInboundInterceptor):
    """Emits lifecycle events on activity start AND completion."""

    async def execute_activity(self, input: ExecuteActivityInput) -> Any:
        start_ns = time.monotonic_ns()
        start_resource = sample()

        # Measure input payload size. At the inbound interceptor level,
        # input.args are deserialized Python objects (not raw Payloads).
        # Use sys.getsizeof as a rough in-memory size estimate.
        input_payload_bytes: int | None = None
        try:
            if input.args:
                input_payload_bytes = sum(sys.getsizeof(a) for a in input.args)
        except Exception:
            pass

        try:
            info = activity.info()
        except Exception:
            return await self.next.execute_activity(input)

        schedule_to_start_ms: float | None = None
        if info.started_time and info.scheduled_time:
            delta = info.started_time - info.scheduled_time
            schedule_to_start_ms = delta.total_seconds() * 1000

        # Emit act.started event (L2) — enables "currently running" dashboards
        # without waiting for completion. Carries full context for drill-down.
        try:
            started_common = _build_common_attrs()
            retry_policy = getattr(info, "retry_policy", None)
            activity_start_time_iso = (
                info.started_time.isoformat() if info.started_time else ""
            )
            started_attrs: dict[str, Any] = {
                **started_common,
                "workflow_id": info.workflow_id or "",
                "workflow_run_id": info.workflow_run_id or "",
                "activity_id": info.activity_id or "",
                "activity_type": info.activity_type or "",
                "task_queue": info.task_queue or "",
                "attempt": info.attempt,
                "retry_max_attempts": (
                    retry_policy.maximum_attempts if retry_policy else 0
                ),
                "activity_start_time": activity_start_time_iso,
                "dimension": "reliability",
                "source": "temporal",
                "metric_name": "app_vitals.reliability.activity_started",
            }
            if schedule_to_start_ms is not None:
                started_attrs["schedule_to_start_ms"] = round(schedule_to_start_ms, 1)
            if input_payload_bytes is not None:
                started_attrs["input_payload_bytes"] = input_payload_bytes
            _emit_log_event("app_vitals.act.started", started_attrs)
        except Exception:
            pass  # never block activity on observability

        status = "succeeded"
        error_type = ""
        error_message = ""
        error_class = ""
        cause_chain: list[str] = []
        retriable = False
        stack_trace = ""
        assets_processed: int | None = None

        try:
            result = await self.next.execute_activity(input)
            assets_processed = _extract_assets_processed(result)
            return result
        except Exception as exc:
            status = "failed"
            error_type = classify_error(exc)
            error_message = str(exc)[:500]
            error_class = type(exc).__name__
            cause_chain = extract_cause_chain(exc)
            retriable = is_retriable(exc, error_type)
            stack_trace = _format_stack_trace(exc)
            raise
        finally:
            duration_ms = (time.monotonic_ns() - start_ns) / 1_000_000
            end_resource = sample()
            cpu_seconds, mem_gb_sec = compute_deltas(
                start_resource, end_resource, duration_ms / 1000.0
            )

            # Timeout budget: compare wall-clock duration against the activity's
            # configured timeout. Prefer schedule_to_close (total budget); fall
            # back to start_to_close if that's the only one configured.
            timeout_budget_total_ms: float | None = None
            timeout_budget_used_pct: float | None = None
            effective_timeout = info.schedule_to_close_timeout or getattr(
                info, "start_to_close_timeout", None
            )
            if effective_timeout is not None:
                total_ms = effective_timeout.total_seconds() * 1000
                timeout_budget_total_ms = total_ms
                if total_ms > 0:
                    timeout_budget_used_pct = round((duration_ms / total_ms) * 100, 2)

            common = _build_common_attrs()

            retry_policy = getattr(info, "retry_policy", None)
            activity_start_time_iso = (
                info.started_time.isoformat() if info.started_time else ""
            )
            activity_end_time_iso = datetime.now(timezone.utc).isoformat()
            event_attrs: dict[str, Any] = {
                **common,
                "workflow_id": info.workflow_id or "",
                "workflow_run_id": info.workflow_run_id or "",
                "activity_id": info.activity_id or "",
                "activity_type": info.activity_type or "",
                "task_queue": info.task_queue or "",
                "attempt": info.attempt,
                "retry_max_attempts": (
                    retry_policy.maximum_attempts if retry_policy else 0
                ),
                "activity_start_time": activity_start_time_iso,
                "activity_end_time": activity_end_time_iso,
                "namespace": getattr(info, "namespace", ""),
                "status": status,
                "error_type": error_type,
                "duration_ms": round(duration_ms, 1),
                "dimension": "reliability",
                "source": "temporal",
                "metric_name": "app_vitals.reliability.activity_completed",
            }
            if error_message:
                event_attrs["error_message"] = error_message
                event_attrs["error_class"] = error_class
                event_attrs["is_retriable"] = retriable
                event_attrs["error_fingerprint"] = _compute_error_fingerprint(
                    info.activity_type or "", error_type, error_class
                )
                if stack_trace:
                    event_attrs["stack_trace"] = stack_trace
                if cause_chain:
                    event_attrs["error_cause_chain"] = cause_chain

            if timeout_budget_total_ms is not None:
                event_attrs["timeout_budget_total_ms"] = round(
                    timeout_budget_total_ms, 1
                )
                event_attrs["timeout_budget_used_pct"] = timeout_budget_used_pct

            if start_resource is not None and end_resource is not None:
                event_attrs["cpu_seconds"] = round(cpu_seconds, 4)
                event_attrs["mem_gb_sec"] = round(mem_gb_sec, 4)

            if assets_processed is not None:
                event_attrs["assets_processed"] = assets_processed
            if input_payload_bytes is not None:
                event_attrs["input_payload_bytes"] = input_payload_bytes

            _emit_log_event("app_vitals.act.completed", event_attrs)


class AppVitalsInterceptor(Interceptor):
    """Temporal interceptor that emits App Vitals lifecycle events.

    Emits structured log events (Path 1) on every workflow/activity
    lifecycle transition. These land in otel_logs.service_logs and are
    extracted into horizon.app_vitals_events via a ClickHouse MV.

    Registration order: AFTER ExecutionContextInterceptor and
    CorrelationContextInterceptor so that ContextVars are populated.
    """

    def workflow_interceptor_class(
        self,
        input: WorkflowInterceptorClassInput,  # noqa: ARG002
    ) -> type[WorkflowInboundInterceptor] | None:
        return _AppVitalsWorkflowInboundInterceptor

    def intercept_activity(
        self, next: ActivityInboundInterceptor
    ) -> ActivityInboundInterceptor:
        return _AppVitalsActivityInboundInterceptor(next)
