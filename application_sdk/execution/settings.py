"""Settings dataclasses for the execution layer.

All settings have environment-variable loaders and reasonable defaults.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# VersioningBehavior is a temporalio type. Importing it directly here leaks the
# execution-backend dependency, but temporalio is a hard core dependency so the
# coupling is acceptable. Re-exporting via _temporal/ would invert the layer
# direction without providing any real isolation.
from temporalio.common import VersioningBehavior

from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class ExecutionSettings:
    """Settings for the Temporal execution layer."""

    host: str = "localhost:7233"
    """Temporal server address."""

    namespace: str = "default"
    """Temporal namespace."""

    task_queue: str = "application-sdk"
    """Default task queue for workers."""

    max_concurrent_activities: int = 100
    """Maximum concurrent activities per worker."""

    graceful_shutdown_timeout_seconds: int = 3600
    """Seconds to wait for in-flight activities to complete during worker shutdown."""

    default_versioning_behavior: VersioningBehavior = VersioningBehavior.PINNED
    """Default Worker Deployment versioning behavior for workflows that do not
    set one explicitly on ``@workflow.defn``.

    ``PINNED`` (the default) keeps an in-flight workflow on the build it started
    on until it finishes, so an incompatible release can never break a running
    workflow mid-execution — the safe choice for the broad connector fleet.
    ``AUTO_UPGRADE`` migrates in-flight workflows to the new ``CURRENT`` build at
    their next workflow task, letting old builds drain and scale down sooner.

    Opt into ``AUTO_UPGRADE`` per-app (set the env var in the app's own
    deployment), and only when the app owner guarantees replay-determinism
    across builds — reordering steps, parallelizing previously-sequential work,
    or renaming activities is a *breaking change* for Temporal replay even when
    the observable behavior is identical, and would fail in-flight workflows at
    the migration boundary.
    """


@dataclass(frozen=True)
class InterceptorSettings:
    """Settings for product-feature Temporal interceptors.

    The three observability interceptors (Log / Metrics / Trace) are
    unconditional and not configurable here.
    """

    enable_event_interceptor: bool = True
    """Enable lifecycle event publishing interceptor."""

    enable_output_interceptor: bool = True
    """Enable structured output collection interceptor (metrics/artifacts)."""

    enable_sizing_telemetry: bool = False
    """Collect per-activity peak memory and CPU throttling for tier sizing.

    Off by default, so an SDK version bump alone changes nothing and collection
    is a per-tenant decision. This is the "collect data" stage of collect →
    classify → productionise; it only measures, and never routes.

    A master kill switch, independent of the allow-list below: flipping this to
    false stops collection without anyone having to edit per-tenant lists.
    """

    sizing_telemetry_activities: frozenset[str] = frozenset()
    """Activity names to collect sizing telemetry for. **Empty collects nothing.**

    Sizing data is only worth collecting for activities whose resource use varies
    with the data they process — a merge, a transform, an extract. Most activities
    are fixed-cost bookkeeping, and measuring them adds rows to the dataset that
    the tier table is fitted from without adding information.

    So this is an allow-list an app author opts into by name, not a default-on
    sweep. Empty means nothing is collected, which is the fail-closed direction:
    a tenant that sets the enable flag and forgets the list gets no telemetry
    rather than telemetry on everything.

    ``"*"`` collects every activity. That is the discovery case — worth running on
    a test tenant to find out *which* activities vary before choosing names, and
    not something to ship fleet-wide.
    """

    sizing_telemetry_poll_seconds: float = 1.0
    """Peak-memory poll interval when the kernel watermark is not resettable.

    Two file reads per tick with no RPC. ``0`` disables polling, which leaves
    peak memory uncollected on any host whose ``memory.peak`` cannot be reset —
    i.e. most kernels before 6.8 — so only set it to 0 to prove a cost concern.
    """

    enable_cleanup_interceptor: bool = False
    """Enable temp-directory cleanup interceptor.

    .. deprecated::
        ``CleanupInterceptor`` is no longer registered by default. Post-run
        cleanup is now handled by ``App.on_complete()`` / ``App.cleanup_files()``.
        This setting and the ``APPLICATION_SDK_ENABLE_CLEANUP_INTERCEPTOR`` env
        var are still read by ``App.on_complete()`` to decide whether to run
        cleanup, but the interceptor itself is no longer added to the worker.
    """


def _load_versioning_behavior(env_var: str) -> VersioningBehavior:
    """Parse a worker versioning behavior from ``env_var``.

    Accepts ``PINNED`` / ``AUTO_UPGRADE`` (case-insensitive). Any unset, empty,
    or unrecognized value falls back to the safe default, ``PINNED``.
    """
    val = os.environ.get(env_var, "").strip().upper()
    if val == "AUTO_UPGRADE":
        return VersioningBehavior.AUTO_UPGRADE
    if val and val != "PINNED":
        logger.warning("%s=%r not recognized; falling back to PINNED", env_var, val)
    return VersioningBehavior.PINNED


def load_execution_settings() -> ExecutionSettings:
    """Load execution settings from environment variables."""
    # v2-compat: remove ATLAN_WORKFLOW_HOST/PORT fallbacks when all deployments use TEMPORAL_HOST.
    # Prefer TEMPORAL_HOST (v3). Fall back to ATLAN_WORKFLOW_HOST + ATLAN_WORKFLOW_PORT (v2).
    _v2_host = os.environ.get("ATLAN_WORKFLOW_HOST", "")
    _v2_port = os.environ.get("ATLAN_WORKFLOW_PORT", "7233")
    _v2_default = f"{_v2_host}:{_v2_port}" if _v2_host else "localhost:7233"
    return ExecutionSettings(
        host=os.environ.get("TEMPORAL_HOST", _v2_default),
        namespace=os.environ.get("TEMPORAL_NAMESPACE", "default"),
        task_queue=os.environ.get("TEMPORAL_TASK_QUEUE", "application-sdk"),
        max_concurrent_activities=int(
            os.environ.get("TEMPORAL_MAX_CONCURRENT_ACTIVITIES", "100")
        ),
        graceful_shutdown_timeout_seconds=int(
            os.environ.get("TEMPORAL_GRACEFUL_SHUTDOWN_TIMEOUT", "3600")
        ),
        default_versioning_behavior=_load_versioning_behavior(
            "TEMPORAL_DEFAULT_VERSIONING_BEHAVIOR"
        ),
    )


def load_interceptor_settings() -> InterceptorSettings:
    """Load interceptor settings from environment variables."""

    def _bool(env_var: str, default: bool = True) -> bool:
        val = os.environ.get(env_var, "").lower()
        if val in ("0", "false", "no"):
            return False
        if val in ("1", "true", "yes"):
            return True
        return default

    def _float(env_var: str, default: float) -> float:
        raw = os.environ.get(env_var, "").strip()
        if not raw:
            return default
        try:
            value = float(raw)
        except ValueError:
            logger.warning(
                "%s=%r is not a number; using %s",
                env_var,
                raw,
                default,
                exc_info=True,
            )
            return default
        # Negative would mean "poll in the past". Clamp rather than raise: a bad
        # value on one tenant must not stop its workers from starting.
        return max(0.0, value)

    def _name_set(env_var: str) -> frozenset[str]:
        """Parse a comma-separated activity-name allow-list.

        Tolerant of stray whitespace and empty entries, because this is hand-edited
        in a Helm values file. Anything unparseable yields an empty set, i.e. no
        collection — never accidental collection on everything.
        """
        raw = os.environ.get(env_var, "")
        return frozenset(name.strip() for name in raw.split(",") if name.strip())

    return InterceptorSettings(
        enable_event_interceptor=_bool("APPLICATION_SDK_ENABLE_EVENT_INTERCEPTOR"),
        enable_output_interceptor=_bool("APPLICATION_SDK_ENABLE_OUTPUT_INTERCEPTOR"),
        enable_sizing_telemetry=_bool(
            "APPLICATION_SDK_ENABLE_SIZING_TELEMETRY", default=False
        ),
        sizing_telemetry_activities=_name_set(
            "APPLICATION_SDK_SIZING_TELEMETRY_ACTIVITIES"
        ),
        sizing_telemetry_poll_seconds=_float(
            "APPLICATION_SDK_SIZING_TELEMETRY_POLL_SECONDS", 1.0
        ),
        enable_cleanup_interceptor=_bool("APPLICATION_SDK_ENABLE_CLEANUP_INTERCEPTOR"),
    )
