"""Unified Log interceptor for Temporal workflows and activities.

Folds the work of four legacy interceptors into one:

* ``ExecutionContextInterceptor`` — sets the ``ExecutionContext`` ContextVar
  at workflow / activity inbound so user code and downstream interceptors can
  read Temporal context without calling ``temporalio.workflow.info()`` /
  ``temporalio.activity.info()``.
* ``CorrelationContextInterceptor`` — reads ``x-correlation-id`` (and the
  legacy ``correlation_id``) header at inbound, generates a fresh ID on
  top-level workflows, restores from memo on continue-as-new, and injects the
  header on outbound ``start_activity`` / ``start_child_workflow``.
* ``TaskFailureLoggingInterceptor`` — folded into the ``activity.ended``
  failure log.
* ``AppVitalsInterceptor`` — replaced by the four lifecycle log lines below
  (``workflow.started``, ``workflow.ended``, ``activity.started``,
  ``activity.ended``) emitted with OTel semantic-convention attributes.
"""

from __future__ import annotations

import dataclasses
import time
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from temporalio import activity, workflow
from temporalio.converter import default as default_converter
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

from application_sdk.observability.context import (
    ExecutionContext,
    set_execution_context,
)
from application_sdk.observability.correlation import (
    CorrelationContext,
    get_correlation_context,
    set_correlation_context,
)
from application_sdk.observability.logger_adaptor import get_logger

if TYPE_CHECKING:
    from collections.abc import Mapping

    from temporalio.api.common.v1 import Payload

logger = get_logger(__name__)

_HEADER_CORRELATION_ID = "x-correlation-id"
# Legacy header used by the AE's older interceptor — kept for compatibility
# so AE-dispatched workflows inherit correlation_id without AE-side changes.
_HEADER_CORRELATION_ID_LEGACY = "correlation_id"


def _correlation_id_or_empty() -> str:
    ctx = get_correlation_context()
    return ctx.correlation_id if ctx and ctx.correlation_id else ""


# ---------------------------------------------------------------------------
# Workflow side
# ---------------------------------------------------------------------------


class _LogWorkflowOutboundInterceptor(WorkflowOutboundInterceptor):
    """Inject ``x-correlation-id`` into outbound workflow → activity / child
    workflow calls."""

    def __init__(
        self,
        next_: WorkflowOutboundInterceptor,
        inbound: _LogWorkflowInboundInterceptor,
    ) -> None:
        super().__init__(next_)
        self._inbound = inbound

    def _inject(self, headers: Mapping[str, Payload]) -> dict[str, Payload]:
        correlation_id = self._inbound._correlation_id
        if not correlation_id:
            return dict(headers)
        try:
            converter = default_converter().payload_converter
            new_headers: dict[str, Payload] = dict(headers)
            new_headers[_HEADER_CORRELATION_ID] = converter.to_payload(correlation_id)
            return new_headers
        except Exception:
            logger.warning("Failed to inject correlation header", exc_info=True)
            return dict(headers)

    def start_activity(self, input: StartActivityInput) -> Any:
        return self.next.start_activity(
            dataclasses.replace(input, headers=self._inject(input.headers))
        )

    def start_child_workflow(self, input: StartChildWorkflowInput) -> Any:
        return self.next.start_child_workflow(
            dataclasses.replace(input, headers=self._inject(input.headers))
        )


class _LogWorkflowInboundInterceptor(WorkflowInboundInterceptor):
    """Workflow inbound: set ContextVars, emit ``workflow.started`` /
    ``workflow.ended`` log lines, propagate correlation_id outbound."""

    def __init__(self, next_: WorkflowInboundInterceptor) -> None:
        super().__init__(next_)
        self._correlation_id: str = ""

    def init(self, outbound: WorkflowOutboundInterceptor) -> None:
        super().init(_LogWorkflowOutboundInterceptor(outbound, self))

    def _resolve_correlation_id(self, input: ExecuteWorkflowInput) -> str:
        # Priority 1: restore from memo (continue-as-new).
        try:
            memo = dict(workflow.memo())
            ctx = CorrelationContext.from_temporal_memo(memo)
            if ctx and ctx.correlation_id:
                return ctx.correlation_id
        except Exception:
            logger.warning(
                "Failed to restore correlation context from memo", exc_info=True
            )

        # Priority 2: inherit from headers (child workflow path).
        try:
            for hdr_key in (_HEADER_CORRELATION_ID, _HEADER_CORRELATION_ID_LEGACY):
                payload = input.headers.get(hdr_key)
                if payload is not None:
                    converter = default_converter().payload_converter
                    correlation_id: str = converter.from_payload(payload, type_hint=str)
                    if correlation_id:
                        return correlation_id
        except Exception:
            logger.warning(
                "Failed to read correlation header in workflow", exc_info=True
            )

        # Priority 3: top-level workflow — generate a fresh correlation ID.
        return str(uuid4())

    async def execute_workflow(self, input: ExecuteWorkflowInput) -> Any:
        # During Temporal replay the workflow code re-executes for determinism;
        # observability events would double-count, so we skip everything.
        if workflow.unsafe.is_replaying():
            return await self.next.execute_workflow(input)

        info = workflow.info()

        set_execution_context(
            ExecutionContext(
                execution_type="workflow",
                workflow_id=info.workflow_id or "",
                workflow_run_id=info.run_id or "",
                workflow_type=info.workflow_type or "",
                namespace=info.namespace or "",
                task_queue=info.task_queue or "",
                attempt=info.attempt or 0,
            )
        )

        correlation_id = self._resolve_correlation_id(input)
        self._correlation_id = correlation_id
        set_correlation_context(CorrelationContext(correlation_id=correlation_id))

        identity = {
            "temporal.workflow.id": info.workflow_id or "",
            "temporal.workflow.run_id": info.run_id or "",
            "temporal.workflow.type": info.workflow_type or "",
            "temporal.task_queue": info.task_queue or "",
            "temporal.namespace": info.namespace or "",
            "atlan.correlation_id": correlation_id,
        }

        try:
            logger.info("workflow.started", **identity)
        except Exception:  # noqa: S110 — best-effort observability; never block the workflow on logging
            pass

        start_ns = time.monotonic_ns()
        status = "OK"
        try:
            return await self.next.execute_workflow(input)
        except Exception:
            status = "ERROR"
            raise
        finally:
            duration_ms = round((time.monotonic_ns() - start_ns) / 1_000_000, 1)
            ended_attrs = {
                **identity,
                "otel.status_code": status,
                "temporal.workflow.duration_ms": duration_ms,
            }
            try:
                if status == "ERROR":
                    logger.error("workflow.ended", exc_info=True, **ended_attrs)
                else:
                    logger.info("workflow.ended", **ended_attrs)
            except Exception:  # noqa: S110 — best-effort observability; never block the workflow on logging
                pass


# ---------------------------------------------------------------------------
# Activity side
# ---------------------------------------------------------------------------


class _LogActivityInboundInterceptor(ActivityInboundInterceptor):
    """Activity inbound: set ContextVars, read correlation header, emit
    ``activity.started`` / ``activity.ended`` log lines."""

    async def execute_activity(self, input: ExecuteActivityInput) -> Any:
        info = activity.info()

        set_execution_context(
            ExecutionContext(
                execution_type="activity",
                workflow_id=info.workflow_id or "",
                workflow_run_id=info.workflow_run_id or "",
                activity_id=info.activity_id or "",
                activity_type=info.activity_type or "",
                task_queue=info.task_queue or "",
                attempt=info.attempt or 0,
            )
        )

        correlation_id = ""
        try:
            for hdr_key in (_HEADER_CORRELATION_ID, _HEADER_CORRELATION_ID_LEGACY):
                payload = input.headers.get(hdr_key)
                if payload is not None:
                    converter = default_converter().payload_converter
                    correlation_id = converter.from_payload(payload, type_hint=str)
                    if correlation_id:
                        set_correlation_context(
                            CorrelationContext(correlation_id=correlation_id)
                        )
                        break
        except Exception:
            logger.warning(
                "Failed to read correlation header in activity", exc_info=True
            )

        if not correlation_id:
            correlation_id = _correlation_id_or_empty()

        identity = {
            "temporal.activity.id": info.activity_id or "",
            "temporal.activity.type": info.activity_type or "",
            "temporal.activity.attempt": str(info.attempt or 0),
            "temporal.task_queue": info.task_queue or "",
            "temporal.namespace": getattr(info, "namespace", "") or "",
            "temporal.workflow.id": info.workflow_id or "",
            "temporal.workflow.run_id": info.workflow_run_id or "",
            "temporal.workflow.type": info.workflow_type or "",
            "atlan.correlation_id": correlation_id,
        }

        try:
            logger.info("activity.started", **identity)
        except Exception:  # noqa: S110 — best-effort observability; never block the activity on logging
            pass

        start_ns = time.monotonic_ns()
        status = "OK"
        try:
            return await self.next.execute_activity(input)
        except BaseException:
            status = "ERROR"
            raise
        finally:
            duration_ms = round((time.monotonic_ns() - start_ns) / 1_000_000, 1)
            ended_attrs = {
                **identity,
                "otel.status_code": status,
                "temporal.activity.duration_ms": duration_ms,
            }
            try:
                if status == "ERROR":
                    logger.error("activity.ended", exc_info=True, **ended_attrs)
                else:
                    logger.info("activity.ended", **ended_attrs)
            except Exception:  # noqa: S110 — best-effort observability; never block the activity on logging
                pass


# ---------------------------------------------------------------------------
# Public interceptor
# ---------------------------------------------------------------------------


class LogInterceptor(Interceptor):
    """Unified observability logging interceptor.

    Emits four lifecycle log lines per execution — ``workflow.started``,
    ``workflow.ended``, ``activity.started``, ``activity.ended`` — with
    OpenTelemetry semantic-convention attributes. Also sets the
    ``ExecutionContext`` and ``CorrelationContext`` ContextVars for downstream
    code, and propagates ``x-correlation-id`` across activity / child-workflow
    boundaries via Temporal headers.
    """

    def workflow_interceptor_class(
        self,
        input: WorkflowInterceptorClassInput,
    ) -> type[WorkflowInboundInterceptor] | None:
        return _LogWorkflowInboundInterceptor

    def intercept_activity(
        self, next: ActivityInboundInterceptor
    ) -> ActivityInboundInterceptor:
        return _LogActivityInboundInterceptor(next)
