"""The typed states the Temporal reader returns, and the one predicate over them.

Split out of the package ``__init__`` when the backend landed (FND-247), for the
same reason :mod:`application_sdk.testing.harness.cluster._states` was: the
backend module has to import these, and a package whose ``__init__`` both defines
them *and* imports the backend is a cycle. Every name here is re-exported from
:mod:`application_sdk.testing.harness.temporal`, which stays the import path.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

__all__ = [
    "PollerInfo",
    "TaskQueueType",
    "WorkflowExecutionStatus",
    "WorkflowStatus",
    "stale_version_pollers",
]


class TaskQueueType(str, Enum):
    """Which half of a task queue a poller is holding.

    A Temporal task queue name addresses *two* queues — one for workflow tasks
    and one for activity tasks — and a worker polls both. They are separate
    ``DescribeTaskQueue`` reads, so "how many pollers does this queue have" has
    no single answer, and collapsing the two into one count is what makes the
    half-polling state invisible: a worker process whose workflow poll loop has
    died while its activity poll loop is alive still answers "1 poller" to a
    union count, which is exactly the zombie shape a harness exists to catch.
    Every :class:`PollerInfo` therefore says which queue it was seen on.
    """

    WORKFLOW = "Workflow"
    ACTIVITY = "Activity"
    NEXUS = "Nexus"


class WorkflowExecutionStatus(str, Enum):
    """Terminal and non-terminal states a workflow execution can report.

    Mirrors Temporal's own ``WorkflowExecutionStatus`` rather than inventing a
    parallel vocabulary — the harness's job is to report what Temporal says.

    :attr:`UNKNOWN` is the one member Temporal's Python enum has no counterpart
    for: it models the proto's ``UNSPECIFIED``, which ``temporalio`` surfaces as
    ``None``. It is here for the reason
    :attr:`~application_sdk.testing.harness.cluster.PodPhase.UNKNOWN` is: a
    status this SDK has never heard of is precisely what "unknown" means, and
    raising on it would turn a new upstream status name into a harness crash.
    """

    RUNNING = "Running"
    COMPLETED = "Completed"
    FAILED = "Failed"
    CANCELED = "Canceled"
    TERMINATED = "Terminated"
    CONTINUED_AS_NEW = "ContinuedAsNew"
    TIMED_OUT = "TimedOut"
    UNKNOWN = "Unknown"


@dataclass(frozen=True, slots=True, kw_only=True)
class PollerInfo:
    """One worker seen polling a task queue.

    Attributes:
        identity: The worker's self-reported identity, usually
            ``{pid}@{hostname}``. This is what tells "the wrong worker is
            polling" from "nothing is polling".
        last_access: When Temporal last saw this poller. An unset timestamp reads
            as the Unix epoch, which is the safe direction for a staleness check:
            a poller whose last access cannot be read must not read as fresh.
        task_queue_type: Which half of the queue this poller was seen on — see
            :class:`TaskQueueType` for why every poller carries it.
        build_id: Worker build identifier, when versioning is in use. Names the
            specific build holding the queue — the shape behind an older build
            keeping the pollers while the current one sits at zero. ``None`` for
            an unversioned worker, which :func:`stale_version_pollers` treats as
            stale rather than as "no opinion".
        deployment_name: Worker Deployment the build belongs to, when versioning
            is in use. Carried alongside :attr:`build_id` because that is the
            pair a Worker Deployment version is: this SDK's worker sets both
            (``ATLAN_APP_DEPLOYMENT_NAME`` / ``ATLAN_APP_BUILD_ID``), so a build
            id on its own cannot be matched against the deployment a
            ``TemporalWorkerDeployment`` names.
    """

    identity: str
    last_access: datetime
    task_queue_type: TaskQueueType = TaskQueueType.WORKFLOW
    build_id: str | None = None
    deployment_name: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class WorkflowStatus:
    """One workflow execution's state.

    Attributes:
        workflow_id: The workflow ID asked about.
        run_id: The specific run this status describes.
        status: Execution status as Temporal reports it.
        task_queue: Queue the execution is dispatched on. Exposed because the
            queue-name seam — ``atlan-{application}-{deployment}``, derived
            independently in several places — is checkable here without a pod
            exec.
        history_length: Events in the execution's history so far. This is the
            *progress* signal, and it is the reason the field exists: a wait on
            :attr:`status` alone has nothing that changes between "running" and
            "finished", so
            :func:`~application_sdk.testing.harness.waiting.poll_until` could
            only ever report that wait as ``Expired``. A history length that
            stops growing is what makes ``Stalled`` — "it started and then
            stopped moving" — available as a verdict at all.
        started_at: When the execution started.
        closed_at: When it reached a terminal state, or ``None`` while running.
    """

    workflow_id: str
    run_id: str
    status: WorkflowExecutionStatus
    task_queue: str
    history_length: int = 0
    started_at: datetime | None = None
    closed_at: datetime | None = None


def stale_version_pollers(
    pollers: Iterable[PollerInfo], *, current_build_id: str | None
) -> Sequence[PollerInfo]:
    """Return the pollers that are *not* on *current_build_id*.

    The second half of the runtime suite's precondition gate — "the intended
    version is Current, **and no stale-version workers are still polling**". The
    first half is a
    :meth:`~application_sdk.testing.harness.cluster.CustomResourceReader.custom_resources`
    read of the ``TemporalWorkerDeployment`` that names the intended version, so
    it needs nothing from this module.

    Lifted from the Go harness's ``hasUnversionedPoller`` rather than left to the
    call site, because the obvious one-liner gets the unversioned case wrong in
    the quiet direction: a worker with **no** build id is stale — it is a build
    that predates versioning, or one whose deployment config did not take — and
    ``poller.build_id and poller.build_id != current`` skips exactly those. A
    gate that passes because it could not see the offending worker is the
    fail-open shape this package exists to remove.

    Args:
        pollers: Pollers read from a task queue.
        current_build_id: The build id the deployment intends to be serving.
            ``None`` means "versioning is not in use here", in which case no
            poller can be stale and the result is empty — the alternative,
            reporting every poller as stale, would red every unversioned
            deployment.

    Returns:
        The stale pollers, in the order given. Empty is the passing state.
    """
    if current_build_id is None:
        return ()
    return tuple(poller for poller in pollers if poller.build_id != current_build_id)
