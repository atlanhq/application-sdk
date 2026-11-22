# conformance: ignore[L002] self_failure_logger must be stdlib: a failing OTel sink logging through get_logger would recurse into itself
import logging
import os
import threading
import time
from collections.abc import Callable

from opentelemetry.sdk.resources import Resource

from application_sdk._install import worker_only_missing
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

#: Minimum seconds between two records of the same observability self-failure.
SELF_FAILURE_LOG_INTERVAL_SECONDS = 60.0


class _SelfFailureRateLimit(logging.Filter):
    """Pass one record per message template per interval and count the rest.

    A broken exporter or sink fails on every record it handles, so an
    unthrottled self-failure log repeats at the application's own log rate.
    The next record that passes says how many were dropped in between.
    """

    def __init__(
        self,
        interval_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__()
        self._interval_seconds = interval_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._last_passed: dict[str, float] = {}
        self._suppressed: dict[str, int] = {}

    def filter(self, record: logging.LogRecord) -> bool:
        key = str(record.msg)
        now = self._clock()
        with self._lock:
            last = self._last_passed.get(key)
            if last is not None and now - last < self._interval_seconds:
                self._suppressed[key] = self._suppressed.get(key, 0) + 1
                return False
            self._last_passed[key] = now
            suppressed = self._suppressed.pop(key, 0)
        if suppressed:
            record.msg = f"{key} (%d similar failures suppressed since the last report)"
            record.args = (*(record.args or ()), suppressed)
        return True

    def reset(self) -> None:
        """Forget every message. Test-only: production never resets."""
        with self._lock:
            self._last_passed.clear()
            self._suppressed.clear()


_self_failure_rate_limit = _SelfFailureRateLimit(SELF_FAILURE_LOG_INTERVAL_SECONDS)

#: Logger for handled failures of the observability pipeline itself — an
#: exporter, sink or buffer that failed while the application carried on.
#: Log at WARNING with ``exc_info=True`` from inside the ``except`` block: the
#: failure was handled, and ERROR is for operations that failed. It is a
#: stdlib logger, never the SDK logger, so a broken sink cannot recurse into
#: itself; records propagate to the root handlers exactly as ``logging.*``
#: calls do. At most one record per message per
#: :data:`SELF_FAILURE_LOG_INTERVAL_SECONDS`.
self_failure_logger = logging.getLogger("application_sdk.observability.self_failure")
self_failure_logger.addFilter(_self_failure_rate_limit)


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
    try:
        from temporalio import (  # noqa: PLC0415 — cold path: consulted only on flush/shutdown paths
            workflow,
        )
    except ModuleNotFoundError as exc:
        if not worker_only_missing(exc):
            raise
        # api-only install (the API host): no Temporal, so never workflow code.
        return False

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
        self_failure_logger.warning(
            "Failed to parse OTLP resource attributes", exc_info=True
        )
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
