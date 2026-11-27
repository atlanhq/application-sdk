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

from application_sdk import _deprecation
from application_sdk import _env as _sdk_env
from application_sdk.common._env import env_bool, env_float, env_int, env_str
from application_sdk.constants import load_shutdown_budget
from application_sdk.observability.logger_adaptor import get_logger

__all__ = [
    "ExecutionSettings",
    "InterceptorSettings",
    "load_execution_settings",
    "load_interceptor_settings",
]


logger = get_logger(__name__)

#: Upper bound on the derived activity-slot default: the fixed default every
#: worker ran with before it was derived, so no pod gets more slots than it had.
_MAX_CONCURRENT_ACTIVITIES_CAP = 100

#: Derived activity slots per whole CPU the container may use. Activities are
#: mostly awaiting I/O, so a CPU carries many.
_ACTIVITY_SLOTS_PER_CPU = 32

#: Container memory budgeted per derived activity slot (64 MiB).
_MEMORY_BYTES_PER_ACTIVITY_SLOT = 64 * 1024 * 1024

#: The derived default never goes below this, however small the pod.
#:
#: The three factors were chosen against a week of fleet slot usage. Peak
#: concurrent activities on default-configured workers were 0-2 for most and
#: never above 30. Most pods are half a CPU with under 1 GiB, so tighter
#: factors (10 per CPU, 256 MiB per slot, no floor) would have capped about
#: one worker in ten below a peak it actually reached. These factors capped
#: none. The point is a cap that follows the pod's resources rather than one
#: fixed number, set generously enough that it limits a pod that cannot hold
#: a burst without trimming normal load.
_MIN_DERIVED_ACTIVITY_SLOTS = 16

#: Env var that sets the activity-slot count outright, bypassing derivation.
_MAX_CONCURRENT_ACTIVITIES_ENV = "TEMPORAL_MAX_CONCURRENT_ACTIVITIES"


@dataclass(frozen=True)
class ExecutionSettings:
    """Settings for the Temporal execution layer."""

    host: str = "localhost:7233"
    """Temporal server address (``TEMPORAL_HOST``).

    .. deprecated:: 3.61
        The SDK's worker and client never read this field: they connect with
        ``AppConfig.temporal_host`` (``ATLAN_TEMPORAL_HOST``). Read that, or
        ``ATLAN_TEMPORAL_HOST`` directly. Removed in v4.0.0, with the
        ``TEMPORAL_HOST`` variable that sets it.
    """

    namespace: str = "default"
    """Temporal namespace (``TEMPORAL_NAMESPACE``).

    .. deprecated:: 3.61
        Never read by the SDK's worker or client, which use
        ``AppConfig.temporal_namespace`` (``ATLAN_TEMPORAL_NAMESPACE``).
        Removed in v4.0.0, with ``TEMPORAL_NAMESPACE``.
    """

    task_queue: str = "application-sdk"
    """Default task queue for workers (``TEMPORAL_TASK_QUEUE``).

    .. deprecated:: 3.61
        Never read by the SDK's worker or client, which use
        ``AppConfig.task_queue`` (``ATLAN_TASK_QUEUE``, else derived from the
        app). Removed in v4.0.0, with ``TEMPORAL_TASK_QUEUE``.
    """

    max_concurrent_activities: int = _MAX_CONCURRENT_ACTIVITIES_CAP
    """Maximum concurrent activities per worker.

    ``TEMPORAL_MAX_CONCURRENT_ACTIVITIES`` when set, else 100. This loader
    never reads the container's limits: a worker created without an explicit
    value and without the env var derives its own default from them at
    construction (``create_worker``), so the cgroup is read once per worker
    start rather than on every settings load.
    """

    graceful_shutdown_timeout_seconds: int = 3600
    """Seconds to wait for in-flight activities to complete during worker shutdown.

    ``TEMPORAL_GRACEFUL_SHUTDOWN_TIMEOUT`` when set; otherwise derived from the
    pod's grace period (``ATLAN_SHUTDOWN_GRACE_SECONDS``, see
    :class:`application_sdk.constants.ShutdownBudget`) when that is set;
    otherwise 3600.
    """

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
    """Enable lifecycle event publishing interceptor.

    Read once at worker start. The workflow side of the interceptor is always
    registered; this value is pinned on each run the worker starts, so a run
    publishes (or not) according to the worker that started it, wherever it
    continues or replays.
    """

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
        This setting (``APPLICATION_SDK_ENABLE_CLEANUP_INTERCEPTOR``) still
        decides whether ``App.on_complete()`` runs cleanup: the worker reads it
        once at start and pins it on each run it starts, and ``on_complete``
        reads the run's pinned value. The interceptor itself is no longer added
        to the worker.
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


def _load_graceful_shutdown_timeout() -> int:
    """Explicit ``TEMPORAL_GRACEFUL_SHUTDOWN_TIMEOUT``, else the grace-derived drain.

    With neither set the drain stays 3600 s, the value every deployment ran
    with before the shutdown budget existed. An empty, malformed or negative
    explicit value warns and falls back the same way.
    """
    budget = load_shutdown_budget()
    fallback = (
        budget.drain_seconds
        if budget is not None
        else ExecutionSettings.graceful_shutdown_timeout_seconds
    )
    return env_int("TEMPORAL_GRACEFUL_SHUTDOWN_TIMEOUT", fallback, minimum=0)


def _derive_max_concurrent_activities(cpus: int, memory_bytes: int | None) -> int:
    """Activity slots a container with these limits can run without oversubscribing.

    ``min(100, 32 x cpus, memory_bytes // 64 MiB)``, never below 16. An unknown
    memory limit (``None``) drops the memory term rather than guessing one.
    """
    slots = min(_MAX_CONCURRENT_ACTIVITIES_CAP, cpus * _ACTIVITY_SLOTS_PER_CPU)
    if memory_bytes is not None:
        slots = min(slots, memory_bytes // _MEMORY_BYTES_PER_ACTIVITY_SLOT)
    return max(_MIN_DERIVED_ACTIVITY_SLOTS, slots)


def _load_max_concurrent_activities() -> int:
    """``TEMPORAL_MAX_CONCURRENT_ACTIVITIES``, else 100. Never reads the cgroup."""
    explicit = _explicit_max_concurrent_activities()
    return _MAX_CONCURRENT_ACTIVITIES_CAP if explicit is None else explicit


def _explicit_max_concurrent_activities() -> int | None:
    """``TEMPORAL_MAX_CONCURRENT_ACTIVITIES`` as a positive int, else ``None``.

    ``None`` when unset or blank, and when the value is not a positive integer
    (which warns): the caller then uses its default as if nothing were set.
    """
    explicit = env_int(_MAX_CONCURRENT_ACTIVITIES_ENV, 0, minimum=1)
    return explicit if explicit >= 1 else None


def _warn_deprecated_temporal_env() -> None:
    """Warn for each ``TEMPORAL_*`` variable that sets a deprecated field.

    They are a third Temporal env family beside ``ATLAN_TEMPORAL_*`` (what the
    worker and client read) and the v2 ``ATLAN_WORKFLOW_*``. Still read until
    v4.0.0 because an app reads ``ExecutionSettings.host``; a deployment that
    sets one is told which name the SDK itself uses.
    """
    if env_str("TEMPORAL_HOST", ""):
        _deprecation.warn_deprecated(
            "TEMPORAL_HOST",
            "ATLAN_TEMPORAL_HOST",
            "4.0.0",
            note="it sets only the deprecated ExecutionSettings.host, which the SDK's worker and client never read",
            stacklevel=3,
        )
    if env_str("TEMPORAL_NAMESPACE", ""):
        _deprecation.warn_deprecated(
            "TEMPORAL_NAMESPACE",
            "ATLAN_TEMPORAL_NAMESPACE",
            "4.0.0",
            note="it sets only the deprecated ExecutionSettings.namespace, which the SDK's worker and client never read",
            stacklevel=3,
        )
    if env_str("TEMPORAL_TASK_QUEUE", ""):
        _deprecation.warn_deprecated(
            "TEMPORAL_TASK_QUEUE",
            "ATLAN_TASK_QUEUE",
            "4.0.0",
            note="it sets only the deprecated ExecutionSettings.task_queue, which the SDK's worker and client never read",
            stacklevel=3,
        )


def load_execution_settings() -> ExecutionSettings:
    """Load execution settings from environment variables.

    ``host``, ``namespace`` and ``task_queue`` are deprecated (see their
    docstrings) but still loaded, so a caller reading them sees no change
    until v4.0.0.
    """
    _warn_deprecated_temporal_env()
    return ExecutionSettings(
        # v2-compat fallback: ATLAN_WORKFLOW_HOST + ATLAN_WORKFLOW_PORT.
        host=env_str(
            "TEMPORAL_HOST", _sdk_env.legacy_workflow_host() or "localhost:7233"
        ),
        namespace=env_str("TEMPORAL_NAMESPACE", "default"),
        task_queue=env_str("TEMPORAL_TASK_QUEUE", "application-sdk"),
        max_concurrent_activities=_load_max_concurrent_activities(),
        graceful_shutdown_timeout_seconds=_load_graceful_shutdown_timeout(),
        default_versioning_behavior=_load_versioning_behavior(
            "TEMPORAL_DEFAULT_VERSIONING_BEHAVIOR"
        ),
    )


def load_interceptor_settings() -> InterceptorSettings:
    """Load interceptor settings from environment variables."""

    def _bool(env_var: str, default: bool = True) -> bool:
        return env_bool(env_var, default)

    def _float(env_var: str, default: float) -> float:
        # Clamp rather than raise: a bad value must not stop workers starting.
        return max(0.0, env_float(env_var, default))

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
