"""Models for structured workflow outputs.

This module contains Pydantic models used to represent metrics and artifacts
that can be collected during workflow and activity execution for display
in the Automation Engine UI.
"""

from typing import Union

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

    Example:
        >>> from application_sdk.workflows.outputs import get_outputs, Metric
        >>> outputs = get_outputs()
        >>> outputs.add_metric(Metric(name="tables-extracted", value=150))
        >>> outputs.add_metric(Metric(name="status", value="completed"))
    """

    name: str
    value: Union[int, float, str]


class Artifact(BaseModel):
    """A reference to a downloadable file artifact.

    Artifacts are references to files in object storage that should be
    exposed as downloadable outputs from the workflow.

    Attributes:
        name: Human-readable name for the artifact, e.g. "debug-logs".
        path: Object store path to the artifact file,
            e.g. "argo-artifacts/.../debug.tgz".

    Example:
        >>> from application_sdk.workflows.outputs import get_outputs, Artifact
        >>> outputs = get_outputs()
        >>> outputs.add_artifact(Artifact(
        ...     name="debug-logs",
        ...     path="argo-artifacts/workflow-123/debug.tgz"
        ... ))
    """

    name: str
    path: str
