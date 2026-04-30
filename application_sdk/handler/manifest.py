"""Typed manifest contracts for Automation Engine DAG execution.

HTTP API zone: these types are Pydantic models. They cross an external HTTP
boundary (GET /workflows/v1/manifest consumed by Heracles / Automation Engine)
so they need schema validation on ingress and clean JSON serialization on
egress. Do NOT use plain dataclasses here — use BaseModel so that
``model_validate`` / ``model_validate_json`` replace hand-rolled parsing and
``model_dump_json()`` eliminates the intermediate dict step.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class DagNodeDependency(BaseModel):
    """Declares that a node may only run after the given node completes."""

    model_config = ConfigDict(frozen=True)

    node_id: str


class ExecuteWorkflowInputs(BaseModel):
    """Inputs for the execute_workflow activity.

    ``args`` values can be:

    - Template placeholders: ``{{workflow-id}}``, ``{{connection}}`` —
      substituted by Heracles from workflow config / frontend form values.
    - JSONPath references: ``$.extract.outputs.foo`` — resolved by AE at
      runtime against upstream node's run() return.
    - Literals: booleans, strings, numbers.

    ``args`` intentionally typed ``dict[str, Any]``: it is a passthrough
    contract with an external orchestrator, so the value space is open by
    design — this is not a missing type annotation.
    """

    model_config = ConfigDict(frozen=True)

    workflow_type: str
    task_queue: str
    args: dict[str, Any] = {}


class DagNode(BaseModel):
    """A single node in the Automation Engine execution DAG."""

    model_config = ConfigDict(frozen=True)

    activity_name: str
    activity_display_name: str
    app_name: str
    inputs: ExecuteWorkflowInputs
    depends_on: DagNodeDependency | None = None


class AppManifest(BaseModel):
    """Manifest describing an app's execution DAG for Automation Engine.

    Returned by GET /workflows/v1/manifest. Tells Heracles how to
    orchestrate the app's workflows as a DAG via Automation Engine.

    Parse from a dict or raw JSON string::

        manifest = AppManifest.model_validate(data)
        manifest = AppManifest.model_validate_json(raw_bytes)

    Serialize to JSON bytes for the HTTP response::

        return Response(content=manifest.model_dump_json(), media_type="application/json")
    """

    model_config = ConfigDict(frozen=True)

    execution_mode: str
    dag: dict[str, DagNode]
    init_endpoint: str | None = None
