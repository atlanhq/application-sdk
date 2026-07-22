"""Shared context variables for observability.

This module contains ContextVar definitions that are shared across
multiple observability modules to avoid circular imports.
"""

import dataclasses
from collections.abc import Callable
from contextvars import ContextVar
from typing import Any

# ---------------------------------------------------------------------------
# Replay-predicate ContextVar
# ---------------------------------------------------------------------------
# Set by the workflow log interceptor to ``workflow.unsafe.is_replaying_history_events``
# (a per-call live check) so the logger adapter can suppress emissions during
# replay without importing temporalio directly.  The ContextVar holds a
# *callable* rather than a captured bool so every invocation evaluates the
# current replay state — a captured bool would be stale after the workflow
# catches up to the live edge and replay ends mid-execution.
#
# Outside Temporal (tests, CLI tools, activities) the default is None, and
# ``is_replaying()`` returns False with a single ContextVar.get() — no
# callable invocation, no exception machinery.
_replay_predicate: ContextVar[Callable[[], bool] | None] = ContextVar(
    "replay_predicate", default=None
)

# Context variable for request-scoped data (e.g., request_id from HTTP middleware)
request_context: ContextVar[dict[str, Any] | None] = ContextVar(
    "request_context", default=None
)

# Context variable for correlation context (atlan- prefixed headers for distributed tracing)
correlation_context: ContextVar[dict[str, Any] | None] = ContextVar(
    "correlation_context", default=None
)


@dataclasses.dataclass(frozen=True)
class ExecutionContext:
    """Immutable snapshot of the current Temporal execution context.

    Set once per workflow/activity execution by ``ExecutionContextInterceptor``
    before any user code runs.  Outside Temporal (tests, CLI tools) the default
    instance is returned, with ``execution_type="none"`` and all fields empty.

    Uses only stdlib (``dataclasses``, ``contextvars``) — safe for the Temporal
    sandbox and importable without temporalio installed.
    """

    execution_type: str = "none"  # "workflow" | "activity" | "none"
    workflow_id: str = ""
    workflow_run_id: str = ""
    workflow_type: str = ""
    namespace: str = ""
    task_queue: str = ""
    attempt: int = 0
    activity_id: str = ""
    activity_type: str = ""
    # Set on child workflows from ``workflow.info().parent``. Empty on
    # top-level workflows and on activities (Temporal's activity.Info does
    # not carry parent info directly).
    parent_workflow_id: str = ""
    parent_run_id: str = ""


_execution_ctx: ContextVar[ExecutionContext] = ContextVar(
    "execution_context", default=ExecutionContext()
)


def get_execution_context() -> ExecutionContext:
    """Return the current execution context (never raises)."""
    return _execution_ctx.get()


def set_execution_context(ctx: ExecutionContext) -> None:
    """Set the current execution context.

    Called by ``ExecutionContextInterceptor`` at the start of each
    workflow/activity execution.
    """
    _execution_ctx.set(ctx)


def set_replay_predicate(pred: Callable[[], bool] | None) -> None:
    """Inject the replay-detection callable into the current async context.

    Called by the workflow log interceptor with
    ``workflow.unsafe.is_replaying_history_events`` before any user code runs
    (and before the ``is_replaying()`` early-return gate, so the predicate is
    available on every replay turn).  Pass ``None`` to clear the predicate
    (e.g. in tests).
    """
    _replay_predicate.set(pred)


def is_replaying() -> bool:
    """Return True only when executing inside a replaying workflow.

    Safe to call from anywhere — never raises.  Returns False immediately
    (single ContextVar.get()) outside Temporal (CLI, tests, activities, HTTP).
    Inside a workflow the predicate is evaluated per-call so the result tracks
    the live replay/live boundary correctly.
    """
    pred = _replay_predicate.get()
    if pred is None:
        return False
    try:
        return bool(pred())
    except Exception:  # conformance: ignore[E004] never-raises predicate on the logger-adapter hot path; logging here would recurse through the adapter that calls is_replaying()
        # conformance: ignore[E007] replay-detection failure safely degrades to "not replaying"; cannot log here (adapter recursion) in this sandbox-safe stdlib-only module
        return False
