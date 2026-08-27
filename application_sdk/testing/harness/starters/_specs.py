"""What each of the three starters takes, and what each hands back.

Split out of the package ``__init__`` when the queue starter landed (FND-246),
for the reason :mod:`application_sdk.testing.harness.temporal._states` was split
out of *its* package init: the implementation module has to import the spec it
takes, and a package whose ``__init__`` both defines the spec and imports the
implementation is a cycle. Every name here is re-exported from
:mod:`application_sdk.testing.harness.starters`, which stays the import path.

Five types, three pairs, no shared base and no shared verb — see the package
docstring for why that is the design rather than an omission.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from application_sdk.common.task_queue import (
    APP_NAME_TOKEN,
    DEPLOYMENT_NAME_TOKEN,
    derive_task_queue,
)
from application_sdk.testing.harness.cluster import ServiceTarget
from application_sdk.testing.harness.starters._errors import UnusableTaskQueueError

__all__ = [
    "AERunHandle",
    "HttpRunHandle",
    "HttpWorkflowSpec",
    "QueueWorkflowSpec",
    "WorkflowRunHandle",
]

#: Template fragments that must not survive into a dispatched queue name. A
#: queue still carrying one is a manifest whose tokens were never resolved, and
#: dispatching to it is accepted by Temporal and then never claimed.
_UNRESOLVED_TOKENS = (APP_NAME_TOKEN, DEPLOYMENT_NAME_TOKEN)


@dataclass(frozen=True, slots=True, kw_only=True)
class AERunHandle:
    """A run started through the Automation Engine.

    Lives here with the other two run handles rather than in
    :mod:`application_sdk.testing.harness.automation_engine`, where it was first
    sketched. Child F moved the AE reader over and found no use for it: the
    submit returns AE's run id, the slug is the caller's own, and giving that
    pair a name inside the reader would have been a fourth vocabulary for the
    same reading. It *is* a starter handle, and the module whose whole subject
    is "three ways to start work, three unrelated handles" is where the third
    one belongs.

    Attributes:
        workflow_slug: AE's slug for the workflow the run belongs to.
        run_id: AE's identifier for this run.
    """

    workflow_slug: str
    run_id: str


@dataclass(frozen=True, slots=True, kw_only=True)
class QueueWorkflowSpec:
    """A workflow to dispatch straight onto a Temporal task queue.

    The queue name is **validated at construction**, and that placement is the
    point rather than a convenience: Temporal accepts a dispatch to any
    well-formed queue name, so an unusable one fails by silence — the execution
    is created, nothing polls for it, and the run reports nothing until its
    24-hour heartbeat backstop. Refusing here turns a whole spent scenario
    budget into an immediate typed failure. See
    :class:`~application_sdk.testing.harness.starters.UnusableTaskQueueError`.

    Attributes:
        workflow_type: Registered workflow type name. For a multi-entrypoint app
            this is the canonical ``{app_name}:{entry_point}`` type the worker
            registers, not a ``legacy_workflow_types`` alias.
        task_queue: Queue to dispatch on. Not derived by this class in the
            general case: the derivation is
            :func:`application_sdk.common.task_queue.derive_task_queue`, and
            re-deriving it in the harness would add a sixth independent
            derivation of the seam this project is trying to collapse.
            :meth:`for_deployment` is the way to *call* that derivation.
        workflow_id: Explicit workflow ID. ``None`` mints one at dispatch — see
            :func:`~application_sdk.testing.harness.starters.start_on_task_queue`.
        argument: The workflow's single input argument. One argument rather than
            a list because that is this SDK's own convention on the production
            path (``args=[input_data]`` in
            :mod:`application_sdk.execution._temporal.backend`), and a harness
            that dispatched a different arity would be testing a shape no
            deployed app is started with.

    Raises:
        UnusableTaskQueueError: If ``task_queue`` is blank or still carries an
            unresolved ``{app_name}`` / ``{deployment_name}`` token.
    """

    workflow_type: str
    task_queue: str
    workflow_id: str | None = None
    argument: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Refuse a queue name nothing could poll.

        Two shapes, one leaf. A blank name is a config value that never
        resolved; a surviving template token is a manifest the contract
        toolkit's bake did not reach (the case
        :func:`application_sdk.common.task_queue.resolve_manifest_tokens` leaves
        visible on purpose, precisely so it is greppable rather than filled with
        a manufactured name).
        """
        if not self.task_queue.strip():
            raise UnusableTaskQueueError(
                message=(
                    "A workflow cannot be dispatched onto a blank task queue. "
                    "Derive the name with "
                    "application_sdk.common.task_queue.derive_task_queue, or "
                    "use QueueWorkflowSpec.for_deployment"
                ),
                task_queue=self.task_queue,
                constraint="non-blank",
            )
        unresolved = [token for token in _UNRESOLVED_TOKENS if token in self.task_queue]
        if unresolved:
            raise UnusableTaskQueueError(
                message=(
                    f"Task queue {self.task_queue!r} still carries the "
                    f"unresolved template token(s) {', '.join(unresolved)}. "
                    "Temporal would accept this dispatch and no worker would "
                    "ever claim it, so the run would report nothing until its "
                    "24h heartbeat backstop. The served manifest this came "
                    "from was not reached by the contract toolkit's bake — "
                    "regenerate it, or derive the queue with "
                    "application_sdk.common.task_queue.derive_task_queue"
                ),
                task_queue=self.task_queue,
                constraint="no unresolved template tokens",
            )

    @classmethod
    def for_deployment(
        cls,
        *,
        workflow_type: str,
        app_name: str | None,
        deployment_name: str | None,
        workflow_id: str | None = None,
        argument: Mapping[str, object] | None = None,
    ) -> QueueWorkflowSpec:
        """Build a spec whose queue comes from the canonical derivation.

        The one place the harness touches queue naming, and it touches it by
        *calling* :func:`application_sdk.common.task_queue.derive_task_queue`
        rather than by reproducing the rule. That distinction is the whole
        reason this constructor exists: FND-195 collapsed five independent
        ``atlan-{app}-{deployment}`` derivations into one, and a harness that
        hand-built the string would be the sixth — agreeing today and diverging
        the first time the rule changes, in the direction that fails silently
        (AE submits to one queue, the worker polls another; CONNECT-183).

        Args:
            workflow_type: Registered workflow type name.
            app_name: Bare app name, **without** the ``atlan-`` prefix — the
                convention both ``ATLAN_APPLICATION_NAME`` and the contract
                toolkit's bake use. Prefixing an already-prefixed value is how
                DISTR-834 shipped a queue no worker polled, which is why the
                prefix is the derivation's to add.
            deployment_name: Deployment name. Blank counts as unset, and the
                derivation's answer for that is a bare, unprefixed queue — the
                same one a local worker polls.
            workflow_id: Explicit workflow ID, or ``None`` to mint one.
            argument: The workflow's single input argument.

        Returns:
            The spec, with :attr:`task_queue` set to the derived name.

        Raises:
            UnusableTaskQueueError: If no queue name is derivable, which happens
                exactly when there is no app name. The derivation answers
                ``None`` there rather than inventing a segment, and this is the
                caller for which there is nothing sensible to do with that: a
                dispatch needs a queue.
        """
        derived = derive_task_queue(app_name, deployment_name)
        if derived is None:
            raise UnusableTaskQueueError(
                message=(
                    "No task queue is derivable without an app name "
                    f"(app_name={app_name!r}, "
                    f"deployment_name={deployment_name!r}). Set "
                    "ATLAN_APPLICATION_NAME on the run, or pass the app's "
                    "registered name — derive_task_queue answers None here "
                    "rather than manufacturing a segment, because "
                    "'atlan-default-prod' looks entirely legitimate and no "
                    "worker polls it"
                ),
                task_queue=None,
                constraint="an app name is required",
            )
        return cls(
            workflow_type=workflow_type,
            task_queue=derived,
            workflow_id=workflow_id,
            argument={} if argument is None else argument,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class HttpWorkflowSpec:
    """A workflow to start by calling the app's own handler Service.

    Attributes:
        target: Handler Service and port to POST to.
        workflow_name: Workflow name the handler routes on.
        body: Request body.
    """

    target: ServiceTarget
    workflow_name: str
    body: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True, kw_only=True)
class WorkflowRunHandle:
    """A run started directly on a task queue.

    Attributes:
        workflow_id: The workflow ID that was started.
        run_id: Temporal's run ID for this execution. Never absent and never
            empty: a handle that cannot name its run can only ask about "the
            latest run of this id", which is a different execution the moment
            anything re-dispatches or continues-as-new. The starter raises
            rather than hand one back — see
            :class:`~application_sdk.testing.harness.starters.WorkflowStartFailedError`.
        task_queue: Queue it was dispatched on, echoed back so a caller can
            assert the queue it *meant* to use is the queue it got.
    """

    workflow_id: str
    run_id: str
    task_queue: str


@dataclass(frozen=True, slots=True, kw_only=True)
class HttpRunHandle:
    """A run started through the app's handler Service.

    Attributes:
        workflow_id: The workflow ID the handler reports. The handler's response
            is the only identifier available on this path — there is no run ID
            until the workflow is looked up in Temporal.
    """

    workflow_id: str
