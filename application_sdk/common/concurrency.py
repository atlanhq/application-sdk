"""Concurrency utilities — CPU detection and thread pool sizing."""

import os


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
        return os.cpu_count() or 1


def get_safe_num_threads() -> int:
    """Get recommended number of threads for parallel processing.

    Returns:
        2x the number of available CPU cores, minimum 2.
    """
    return get_actual_cpu_count() * 2 or 2
