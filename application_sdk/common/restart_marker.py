"""Pod-scoped record of whether this container start is a restart, and what to do
about one.

Written by the worker on its first start in a pod and removed when it returns
cleanly, so an abnormal exit is what leaves it behind. A later container start in
the same pod finds it there and knows it is a restart.

A container that exceeds its memory limit is killed by the kernel and restarted
by the kubelet *inside the same pod*. Kubernetes 1.33 made a running pod's
resources mutable through the ``pods/resize`` subresource, so the spec is no
longer immutable in general - but on the vcluster platform this deploys to it is
accepted and never actuated (verified 2026-09-15 on EKS 1.33: VPA in
``InPlaceOrRecreate`` and a direct ``kubectl patch --subresource resize`` both
left ``status.allocatedResources`` and the container's own ``memory.max``
unchanged, with no ``PodResizePending`` condition ever set). So the container
comes back on the limit that just killed it, and something outside has to
replace the pod before the memory can change. Until it
does, a restarted worker that resumes polling takes work straight back onto a
pod that cannot hold it, and the retries cannot succeed. So a restarted worker
idles instead of polling, for a bounded time, and resumes either way.

Written at birth, not at death: the kill arrives without warning and no handler
runs. Where cgroup v2 ``memory.oom.group`` is 1 the kernel takes every process in
the container's cgroup, PID 1 included; where it is 0 it takes the largest, which
for a single-process worker is that worker. (Measured 2026-09-15: the value is
not consistent across pods on one node, so neither case can be assumed.)
Recording the start and clearing it on a clean return inverts that into something
always observable.

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

**The marker says a restart happened; it cannot say why.** The kubelet records
that in the pod's own status, so learning the cause means one point read of this
pod from the apiserver - and reading a pod needs RBAC. Whether to spend that call
is ``ATLAN_OOM_RESTART_CHECK``:

``none``
    No apiserver call, so no RBAC and no per-pod cost. Every restart is treated
    as if memory could have been the cause, which means a worker whose container
    merely crashed idles too.
``api``
    One GET of this pod per restart. A restart the kubelet recorded as anything
    other than ``OOMKilled`` resumes immediately, because a smaller-than-needed
    limit is not what stopped it. A read that cannot be answered idles, the same
    as an out-of-memory one: "could not look" and "looked, and it was fine" have
    very different costs when the guess is wrong.

An established out-of-memory restart then idles, and the replacing is left to
whatever watches pods from outside. The wait is bounded either way, so a
replacement that never comes costs a delay and not a stuck worker.

The question goes to ``ATLAN_RESTART_ADVICE_URL`` rather than to Kubernetes.
Reading pod status directly would work and needs one read-only grant, but it
answers only *why* the container died - not whether anything is coming, which
also depends on the replacing side's own budget and on whether this pod is one a
recommendation can be applied to at all. That policy belongs where it can change
without releasing this package to the fleet, so it is asked for rather than
worked out here.

"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import httpx

from application_sdk.constants import DIRTY_RESTART_IDLE_MAX_SECONDS, OOM_RESTART_CHECK
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

#: Which of the idle's three exits ended it. Callers act on the difference: an
#: idle that ended early was overtaken by something that owns the pod already.
ELAPSED = "elapsed"
RELEASED = "released"
SHUTDOWN = "shutdown"

#: The one value of the check switch that does anything. The other is the
#: default, so only this needs naming.
CHECK_API = "api"

#: The namespace this pod runs in, mounted by the kubelet for every pod. Read as
#: a file, which needs no permission of any kind.
SERVICE_ACCOUNT_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")

#: Names this process has to be told, because a process cannot ask the kernel
#: which pod or container it is in. Both come from the downward API in the chart.
POD_NAME_ENV = "K8S_POD_NAME"
CONTAINER_NAME_ENV = "K8S_CONTAINER_NAME"


#: Where to ask what this restart earns. The rerouter serves it; a worker that
#: cannot reach it polls, so an unset value turns the question off rather than
#: breaking a start.
ADVICE_URL_ENV = "ATLAN_RESTART_ADVICE_URL"

#: Short and unretried on purpose. This sits between a restarted container and
#: its first poll, so a slow answer has to cost less than the answer is worth. A
#: miss means polling now, which is what happened before any of this existed.
ADVICE_TIMEOUT_SECONDS = 2.0


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

    path = directory / MARKER_NAME
    restarted_count = 0
    try:
        raw = path.read_text()
    except FileNotFoundError:
        raw = ""  # a fresh pod, not an error - and it still needs its marker
    except UnicodeDecodeError:
        # The read reached the file, so a marker is there; only its bytes are
        # unusable. Presence is the signal, so this is a restart. Falling through
        # also replaces the file, which an early return would leave in place for
        # every later start to trip over.
        raw = ""
        restarted_count = 1
        logger.warning(
            "%s is not valid UTF-8; treating it as one start", path, exc_info=True
        )
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
                "%s is not readable as a marker; treating it as one start",
                path,
                exc_info=True,
            )
            restarted_count = 1

    try:
        path.write_text(
            json.dumps(
                {
                    "pod": os.getenv(POD_NAME_ENV, ""),
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
        marker_dir(),
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
        logger.exception("could not hold this worker back; starting it normally")


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
    pod = os.getenv(POD_NAME_ENV, "").strip()
    container = os.getenv(CONTAINER_NAME_ENV, "").strip()
    if not url or not pod or not container:
        logger.info(
            "cannot ask what this restart earns (%s=%r, %s=%r, %s=%r), so this worker "
            "starts polling",
            ADVICE_URL_ENV,
            url,
            POD_NAME_ENV,
            pod,
            CONTAINER_NAME_ENV,
            container,
        )
        return None

    try:
        namespace = (SERVICE_ACCOUNT_DIR / "namespace").read_text().strip()
        async with httpx.AsyncClient(timeout=ADVICE_TIMEOUT_SECONDS) as client:
            response = await client.get(
                url,
                params={"namespace": namespace, "pod": pod, "container": container},
            )
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
    """Idle until something else replaces this pod, or the budget is spent."""
    logger.warning(
        "not polling for up to %ds, waiting to be replaced by a pod that can hold this "
        "work. Create %s to end the wait early.",
        budget,
        marker_dir() / RELEASE_NAME,
    )
    outcome = await hold(shutdown_event, budget)
    if outcome == ELAPSED:
        logger.warning(
            "waited %ds and nothing replaced this pod; starting the worker on the "
            "limit that already failed",
            budget,
        )
    elif outcome == SHUTDOWN:
        logger.info(
            "shutdown requested while waiting, which is this pod being replaced"
        )
    else:
        logger.warning("released; starting the worker")


async def hold(shutdown_event: asyncio.Event, seconds: float) -> str:
    """Idle for up to ``seconds`` without polling. Returns which exit ended it."""
    release = marker_dir() / RELEASE_NAME
    deadline = time.monotonic() + seconds
    last_beat = time.monotonic()
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return ELAPSED
        if release.exists():
            logger.warning("released by %s", release)
            return RELEASED
        try:
            # One await does three jobs: it is the sleep, it is the shutdown
            # listener, and the min() lands the last pass exactly on the deadline.
            await asyncio.wait_for(
                shutdown_event.wait(), timeout=min(RECHECK_SECONDS, remaining)
            )
        except TimeoutError:  # conformance: ignore[E002] the timeout is the sleep expiring, not a failure: it fires every RECHECK_SECONDS for the whole wait and means nobody asked us to stop
            pass
        else:
            return SHUTDOWN
        now = time.monotonic()
        if now - last_beat >= HEARTBEAT_SECONDS:
            last_beat = now
            # conformance: ignore[L006] one row a minute, not a tight loop: this is
            # the only signal that a pod is deliberately idle rather than wedged,
            # and it has to be visible for the whole wait.
            logger.info(
                "still not polling, %ds of %ds left", int(deadline - now), seconds
            )
