"""Pod-scoped record of whether this container start is a restart.

Written by the worker on its first start in a pod and removed when it returns
cleanly, so an abnormal exit is what leaves it behind. A later container start in
the same pod finds it there and knows it is a restart.

A container that exceeds its memory limit is killed by the kernel and restarted
by the kubelet *inside the same pod*, and a pod's resource spec is immutable for
its lifetime - so the container comes back on the limit that just killed it.
Something outside has to replace the pod before the memory can change. Until it
does, a restarted worker that resumes polling takes work straight back onto a
pod that cannot hold it, and the retries cannot succeed. So a restarted worker
waits instead of polling, for a bounded time, and resumes either way.

Written at birth, not at death: under cgroup v2 ``memory.oom.group=1`` the kernel
kills every process in the container's cgroup, PID 1 included, so no handler
runs. Recording the start and clearing it on a clean return inverts that into
something always observable.

The marker lives on a small memory-backed ``emptyDir``, whose lifetime is the
pod's: contents survive every container restart and vanish with the pod. Nothing
here creates that directory - it exists only if the volume is mounted, and an
absent one is reported rather than worked around: on a container filesystem the
marker would be discarded with every restart, so nothing would ever be detected
and nothing would say why.

The volume is the switch. Without it nothing is detected and nothing waits, so
the behaviour arrives with the deployment that mounts it rather than with an
upgrade of this package. ``ATLAN_DIRTY_RESTART_IDLE_MAX_SECONDS`` sizes the wait
and ``0`` turns it off where the volume is mounted. Every failure path falls
through to a normal start: an absent or unreadable directory, a corrupt marker,
anything raised while setting up the wait.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

from application_sdk.constants import DIRTY_RESTART_IDLE_MAX_SECONDS
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)

#: Directory holding the marker. A memory-backed ``emptyDir`` in the chart.
MARKER_DIR_ENV = "ATLAN_RESTART_MARKER_DIR"
DEFAULT_MARKER_DIR = "/run/atlan"
MARKER_NAME = "worker.json"

#: Creating this file ends a wait early, for an operator who knows the pod is
#: not going to be replaced.
RELEASE_NAME = "resume"

#: One row a minute while waiting, so a waiting pod is visible in logs without
#: the wait itself becoming log volume.
HEARTBEAT_SECONDS = 60

#: How often the wait re-checks the release file.
RECHECK_SECONDS = 5


def marker_dir() -> Path:
    return Path(os.getenv(MARKER_DIR_ENV) or DEFAULT_MARKER_DIR)


def check_and_update_the_marker() -> int:
    """Read the marker left by earlier containers in this pod, then leave one for
    this start. Returns how many times a container has already restarted here.

    0 means a fresh pod, or that the volume is missing and this cannot be known.
    Anything above 0 means an earlier container here started and did not return
    cleanly, because a clean return removes the marker.

    Reading and writing are one operation on purpose: the read has to happen
    first, and doing them separately makes it possible to write first, after
    which every start looks clean - silently, and forever.
    """
    directory = marker_dir()
    if not directory.is_dir():
        # Nothing here creates it. Its absence means the volume is not mounted,
        # and a marker on the container filesystem would be discarded with every
        # restart - detecting nothing, forever. The writes below would fail
        # anyway; this is the only thing that says why.
        logger.warning(
            "%s is not a directory, so a restarted worker cannot be told apart from a "
            "fresh one and this worker will always poll immediately. Mount a small "
            "memory-backed emptyDir there to enable it.",
            directory,
        )
        return 0

    path = directory / MARKER_NAME
    restarted_count = 0
    try:
        raw = path.read_text()
    except FileNotFoundError:
        raw = ""  # a fresh pod, not an error - and it still needs its marker
    except OSError:
        logger.warning(
            "could not read %s, so this start is treated as clean", path, exc_info=True
        )
        return 0

    if raw:
        try:
            restarted_count = max(0, int(json.loads(raw).get("starts", 0)))
        except (ValueError, TypeError, AttributeError):
            # Truncated or hand-edited. The file existing is the signal; only the
            # count is lost, and one is the answer that changes behaviour.
            logger.warning(
                "%s is not readable as a marker; treating it as one start", path
            )
            restarted_count = 1

    try:
        path.write_text(
            json.dumps(
                {
                    "pod": os.getenv("K8S_POD_NAME", ""),
                    "starts": restarted_count + 1,
                    "started_at": time.time(),
                }
            )
        )
    except OSError:
        # Only the next start's count is lost, not any decision taken here.
        logger.warning("could not write %s", path, exc_info=True)

    return restarted_count


def clear() -> None:
    """Remove the marker after a clean return, so the next start is not a restart."""
    path = marker_dir() / MARKER_NAME
    try:
        path.unlink()
    except FileNotFoundError:
        pass  # the worker may return before ever writing one
    except OSError:
        logger.warning(
            "could not remove %s, so the next container start in this pod will be "
            "treated as a restart",
            path,
            exc_info=True,
        )


async def wait_if_pod_restarted(shutdown_event: asyncio.Event) -> None:
    """Wait before polling if an earlier container already ran in this pod.

    Call this after the health server is serving and signal handlers are
    installed, and before anything builds a worker: the process is up and
    answering probes while it waits, but it has no pollers.

    Returns when the worker should proceed. A shutdown requested while waiting
    also returns; the caller sees it on ``shutdown_event`` as it would anywhere
    else.
    """
    restarted_count = check_and_update_the_marker()
    if restarted_count == 0:
        logger.debug("clean container start in this pod")
        return

    logger.warning(
        "this is container start %d in this pod - an earlier one did not return cleanly. "
        "A container killed for memory restarts here on the same limit, so polling now "
        "would take work back onto a pod that cannot hold it.",
        restarted_count + 1,
    )

    budget = DIRTY_RESTART_IDLE_MAX_SECONDS
    if budget <= 0:
        # A zero budget would fall straight through the wait below, but silently
        # and after announcing a wait of 0s. Say it is switched off instead.
        logger.warning(
            "waiting is switched off (ATLAN_DIRTY_RESTART_IDLE_MAX_SECONDS=0), so this "
            "worker starts polling anyway"
        )
        return

    try:
        await wait_for_pod_to_get_replaced(shutdown_event, budget)
    except Exception:
        # The worst outcome of this whole feature has to be a worker that starts
        # normally, so nothing raised in here reaches the caller.
        logger.exception("could not hold this worker back; starting it normally")


async def wait_for_pod_to_get_replaced(
    shutdown_event: asyncio.Event, budget: int
) -> None:
    """The bounded wait. Three exits: released, shutdown, or the budget spent."""
    release = marker_dir() / RELEASE_NAME
    deadline = time.monotonic() + budget
    last_beat = time.monotonic()
    logger.warning(
        "not polling for up to %ds, waiting to be replaced by a pod that can hold this "
        "work. Create %s to end the wait early.",
        budget,
        release,
    )
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            logger.warning(
                "waited %ds and nothing replaced this pod; starting the worker on the "
                "limit that already failed",
                budget,
            )
            return
        if release.exists():
            logger.warning("released by %s; starting the worker", release)
            return
        try:
            # One await does three jobs: it is the sleep, it is the shutdown
            # listener, and the min() lands the last pass exactly on the deadline.
            await asyncio.wait_for(
                shutdown_event.wait(), timeout=min(RECHECK_SECONDS, remaining)
            )
        except TimeoutError:
            pass  # nobody asked us to stop; keep waiting
        else:
            logger.info(
                "shutdown requested while waiting, which is this pod being replaced"
            )
            return
        now = time.monotonic()
        if now - last_beat >= HEARTBEAT_SECONDS:
            last_beat = now
            # conformance: ignore[L006] one row a minute, not a tight loop: this is
            # the only signal that a pod is deliberately idle rather than wedged,
            # and it has to be visible for the whole wait.
            logger.info(
                "still not polling, %ds of %ds left", int(deadline - now), budget
            )
