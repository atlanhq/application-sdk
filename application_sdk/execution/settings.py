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
    """Collect per-activity peak memory and CPU throttling. Off by default, and
    independent of the allow-list so collection can be stopped in one place.
    """

    sizing_telemetry_activities: frozenset[str] = frozenset()
    """Activity names to measure; empty collects nothing, so a half-finished config
    change is fail-closed. ``"*"`` measures all of them.
    """

    sizing_telemetry_poll_seconds: float = 1.0
    """Poll interval when the kernel watermark is unusable. Two file reads a tick,
    no RPC; ``0`` disables polling and collects no peak on kernels before 6.8.
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
        # Clamp rather than raise: a bad value must not stop workers starting.
        return max(0.0, value)

    def _name_set(env_var: str) -> frozenset[str]:
        """Parse a comma-separated allow-list, tolerating Helm-file whitespace."""
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
