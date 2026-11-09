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
    - _current_outputs: ContextVar holding the collector for the current
      activity or workflow execution.
    - OutputInterceptor: Temporal interceptor managing the lifecycle. There is
      no process-level store: the activity and its workflow may run in
      different worker processes, so outputs travel through Temporal history
      on the activity result.
"""

import contextvars

from application_sdk.outputs.collector import OutputCollector
from application_sdk.outputs.models import Artifact, Metric

_current_outputs: contextvars.ContextVar[OutputCollector | None] = (
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
    collector = _current_outputs.get()
    if collector is None:
        collector = OutputCollector()
        _current_outputs.set(collector)
    return collector


__all__ = ["get_outputs", "Metric", "Artifact", "OutputCollector"]
