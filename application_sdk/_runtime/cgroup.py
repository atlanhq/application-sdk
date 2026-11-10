"""The container's CPU limit, readable from the bottom layer (ADR-0019).

``os.cpu_count()`` reports the node's cores, not the pod's: a worker limited to
one CPU on a 16-core node sees 16. Anything sized by CPU — the ``sdk-blocking-``
thread pool and the fault-isolation process pool in
:mod:`application_sdk._runtime.offload`, and the worker's default activity slots
— reads the limit here instead. :mod:`application_sdk.observability.cgroup` reports the
same number for sizing telemetry and delegates to this module, so the two can
never disagree about what the limit is.

Readers return ``None`` for "no usable limit", never a guessed number.
"""

from __future__ import annotations

import math
import os

from application_sdk._logging import get_logger

logger = get_logger(__name__)

# v2 first: a host with both mounted must read v2, which is what the kernel enforces.
CPU_MAX_V2 = "/sys/fs/cgroup/cpu.max"
CPU_QUOTA_V1 = "/sys/fs/cgroup/cpu/cpu.cfs_quota_us"
CPU_PERIOD_V1 = "/sys/fs/cgroup/cpu/cpu.cfs_period_us"


def _read(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip()
    # conformance: ignore[E014] a missing cgroup file is the expected case off Linux and under partial hierarchies; None is the meaningful answer, and logging per read would fire on every call
    except OSError:
        return None


def _read_positive_int(path: str) -> int | None:
    raw = _read(path)
    if not raw:
        return None
    try:
        value = int(raw)
    # conformance: ignore[E014] an unparseable v1 file means "no usable number", the same answer as a missing one
    except ValueError:
        return None
    # v1 writes -1 for unlimited.
    return value if value > 0 else None


def cpu_quota_cores(cpu_max_v2: str, quota_v1: str, period_v1: str) -> float | None:
    """The container's CPU limit in cores, or ``None`` when there is none.

    The paths are parameters so a caller that keeps its own (patchable) copies
    reads through the same parser rather than a second one; pass this module's
    ``CPU_MAX_V2`` / ``CPU_QUOTA_V1`` / ``CPU_PERIOD_V1`` for the real ones.
    """
    raw = _read(cpu_max_v2)
    if raw:
        parts = raw.split()
        if len(parts) == 2 and parts[0] != "max":
            try:
                quota, period = float(parts[0]), float(parts[1])
            except ValueError:
                logger.debug("unparseable cpu.max: %r", raw, exc_info=True)
            else:
                if quota > 0 and period > 0:
                    return quota / period

    quota_us = _read_positive_int(quota_v1)
    period_us = _read_positive_int(period_v1)
    if quota_us and period_us:
        return quota_us / period_us
    return None


def _affinity_count() -> int:
    """CPUs in this process's affinity mask, or the node's count without one."""
    try:
        return len(os.sched_getaffinity(0)) or 1  # type: ignore[attr-defined]
    except AttributeError:
        # No affinity API (macOS, Windows): the node's count is all there is.
        return os.cpu_count() or 1


def available_cpus() -> int:
    """Whole CPUs this process may use: the tighter of affinity and the CFS quota.

    A fractional quota rounds down (1.5 cores → 1), never below 1: a pool sized
    to the rounded-up figure would run more CPU-bound children than the quota
    can schedule at once, and the extra ones only add memory and throttling.
    """
    cpus = _affinity_count()
    quota = cpu_quota_cores(CPU_MAX_V2, CPU_QUOTA_V1, CPU_PERIOD_V1)
    if quota is not None:
        cpus = min(cpus, max(1, math.floor(quota)))
    return cpus
