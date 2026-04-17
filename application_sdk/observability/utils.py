import logging
import os

from opentelemetry.sdk.resources import Resource

from application_sdk.constants import (
    APP_SDK_VERSION,
    APP_TYPE,
    APPLICATION_NAME,
    APPLICATION_VERSION,
    DEPLOYMENT_NAME,
    DOMAIN_NAME,
    OBSERVABILITY_DIR,
    OTEL_RESOURCE_ATTRIBUTES,
    OTEL_WF_NODE_NAME,
    PUBLISHED_AT,
    RELEASE_CHANNEL,
    RELEASE_ID,
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
    # Deployment-level attributes — constant per pod, don't duplicate in log attrs.
    # tenant.id is intentionally omitted: k8s.cluster.name (injected by the
    # central OTel collector's resource processor) identifies the tenant at
    # the deployment level.
    # NOTE: app.name is intentionally NOT a resource attribute — a deployment
    # can host multiple apps; app_name stays in log attrs per-event.
    # app.build_id is also omitted — app.version carries the same signal.
    if APPLICATION_VERSION:
        resource_attributes["app.version"] = APPLICATION_VERSION
    if RELEASE_ID:
        resource_attributes["app.release_id"] = RELEASE_ID
    if RELEASE_CHANNEL:
        resource_attributes["app.release_channel"] = RELEASE_CHANNEL
    if APP_SDK_VERSION:
        resource_attributes["app.sdk_version"] = APP_SDK_VERSION
    if APP_TYPE:
        resource_attributes["app.type"] = APP_TYPE
    if PUBLISHED_AT:
        resource_attributes["app.published_at"] = PUBLISHED_AT
    if DOMAIN_NAME:
        resource_attributes["k8s.domain.name"] = DOMAIN_NAME
    pod_name = os.environ.get("K8S_POD_NAME") or os.environ.get("HOSTNAME", "")
    if pod_name:
        resource_attributes["k8s.pod.name"] = pod_name
    if extra_attrs:
        resource_attributes.update(extra_attrs)
    return Resource.create(resource_attributes)
