"""Models for structured workflow outputs.

This module contains Pydantic models used to represent metrics and artifacts
that can be collected during workflow and activity execution for display
in the Automation Engine UI.
"""

from typing import Optional, Union

from pydantic import BaseModel


class Metric(BaseModel):
    """A single metric to expose in workflow output.

    Metrics are automatically collected by the SDK's output interceptor and
    merged into the workflow's return dict under the "metrics" namespace.

    Attributes:
        name: Kebab-case key identifying the metric, e.g. "tables-extracted".
        value: The metric value. Numeric values (int, float) are summed when
            multiple metrics with the same name are collected. String values
            use last-write-wins semantics.
        display_name: Optional human-readable label for UI display. If provided,
            the metric is serialized as {"value": ..., "display_name": ...}.
            If omitted, the metric is serialized as a plain value for backward
            compatibility.

    Example:
        >>> from application_sdk.outputs import get_outputs, Metric
        >>> outputs = get_outputs()
        >>> outputs.add_metric(Metric(name="tables-extracted", value=150))
        >>> outputs.add_metric(Metric(name="status", value="completed"))
        >>> outputs.add_metric(Metric(
        ...     name="qi-queries-parsed",
        ...     value=42,
        ...     display_name="QI Queries Parsed"
        ... ))
    """

    name: str
    value: Union[int, float, str]
    display_name: Optional[str] = None


class Artifact(BaseModel):
    """A reference to a downloadable file artifact.

    Artifacts are references to files in object storage that should be
    exposed as downloadable outputs from the workflow.

    Attributes:
        name: Unique identifier for the artifact, e.g. "debug-logs".
        path: Object store path to the artifact file,
            e.g. "argo-artifacts/.../debug.tgz".
        display_name: Optional human-readable label for UI display. If provided,
            the artifact is serialized as {"path": ..., "display_name": ...}.
            If omitted, the artifact is serialized as a plain path for backward
            compatibility.

    Example:
        >>> from application_sdk.outputs import get_outputs, Artifact
        >>> outputs = get_outputs()
        >>> outputs.add_artifact(Artifact(
        ...     name="debug-logs",
        ...     path="argo-artifacts/workflow-123/debug.tgz",
        ...     display_name="Debug Logs"
        ... ))
    """

    name: str
    path: str
    display_name: Optional[str] = None
