"""Context-scoped access to the active lineage tracker.

Mirrors :mod:`application_sdk.outputs` (``get_outputs``): a connector calls
``create_tracker(...)`` once at the top of its lineage activity, which registers
the tracker in a :class:`~contextvars.ContextVar`; deep call-site code then uses
:func:`get_lineage_tracker` without threading the tracker through every signature.

When no tracker is set (default-safe), :func:`get_lineage_tracker` returns a
shared NoOp so call sites never need a ``None`` check. The
``LineageObservabilityInterceptor``
(:mod:`application_sdk.execution._temporal.interceptors.lineage`) resets this
ContextVar per activity so a tracker never leaks across activities/tasks.
"""

from __future__ import annotations

import contextvars
from typing import Union

from application_sdk.observability.lineage.noop_tracker import (
    NoOpLineageObservabilityTracker,
)
from application_sdk.observability.lineage.tracker import LineageObservabilityTracker

AnyTracker = Union[LineageObservabilityTracker, NoOpLineageObservabilityTracker]

_current_tracker: contextvars.ContextVar[AnyTracker | None] = contextvars.ContextVar(
    "lineage_tracker", default=None
)

# Shared, stateless NoOp returned when no tracker is set in the current context.
_NOOP_TRACKER = NoOpLineageObservabilityTracker()


def get_lineage_tracker() -> AnyTracker:
    """Return the tracker for the current execution context.

    Never returns ``None``: a shared NoOp is returned when unset, so connector
    call sites can unconditionally call ``get_lineage_tracker().record_missing_reason(...)``.
    """
    return _current_tracker.get() or _NOOP_TRACKER


def set_lineage_tracker(tracker: AnyTracker) -> None:
    """Register *tracker* as the active tracker for the current context."""
    _current_tracker.set(tracker)


def reset_lineage_tracker() -> None:
    """Clear the active tracker (used by the interceptor to scope per activity)."""
    _current_tracker.set(None)


__all__ = [
    "get_lineage_tracker",
    "set_lineage_tracker",
    "reset_lineage_tracker",
    "AnyTracker",
]
