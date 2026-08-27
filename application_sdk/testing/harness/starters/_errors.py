"""Typed error leaves for the workflow starters.

Private module: the leaves that are public surface are re-exported from
:mod:`application_sdk.testing.harness.starters`. Mirrors
:mod:`application_sdk.testing.harness.temporal._errors`.

Three leaves, and the split is the same shape as the reader's — with one
difference worth being explicit about, because it is where a starter could
plausibly have diverged from its sibling and did not.

* the queue name is not one anything could ever poll
  (:class:`UnusableTaskQueueError`), raised before a byte leaves the process;
* the frontend accepted the dispatch and answered that this workflow id is
  already running (:class:`WorkflowStartConflictError`);
* everything else the dispatch could not get past
  (:class:`WorkflowStartFailedError`).

**The rule is the reader's rule, deliberately.**
:mod:`application_sdk.testing.harness.temporal.client`'s reads are
``DEPENDENCY_UNAVAILABLE`` for every gRPC status except the one that names a
different fix, and the start follows it: one ``DEPENDENCY_UNAVAILABLE`` leaf
carrying ``rpc_status``, plus one non-retryable leaf for the conflict. An
``INVALID_ARGUMENT`` from a malformed workflow type therefore arrives in the
same class as an ``UNAVAILABLE`` from a restarting frontend, with the status
name on the leaf to tell them apart.

That is a real trade, so here is why it is the right one. Splitting
"request-side rejection" into its own non-retryable leaf would be more precise
in isolation and would matter if a start sat inside a bounded wait, where
absorbing a typo as transient burns the whole budget — the reasoning that earns
:class:`~application_sdk.testing.harness.temporal.WorkflowNotFoundError` its own
category. A start does not: it is a one-shot at the top of a scenario, and its
failure is read by a person rather than classified by a poll loop. So the
precision would buy nothing here, while a second classification rule over the
same gRPC statuses is exactly the divergence FND-224 exists to remove. The
status is on the leaf either way.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from application_sdk.errors.leaves import (
    AlreadyExistsError,
    DependencyUnavailableError,
    InvalidInputError,
)

__all__ = [
    "UnusableTaskQueueError",
    "WorkflowStartConflictError",
    "WorkflowStartFailedError",
]


@dataclass(kw_only=True)
class UnusableTaskQueueError(InvalidInputError):
    """The task-queue name could not name a queue any worker polls.

    Raised at construction of
    :class:`~application_sdk.testing.harness.starters.QueueWorkflowSpec`, not at
    dispatch, because the failure mode it removes is *silence*: Temporal accepts
    a dispatch to any well-formed queue name, so a workflow sent to an
    unresolved ``atlan-{app_name}-{deployment_name}`` template — or to a blank
    name — is accepted, sits unclaimed, and reports nothing until its 24-hour
    heartbeat backstop. That is CONNECT-183, the precise failure
    :mod:`application_sdk.common.task_queue` exists to prevent on the production
    side; a harness that reproduced it would spend a whole scenario budget
    learning that a template was never filled in.

    ``INVALID_INPUT`` rather than ``PRECONDITION``: nothing about the cluster
    has to change, the string handed in is wrong.

    Attributes:
        task_queue: The rejected queue name, or ``None`` when no name could be
            derived at all. Carried verbatim — an unresolved template is only
            diagnosable if the token is visible in the failure.
    """

    code: ClassVar[str] = "INVALID_INPUT_HARNESS_TASK_QUEUE"
    component: str | None = "harness_starters"
    field: str | None = "task_queue"
    task_queue: str | None = None


@dataclass(kw_only=True)
class WorkflowStartConflictError(AlreadyExistsError):
    """A run is already using this workflow id.

    ``ALREADY_EXISTS`` is the category whose own definition is "the resource
    exists and that is the problem", and ``default_retryable = False`` follows
    from it: the id is taken until the run holding it closes, so the same
    dispatch cannot succeed while nothing changes. The fixes are all caller-side
    — pass a different :attr:`~QueueWorkflowSpec.workflow_id`, let the starter
    mint one, or terminate the leftover run — which is why this is not folded
    into :class:`WorkflowStartFailedError`.

    Attributes:
        workflow_id: The id that was already in use.
        existing_run_id: Temporal's run id for the execution already holding it,
            when the frontend named one. Named ``existing_`` rather than
            ``run_id`` because :class:`~application_sdk.errors.base.AppError`
            already carries a ``run_id`` meaning *this* app run, and a report
            reading one for the other would send an operator to the wrong
            execution.
        task_queue: Queue the dispatch was aimed at.
        temporal_namespace: Namespace the client is bound to. Named for Temporal
            explicitly: an unqualified "namespace" beside a Kubernetes reader is
            a genuine ambiguity.
    """

    code: ClassVar[str] = "ALREADY_EXISTS_TEMPORAL_WORKFLOW_RUN"
    component: str | None = "harness_starters"
    resource_type: str | None = "workflow_execution"
    workflow_id: str | None = None
    existing_run_id: str | None = None
    task_queue: str | None = None
    temporal_namespace: str | None = None


@dataclass(kw_only=True)
class WorkflowStartFailedError(DependencyUnavailableError):
    """The dispatch did not come back with a started run.

    ``DEPENDENCY_UNAVAILABLE`` for the reason
    :class:`~application_sdk.testing.harness.temporal.TemporalReadFailedError`
    is: an ``UNAVAILABLE`` from a restarting frontend or a ``DEADLINE_EXCEEDED``
    over a VPN is neither a pass nor a regression in the thing under test. See
    this module's docstring for why the request-side statuses arrive here too,
    with :attr:`rpc_status` as the field that separates them.

    Also raised — with no :attr:`rpc_status` — when the frontend reports a
    *successful* start that carries no run id. That is not pedantry: without the
    run id the caller can only ask about "the latest run of this id", so a
    scenario that later re-dispatches, or an app that continues-as-new, would
    grade a different execution than the one it started and never know. A handle
    that cannot name its run is worse than a failure, because it reads as one
    that can.

    Attributes:
        workflow_id: The id that was being dispatched.
        task_queue: Queue the dispatch was aimed at. On the leaf because a
            dispatch rejected for a malformed queue name is the failure this
            field answers in one step.
        temporal_namespace: Namespace the client is bound to.
        address: ``host:port`` the dispatch went through, so a run pointed at
            the wrong frontend — a stale tunnel, a kubeconfig context that was
            not the one the cluster reads went through — is visible in the
            failure rather than inferred.
        rpc_status: gRPC status name the frontend returned, e.g.
            ``"UNAVAILABLE"``. A name rather than a number because it is what
            the reader of a report recognises, and ``None`` when no status came
            back — either because the call failed before one did, or because the
            call succeeded and the answer itself was unusable.
    """

    code: ClassVar[str] = "DEPENDENCY_UNAVAILABLE_TEMPORAL_START_FAILED"
    component: str | None = "harness_starters"
    service: str | None = "temporal"
    workflow_id: str | None = None
    task_queue: str | None = None
    temporal_namespace: str | None = None
    address: str | None = None
    rpc_status: str | None = None
