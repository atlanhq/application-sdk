import logging
import os

from opentelemetry.sdk.resources import Resource
from pydantic import BaseModel, Field

from application_sdk.constants import (
    APPLICATION_NAME,
    DEPLOYMENT_NAME,
    OBSERVABILITY_DIR,
    OTEL_RESOURCE_ATTRIBUTES,
    OTEL_WF_NODE_NAME,
    SERVICE_NAME,
    SERVICE_VERSION,
    TEMPORARY_PATH,
)
from application_sdk.observability.context import correlation_context


class WorkflowContext(BaseModel):
    """Workflow context.

    This model supports dynamic correlation context fields (atlan- prefixed)
    through Pydantic's extra="allow" configuration.
    """

    model_config = {"extra": "allow"}

    in_workflow: str = Field(default="false")
    in_activity: str = Field(default="false")
    workflow_id: str = Field(init=False, default="")
    workflow_type: str = Field(init=False, default="")
    namespace: str = Field(init=False, default="")
    task_queue: str = Field(init=False, default="")
    attempt: str = Field(init=False, default="0")
    activity_id: str = Field(init=False, default="")
    activity_type: str = Field(init=False, default="")
    workflow_run_id: str = Field(init=False, default="")


def get_observability_dir() -> str:
    """Build the observability path using deployment name.

    Returns:
        str: The built observability path using deployment name.
    """
    return os.path.join(
        TEMPORARY_PATH,
        OBSERVABILITY_DIR.format(
            application_name=APPLICATION_NAME, deployment_name=DEPLOYMENT_NAME
        ),
    )


def get_workflow_context() -> WorkflowContext:
    """Get the workflow context.

    Reads from the ``ExecutionContext`` ContextVar set by
    ``ExecutionContextInterceptor`` — no Temporal imports required.
    Outside Temporal (tests, CLI) the default context returns
    ``in_workflow="false"`` and ``in_activity="false"``.

    Returns:
        WorkflowContext: The workflow context.
    """
    from application_sdk.observability.context import get_execution_context

    ctx = get_execution_context()
    context = WorkflowContext(
        in_workflow=str(ctx.execution_type == "workflow").lower(),
        in_activity=str(ctx.execution_type == "activity").lower(),
    )
    context.workflow_id = ctx.workflow_id
    context.workflow_run_id = ctx.workflow_run_id
    context.workflow_type = ctx.workflow_type
    context.namespace = ctx.namespace
    context.task_queue = ctx.task_queue
    context.attempt = str(ctx.attempt)
    context.activity_id = ctx.activity_id
    context.activity_type = ctx.activity_type

    # Merge correlation context (atlan- prefixed headers for distributed tracing)
    corr_ctx = correlation_context.get()
    if corr_ctx:
        for key, value in corr_ctx.items():
            if key.startswith("atlan-") and value:
                setattr(context, key, str(value))

    return context


def parse_otel_resource_attributes(env_var: str) -> dict[str, str]:
    """Parse 'key=val,key=val' OTEL_RESOURCE_ATTRIBUTES into a dict."""
    try:
        if env_var:
            attributes = env_var.split(",")
            return {
                item.split("=")[0].strip(): item.split("=")[1].strip()
                for item in attributes
                if "=" in item
            }
    except Exception as e:
        logging.error(f"Failed to parse OTLP resource attributes: {e}")
    return {}


def build_otel_resource(extra_attrs: dict[str, str] | None = None) -> Resource:
    """Build an OTel Resource with standard Atlan service attributes."""
    resource_attributes: dict[str, str] = {}
    if OTEL_RESOURCE_ATTRIBUTES:
        resource_attributes = parse_otel_resource_attributes(OTEL_RESOURCE_ATTRIBUTES)
    if "service.name" not in resource_attributes:
        resource_attributes["service.name"] = SERVICE_NAME
    if "service.version" not in resource_attributes:
        resource_attributes["service.version"] = SERVICE_VERSION
    if OTEL_WF_NODE_NAME:
        resource_attributes["k8s.workflow.node.name"] = OTEL_WF_NODE_NAME
    if extra_attrs:
        resource_attributes.update(extra_attrs)
    return Resource.create(resource_attributes)
