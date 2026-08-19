"""Settings dataclasses for the execution layer.

All settings have environment-variable loaders and reasonable defaults.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


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

    return InterceptorSettings(
        enable_event_interceptor=_bool("APPLICATION_SDK_ENABLE_EVENT_INTERCEPTOR"),
        enable_output_interceptor=_bool("APPLICATION_SDK_ENABLE_OUTPUT_INTERCEPTOR"),
        enable_sizing_telemetry=_bool(
            "APPLICATION_SDK_ENABLE_SIZING_TELEMETRY", default=False
        ),
        sizing_telemetry_poll_seconds=_float(
            "APPLICATION_SDK_SIZING_TELEMETRY_POLL_SECONDS", 1.0
        ),
        enable_cleanup_interceptor=_bool("APPLICATION_SDK_ENABLE_CLEANUP_INTERCEPTOR"),
    )
