"""Typed error leaves for the Temporal reader.

Private module: the leaves that are public surface are re-exported from
:mod:`application_sdk.testing.harness.temporal`. Mirrors
:mod:`application_sdk.testing.harness.cluster._errors`.

Three leaves, and the split between them is the whole design:

* the frontend could not be reached at all
  (:class:`TemporalConnectFailedError`),
* it was reached and answered, but not with an answer
  (:class:`TemporalReadFailedError`),
* it answered, and the answer is that there is no such execution
  (:class:`WorkflowNotFoundError`).

The first two are ``DEPENDENCY_UNAVAILABLE``, so a bounded wait's transient
classifier absorbs them and the wait reports
:class:`~application_sdk.testing.harness.outcome.Indeterminate` — "could not
look" rather than "looked and it was bad". The third is ``NOT_FOUND`` and
deliberately *not* retryable: a workflow id that does not exist is a wrong id, a
wrong namespace or a purged run, and none of those changes while a wait sleeps.
Folding it into the first two would spend a whole 25-minute budget on a typo and
then report it as a Temporal outage.

What none of the three may become is an empty poller list. That is the fail-open
shape this package exists to remove, and it is worse here than in the cluster
readers: an empty list is exactly the finding
:meth:`~application_sdk.testing.harness.temporal.TemporalReader.task_queue_pollers`
exists to deliver, so a failure answering empty would not just lose information —
it would fabricate the diagnosis.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from application_sdk.errors.leaves import (
    DependencyUnavailableError,
    NotFoundError,
    PreconditionError,
)

__all__ = [
    "TemporalConnectFailedError",
    "TemporalReadFailedError",
    "TemporalReaderLoopMismatchError",
    "WorkflowNotFoundError",
]


@dataclass(kw_only=True)
class TemporalConnectFailedError(DependencyUnavailableError):
    """Could not establish a client against the Temporal frontend.

    Distinct from :class:`TemporalReadFailedError` because the fix is different:
    nothing was read, and nothing about the namespace is known — including
    whether the address is even the right one. The address is carried as a field
    so a run pointed at the wrong frontend says so in the failure rather than
    leaving it to be inferred from a gRPC message.

    Attributes:
        address: ``host:port`` the connection was attempted against.
        temporal_namespace: Namespace the client was being bound to. Named for
            Temporal explicitly: a harness failure mentioning an unqualified
            "namespace" next to a Kubernetes reader is a genuine ambiguity.
    """

    code: ClassVar[str] = "DEPENDENCY_UNAVAILABLE_TEMPORAL_CONNECT_FAILED"
    component: str | None = "harness_temporal"
    service: str | None = "temporal"
    address: str | None = None
    temporal_namespace: str | None = None


@dataclass(kw_only=True)
class TemporalReadFailedError(DependencyUnavailableError):
    """A Temporal read reached the frontend and did not come back with data.

    ``DEPENDENCY_UNAVAILABLE`` for the reason
    :class:`~application_sdk.testing.harness.cluster.ClusterReadFailedError` is:
    an ``UNAVAILABLE`` from a restarting frontend, a ``DEADLINE_EXCEEDED`` over a
    VPN or an expired token is neither a pass nor a regression in the thing under
    test, and this is the category whose definition is "the same call would work
    once the dependency recovers".

    Attributes:
        rpc_status: gRPC status name the frontend returned, e.g.
            ``"UNAVAILABLE"``. A name rather than a number because it is what the
            reader of a report recognises, and ``None`` when the call failed
            before any status came back.
        address: ``host:port`` the read went through, so a run against the wrong
            frontend is visible in the failure.
        temporal_namespace: Namespace the read was scoped to.
    """

    code: ClassVar[str] = "DEPENDENCY_UNAVAILABLE_TEMPORAL_READ_FAILED"
    component: str | None = "harness_temporal"
    service: str | None = "temporal"
    rpc_status: str | None = None
    address: str | None = None
    temporal_namespace: str | None = None


@dataclass(kw_only=True)
class WorkflowNotFoundError(NotFoundError):
    """Temporal has no execution under this workflow ID.

    ``NOT_FOUND`` rather than ``DEPENDENCY_UNAVAILABLE``, and that is the
    load-bearing choice: ``NotFoundError`` is ``default_retryable = False``, so a
    transient classifier keyed on retryability leaves it alone and the wait fails
    at once instead of burning its budget. A wrong workflow id, a wrong namespace
    and a run past its retention window are all states that must change before
    the call can succeed, which is the litmus test.

    A caller that is *waiting for* an execution to appear should not be catching
    this at all — it wants the nullable read
    (:meth:`~application_sdk.testing.harness.temporal.TemporalServiceReader.find_workflow_status`),
    where absence is a value its predicate can test.

    Attributes:
        workflow_id: The workflow ID that had no execution.
        run_id: The specific run asked for, or ``None`` when the latest run of
            that ID was asked for.
        temporal_namespace: Namespace that was searched. Carried because the
            single most common cause of this leaf is a client bound to the wrong
            namespace, and the message cannot be read as a queue-name problem
            once the namespace is on the record.
    """

    code: ClassVar[str] = "NOT_FOUND_TEMPORAL_WORKFLOW"
    component: str | None = "harness_temporal"
    resource_type: str | None = "workflow_execution"
    workflow_id: str | None = None
    run_id: str | None = None
    temporal_namespace: str | None = None


@dataclass(kw_only=True)
class TemporalReaderLoopMismatchError(PreconditionError):
    """A connected reader was used from a different event loop than it opened on.

    ``PRECONDITION`` on its own litmus test: retrying the same call changes
    nothing, and the state that has to change first is that the existing
    connection be closed. Raised rather than handled silently because the silent
    handling leaks — a ``temporalio`` client is bound to its creating loop, so a
    reader that rebound would drop the previous connection *without* awaiting its
    close, and for a port-forwarded connection that close is what reaps a
    ``kubectl`` child process.

    Only raised when there is something to lose. A reader that has never
    connected, or whose connection has already been closed, rebinds to the new
    loop without complaint: there is no leak, so there is nothing to report.

    The fix is almost never to close and reopen — it is to stop sharing one
    reader across loops. :func:`~application_sdk.testing.harness.bridge.run_sync`
    owns exactly one loop per thread, so a suite that reaches the reader only
    through it never sees this.

    Attributes:
        address: ``host:port`` the existing connection is pointed at, so the
            message names what would have been dropped.
    """

    code: ClassVar[str] = "PRECONDITION_TEMPORAL_READER_LOOP_MISMATCH"
    component: str | None = "harness_temporal"
    address: str | None = None
