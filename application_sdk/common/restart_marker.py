"""Pod-scoped record of whether this container start is a restart, and what to do
about one.

Written by the worker on its first start in a pod and removed when it returns
cleanly, so an abnormal exit is what leaves it behind. A later container start in
the same pod finds it there and knows it is a restart.

A container that exceeds its memory limit is killed by the kernel and restarted
by the kubelet *inside the same pod*, and a pod's resource spec is immutable for
its lifetime - so the container comes back on the limit that just killed it.
Something outside has to replace the pod before the memory can change. Until it
does, a restarted worker that resumes polling takes work straight back onto a
pod that cannot hold it, and the retries cannot succeed. So a restarted worker
idles instead of polling, for a bounded time, and resumes either way.

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

An established out-of-memory restart then gets ``ATLAN_OOM_RESTART_ACTION``:

``park``
    Idle, and leave the replacing to whatever watches pods from outside.
``delete``
    Idle for ``ATLAN_OOM_RESTART_SETTLE_SECONDS``, delete this pod, and go on
    idling until the shutdown that follows. Nothing outside is needed, at the
    price of one more verb on the service account and of bypassing the eviction
    API - and with it any PodDisruptionBudget over these pods.
``eject``
    Idle for the same window, then overflow a small disk-backed volume so the
    kubelet's own eviction manager takes the pod out. No apiserver call and no
    permission at all: the node already watches local-storage use against each
    ``emptyDir``'s ``sizeLimit``, and this asks it to act on what it is already
    watching. Inert without that volume, the same way detection is inert without
    the marker's.

The delay before the action is not politeness. The replacement's size is fixed
when it is admitted, from whatever the recommendation says at that moment, and
the recommendation rises only seconds *after* the kill is observed. Acting
immediately therefore buys another pod of the size that just died.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx

from application_sdk.constants import (
    DIRTY_RESTART_IDLE_MAX_SECONDS,
    OOM_RESTART_ACTION,
    OOM_RESTART_CHECK,
    OOM_RESTART_SETTLE_SECONDS,
)
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

#: The values of the two switches this module branches on. The remaining value of
#: each is the default, so only these need naming.
CHECK_API = "api"
ACTION_DELETE = "delete"
ACTION_EJECT = "eject"

#: The in-cluster apiserver, its CA and the pod's own token, all mounted by the
#: kubelet. Reachable from any pod with a service account, which is every pod
#: this runs in; nothing here is configurable, because a worker reading a
#: *different* cluster's pod would be reading about someone else's memory.
API_BASE = "https://kubernetes.default.svc"
SERVICE_ACCOUNT_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")

#: Names this process has to be told, because a process cannot ask the kernel
#: which pod or container it is in. Both come from the downward API in the chart.
POD_NAME_ENV = "K8S_POD_NAME"
CONTAINER_NAME_ENV = "K8S_CONTAINER_NAME"

#: The kubelet's word for a container the kernel killed for memory.
OOM_REASON = "OOMKilled"

#: The volume ``eject`` overflows, and how far past its ``sizeLimit`` to go.
#: Disk-backed, unlike the marker's: local-storage use is what the node's
#: eviction manager measures, and a ``medium: Memory`` volume counts against the
#: memory limit instead - which would OOM-kill the container again rather than
#: replace the pod. The write has to clear the limit by enough that the node sees
#: it on its next sweep whatever the block size, and small enough that the node's
#: disk is never the thing at stake.
EJECT_DIR_ENV = "ATLAN_RESTART_EJECT_DIR"
DEFAULT_EJECT_DIR = "/run/atlan-eject"
EJECT_NAME = "ballast"
EJECT_BYTES = 4 * 1024 * 1024

#: A read that has not answered in this long has failed as far as the decision is
#: concerned. The decision then idles, so a slow apiserver costs a wait and not a
#: worker that resumes onto the limit that already failed.
API_TIMEOUT_SECONDS = 10.0


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
        "restart handling: check=%s action=%s settle=%ds budget=%ds marker=%s",
        OOM_RESTART_CHECK,
        OOM_RESTART_ACTION,
        OOM_RESTART_SETTLE_SECONDS,
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
    """Decide what this restart earns, and do it.

    Everything that can go wrong on the way to the decision leads to the idle,
    never past it. The one exit that resumes immediately is a cause this pod
    positively read as something other than memory.
    """
    if OOM_RESTART_CHECK != CHECK_API:
        logger.info(
            "not asking why this container restarted (ATLAN_OOM_RESTART_CHECK=%s), so "
            "this restart is handled as if memory caused it",
            OOM_RESTART_CHECK,
        )
        await wait_for_pod_to_get_replaced(shutdown_event, budget)
        return

    try:
        reason = await last_termination_reason()
    except Exception:
        # Fail closed. The alternative reads "we could not find out, so we assume
        # it is fine" and puts the work back on a pod that cannot hold it.
        logger.warning(
            "could not read why the earlier container in this pod ended, so this "
            "restart is handled as if memory caused it",
            exc_info=True,
        )
        reason = None

    if reason is None:
        await wait_for_pod_to_get_replaced(shutdown_event, budget)
        return

    if reason != OOM_REASON:
        logger.warning(
            "the earlier container in this pod ended with reason %s, not %s, so its "
            "memory limit is not what stopped it and a bigger pod would not help; "
            "this worker starts polling now",
            reason,
            OOM_REASON,
        )
        return

    if OOM_RESTART_ACTION == ACTION_DELETE:
        await act_when_it_settles(
            shutdown_event, budget, "deleting this pod", delete_this_pod
        )
    elif OOM_RESTART_ACTION == ACTION_EJECT:
        await act_when_it_settles(
            shutdown_event,
            budget,
            "overflowing this pod's eject volume",
            eject_this_pod,
        )
    else:
        await wait_for_pod_to_get_replaced(shutdown_event, budget)


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


async def act_when_it_settles(
    shutdown_event: asyncio.Event,
    budget: int,
    description: str,
    replace_this_pod: Callable[[], Awaitable[bool]],
) -> None:
    """Idle out the settle window, get this pod replaced, then go on idling.

    The idle afterwards is not a formality: replacing a pod only asks for it, and
    what comes back is a graceful shutdown some seconds later. Polling in the gap
    would pick up work this process is about to be told to drop. An attempt that
    did not take idles for exactly as long, because a worker that could not get
    itself replaced is in the same position as one waiting for somebody else to
    do it.

    Both phases come out of the one budget, so this can never idle longer than
    ``park`` would.
    """
    settle = min(OOM_RESTART_SETTLE_SECONDS, budget)
    logger.warning(
        "not polling; %s in %ds so that the replacement is admitted against a "
        "recommendation raised by the kill",
        description,
        settle,
    )
    if await hold(shutdown_event, settle) != ELAPSED:
        # Released or already shutting down. Whoever did that is replacing this
        # pod, and acting on top of it would take out the replacement instead.
        logger.info("this pod is being replaced already; leaving it alone")
        return

    remaining = budget - settle
    if not await replace_this_pod():
        logger.warning(
            "could not get this pod replaced, so it now waits out the remaining %ds "
            "for something else to replace it",
            remaining,
        )
    await wait_for_pod_to_get_replaced(shutdown_event, remaining)


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


async def last_termination_reason() -> str | None:
    """Why the earlier container in this pod ended, as the kubelet recorded it.

    ``None`` is "unanswerable", not "nothing wrong": no status for this container
    yet, or no record of an earlier termination. Callers treat it the way they
    treat a failed read.
    """
    url = own_pod_url()
    if url is None:
        return None
    pod = await request_own_pod("GET", url)
    statuses = (pod.get("status") or {}).get("containerStatuses") or []
    status = own_container_status(statuses)
    if status is None:
        return None
    terminated = (status.get("lastState") or {}).get("terminated") or {}
    reason = terminated.get("reason")
    if not reason:
        logger.warning(
            "this pod's status carries no earlier termination for container %s, so why "
            "it restarted cannot be read from it",
            status.get("name"),
        )
        return None
    return str(reason)


def own_container_status(
    statuses: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Pick this process's own container out of the pod's statuses.

    Which one it is has to be told, so the guard here is against reading someone
    else's: a sidecar that ran out of memory says nothing about this worker's
    limit, and acting on it would replace a pod that was fine. The single-container
    case is the exception where the answer is unambiguous without being told.
    """
    name = os.getenv(CONTAINER_NAME_ENV, "").strip()
    if name:
        for status in statuses:
            if status.get("name") == name:
                return status
        logger.warning(
            "this pod has no container named %s, which is what %s says this one is; "
            "why it restarted cannot be read from a status that is not ours",
            name,
            CONTAINER_NAME_ENV,
        )
        return None
    if len(statuses) == 1:
        return statuses[0]
    logger.warning(
        "%s is not set and this pod reports %d containers, so which status is this "
        "container's cannot be known; set it from the container's own name",
        CONTAINER_NAME_ENV,
        len(statuses),
    )
    return None


async def request_own_pod(method: str, url: str) -> dict[str, Any]:
    """One apiserver call, about this pod and nothing else.

    The token and CA are read per call, not at import: the token is rotated in
    place, and a worker can sit idle here for longer than one lives.
    """
    token = (SERVICE_ACCOUNT_DIR / "token").read_text().strip()
    # One row per apiserver call, because the cost of this design is exactly the
    # number of these rows and it is counted from the logs.
    logger.info("apiserver call: %s %s", method, url)
    async with httpx.AsyncClient(
        verify=str(SERVICE_ACCOUNT_DIR / "ca.crt"), timeout=API_TIMEOUT_SECONDS
    ) as client:
        response = await client.request(
            method, url, headers={"Authorization": f"Bearer {token}"}
        )
    response.raise_for_status()
    return dict(response.json())


def own_pod_url() -> str | None:
    """This pod's own URL on the in-cluster apiserver, or ``None`` if it cannot be
    named.

    It is built from this pod's own name every time rather than taken from
    configuration, so nothing here can be pointed at another pod - which is also
    what keeps the grant a Role over one namespace instead of a cluster-wide read.
    """
    pod = os.getenv(POD_NAME_ENV, "").strip()
    if not pod:
        logger.warning(
            "%s is not set, so this process cannot name its own pod and cannot ask "
            "about it; set it from the downward API",
            POD_NAME_ENV,
        )
        return None
    namespace = (SERVICE_ACCOUNT_DIR / "namespace").read_text().strip()
    return f"{API_BASE}/api/v1/namespaces/{namespace}/pods/{pod}"


async def delete_this_pod() -> bool:
    """Ask the apiserver to delete this pod. ``False`` means it did not.

    A refusal is returned rather than raised because it is not exceptional: a
    service account without the verb and a pod already on its way out both land
    here, and the caller's answer to either is to keep idling.
    """
    try:
        url = own_pod_url()
        if url is None:
            return False
        await request_own_pod("DELETE", url)
    except Exception:
        logger.warning("the apiserver did not delete this pod", exc_info=True)
        return False
    logger.warning("deleted this pod; waiting for the shutdown that follows")
    return True


async def eject_this_pod() -> bool:
    """Overflow the eject volume so the node evicts this pod. ``False`` means it
    could not be done, and nothing has been asked of anything.

    The node's eviction manager already measures every pod's local-storage use
    against each ``emptyDir``'s ``sizeLimit`` and evicts the pod that exceeds it.
    So a pod can get itself replaced with no apiserver call and nothing granted
    to its service account - the one path here that needs no permission at all.
    What it costs instead: the eviction is the node's, taken on its own sweep, so
    the pod is gone whether or not a PodDisruptionBudget would have refused it.

    Without the volume there is nothing to overflow. That is reported and left
    alone, exactly as a missing marker volume is: writing past a *container
    filesystem* would fill the node's disk instead of this pod's allowance, and
    the pod it evicted might be somebody else's.
    """
    directory = Path(os.getenv(EJECT_DIR_ENV) or DEFAULT_EJECT_DIR)
    if not directory.is_dir():
        logger.warning(
            "%s is not a directory, so there is no small volume of this pod's own to "
            "overflow and the node has nothing to evict it for. Mount a disk-backed "
            "emptyDir with a sizeLimit there to enable it.",
            directory,
        )
        return False
    path = directory / EJECT_NAME
    try:
        with path.open("wb") as handle:
            handle.write(b"\0" * EJECT_BYTES)
            handle.flush()
            # The bytes have to be on the filesystem, not in a page cache: what
            # the node reads is the volume's block usage, and a delayed
            # allocation leaves that at zero for as long as it lasts.
            os.fsync(handle.fileno())
    except OSError:
        logger.warning("could not write %s", path, exc_info=True)
        return False
    logger.warning(
        "wrote %d bytes to %s, past the volume's sizeLimit; waiting for the node to "
        "evict this pod",
        EJECT_BYTES,
        path,
    )
    return True
