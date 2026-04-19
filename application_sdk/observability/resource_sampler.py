"""Process-level resource sampling for App Vitals efficiency metrics.

Captures CPU time and memory usage at activity boundaries so the
interceptor can emit ``activity_cpu_seconds`` and ``activity_mem_gb_sec``
(RFC metrics #10 and #11 from DESIGN.md §4.1).

Scope limitation: these are **process-level** samples via psutil, not
per-task attribution. For a worker running one activity at a time, the
delta is accurate. For a worker running multiple activities concurrently,
concurrent activities share the same process so the values over-attribute
CPU/memory to each concurrent activity individually — but the sum across
activities remains correct for workload-total calculations.

Per-activity attribution (cgroup delegation, cgroup v2 PID controllers,
or per-task CPU accounting in Python 3.12+) is on the look-later list.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

try:
    import psutil

    _PROCESS: psutil.Process | None = psutil.Process(os.getpid())
except Exception:
    _PROCESS = None


@dataclass(frozen=True)
class ResourceSample:
    """A single point-in-time sample of process CPU + memory."""

    cpu_time_s: float  # user + system CPU seconds consumed by the process so far
    rss_bytes: int  # resident set size in bytes (memory currently in RAM)


def sample() -> ResourceSample | None:
    """Capture current process CPU time and memory.

    Returns:
        ResourceSample, or None if sampling is unavailable (e.g. psutil
        import failed or the process handle is invalid).
    """
    if _PROCESS is None:
        return None
    try:
        times = _PROCESS.cpu_times()
        mem = _PROCESS.memory_info()
        return ResourceSample(
            cpu_time_s=times.user + times.system,
            rss_bytes=mem.rss,
        )
    except Exception:
        return None


def compute_deltas(
    start: ResourceSample | None,
    end: ResourceSample | None,
    duration_s: float,
) -> tuple[float, float]:
    """Compute ``(cpu_seconds, mem_gb_sec)`` for the activity.

    ``cpu_seconds`` = CPU time consumed during the activity window.
    ``mem_gb_sec`` = average RSS in GiB multiplied by wall-clock duration
    in seconds. This is the memory-time integral approximation the RFC
    uses (unit: ``GiB.s``).

    Returns (0.0, 0.0) if either sample is None.
    """
    if start is None or end is None:
        return 0.0, 0.0

    cpu_seconds = max(0.0, end.cpu_time_s - start.cpu_time_s)

    avg_rss_bytes = (start.rss_bytes + end.rss_bytes) / 2.0
    avg_rss_gib = avg_rss_bytes / (1024**3)
    mem_gb_sec = avg_rss_gib * max(0.0, duration_s)

    return cpu_seconds, mem_gb_sec
