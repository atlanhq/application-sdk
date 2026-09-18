"""Pod-scoped record of whether this container start is a restart, and what to do
about one.

A container that exceeds its memory limit is killed by the kernel and restarted
by the kubelet *inside the same pod*, on the limit that just killed it. Something
outside has to replace the pod before the memory can change, and until it does a
worker that resumes polling takes work straight back onto a pod that cannot hold
it. So a restarted worker idles instead, for a bounded time, and resumes either
way.

Kubernetes 1.33 made a running pod's resources mutable through ``pods/resize``,
so the spec is no longer immutable in general. On the vcluster platform this
deploys to, a resize is accepted and never actuated: VPA in ``InPlaceOrRecreate``
and a direct patch of the subresource both leave ``allocatedResources`` and the
container's own ``memory.max`` unchanged, and no ``PodResizePending`` condition
is ever set. Replacing the pod remains the only thing that changes its memory.

That last paragraph is a claim about a platform that will move. Measured
2026-09-15 on an internal tenant, EKS 1.33, VPA 1.6.0. On the day the vcluster
starts actuating a resize, this module has no reason to exist - so re-run it
before trusting the date, and delete the module rather than the paragraph.

The marker is written on a start and removed on a clean return, so an abnormal
exit is what leaves it behind. Written at birth rather than at death because the
kill arrives without warning and no handler runs.

It lives on a memory-backed ``emptyDir`` whose lifetime is the pod's: it survives
every container restart and vanishes with the pod. Nothing here creates that
directory, so the volume is the switch - without it nothing is detected and
nothing waits.

The marker says a restart happened; it cannot say why, and it cannot tell whether
anything is coming to replace the pod. Both are asked of the activity rerouter
over ``ATLAN_RESTART_ADVICE_URL``, which sees the kill and owns the eviction.
Asking rather than reading Kubernetes directly keeps this module free of any
grant, and keeps the policy somewhere it can change without releasing this
package to the fleet.

Every failure path falls through to a normal start: no volume, an unreadable
marker, no endpoint, an answer that does not arrive or does not parse.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import httpx

from application_sdk.constants import DIRTY_RESTART_IDLE_MAX_SECONDS, OOM_RESTART_CHECK
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)

#: The marker: a memory-backed ``emptyDir`` in the chart, so it survives a
#: container restart and dies with the pod. Nothing here creates it.
MARKER_DIR = Path("/run/atlan")
MARKER_NAME = "worker.json"

#: One row a minute while waiting, so a parked pod is visible in logs without
#: the wait itself becoming log volume.
HEARTBEAT_SECONDS = 60

#: How often the wait wakes to check whether it is done.
RECHECK_SECONDS = 5

#: The one value of the check switch that does anything. The other is the
#: default, so only this needs naming.
CHECK_API = "api"


#: Where to ask what this restart earns. The rerouter serves it; a worker that
#: cannot reach it polls, so an unset value turns the question off rather than
#: breaking a start.
ADVICE_URL_ENV = "ATLAN_RESTART_ADVICE_URL"

#: Short and unretried on purpose. This sits between a restarted container and
#: its first poll, so a slow answer has to cost less than the answer is worth. A
#: miss means polling now, which is what happened before any of this existed.
ADVICE_TIMEOUT_SECONDS = 2.0


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
    directory = MARKER_DIR
    if not directory.is_dir():
        # Nothing here creates it. Its absence means the volume is not mounted,
        # and a marker on the container filesystem would be discarded with every
        # restart - detecting nothing, forever. The writes below would fail
        # anyway; this is the only thing that says why.
        # Debug, not warning: most of the fleet does not mount the volume, so this
        # is the intended default rather than a problem. It still has to say why
        # detection is off for anyone who mounted it and expected it to work.
        logger.debug(
            "%s is not a directory, so a restarted worker cannot be told apart from a "
            "fresh one and this worker will always poll immediately. Mount a small "
            "memory-backed emptyDir there to enable it.",
            directory,
        )
        return 0

    path = MARKER_DIR / MARKER_NAME
    try:
        # A count, not a document: the file existing is the signal and the number
        # only separates the first restart from later ones.
        restarted_count = max(0, int(path.read_text()))
    except FileNotFoundError:
        restarted_count = 0  # a fresh pod, not an error - and it still needs its marker
    except (OSError, ValueError):  # UnicodeDecodeError is a ValueError
        # The file is there but unreadable, which still means an earlier container
        # started here. One is the answer that changes behaviour.
        logger.warning(
            "%s is not readable as a marker; treating it as one start",
            path,
            exc_info=True,
        )
        restarted_count = 1

    try:
        path.write_text(str(restarted_count + 1))
    except OSError:
        # Only the next start's count is lost, not any decision taken here.
        logger.warning("could not write %s", path, exc_info=True)

    return restarted_count


def clear() -> None:
    """Remove the marker after a clean return, so the next start is not a restart."""
    path = MARKER_DIR / MARKER_NAME
    try:
        path.unlink()
    except FileNotFoundError:
        logger.debug("no marker at %s to remove; nothing wrote one", path)
    except OSError:
        logger.warning(
            "could not remove %s, so the next container start in this pod will be "
            "treated as a restart",
            path,
            exc_info=True,
        )


async def wait_if_pod_restarted(shutdown_event: asyncio.Event) -> None:
    """Hold this worker back if an earlier container already ran in this pod.

    Call this after the health server is serving and signal handlers are
    installed, and before anything builds a worker: the process is up and
    answering probes while it waits, but it has no pollers.

    Returns when the worker should proceed. A shutdown requested while waiting
    also returns; the caller sees it on ``shutdown_event`` as it would anywhere
    else.
    """
    # Two deployments of one image differ only in this configuration, so this row
    # is the only way to read back from a pod's own log which handling it is
    # running - and to tell a pod that decided not to act from one that was never
    # able to.
    logger.info(
        "restart handling: check=%s advice=%s budget=%ds marker=%s",
        OOM_RESTART_CHECK,
        os.getenv(ADVICE_URL_ENV, "") or "(unset)",
        DIRTY_RESTART_IDLE_MAX_SECONDS,
        MARKER_DIR,
    )

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

    if restarted_count > 1:
        # The wait on the previous start did not get this pod replaced, so the
        # thing it waits for is not coming and waiting again buys nothing. One
        # bounded penalty per pod instead of one per restart, which is what a
        # crash loop would otherwise pay forever.
        logger.warning(
            "container start %d in this pod: the wait on the previous start did not get "
            "it replaced, so this one polls instead of waiting again",
            restarted_count + 1,
        )
        return

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
        await act_on_restart(shutdown_event, budget)
    except Exception:
        # The worst outcome of this whole feature has to be a worker that starts
        # normally, so nothing raised in here reaches the caller.
        logger.error(
            "could not hold this worker back; starting it normally", exc_info=True
        )


async def act_on_restart(shutdown_event: asyncio.Event, budget: int) -> None:
    """Ask what this restart earns, and wait only if the answer says to.

    The worker cannot answer this itself: it knows an earlier container in its
    pod did not exit cleanly, not whether the kernel took it for memory and not
    whether anything is coming to replace the pod. Both live in the rerouter, so
    they are asked for rather than worked out here - which is also what keeps
    this file free of Kubernetes and free of any grant.

    Every way of not getting a clear yes leads to polling: the check switched
    off, no endpoint configured, a timeout, a refusal, a body that will not
    parse, or an answer of no. Holding a worker back is only worth doing while
    something is on its way, and none of those establish that.
    """
    if OOM_RESTART_CHECK != CHECK_API:
        logger.info(
            "not asking what this restart earns (ATLAN_OOM_RESTART_CHECK=%s), so this "
            "worker starts polling",
            OOM_RESTART_CHECK,
        )
        return

    advice = await ask_what_this_restart_earns()
    if advice is None:
        return

    wait, reason, seconds = advice
    if not wait:
        logger.warning(
            "nothing is coming to replace this pod (%s), so this worker starts polling "
            "on the limit it already has",
            reason,
        )
        return

    # The answer is advisory and the budget is the bound: a wait longer than the
    # budget would outlive the retries it is protecting.
    seconds = min(seconds, budget) if seconds > 0 else budget
    logger.warning(
        "this pod is due to be replaced (%s), so this worker holds off polling for up "
        "to %ds",
        reason,
        seconds,
    )
    await wait_for_pod_to_get_replaced(shutdown_event, seconds)


async def ask_what_this_restart_earns() -> tuple[bool, str, int] | None:
    """Ask the rerouter about this pod. ``None`` when there was no usable answer.

    Named from this pod's own identity every time, so it can only ever ask about
    itself.
    """
    url = os.getenv(ADVICE_URL_ENV, "").strip()
    # Its own name is all this pod has to say. Which namespace it is in and which
    # container died are things the rerouter recorded when it saw the kill, so
    # sending them would be repeating what is already known - and would let a pod
    # ask about a pod that is not itself.
    #
    # Same source the OTel resource attributes and the sizing interceptor use, so
    # a pod names itself one way across the SDK. HOSTNAME is the kubelet's own
    # copy of the pod name, which makes the explicit variable optional.
    pod = (os.getenv("K8S_POD_NAME") or os.getenv("HOSTNAME") or "").strip()
    if not url or not pod:
        logger.info(
            "cannot ask what this restart earns (%s=%r, pod=%r), so this worker "
            "starts polling",
            ADVICE_URL_ENV,
            url,
            pod,
        )
        return None

    try:
        async with httpx.AsyncClient(timeout=ADVICE_TIMEOUT_SECONDS) as client:
            response = await client.get(url, params={"pod": pod})
        response.raise_for_status()
        body = response.json()
        return (
            bool(body.get("wait")),
            str(body.get("reason") or "no reason given"),
            int(body.get("waitSeconds") or 0),
        )
    except Exception:
        logger.warning(
            "could not find out what this restart earns, so this worker starts polling",
            exc_info=True,
        )
        return None


async def wait_for_pod_to_get_replaced(
    shutdown_event: asyncio.Event, budget: int
) -> None:
    """Idle until this pod is replaced, or the budget is spent."""
    logger.warning(
        "not polling for up to %ds, waiting to be replaced by a pod that can hold "
        "this work",
        budget,
    )
    deadline = time.monotonic() + budget
    last_beat = time.monotonic()
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            logger.warning(
                "waited %ds and nothing replaced this pod; starting the worker on the "
                "limit that already failed",
                budget,
            )
            return
        try:
            # One await does two jobs: it is the sleep, and it is the shutdown
            # listener. The min() lands the last pass exactly on the deadline.
            await asyncio.wait_for(
                shutdown_event.wait(), timeout=min(RECHECK_SECONDS, remaining)
            )
        except TimeoutError:  # conformance: ignore[E002] the timeout is the sleep expiring, not a failure: it fires every RECHECK_SECONDS for the whole wait and means nobody asked us to stop
            pass
        else:
            # conformance: ignore[L006] fires once and returns: this is the loop
            # ending, not a row per pass through it.
            logger.info(
                "shutdown requested while waiting, which is this pod going away"
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
