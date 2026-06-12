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
    """Maximum concurrent activities per worker. Only used when
    ``worker_tuner_mode`` is ``fixed``."""

    worker_tuner_mode: str = "fixed"
    """Worker slot tuning mode: ``fixed`` or ``resource``.

    ``fixed`` (the default) hands out a static number of activity slots
    (``max_concurrent_activities``) regardless of pod size — resource-blind,
    so pods must be provisioned for worst-case parallelism.
    ``resource`` uses Temporal's resource-based ``WorkerTuner`` to throttle
    slot handout based on observed CPU/memory usage, letting small pods run
    safely without OOMing under fanout. When ``resource`` is active, the
    fixed ``max_concurrent_activities`` / ``max_concurrent_workflow_tasks``
    limits are not passed to the worker (Temporal forbids combining them
    with a tuner). See ADR-0016.
    """

    tuner_target_memory_usage: float = 0.8
    """Target fraction of system memory the resource-based tuner aims to keep
    in use, in ``(0, 1]``. Only used when ``worker_tuner_mode`` is
    ``resource``."""

    tuner_target_cpu_usage: float = 0.9
    """Target fraction of system CPU the resource-based tuner aims to keep
    in use, in ``(0, 1]``. Only used when ``worker_tuner_mode`` is
    ``resource``."""

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


def _load_tuner_mode(env_var: str) -> str:
    """Parse a worker tuner mode from ``env_var``.

    Accepts ``fixed`` / ``resource`` (case-insensitive). Any unset, empty, or
    unrecognized value falls back to the safe default, ``fixed``.
    """
    val = os.environ.get(env_var, "").strip().lower()
    if val == "resource":
        return "resource"
    if val and val != "fixed":
        logger.warning("%s=%r not recognized; falling back to fixed", env_var, val)
    return "fixed"


def _load_tuner_target(env_var: str, default: float) -> float:
    """Parse a resource-tuner target fraction from ``env_var``.

    Valid values are floats in ``(0, 1]``. Any unset value uses ``default``;
    invalid or out-of-range values warn and fall back to ``default``.
    """
    raw = os.environ.get(env_var, "").strip()
    if not raw:
        return default
    try:
        val = float(raw)
    except ValueError:
        logger.warning(
            "%s=%r is not a valid float; falling back to %s", env_var, raw, default
        )
        return default
    if not 0.0 < val <= 1.0:
        logger.warning(
            "%s=%r must be in (0, 1]; falling back to %s", env_var, raw, default
        )
        return default
    return val


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
        worker_tuner_mode=_load_tuner_mode("ATLAN_WORKER_TUNER_MODE"),
        tuner_target_memory_usage=_load_tuner_target(
            "ATLAN_WORKER_TUNER_TARGET_MEMORY", 0.8
        ),
        tuner_target_cpu_usage=_load_tuner_target("ATLAN_WORKER_TUNER_TARGET_CPU", 0.9),
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

    return InterceptorSettings(
        enable_event_interceptor=_bool("APPLICATION_SDK_ENABLE_EVENT_INTERCEPTOR"),
        enable_output_interceptor=_bool("APPLICATION_SDK_ENABLE_OUTPUT_INTERCEPTOR"),
        enable_cleanup_interceptor=_bool("APPLICATION_SDK_ENABLE_CLEANUP_INTERCEPTOR"),
    )
