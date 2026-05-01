"""Shared context variables for observability.

This module contains ContextVar definitions that are shared across
multiple observability modules to avoid circular imports.
"""

import dataclasses
from contextvars import ContextVar
from typing import Any, Dict

# Context variable for request-scoped data (e.g., request_id from HTTP middleware)
request_context: ContextVar[Dict[str, Any] | None] = ContextVar(
    "request_context", default=None
)

# Context variable for correlation context (atlan- prefixed headers for distributed tracing)
correlation_context: ContextVar[Dict[str, Any] | None] = ContextVar(
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
