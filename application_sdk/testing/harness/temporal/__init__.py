"""Read-only Protocol over Temporal, the states it returns, and its backend.

Neither reader existed in ``testing/e2e/`` before FND-247, and the gap is
load-bearing: ``testing.e2e``'s ``NoWorkerOnTaskQueueError`` is currently
*inferred* — "no DAG node started inside the grace window, so probably nothing is
polling the queue". A real poller read makes that **observable**, which is what
downgrades the single most common harness wedge (a suite whose ``agent_spec()``
queue does not match the deployed worker's) from a 180-second guess to a fact
available on the first probe. The largest regression class it catches has no
other detector at all: a queue name that is well formed and refers to nothing,
where every string comparison passes and the queue simply does not exist.

**Available, not yet adopted.** ``poll_native_status`` still infers, and nothing
in ``testing/e2e`` calls this module — swapping the inference for the read is
child H's, with the rest of the re-expression. The distinction is worth keeping
straight in both directions: claiming the upgrade here would misreport the state
of the tree, and describing this module as merely optional would understate why
the read has to exist before that swap can happen.

Read-only, like :mod:`application_sdk.testing.harness.cluster`, and for the same
reason: scenario-side mutation stays scenario-side. Starting work already has a
home in :mod:`application_sdk.testing.harness.starters`, and a reader that could
also terminate would be the one surface where a probe inside a bounded wait could
change the thing it is measuring.

**Two answers, three leaves.** Both reads refuse the fail-open shape, and the
poller read refuses it hardest: an empty poller list is the finding, so a failure
that answered empty would fabricate a diagnosis rather than merely lose one.
Every failure raises — ``DEPENDENCY_UNAVAILABLE`` for a frontend that could not
be reached or could not answer, which a bounded wait's transient classifier turns
into :class:`~application_sdk.testing.harness.outcome.Indeterminate`, and
``NOT_FOUND`` for a workflow id that does not exist, which is deliberately *not*
retryable so a wait fails on a typo at once instead of spending its budget on it.

**No extra to declare, and nothing to import-guard.** FND-247's amendment
expected ``temporalio`` to be optional behind ``[workflows]`` and these readers
to be guarded accordingly. It is a *core* dependency since v3.1 and
``[workflows]`` is a backwards-compatibility alias resolving to it, so unlike
``cluster``'s ``harness`` extra there is nothing to guard — and no cost to defer
either, since ``application_sdk.testing``'s own package ``__init__`` already
imports the client and its protos before any harness module is reached.

Module map:

``_states``
    :class:`PollerInfo`, :class:`WorkflowStatus`,
    :class:`WorkflowExecutionStatus`, :class:`TaskQueueType`, and the one pure
    predicate over them, :func:`stale_version_pollers`.
``_protocols``
    :class:`TemporalReader`.
``client``
    :class:`TemporalServiceReader` and the two connection factories,
    :func:`frontend_connection` and :func:`port_forwarded_connection`.
``_errors``
    The three leaves a read can raise before there is a verdict.
"""

from application_sdk.testing.harness.temporal._errors import (
    TemporalConnectFailedError,
    TemporalReaderLoopMismatchError,
    TemporalReadFailedError,
    WorkflowNotFoundError,
)
from application_sdk.testing.harness.temporal._protocols import TemporalReader
from application_sdk.testing.harness.temporal._states import (
    PollerInfo,
    TaskQueueType,
    WorkflowExecutionStatus,
    WorkflowStatus,
    stale_version_pollers,
)
from application_sdk.testing.harness.temporal.client import (
    TemporalConnection,
    TemporalServiceReader,
    frontend_connection,
    port_forwarded_connection,
)

__all__ = [
    # Protocol
    "TemporalReader",
    # States, and the one predicate over them
    "PollerInfo",
    "TaskQueueType",
    "WorkflowExecutionStatus",
    "WorkflowStatus",
    "stale_version_pollers",
    # The backend, and where its credentials come from
    "TemporalConnection",
    "TemporalServiceReader",
    "frontend_connection",
    "port_forwarded_connection",
    # Leaves
    "TemporalConnectFailedError",
    "TemporalReadFailedError",
    "TemporalReaderLoopMismatchError",
    "WorkflowNotFoundError",
]
