import logging
import os

from opentelemetry.sdk.resources import Resource

from application_sdk.constants import (
    APP_TENANT_ID,
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


def get_workflow_context() -> dict[str, str]:
    """Get the workflow context as a plain dict.

    Reads from the ``ExecutionContext`` ContextVar set by
    ``ExecutionContextInterceptor`` — no Temporal imports required.
    Outside Temporal (tests, CLI) the default context returns
    ``in_workflow="false"`` and ``in_activity="false"``.

    Returns:
        dict[str, str]: The workflow context fields.
    """
    from application_sdk.observability.context import get_execution_context

    ctx = get_execution_context()
    context: dict[str, str] = {
        "in_workflow": str(ctx.execution_type == "workflow").lower(),
        "in_activity": str(ctx.execution_type == "activity").lower(),
        "workflow_id": ctx.workflow_id,
        "workflow_run_id": ctx.workflow_run_id,
        "workflow_type": ctx.workflow_type,
        "namespace": ctx.namespace,
        "task_queue": ctx.task_queue,
        "attempt": str(ctx.attempt),
        "activity_id": ctx.activity_id,
        "activity_type": ctx.activity_type,
    }

    # Merge correlation context (atlan- prefixed headers for distributed tracing)
    corr_ctx = correlation_context.get()
    if corr_ctx:
        for key, value in corr_ctx.items():
            if key.startswith("atlan-") and value:
                context[key] = str(value)

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
    except Exception:
        logging.error("Failed to parse OTLP resource attributes", exc_info=True)
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
    # App Vitals: ensure app identity is on every OTLP export
    if "app.name" not in resource_attributes:
        resource_attributes["app.name"] = APPLICATION_NAME
    if "tenant.id" not in resource_attributes:
        resource_attributes["tenant.id"] = APP_TENANT_ID
    if extra_attrs:
        resource_attributes.update(extra_attrs)
    return Resource.create(resource_attributes)
