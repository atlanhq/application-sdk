"""Structured workflow outputs for the Automation Engine.

This module provides a mechanism for activities and workflows to expose
metrics and artifacts that appear in the CUE UI after workflow completion.

Usage:
    In any activity or workflow code, call get_outputs() and add metrics:

    >>> from application_sdk.workflows.outputs import get_outputs, Metric
    >>> outputs = get_outputs()
    >>> outputs.add_metric(Metric(name="tables-extracted", value=150))

    The SDK's output interceptor automatically:
    - Creates fresh collectors for each execution context
    - Merges activity outputs at workflow exit
    - Enriches the workflow's return dict with collected data

Architecture:
    - _current_outputs: ContextVar holding the collector for the current
      activity or workflow execution.
    - _collected_outputs: Process-level dict storing activity collectors
      keyed by workflow_run_id, waiting for workflow exit merge.
    - OutputInterceptor: Temporal interceptor managing the lifecycle.
"""

import contextvars
import threading
from collections import defaultdict
from typing import Dict, List

from application_sdk.workflows.outputs.collector import OutputCollector
from application_sdk.workflows.outputs.models import Artifact, Metric

_current_outputs: contextvars.ContextVar[OutputCollector | None] = (
    contextvars.ContextVar("workflow_outputs", default=None)
)

_collected_outputs: Dict[str, List[OutputCollector]] = defaultdict(list)
_lock = threading.Lock()


def get_outputs() -> OutputCollector:
    """Get the output collector for the current execution context.

    Call add_metric() / add_artifact() on the returned collector.
    The SDK interceptor handles everything else automatically.

    Returns:
        OutputCollector: The collector for the current activity or workflow.
            Creates a new collector if one doesn't exist for this context.

    Example:
        >>> from application_sdk.workflows.outputs import get_outputs, Metric, Artifact
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
