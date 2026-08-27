"""The read-only Temporal Protocol.

Split out of the package ``__init__`` alongside :mod:`._states` when the backend
landed (FND-247), for the reason
:mod:`application_sdk.testing.harness.cluster._protocols` was: the backend
imports this to declare what it satisfies. The name is re-exported from
:mod:`application_sdk.testing.harness.temporal`, which stays the import path.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from application_sdk.testing.harness.temporal._states import PollerInfo, WorkflowStatus

__all__ = ["TemporalReader"]


@runtime_checkable
class TemporalReader(Protocol):
    """Read Temporal state. No mutation, by decision.

    Read-only for the reason
    :class:`~application_sdk.testing.harness.cluster.ClusterReader` is: the
    scenario suite's mutations — starting work, terminating a leftover run — stay
    scenario-side, and
    :mod:`application_sdk.testing.harness.starters` already owns starting. A
    reader that could also terminate would be the one surface where a probe
    inside a bounded wait could change the thing it is measuring.

    Every method is ``async`` (decision D1), which here costs nothing: unlike the
    Kubernetes client, ``temporalio`` is asyncio-native, so the backend has no
    offload to pay.
    """

    async def task_queue_pollers(
        self, queue: str, *, namespace: str
    ) -> Sequence[PollerInfo]:
        """Return the workers Temporal currently sees polling *queue*.

        Args:
            queue: Task-queue name.
            namespace: Temporal namespace. On the request rather than on the
                connection because ``DescribeTaskQueue`` takes it there — see
                :meth:`workflow_status`, which cannot offer the same parameter.

        Returns:
            One :class:`PollerInfo` per poller. **Empty is a real answer** — it
            is the observed form of "no worker on this task queue", and the
            caller should report it as such rather than retrying into a timeout.

        Raises:
            Exception: If the read failed. An unreadable frontend is never an
                empty poller list — that fail-open shape is what FND-224's C4 is
                about, and it matters more here than anywhere else in the
                harness: empty is *the* diagnosis this read exists to deliver, so
                a failure that answered empty would not merely be unhelpful, it
                would manufacture the exact finding.
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

        Raises:
            Exception: If there is no such execution, or if the read failed. The
                two are distinguishable by type, and a backend must not collapse
                them: "no such workflow" does not get better on retry, so a
                bounded wait that absorbed it as transient would spend its whole
                budget on a typo. A caller that is *waiting for* an execution to
                appear wants a nullable read instead — the backend offers one.
        """
        ...
