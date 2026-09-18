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

#: Resource attribute keys we inline as labels onto every metric series.
#: Kept deliberately minimal — every additional inline key multiplies the
#: per-series label set across the entire fleet of pods.
#:
#: Why ``app.name`` only:
#:   - ``app.name`` is the connector identity; filtering by app is the most
#:     common operator query, and the alternative (``target_info`` join) is
#:     awkward to type for ad-hoc PromQL.
#:   - ``app.version`` / ``app.release_id`` / ``app.sdk_version`` /
#:     ``app.release_channel`` change across deploys or channel promotions
#:     and would multiply indexdb cardinality across the retention window.
#:
#: All non-inlined Resource attributes still travel via the OTel exporter's
#: ``target_info`` gauge (one row per pod with the full Resource), so
#: PromQL joins continue to work::
#:
#:     sum by (app_release_id) (
#:       rate(http_server_request_duration_seconds_count[5m])
#:         * on(instance) group_left(app_release_id) target_info
#:     )
METRIC_ENRICHMENT_KEYS: tuple[str, ...] = ("app.name",)


def get_metric_enrichment_labels() -> dict[str, str]:
    """Return ``METRIC_ENRICHMENT_KEYS`` resolved against the process resource,
    with dots transliterated to underscores for Prometheus label compatibility.

    Used by both the OTel Prometheus reader (server scrape, worker push) and
    the Temporal Rust core's ``TelemetryConfig.global_tags`` to keep the
    enrichment surface consistent across the two metric pipelines.
    """
    resource = build_otel_resource()
    return {
        k.replace(".", "_"): str(v)
        for k, v in resource.attributes.items()
        if k in METRIC_ENRICHMENT_KEYS
    }


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


def get_metric_labels() -> dict[str, str]:
    """Return low-cardinality labels for Prometheus metrics.

    Only workflow_type, activity_type, and app_name are included.
    High-cardinality identifiers (workflow_id, run_id, activity_id, task_queue,
    namespace, attempt) are intentionally excluded to prevent time-series explosion.
    """
    from application_sdk.observability.context import (  # noqa: PLC0415 — circular: observability.context imports observability.utils transitively
        get_execution_context,
    )

    ctx = get_execution_context()
    return {
        # Deliberately CONNECTOR-LEVEL (the process-wide env value), NOT the
        # per-entrypoint ExecutionContext.app_name that logs carry (CNCT-93).
        # All metric families stay keyed on the connector name so existing
        # dashboards/alerts are unaffected; per-entrypoint breakdown is
        # available on the log app_name. Routing ctx.app_name through here is a
        # deliberate non-goal of the log fix — revisit only as its own change.
        "app_name": APPLICATION_NAME,
        "workflow_type": ctx.workflow_type,
        "activity_type": ctx.activity_type,
    }


def in_temporal_workflow() -> bool:
    """Return True when the caller is executing inside a Temporal workflow.

    Single predicate for every observability guard that must not perform
    work Temporal's deterministic workflow loop cannot support — blocking
    file I/O, thread offloads, and cross-event-loop bridges
    (``asyncio.wrap_future``, whose done-callback calls ``is_closed()`` on the
    destination loop; Temporal's workflow loop raises ``NotImplementedError``
    there, so the awaited future never resolves — SYSAPPS-328).

    Deliberately ``temporalio.workflow.in_workflow()`` rather than either of
    the two alternatives previously used in this package:

    - ``workflow.unsafe.in_sandbox()`` answers "is the sandbox active", which
      is False for a passed-through module or an unsandboxed worker even
      though workflow rules still apply.
    - The ``ExecutionContext`` ContextVar (see :func:`get_workflow_context`)
      is only populated after ``ExecutionContextInterceptor`` has run, so it
      reads False in workflow code that executes before or outside it.

    ``in_workflow()`` reads Temporal's own ContextVar, so it is correct in
    both cases and returns False outside Temporal entirely.

    Returns:
        bool: True inside workflow code; False in activities, the API server,
        worker process shutdown, tests and CLI tools.
    """
    # conformance: ignore[P006] safety guard, not orchestration. Relocating this behind execution/_temporal/ would invert the dependency (observability would import execution, which already imports observability). The ExecutionContext ContextVar alternative is a derived signal that reads False for any worker not built by create_worker, failing the guard open; temporalio's own in_workflow() is ground truth.
    from temporalio import (  # noqa: PLC0415 — cold path: consulted only on flush/shutdown paths
        workflow,
    )

    return workflow.in_workflow()


def get_workflow_context() -> dict[str, str]:
    """Get the workflow context as a plain dict.

    Reads from the ``ExecutionContext`` ContextVar set by
    ``ExecutionContextInterceptor`` — no Temporal imports required.
    Outside Temporal (tests, CLI) the default context returns
    ``in_workflow="false"`` and ``in_activity="false"``.

    Returns:
        dict[str, str]: The workflow context fields.
    """
    from application_sdk.observability.context import (  # noqa: PLC0415 — circular: observability is imported transitively by many modules; lifting risks circles
        get_execution_context,
    )

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
    # Parent identity is only populated on child workflows; omit when empty
    # so we don't pollute the extra dict on top-level workflows / activities.
    if ctx.parent_workflow_id:
        context["parent_workflow_id"] = ctx.parent_workflow_id
    if ctx.parent_run_id:
        context["parent_run_id"] = ctx.parent_run_id

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
    if APPLICATION_NAME:
        # CNCT-93: app.name stays connector-level (ATLAN_APPLICATION_NAME), NOT
        # per-entrypoint — unlike the log app_name. This is a per-process OTel
        # Resource shared by the tracer, the metrics meter, and OTLP log export;
        # it is built once at process start, before any workflow resolves its
        # entrypoint app_name, and cannot vary per workflow execution. Bundle
        # traces therefore attribute at the connector level by design.
        resource_attributes["app.name"] = APPLICATION_NAME
    if OTEL_WF_NODE_NAME:
        resource_attributes["k8s.workflow.node.name"] = OTEL_WF_NODE_NAME
    # Deployment-level attributes — constant per pod, don't duplicate in log attrs.
    # tenant.id is intentionally omitted: k8s.cluster.name (injected by the
    # central OTel collector's resource processor) identifies the tenant at
    # the deployment level.
    # app.build_id is omitted — app.version carries the same signal.
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
