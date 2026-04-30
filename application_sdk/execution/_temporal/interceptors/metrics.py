"""Unified Metrics interceptor for Temporal workflows and activities.

Emits real OTel instruments for workflow / activity execution outcomes. These
are the queryable, alertable counterpart to the lifecycle log lines emitted
by ``LogInterceptor`` — they flow through the global ``MeterProvider``
(configured in ``application_sdk.observability.metrics_adaptor``) and out to
both the Prometheus reader (server scrape) and the Pushgateway pusher
(workers).
"""

from __future__ import annotations

import time
from typing import Any

from opentelemetry import metrics as _otel_metrics
from temporalio import activity, workflow
from temporalio.worker import (
    ActivityInboundInterceptor,
    ExecuteActivityInput,
    ExecuteWorkflowInput,
    Interceptor,
    WorkflowInboundInterceptor,
    WorkflowInterceptorClassInput,
)

from application_sdk.observability.error_classifier import classify_error

_METER_NAME = "application_sdk.temporal"


def _meter():
    return _otel_metrics.get_meter(_METER_NAME)


# Lazily created singletons — meters/instruments are cheap to look up but we
# only want one of each per process.
_INSTRUMENTS: dict[str, Any] = {}


def _workflow_executions():
    if "wf_exec" not in _INSTRUMENTS:
        _INSTRUMENTS["wf_exec"] = _meter().create_counter(
            "temporal.workflow.executions",
            unit="1",
            description="Workflow executions, partitioned by type and outcome",
        )
    return _INSTRUMENTS["wf_exec"]


def _workflow_duration():
    if "wf_dur" not in _INSTRUMENTS:
        _INSTRUMENTS["wf_dur"] = _meter().create_histogram(
            "temporal.workflow.duration",
            unit="s",
            description="Workflow wall-clock duration in seconds",
        )
    return _INSTRUMENTS["wf_dur"]


def _activity_executions():
    if "act_exec" not in _INSTRUMENTS:
        _INSTRUMENTS["act_exec"] = _meter().create_counter(
            "temporal.activity.executions",
            unit="1",
            description="Activity executions, partitioned by type and outcome",
        )
    return _INSTRUMENTS["act_exec"]


def _activity_duration():
    if "act_dur" not in _INSTRUMENTS:
        _INSTRUMENTS["act_dur"] = _meter().create_histogram(
            "temporal.activity.duration",
            unit="s",
            description="Activity wall-clock duration in seconds",
        )
    return _INSTRUMENTS["act_dur"]


def _activity_errors():
    if "act_err" not in _INSTRUMENTS:
        _INSTRUMENTS["act_err"] = _meter().create_counter(
            "temporal.activity.errors",
            unit="1",
            description="Activity errors partitioned by exception type",
        )
    return _INSTRUMENTS["act_err"]


def _outcome_from_error_type(error_type: str) -> str:
    if error_type == "cancelled":
        return "CANCELED"
    if error_type == "timeout":
        return "TIMED_OUT"
    return "ERROR"


class _MetricsWorkflowInboundInterceptor(WorkflowInboundInterceptor):
    async def execute_workflow(self, input: ExecuteWorkflowInput) -> Any:
        if workflow.unsafe.is_replaying():
            return await self.next.execute_workflow(input)

        info = workflow.info()
        attrs = {
            "temporal.workflow.type": info.workflow_type or "",
        }
        start_ns = time.monotonic_ns()
        status = "OK"
        try:
            return await self.next.execute_workflow(input)
        except BaseException as exc:
            status = _outcome_from_error_type(classify_error(exc))
            raise
        finally:
            duration_s = (time.monotonic_ns() - start_ns) / 1_000_000_000
            tagged = {**attrs, "otel.status_code": status}
            try:
                _workflow_executions().add(1, tagged)
                _workflow_duration().record(duration_s, tagged)
            except Exception:  # noqa: S110 — best-effort observability; never block the workflow on metric emission
                pass


class _MetricsActivityInboundInterceptor(ActivityInboundInterceptor):
    async def execute_activity(self, input: ExecuteActivityInput) -> Any:
        info = activity.info()
        attrs = {
            "temporal.activity.type": info.activity_type or "",
            "temporal.task_queue": info.task_queue or "",
        }
        start_ns = time.monotonic_ns()
        status = "OK"
        error_type = ""
        try:
            return await self.next.execute_activity(input)
        except BaseException as exc:
            error_type = classify_error(exc)
            status = _outcome_from_error_type(error_type)
            raise
        finally:
            duration_s = (time.monotonic_ns() - start_ns) / 1_000_000_000
            tagged = {**attrs, "otel.status_code": status}
            try:
                _activity_executions().add(1, tagged)
                _activity_duration().record(duration_s, tagged)
                if status != "OK":
                    _activity_errors().add(
                        1,
                        {
                            "temporal.activity.type": attrs["temporal.activity.type"],
                            "exception.type": error_type or "Unknown",
                        },
                    )
            except Exception:  # noqa: S110 — best-effort observability; never block the activity on metric emission
                pass


class MetricsInterceptor(Interceptor):
    """Emits OTel counters / histograms for workflow + activity execution.

    Instruments:
      * ``temporal.workflow.executions`` (counter)
      * ``temporal.workflow.duration`` (histogram, seconds)
      * ``temporal.activity.executions`` (counter)
      * ``temporal.activity.duration`` (histogram, seconds)
      * ``temporal.activity.errors`` (counter, partitioned by ``exception.type``)

    Independent of, and complementary to, Temporal's Rust-core metrics —
    those measure scheduling / cache / poll latencies; these measure business
    completion outcomes.
    """

    def workflow_interceptor_class(
        self,
        input: WorkflowInterceptorClassInput,
    ) -> type[WorkflowInboundInterceptor] | None:
        return _MetricsWorkflowInboundInterceptor

    def intercept_activity(
        self, next: ActivityInboundInterceptor
    ) -> ActivityInboundInterceptor:
        return _MetricsActivityInboundInterceptor(next)
