"""Output collector for structured workflow outputs.

This module provides the OutputCollector class which collects metrics and
artifacts during workflow and activity execution. The SDK's output interceptor
automatically manages collectors and merges their contents into the workflow's
return dict.
"""

from typing import Any, Dict

from application_sdk.workflows.outputs.models import Artifact, Metric


class OutputCollector:
    """Collects metrics and artifacts during workflow or activity execution.

    The OutputCollector is the primary interface for activities and workflows
    to record metrics and artifacts. Use get_outputs() to obtain the collector
    for the current execution context.

    The SDK's output interceptor handles:
    - Creating fresh collectors for each activity/workflow execution
    - Stashing activity collectors for later merge
    - Merging all collected outputs into the workflow's return dict

    Example:
        >>> from application_sdk.workflows.outputs import get_outputs, Metric
        >>> collector = get_outputs()
        >>> collector.add_metric(Metric(name="rows-processed", value=1000))
        >>> collector.add_metric(Metric(name="rows-processed", value=500))
        >>> # Numeric metrics with same name are summed: 1500
    """

    def __init__(self) -> None:
        """Initialize an empty output collector."""
        self._metrics: Dict[str, Metric] = {}
        self._artifacts: Dict[str, Artifact] = {}

    def add_metric(self, metric: Metric) -> None:
        """Add or accumulate a metric.

        If a metric with the same name exists:
        - Both numeric (int/float): values are summed
        - Otherwise: last write wins

        Args:
            metric: The metric to add or accumulate.
        """
        if metric.name in self._metrics:
            existing = self._metrics[metric.name]
            if isinstance(metric.value, (int, float)) and isinstance(
                existing.value, (int, float)
            ):
                self._metrics[metric.name] = Metric(
                    name=metric.name,
                    value=existing.value + metric.value,
                    display_name=metric.display_name or existing.display_name,
                )
                return
        self._metrics[metric.name] = metric

    def add_artifact(self, artifact: Artifact) -> None:
        """Add an artifact reference.

        If an artifact with the same name exists, last write wins.

        Args:
            artifact: The artifact reference to add.
        """
        self._artifacts[artifact.name] = artifact

    def merge(self, other: "OutputCollector") -> None:
        """Merge another collector's contents into this one.

        Metrics are merged using add_metric semantics (numeric values sum).
        Artifacts use last-write-wins from the other collector.

        Args:
            other: The collector to merge from.
        """
        for metric in other._metrics.values():
            self.add_metric(metric)
        for artifact in other._artifacts.values():
            self.add_artifact(artifact)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize collected outputs to a namespaced dict.

        Returns:
            Dict with "metrics" and/or "artifacts" namespaces.
            Empty namespaces are omitted.

        Example:
            >>> collector.add_metric(Metric(name="count", value=100))
            >>> collector.to_dict()
            {'metrics': {'count': 100}}
        """
        result: Dict[str, Any] = {}
        if self._metrics:
            result["metrics"] = {}
            for m in self._metrics.values():
                if m.display_name:
                    result["metrics"][m.name] = {
                        "value": m.value,
                        "display_name": m.display_name,
                    }
                else:
                    result["metrics"][m.name] = m.value
        if self._artifacts:
            result["artifacts"] = {a.name: a.path for a in self._artifacts.values()}
        return result

    def merge_with(self, base: Dict[str, Any]) -> Dict[str, Any]:
        """Merge collected outputs into an existing return dict.

        Args:
            base: The original return dict from the workflow.

        Returns:
            New dict containing base keys plus collected outputs.
        """
        return {**base, **self.to_dict()}

    def has_data(self) -> bool:
        """Check if any outputs have been collected.

        Returns:
            True if any metrics or artifacts have been added.
        """
        return bool(self._metrics or self._artifacts)
