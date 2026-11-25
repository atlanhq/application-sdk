"""Concurrency utilities — CPU detection, thread pool sizing, and offloading.

``run_in_thread`` is re-exported here for handler code, which the consolidated
API host serves without the worker SDK: its usual app-facing path,
``application_sdk.execution.heartbeat``, loads the Temporal execution layer.
Both paths are the same function.
"""

from typing_extensions import deprecated

from application_sdk._logging import get_logger
from application_sdk._runtime.cgroup import available_cpus
from application_sdk._runtime.offload import (  # noqa: F401 — re-exported for handler code
    run_in_thread,
)

logger = get_logger(__name__)


def get_actual_cpu_count() -> int:
    """Whole CPUs this process may use: the tighter of affinity and the CPU limit.

    ``os.cpu_count()`` and the affinity mask both report the node's cores, not
    the pod's: a worker limited to one CPU on a 16-core node sees 16 from
    either. This also reads the container's CFS quota (cgroup v2 ``cpu.max``,
    else v1 ``cpu.cfs_quota_us``), so it returns 1 there. A fractional quota
    rounds down, never below 1.

    It is the same number the SDK sizes its own thread and process pools
    from (:func:`application_sdk._runtime.cgroup.available_cpus`).

    Returns:
        Number of CPUs available, at least 1.
    """
    return available_cpus()


@deprecated(
    "get_safe_num_threads is deprecated; use get_actual_cpu_count() and size "
    "the pool for your workload, or run_in_thread, whose pool the SDK already "
    "sizes from the CPU limit — will be removed in v4.0."
)
def get_safe_num_threads() -> int:
    """Get recommended number of threads for parallel processing.

    .. deprecated:: 3.58.0
        Use :func:`get_actual_cpu_count` and pick a multiple that suits the
        workload, or :func:`run_in_thread`. Will be removed in v4.0.

    Returns:
        2x the number of available CPU cores, minimum 2.
    """
    return available_cpus() * 2
