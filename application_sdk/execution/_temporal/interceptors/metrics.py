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
from typing import Any, Final

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

from application_sdk.errors.categories import Audience, FailureCategory
from application_sdk.execution._temporal.interceptors.log import _extract_failure_attrs
from application_sdk.observability import resource_sampler

_METER_NAME = "application_sdk.temporal"

# ---------------------------------------------------------------------------
# failure.classified — the closed vocabulary that separates a real verdict from
# the INTERNAL / APP_OWNER fallback.
# ---------------------------------------------------------------------------

CLASSIFIED_TYPED: Final = "typed"
"""The failure carried SDK-typed ``FailureDetails``. ``failure.category`` and
``failure.audience`` are the error's own declared values — a verdict somebody made."""

CLASSIFIED_UNTYPED: Final = "untyped"
"""No SDK-typed failure was recoverable (raw ``ValueError``, third-party exception).
``failure.category`` / ``failure.audience`` carry the ``INTERNAL`` / ``APP_OWNER``
fallback, which is a default rather than a classification.

Without this label the ``APP_OWNER`` bucket mixes two unrelated populations — real
app bugs, and code that never adopted typed errors — so the bucket moves when an app
adopts ``application_sdk.errors``, not when its reliability changes. Splitting on this
label is what makes an ``APP_OWNER`` rate interpretable, and
``sum(...{failure_classified="untyped"}) / sum(...)`` is the fleet's instrumentation
coverage measured from live traffic."""

FAILURE_CLASSIFIED_VALUES: Final[frozenset[str]] = frozenset(
    {CLASSIFIED_TYPED, CLASSIFIED_UNTYPED}
)
"""Runtime membership test for the two ``failure.classified`` values."""


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


def _workflow_failures_classified():
    if "wf_fail_cls" not in _INSTRUMENTS:
        _INSTRUMENTS["wf_fail_cls"] = _meter().create_counter(
            "temporal.workflow.failures.classified",
            unit="1",
            description=(
                "Workflow failures partitioned by failure.category, "
                "failure.audience and failure.classified. Emitted for every "
                "failure across all audiences (USER, PLATFORM, APP_OWNER); "
                "consumers filter at query time — e.g. drop USER for actionable "
                "alerts, or keep USER for customer-failure dashboards. "
                "failure.classified is typed|untyped: untyped means no SDK-typed "
                "failure was recoverable and category/audience are the "
                "INTERNAL/APP_OWNER fallback, so split APP_OWNER on it before "
                "reading it as a defect rate."
            ),
        )
    return _INSTRUMENTS["wf_fail_cls"]


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


def _activity_cpu_seconds():
    if "act_cpu" not in _INSTRUMENTS:
        _INSTRUMENTS["act_cpu"] = _meter().create_histogram(
            "temporal.activity.cpu_seconds",
            unit="s",
            description=(
                "CPU time (user + system) attributed to one activity execution. "
                "The process's CPU is sampled at every activity boundary and "
                "split evenly across the activities in flight, so values sum to "
                "the process's busy CPU and never exceed it. Exact for a worker "
                "running one activity at a time; a fair share, not a "
                "measurement, under concurrency."
            ),
        )
    return _INSTRUMENTS["act_cpu"]


def _activity_mem_gib_seconds():
    if "act_mem" not in _INSTRUMENTS:
        _INSTRUMENTS["act_mem"] = _meter().create_histogram(
            "temporal.activity.mem_gib_seconds",
            unit="GiBy.s",
            description=(
                "Memory-time integral attributed to one activity execution: "
                "process RSS in GiB integrated over wall-clock seconds, split "
                "evenly across the activities in flight. Same fair-share "
                "attribution as temporal.activity.cpu_seconds."
            ),
        )
    return _INSTRUMENTS["act_mem"]


def _classify_failure(exc: BaseException | None) -> dict[str, str]:
    """Extract bounded {category, audience, classified} labels for the counter.

    Reuses :func:`_extract_failure_attrs` from the log interceptor so log and
    metric paths agree on classification. When extraction yields no SDK-typed
    failure (raw ``ValueError`` / 3rd-party exception), falls back to
    ``INTERNAL`` / ``APP_OWNER`` per the SDK doctrine that unowned failures
    default to the app team (see :class:`Audience` docstring).

    ``failure.classified`` records **which of those two branches ran**, because the
    labels are otherwise indistinguishable: a deliberate :class:`InternalError` and
    an unadopted app's raw ``ValueError`` both emit ``INTERNAL`` / ``APP_OWNER``.
    One is a bug to fix, the other is instrumentation debt, and a consumer that
    cannot tell them apart cannot set a target on either. See
    :data:`CLASSIFIED_UNTYPED`.
    """
    attrs = _extract_failure_attrs(exc)
    if attrs:
        return {
            "failure.category": attrs["failure.category"],
            "failure.audience": attrs["failure.audience"],
            "failure.classified": CLASSIFIED_TYPED,
        }
    return {
        "failure.category": FailureCategory.INTERNAL.value,
        "failure.audience": Audience.APP_OWNER.value,
        "failure.classified": CLASSIFIED_UNTYPED,
    }


def _begin_share(now_ns: int) -> resource_sampler.ShareMark | None:
    """Open this activity's slot in the process share ledger. Never raises."""
    # conformance: ignore[E004] best-effort observability; a sampling failure must never fail the activity it measures
    try:
        return resource_sampler.ACTIVITY_SHARES.begin(now_ns / 1_000_000_000)
    # conformance: ignore[E004] best-effort observability; a sampling failure must never fail the activity it measures
    except Exception:
        return None


def _end_share(
    mark: resource_sampler.ShareMark | None, now_ns: int
) -> tuple[float, float] | None:
    """Close the slot opened by :func:`_begin_share`; its ``(cpu_s, mem_gib_s)``."""
    if mark is None:
        return None
    # conformance: ignore[E004] best-effort observability; a sampling failure must never fail the activity it measures
    try:
        return resource_sampler.ACTIVITY_SHARES.end(mark, now_ns / 1_000_000_000)
    # conformance: ignore[E004] best-effort observability; a sampling failure must never fail the activity it measures
    except Exception:
        return None


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
        exc_caught: BaseException | None = None
        try:
            return await self.next.execute_workflow(input)
        # conformance: ignore[E004] measurement wrapper; re-raises immediately after capturing exc for metric tagging
        except BaseException as exc:
            status = "ERROR"
            exc_caught = exc
            raise
        finally:
            duration_s = (time.monotonic_ns() - start_ns) / 1_000_000_000
            tagged = {**attrs, "otel.status_code": status}
            # conformance: ignore[E004] best-effort observability; metric emission must never block or raise in a workflow finally block
            try:
                _workflow_executions().add(1, tagged)
                _workflow_duration().record(duration_s, tagged)
                if status == "ERROR":
                    classified = _classify_failure(exc_caught)
                    _workflow_failures_classified().add(
                        1,
                        {**attrs, **classified},
                    )
            # conformance: ignore[E004] best-effort observability; metric emission must never block or raise in a workflow finally block
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
        share_mark = _begin_share(start_ns)
        status = "OK"
        exception_class = ""
        try:
            return await self.next.execute_activity(input)
        # conformance: ignore[E004] measurement wrapper; re-raises immediately after capturing exc class for metric tagging
        except BaseException as exc:
            status = "ERROR"
            exception_class = type(exc).__name__
            raise
        finally:
            end_ns = time.monotonic_ns()
            duration_s = (end_ns - start_ns) / 1_000_000_000
            # Before any emission: the in-flight slot must be released even if
            # emission fails, or every later activity's share is diluted.
            shares = _end_share(share_mark, end_ns)
            tagged = {**attrs, "otel.status_code": status}
            # conformance: ignore[E004] best-effort observability; metric emission must never block or raise in an activity finally block
            try:
                _activity_executions().add(1, tagged)
                _activity_duration().record(duration_s, tagged)
                if status != "OK":
                    _activity_errors().add(
                        1,
                        {
                            "temporal.activity.type": attrs["temporal.activity.type"],
                            "exception.type": exception_class or "Unknown",
                        },
                    )
                if shares is not None:
                    cpu_s, mem_gib_s = shares
                    _activity_cpu_seconds().record(cpu_s, tagged)
                    _activity_mem_gib_seconds().record(mem_gib_s, tagged)
            # conformance: ignore[E004] best-effort observability; metric emission must never block or raise in an activity finally block
            except Exception:  # noqa: S110 — best-effort observability; never block the activity on metric emission
                pass


class MetricsInterceptor(Interceptor):
    """Emits OTel counters / histograms for workflow + activity execution.

    Instruments:
      * ``temporal.workflow.executions`` (counter)
      * ``temporal.workflow.duration`` (histogram, seconds)
      * ``temporal.workflow.failures.classified`` (counter, partitioned by
        ``failure.category`` and ``failure.audience`` — consumers filter at
        query time, e.g. drop ``failure_audience="USER"`` for actionable
        alerts)
      * ``temporal.activity.executions`` (counter)
      * ``temporal.activity.duration`` (histogram, seconds)
      * ``temporal.activity.errors`` (counter, partitioned by ``exception.type``)
      * ``temporal.activity.cpu_seconds`` (histogram) — the activity's fair share
        of process CPU time (see :class:`~application_sdk.observability.resource_sampler.ActivityShareLedger`)
      * ``temporal.activity.mem_gib_seconds`` (histogram) — the activity's fair
        share of the process memory-time integral

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
