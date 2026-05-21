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
from temporalio.exceptions import ApplicationError as _TemporalApplicationError
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

from application_sdk.errors.base import AppError
from application_sdk.errors.wire import FailureDetails
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


def _extract_failure_attrs(exc: BaseException | None) -> dict[str, str]:
    """Flatten SDK error classification onto OTel attributes for ERROR logs.

    Walks ``__cause__`` and ``__context__`` looking for either:
      * an :class:`application_sdk.errors.base.AppError` (raised directly), or
      * a ``temporalio.exceptions.ApplicationError`` whose first ``details``
        entry carries the :class:`application_sdk.errors.wire.FailureDetails`
        envelope — either as a live model (activity side, before serde) or as
        a deserialized mapping (workflow side, after activity → workflow
        boundary, where ``pydantic_data_converter`` round-trips the envelope
        as JSON and ``ApplicationError.details`` is reconstructed without
        the typed model since ``details`` is annotated ``Sequence[Any]``).

    Returns ``{"failure.category", "failure.audience", "failure.code"}`` —
    OTel attribute keys that ride the ``failure.`` passthrough prefix in
    ``logger_adaptor``. Empty dict when no SDK classification is recoverable
    (e.g. raw ``ValueError`` or non-SDK exception); callers append the result
    to ``ended_attrs`` unconditionally and the log line simply omits the keys.
    """
    if exc is None:
        return {}
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, AppError):
            return {
                "failure.category": current.category.value,
                "failure.audience": type(current).audience.value,
                "failure.code": current.code,
            }
        if isinstance(current, _TemporalApplicationError):
            for detail in getattr(current, "details", None) or ():
                attrs = _failure_details_from_detail(detail)
                if attrs:
                    return attrs
        current = current.__cause__ or current.__context__
    return {}


def _failure_details_from_detail(detail: Any) -> dict[str, str]:
    """Recover ``{category, audience, code}`` from one ``ApplicationError.details`` entry.

    Two shapes are accepted:

    1. A live :class:`FailureDetails` Pydantic model — emitted at the activity
       raise site (``activities.py``).
    2. A mapping with ``category`` / ``audience`` / ``code`` keys — what
       Temporal's ``pydantic_data_converter`` reconstructs on the workflow
       side after the activity → workflow boundary. ``ApplicationError.details``
       is annotated ``Sequence[Any]`` so the converter has no type hint to
       rehydrate the Pydantic model; it returns the raw JSON object instead.

    Returns an empty dict for any other shape so the caller falls through to
    the next link in the ``__cause__`` chain.
    """
    if isinstance(detail, FailureDetails):
        return {
            "failure.category": detail.category.value,
            "failure.audience": detail.audience.value,
            "failure.code": detail.code,
        }
    if isinstance(detail, dict):
        category = detail.get("category")
        audience = detail.get("audience")
        code = detail.get("code")
        # Enum members serialize as their ``.value`` (str); accept either the
        # string form or a live enum instance (defensive — covers callers that
        # construct ApplicationError with a half-converted dict).
        cat_value = getattr(category, "value", category)
        aud_value = getattr(audience, "value", audience)
        if (
            isinstance(cat_value, str)
            and isinstance(aud_value, str)
            and isinstance(code, str)
        ):
            return {
                "failure.category": cat_value,
                "failure.audience": aud_value,
                "failure.code": code,
            }
    return {}


_HEADER_CORRELATION_ID = "x-correlation-id"
# Legacy header used by the AE's older interceptor — kept for compatibility
# so AE-dispatched workflows inherit correlation_id without AE-side changes.
_HEADER_CORRELATION_ID_LEGACY = "correlation_id"

# Activity-only: workflow's own ``info.parent`` propagated to activity inbound
# so activity logs carry the same ``parent_workflow_id`` / ``parent_run_id`` as
# the workflow that scheduled them. Child workflows don't need this — Temporal
# sets ``workflow.info().parent`` natively from the parent-child relationship.
_HEADER_PARENT_WORKFLOW_ID = "atlan-parent-workflow-id"
_HEADER_PARENT_RUN_ID = "atlan-parent-run-id"


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
        # All read from the workflow-execution-scoped inbound interceptor
        # instance — Temporal creates a fresh inbound per workflow execution,
        # so these attrs are isolated across concurrent workflows on the
        # same worker (see _workflow_instance.py:390).
        correlation_id = self._inbound._correlation_id
        parent_workflow_id = self._inbound._parent_workflow_id
        parent_run_id = self._inbound._parent_run_id

        if not correlation_id and not parent_workflow_id and not parent_run_id:
            return dict(headers)
        try:
            converter = default_converter().payload_converter
            new_headers: dict[str, Payload] = dict(headers)
            if correlation_id:
                new_headers[_HEADER_CORRELATION_ID] = converter.to_payload(
                    correlation_id
                )
            if parent_workflow_id:
                new_headers[_HEADER_PARENT_WORKFLOW_ID] = converter.to_payload(
                    parent_workflow_id
                )
            if parent_run_id:
                new_headers[_HEADER_PARENT_RUN_ID] = converter.to_payload(parent_run_id)
            return new_headers
        except Exception:
            logger.warning("Failed to inject correlation/parent headers", exc_info=True)
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
        # Cached on entry; the outbound interceptor reads these to inject
        # ``atlan-parent-*`` headers on activities so they inherit the same
        # parent identity. Per-workflow-execution (Temporal creates a fresh
        # interceptor instance per workflow run).
        self._parent_workflow_id: str = ""
        self._parent_run_id: str = ""

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

        # Priority 3: legacy args-based propagation. The pre-v3
        # CorrelationContextInterceptor read ``correlation_id`` from the
        # first workflow argument when it was a dict; many existing callers
        # (notably the automation-engine on SDK 2.8.7) still rely on this
        # convention and have no way to inject memo / header on workflow
        # start. Reading args here keeps those callers' correlation chains
        # intact without forcing each one to migrate immediately.
        #
        # Three shapes covered, in order:
        #   1. ``args[0]`` is a ``dict`` → ``correlation_id`` key. Plain
        #      dict-shaped configs (AE 2.8.7, scripted starts, tests).
        #   2. ``args[0]`` is a typed object (Pydantic model, dataclass,
        #      namespace) with a ``correlation_id`` attribute. Catches v3
        #      SDK-generated workflow wrappers whose ``run(input: Input)``
        #      converted the caller's dict into a typed Input before the
        #      interceptor was called.
        #   3. ``args[0]`` is a Pydantic v2 model with ``extra='allow'`` and
        #      ``correlation_id`` ended up in ``__pydantic_extra__`` because
        #      the field wasn't declared on the model. Pydantic still
        #      preserves it on the instance even though it's not a typed
        #      attribute.
        #
        # Falls through silently for any other shape — primitives, models
        # without the field and ``extra='ignore'`` (default), etc. Those
        # callers should use memo / header at start time, which are the
        # preferred OTel-aligned channels.
        try:
            if input.args:
                first = input.args[0]
                cid: str | None = None
                if isinstance(first, dict):
                    cid = first.get("correlation_id")
                else:
                    raw = getattr(first, "correlation_id", None)
                    if not raw:
                        extras = getattr(first, "__pydantic_extra__", None)
                        if isinstance(extras, dict):
                            raw = extras.get("correlation_id")
                    if raw:
                        cid = str(raw)
                if cid:
                    return str(cid)
        except Exception:
            logger.warning(
                "Failed to read correlation_id from workflow args", exc_info=True
            )

        # Priority 4: top-level workflow — generate a fresh correlation ID.
        return str(uuid4())

    async def execute_workflow(self, input: ExecuteWorkflowInput) -> Any:
        # State setup (ContextVars + interceptor-instance attrs) must run on
        # every replay, not just the first execution: the outbound interceptor
        # reads ``self._correlation_id`` / ``self._parent_*`` to inject
        # ``x-correlation-id`` and ``atlan-parent-*`` headers on workflow-issued
        # commands. A fresh worker that picks up an in-flight workflow rebuilds
        # state by replaying history with ``is_replaying() == True``; if we
        # short-circuit here, those instance attrs stay at their ``__init__``
        # defaults and outbound commands issued post-replay lose the headers,
        # breaking the correlation chain at child-workflow / activity calls.
        # Only the side-effectful log emission (workflow.started /
        # workflow.ended) is gated on ``is_replaying()`` to avoid double-count.
        info = workflow.info()
        parent = getattr(info, "parent", None)
        self._parent_workflow_id = (parent.workflow_id if parent else "") or ""
        self._parent_run_id = (parent.run_id if parent else "") or ""

        set_execution_context(
            ExecutionContext(
                execution_type="workflow",
                workflow_id=info.workflow_id or "",
                workflow_run_id=info.run_id or "",
                workflow_type=info.workflow_type or "",
                namespace=info.namespace or "",
                task_queue=info.task_queue or "",
                attempt=info.attempt or 0,
                parent_workflow_id=self._parent_workflow_id,
                parent_run_id=self._parent_run_id,
            )
        )

        correlation_id = self._resolve_correlation_id(input)
        self._correlation_id = correlation_id
        set_correlation_context(CorrelationContext(correlation_id=correlation_id))

        if workflow.unsafe.is_replaying():
            return await self.next.execute_workflow(input)

        identity: dict[str, str | int | float] = {
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
        exc_caught: BaseException | None = None
        try:
            return await self.next.execute_workflow(input)
        except Exception as e:
            status = "ERROR"
            exc_caught = e
            raise
        finally:
            duration_ms = round((time.monotonic_ns() - start_ns) / 1_000_000, 1)
            ended_attrs: dict[str, str | int | float] = {
                **identity,
                "otel.status_code": status,
                "temporal.workflow.duration_ms": duration_ms,
            }
            try:
                if status == "ERROR":
                    ended_attrs.update(_extract_failure_attrs(exc_caught))
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

        # Read parent identity from headers injected by the workflow's
        # outbound interceptor. ``activity.Info`` itself doesn't expose
        # parent info, so we propagate it via Temporal headers.
        parent_workflow_id = ""
        parent_run_id = ""
        try:
            converter = default_converter().payload_converter
            payload = input.headers.get(_HEADER_PARENT_WORKFLOW_ID)
            if payload is not None:
                parent_workflow_id = converter.from_payload(payload, type_hint=str)
            payload = input.headers.get(_HEADER_PARENT_RUN_ID)
            if payload is not None:
                parent_run_id = converter.from_payload(payload, type_hint=str)
        except Exception:
            logger.warning(
                "Failed to read parent identity headers in activity", exc_info=True
            )

        set_execution_context(
            ExecutionContext(
                execution_type="activity",
                workflow_id=info.workflow_id or "",
                workflow_run_id=info.workflow_run_id or "",
                activity_id=info.activity_id or "",
                activity_type=info.activity_type or "",
                task_queue=info.task_queue or "",
                attempt=info.attempt or 0,
                parent_workflow_id=parent_workflow_id,
                parent_run_id=parent_run_id,
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

        identity: dict[str, str | int | float] = {
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
        exc_caught: BaseException | None = None
        try:
            return await self.next.execute_activity(input)
        except BaseException as e:
            status = "ERROR"
            exc_caught = e
            raise
        finally:
            duration_ms = round((time.monotonic_ns() - start_ns) / 1_000_000, 1)
            ended_attrs: dict[str, str | int | float] = {
                **identity,
                "otel.status_code": status,
                "temporal.activity.duration_ms": duration_ms,
            }
            try:
                if status == "ERROR":
                    ended_attrs.update(_extract_failure_attrs(exc_caught))
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
