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
    """Settings for Temporal interceptors."""

    enable_event_interceptor: bool = True
    """Enable lifecycle event publishing interceptor."""

    enable_correlation_interceptor: bool = True
    """Enable correlation context propagation interceptor."""

    enable_cleanup_interceptor: bool = True
    """Enable temp-directory cleanup interceptor."""


def load_execution_settings() -> ExecutionSettings:
    """Load execution settings from environment variables."""
    return ExecutionSettings(
        host=os.environ.get("TEMPORAL_HOST", "localhost:7233"),
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

    return InterceptorSettings(
        enable_event_interceptor=_bool("APPLICATION_SDK_ENABLE_EVENT_INTERCEPTOR"),
        enable_correlation_interceptor=_bool(
            "APPLICATION_SDK_ENABLE_CORRELATION_INTERCEPTOR"
        ),
        enable_cleanup_interceptor=_bool("APPLICATION_SDK_ENABLE_CLEANUP_INTERCEPTOR"),
    )
