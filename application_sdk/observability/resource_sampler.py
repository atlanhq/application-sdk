"""Process-level resource sampling for App Vitals efficiency metrics.

Captures CPU time and memory usage at activity boundaries so the
interceptor can emit ``activity_cpu_seconds`` and ``activity_mem_gb_sec``
(RFC metrics #10 and #11 from DESIGN.md §4.1).

Uses stdlib ``resource`` module — no external dependencies (psutil removed).
On Linux, reads ``/proc/self/stat`` for RSS when available. Falls back to
``resource.getrusage()`` on macOS and other platforms.

Scope limitation: these are **process-level** samples, not per-task
attribution. For a worker running one activity at a time, the delta is
accurate. For concurrent activities, values over-attribute per activity
but the sum remains correct.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class ResourceSample:
    """A single point-in-time sample of process CPU + memory."""

    cpu_time_s: float  # user + system CPU seconds consumed by the process so far
    rss_bytes: int  # resident set size in bytes (memory currently in RAM)


def _read_proc_rss() -> int | None:
    """Read RSS from /proc/self/stat (Linux only, no dependencies)."""
    try:
        with open("/proc/self/stat", "rb") as f:
            parts = f.read().split()
        # Field 24 (index 23) is RSS in pages
        rss_pages = int(parts[23])
        page_size = os.sysconf("SC_PAGE_SIZE")
        return rss_pages * page_size
    except Exception:
        return None


def sample() -> ResourceSample | None:
    """Capture current process CPU time and memory.

    Returns:
        ResourceSample, or None if sampling fails (e.g. on Windows).
    """
    try:
        import resource
    except ImportError:
        # resource module not available on Windows
        return None

    try:
        usage = resource.getrusage(resource.RUSAGE_SELF)
        cpu_time = usage.ru_utime + usage.ru_stime

        # Try /proc for accurate RSS on Linux
        rss = _read_proc_rss()
        if rss is None:
            # macOS: ru_maxrss is in bytes; Linux: in KB (but we use /proc above)
            rss = usage.ru_maxrss
            if sys.platform == "linux":
                rss *= 1024  # Linux reports KB

        return ResourceSample(cpu_time_s=cpu_time, rss_bytes=rss)
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
