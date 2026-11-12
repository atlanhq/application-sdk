"""Structured workflow outputs for the Automation Engine.

This module provides a mechanism for activities and workflows to expose
metrics and artifacts that appear in the CUE UI after workflow completion.

Usage:
    In any activity or workflow code, call get_outputs() and add metrics:

    >>> from application_sdk.outputs import get_outputs, Metric
    >>> outputs = get_outputs()
    >>> outputs.add_metric(Metric(name="tables-extracted", value=150))

    The SDK's output interceptor automatically:
    - Creates fresh collectors for each execution context
    - Carries each activity's outputs back to the workflow on the
      activity's own result
    - Enriches the workflow's return value with collected data

Architecture:
    - current_outputs: ContextVar holding the collector for the current
      activity or workflow execution. A test that drives an activity body
      directly can install its own collector with ``current_outputs.set(...)``
      and restore it with ``current_outputs.reset(token)``.
    - OutputInterceptor: Temporal interceptor managing the lifecycle. There is
      no process-level store: the activity and its workflow may run in
      different worker processes, so outputs travel through Temporal history
      on the activity result.
"""

import contextvars
import warnings

from application_sdk.outputs.collector import OutputCollector
from application_sdk.outputs.models import Artifact, Metric
from application_sdk.outputs.skipped import (
    MAX_SKIPPED_RECORDS,
    BlankIdentity,
    MalformedRecord,
    MissingRequiredField,
    RecordSkipped,
    SkipReason,
    UnsupportedType,
    skip_record,
)

current_outputs: contextvars.ContextVar[OutputCollector | None] = (
    contextvars.ContextVar("workflow_outputs", default=None)
)


def get_outputs() -> OutputCollector:
    """Get the output collector for the current execution context.

    Call add_metric() / add_artifact() on the returned collector.
    The SDK interceptor handles everything else automatically.

    Returns:
        OutputCollector: The collector for the current activity or workflow.
            Creates a new collector if one doesn't exist for this context.

    Example:
        >>> from application_sdk.outputs import get_outputs, Metric, Artifact
        >>> outputs = get_outputs()
        >>> outputs.add_metric(Metric(name="tables-extracted", value=150))
        >>> outputs.add_artifact(Artifact(name="debug-logs", path="artifacts/debug.tgz"))
    """
    collector = current_outputs.get()
    if collector is None:
        collector = OutputCollector()
        current_outputs.set(collector)
    return collector


#: name -> (replacement, why), served by ``__getattr__`` below (PEP 562).
_DEPRECATED_CONSTANTS: dict[str, tuple[str, str]] = {
    "_current_outputs": (
        "application_sdk.outputs.current_outputs",
        "the same ContextVar under a public name (FND-4661)",
    ),
}


def __getattr__(name: str) -> object:
    """Serve the renamed ContextVar once more, with a deprecation warning (PEP 562)."""
    entry = _DEPRECATED_CONSTANTS.get(name)
    if entry is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    replacement, note = entry
    warnings.warn(
        f"{name} is deprecated; use {replacement} instead — {note}. "
        "Will be removed in v4.0.0.",
        DeprecationWarning,
        stacklevel=2,
    )
    return current_outputs


__all__ = [
    "get_outputs",
    "current_outputs",
    "Metric",
    "Artifact",
    "OutputCollector",
    "MAX_SKIPPED_RECORDS",
    "SkipReason",
    "skip_record",
    "RecordSkipped",
    "BlankIdentity",
    "MissingRequiredField",
    "MalformedRecord",
    "UnsupportedType",
]
