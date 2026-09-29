"""Concurrency utilities — CPU detection, thread pool sizing, and offloading.

``run_in_thread`` is re-exported here for handler code, which the consolidated
API host serves without the worker SDK: its usual app-facing path,
``application_sdk.execution.heartbeat``, loads the Temporal execution layer.
Both paths are the same function.
"""

import os

from application_sdk._runtime.offload import (  # noqa: F401 — re-exported for handler code
    run_in_thread,
)
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


def get_actual_cpu_count() -> int:
    """Get the actual number of CPUs available to the current process.

    Uses CPU affinity when available (handles cgroups/container limits
    correctly). Falls back to ``os.cpu_count()`` on platforms without
    ``sched_getaffinity``.

    Returns:
        Number of CPUs available.
    """
    try:
        return len(os.sched_getaffinity(0)) or 1  # type: ignore[attr-defined]
    except AttributeError:
        logger.warning(
            "sched_getaffinity unavailable on this platform; falling back to os.cpu_count()",
            exc_info=True,
        )
        return os.cpu_count() or 1


def get_safe_num_threads() -> int:
    """Get recommended number of threads for parallel processing.

    Returns:
        2x the number of available CPU cores, minimum 2.
    """
    return get_actual_cpu_count() * 2 or 2
