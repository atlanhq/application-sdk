"""Read-only Protocol over Temporal, plus the states it returns.

Neither reader exists in ``testing/e2e/`` today, and the gap is load-bearing:
:class:`~application_sdk.testing.e2e._errors.NoWorkerOnTaskQueueError` is
currently *inferred* — "no DAG node started inside the grace window, so probably
nothing is polling the queue". A real poller read makes it **observed**, which
turns the single most common harness wedge (a suite whose ``agent_spec()``
queue does not match the deployed worker's) from a 180-second guess into a fact.

Read-only, like :mod:`application_sdk.testing.harness.cluster`, and for the same
reason: scenario-side mutation stays scenario-side.

Implementation lands as a separate issue outside FND-224, per the decomposition —
this module scaffolds the vocabulary its callers are written against.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Protocol, runtime_checkable

__all__ = [
    "PollerInfo",
    "TemporalReader",
    "WorkflowExecutionStatus",
    "WorkflowStatus",
]


class WorkflowExecutionStatus(str, Enum):
    """Terminal and non-terminal states a workflow execution can report.

    Mirrors Temporal's own ``WorkflowExecutionStatus`` rather than inventing a
    parallel vocabulary — the harness's job is to report what Temporal says.
    """

    RUNNING = "Running"
    COMPLETED = "Completed"
    FAILED = "Failed"
    CANCELED = "Canceled"
    TERMINATED = "Terminated"
    CONTINUED_AS_NEW = "ContinuedAsNew"
    TIMED_OUT = "TimedOut"


@dataclass(frozen=True, slots=True, kw_only=True)
class PollerInfo:
    """One worker seen polling a task queue.

    Attributes:
        identity: The worker's self-reported identity, usually
            ``{pid}@{hostname}``. This is what tells "the wrong worker is
            polling" from "nothing is polling".
        last_access: When Temporal last saw this poller.
        build_id: Worker build identifier, when versioning is in use. Names the
            specific build holding the queue — the shape behind an older build
            keeping the pollers while the current one sits at zero.
    """

    identity: str
    last_access: datetime
    build_id: str | None = None


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
        started_at: When the execution started.
        closed_at: When it reached a terminal state, or ``None`` while running.
    """

    workflow_id: str
    run_id: str
    status: WorkflowExecutionStatus
    task_queue: str
    started_at: datetime | None = None
    closed_at: datetime | None = None


@runtime_checkable
class TemporalReader(Protocol):
    """Read Temporal state. No mutation, by decision."""

    async def task_queue_pollers(
        self, queue: str, *, namespace: str
    ) -> Sequence[PollerInfo]:
        """Return the workers Temporal currently sees polling *queue*.

        Args:
            queue: Task-queue name.
            namespace: Temporal namespace.

        Returns:
            One :class:`PollerInfo` per poller. **Empty is a real answer** — it
            is the observed form of "no worker on this task queue", and the
            caller should report it as such rather than retrying into a timeout.
        """
        ...

    async def workflow_status(
        self, workflow_id: str, *, run_id: str | None = None
    ) -> WorkflowStatus:
        """Return the state of one workflow execution.

        Args:
            workflow_id: Workflow ID to describe.
            run_id: Specific run, or ``None`` for the latest run of that ID.

        Returns:
            The execution's :class:`WorkflowStatus`.
        """
        ...
