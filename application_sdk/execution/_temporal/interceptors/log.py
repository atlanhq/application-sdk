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
import posixpath
import time
import traceback as tb_module
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
from application_sdk.execution._temporal.preflight_gate import is_preflight_block
from application_sdk.observability.context import (
    ExecutionContext,
    set_execution_context,
    set_replay_predicate,
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


# Must match _MAX_CHAIN_DEPTH in activities.py: an AppError sitting between the
# walk cap and the sever cap would be silently invisible to OTel attributes.
_MAX_CHAIN_WALK = 50

# Cap the exception message folded into a lifecycle message body — the full
# text still ships in exception.message / the traceback in exception.stacktrace.
_FAILURE_MSG_MAX_CHARS = 200

# Cap on the resolved app_name (CNCT-93). app_name used to be a per-process env
# constant; now it is per-run data read from args[0] — an untrusted boundary
# (anything with Temporal access can start a workflow) — and it is stamped on
# every log line and OTLP log attribute. Bound it here — reject non-strings,
# cap length — rather than trusting every caller. (Metric labels are NOT fed
# from this value; get_metric_labels() stays on the env constant by design.)
_APP_NAME_MAX_CHARS = 64


def _lifecycle_message(event: str, subject: str) -> str:
    """Build a lifecycle log message body (CNCT-105).

    The event token (``activity.ended`` …) is the **exact message prefix** —
    the stable, greppable contract for downstream consumers — and the
    subject makes the line self-describing: before this, every lifecycle
    line rendered as a bare token and a reader could not tell *which* task
    started or *why* one failed without the (dropped) structured attributes.

    The task-level tokens are ``activity.started``/``activity.ended`` (Temporal's
    activity vocabulary — the unit an app author writes as a ``@task`` runs as a
    Temporal activity); the workflow-level tokens are ``workflow.started``/
    ``workflow.ended``. These literals are the stable contract operators match on;
    see ``docs/concepts/monitoring.md`` (Lifecycle log lines).
    """
    return f"{event} {subject}".rstrip() if subject else event


def _failure_suffix(exc: BaseException | None, attrs: dict[str, Any]) -> str:
    """One-line failure summary for a lifecycle ERROR message body.

    Shape: ``FAILED (<failure.code|exception type>): <message> — at
    <file>:<line> in <fn>``. The root-cause frame is the innermost traceback
    frame; the full stacktrace still rides ``exc_info=True`` → OTel
    ``exception.stacktrace``. Deterministic (string handling only) — safe in
    the Temporal workflow sandbox.
    """
    code = str(attrs.get("failure.code") or "") or (
        type(exc).__name__ if exc is not None else "unknown"
    )
    msg = ""
    if exc is not None:
        # ``or [""]`` guards a whitespace-only message ("\n"), where strip()
        # empties the string and splitlines() yields [] — an IndexError here
        # would be swallowed by the caller's best-effort guard and take the
        # whole ended log with it.
        first_line = (str(exc).strip().splitlines() or [""])[0]
        msg = first_line[:_FAILURE_MSG_MAX_CHARS]
    frame = ""
    try:
        if exc is not None and exc.__traceback__ is not None:
            last = tb_module.extract_tb(exc.__traceback__)[-1]
            frame = (
                f" — at {posixpath.basename(str(last.filename))}"
                f":{last.lineno} in {last.name}"
            )
    # conformance: ignore[E004] best-effort message enrichment; a broken traceback must never block the ended log
    except Exception:  # noqa: S110 — degrade to code+message, never drop the log
        pass
    return f"FAILED ({code}): {msg}{frame}" if msg else f"FAILED ({code}){frame}"


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
    depth = 0
    while current is not None and id(current) not in seen and depth < _MAX_CHAIN_WALK:
        seen.add(id(current))
        depth += 1
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

    Accepts either a live :class:`FailureDetails` Pydantic model (activity side,
    pre-serde) or a plain dict (workflow side, post-serde — Temporal's
    ``pydantic_data_converter`` returns raw JSON objects for ``Sequence[Any]``
    fields). Returns an empty dict for any other shape.
    """
    if not isinstance(detail, (FailureDetails, dict)):
        return {}
    try:
        fd = (
            detail
            if isinstance(detail, FailureDetails)
            else FailureDetails.model_validate(detail)
        )
        return {
            "failure.category": fd.category.value,
            "failure.audience": fd.audience.value,
            "failure.code": fd.code,
        }
    # conformance: ignore[E004] probe that recovers FailureDetails from arbitrary detail shapes; exc_info already on the debug call below
    except Exception:
        logger.debug(
            "Failed to extract failure details for log enrichment", exc_info=True
        )
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

# Per-entrypoint app_name (CNCT-93), propagated workflow -> activity so activity
# logs carry the same app_name the workflow resolved from its own input args.
# Child workflows do NOT read this header (each resolves its own app_name from
# its own input), so there is no parent -> child inheritance.
_HEADER_APP_NAME = "x-app-name"


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
        app_name = self._inbound._app_name

        if (
            not correlation_id
            and not parent_workflow_id
            and not parent_run_id
            and not app_name
        ):
            return dict(headers)
        try:
            converter = default_converter().payload_converter
            new_headers: dict[str, Payload] = dict(headers)
            if correlation_id:
                new_headers[_HEADER_CORRELATION_ID] = converter.to_payload(
                    correlation_id
                )
            # Propagate the workflow's resolved app_name so its activities'
            # logs carry it. A child workflow will receive this header too but
            # ignores it (it resolves app_name from its own input args).
            if app_name:
                new_headers[_HEADER_APP_NAME] = converter.to_payload(app_name)
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
        # CNCT-93: the workflow's own app_name, resolved from its input args.
        # The outbound interceptor injects it into activity headers so activity
        # logs inherit it. Per-workflow-execution.
        self._app_name: str = ""

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

        # Priority 4: top-level workflow with no caller-supplied correlation —
        # default to this run's Temporal run_id. A random uuid4 here (the
        # pre-CNCT-104 behavior) produced an identity that existed nowhere
        # else in the platform: the run page queries logs by the caller's
        # correlation_id, so uuid4-stamped runs rendered as "no logs". The
        # run_id is at least discoverable from run metadata, and it matches
        # the documented invariant ("correlation_id defaults to the Temporal
        # run_id") that app/base.py and AppContext already implement — every
        # layer now agrees on the same fallback.
        try:
            run_id = workflow.info().run_id
            if run_id:
                return str(run_id)
        except Exception:
            logger.warning(
                "Failed to read workflow run_id for correlation fallback",
                exc_info=True,
            )
        # Defensive last resort — should be unreachable inside a workflow.
        return str(uuid4())

    def _resolve_app_name(self, input: ExecuteWorkflowInput) -> str:
        """Resolve this workflow's ``app_name`` from its own input args (CNCT-93).

        The contract toolkit emits each DAG node's ``app_name`` into that node's
        ``inputs.args``, so a multi-entrypoint bundle's crawler / miner / publish
        each carry their own value. We read it **only from the workflow's own
        first argument** — never from the memo or an inherited header — so there
        is no parent -> child inheritance (each entrypoint is its own workflow
        with its own input). Returns ``""`` when absent (older / not-yet-
        regenerated apps); the logger then falls back to ``ATLAN_APPLICATION_NAME``.

        Mirrors the args branch of :meth:`_resolve_correlation_id` — handles a
        dict first arg, a typed object with an ``app_name`` attribute, and a
        Pydantic v2 ``extra='allow'`` model where it lands in ``__pydantic_extra__``.

        The value is bounded at this boundary because it comes from an
        untrusted source (whatever started the workflow) and is stamped on
        every log line and OTLP log attribute. The two cases differ:

        * A non-string (or empty) value is **rejected** — returns ``""``, so the
          logger falls back to ``ATLAN_APPLICATION_NAME``. It is never coerced
          via ``str()``, which would stamp an arbitrary object's repr (e.g. a
          memory address) as the attribution value.
        * An oversized string is **truncated** to ``_APP_NAME_MAX_CHARS``, not
          rejected: a too-long name is still the right attribution, just
          abbreviated.

        Truncation is lossy, so two entrypoints sharing a 64-character prefix
        would collapse onto one attribution value. Harmless for today's short
        slugs (``powerbi-crawler``, ``powerbi-miner``), and the bound is worth
        more than the collision costs — but that is the tradeoff the cap makes.
        """
        try:
            if input.args:
                first = input.args[0]
                if isinstance(first, dict):
                    val = first.get("app_name")
                else:
                    val = getattr(first, "app_name", None)
                    if not val:
                        extras = getattr(first, "__pydantic_extra__", None)
                        if isinstance(extras, dict):
                            val = extras.get("app_name")
                if isinstance(val, str) and val:
                    return val[:_APP_NAME_MAX_CHARS]
        except Exception:
            logger.warning("Failed to read app_name from workflow args", exc_info=True)
        return ""

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

        # CNCT-93: resolve the per-entrypoint app_name from this workflow's own
        # input args (empty when absent) BEFORE building the ExecutionContext, so
        # it rides on that single shared context read by the logger. Cached on
        # the instance too, for the outbound interceptor to propagate to
        # activities via the x-app-name header.
        app_name = self._resolve_app_name(input)
        self._app_name = app_name

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
                app_name=app_name,
            )
        )

        correlation_id = self._resolve_correlation_id(input)
        self._correlation_id = correlation_id
        set_correlation_context(CorrelationContext(correlation_id=correlation_id))

        # Inject the live replay predicate so the SDK logger can suppress
        # workflow-body log emissions during replay without importing temporalio.
        # Uses ``is_replaying_history_events`` (not plain ``is_replaying``) to
        # match Temporal's own LoggerAdapter: it returns False during
        # read-only operations (queries, update validators) where
        # ``is_replaying`` may still be True, preventing over-suppression.
        set_replay_predicate(workflow.unsafe.is_replaying_history_events)

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
            started_msg = _lifecycle_message(
                "workflow.started", str(identity["temporal.workflow.type"])
            )
            logger.info(started_msg, **identity)
        # conformance: ignore[E004] best-effort observability guard; logging failure must never block workflow execution
        except Exception:  # noqa: S110 — best-effort observability; never block the workflow on logging
            pass

        start_ns = time.monotonic_ns()
        status = "OK"
        exc_caught: BaseException | None = None
        try:
            return await self.next.execute_workflow(input)
        # conformance: ignore[E004] captures exception for ended log enrichment then unconditionally re-raises; exc logged in finally block
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
                wf_type = str(identity["temporal.workflow.type"])
                if status == "ERROR":
                    ended_attrs.update(_extract_failure_attrs(exc_caught))
                    # A deliberate preflight-gate block is an expected, typed
                    # outcome, not a crash — log it terse (no stack). Its
                    # classification is already in ended_attrs via the failure
                    # details. Real failures keep the ERROR traceback.
                    if is_preflight_block(exc_caught):
                        blocked_msg = _lifecycle_message(
                            "workflow.ended", f"{wf_type} BLOCKED (preflight gate)"
                        )
                        logger.warning(blocked_msg, **ended_attrs)
                    else:
                        failed_msg = _lifecycle_message(
                            "workflow.ended",
                            f"{wf_type} {_failure_suffix(exc_caught, ended_attrs)}",
                        )
                        logger.error(failed_msg, exc_info=True, **ended_attrs)
                else:
                    ok_msg = _lifecycle_message(
                        "workflow.ended", f"{wf_type} OK ({duration_ms}ms)"
                    )
                    logger.info(ok_msg, **ended_attrs)
            # conformance: ignore[E004] best-effort observability guard in finally; logging failure must never block workflow completion
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

        # CNCT-93: inherit the parent workflow's app_name (activity.Info exposes
        # no app_name of its own) via the x-app-name header the workflow's
        # outbound interceptor injected, so activity.started/ended and @task
        # app-code logs stamp the same per-entrypoint app_name the workflow
        # resolved. (All metric surfaces — get_metric_labels() and the Temporal
        # lifecycle temporal_* families — stay connector-level by design; only
        # logs carry the per-entrypoint value.) Read BEFORE building the
        # ExecutionContext so it rides on that shared context. Absent -> the
        # logger falls back to ATLAN_APPLICATION_NAME.
        app_name = ""
        try:
            payload = input.headers.get(_HEADER_APP_NAME)
            if payload is not None:
                converter = default_converter().payload_converter
                decoded = converter.from_payload(payload, type_hint=str)
                # Bound the header value the same way the workflow bounds it
                # before it reaches the log fields — reject non-strings, cap
                # length — rather than trusting the wire payload.
                if isinstance(decoded, str) and decoded:
                    app_name = decoded[:_APP_NAME_MAX_CHARS]
        except Exception:
            logger.warning("Failed to read app_name header in activity", exc_info=True)

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
                app_name=app_name,
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
            started_msg = _lifecycle_message(
                "activity.started", str(identity["temporal.activity.type"])
            )
            logger.info(started_msg, **identity)
        # conformance: ignore[E004] best-effort observability guard; logging failure must never block activity execution
        except Exception:  # noqa: S110 — best-effort observability; never block the activity on logging
            pass

        start_ns = time.monotonic_ns()
        status = "OK"
        exc_caught: BaseException | None = None
        try:
            return await self.next.execute_activity(input)
        # conformance: ignore[E004] captures exception for ended log enrichment then unconditionally re-raises; exc logged in finally block
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
                act_type = str(identity["temporal.activity.type"])
                if status == "ERROR":
                    ended_attrs.update(_extract_failure_attrs(exc_caught))
                    # A deliberate preflight-gate block logs terse (no stack);
                    # the activity's Temporal redness comes from the raise, not
                    # the log. Every other failure keeps the ERROR traceback.
                    if is_preflight_block(exc_caught):
                        blocked_msg = _lifecycle_message(
                            "activity.ended", f"{act_type} BLOCKED (preflight gate)"
                        )
                        logger.warning(blocked_msg, **ended_attrs)
                    else:
                        failed_msg = _lifecycle_message(
                            "activity.ended",
                            f"{act_type} {_failure_suffix(exc_caught, ended_attrs)}",
                        )
                        logger.error(failed_msg, exc_info=True, **ended_attrs)
                else:
                    ok_msg = _lifecycle_message(
                        "activity.ended", f"{act_type} OK ({duration_ms}ms)"
                    )
                    logger.info(ok_msg, **ended_attrs)
            # conformance: ignore[E004] best-effort observability guard in finally; logging failure must never block activity completion
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
