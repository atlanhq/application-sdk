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
from application_sdk.testing.harness.automation_engine.retry import (
    cold_start_submit_kwargs,
)
from application_sdk.testing.harness.cluster import ServiceTarget
from application_sdk.testing.harness.starters._errors import UnusableTaskQueueError

__all__ = [
    "AERunHandle",
    "AEWorkflowSpec",
    "SeededWorkflow",
    "HttpRunHandle",
    "HttpWorkflowSpec",
    "QueueWorkflowSpec",
    "SubmitRetry",
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
        seed_version: The version number the starter published this run's seed
            DAG under, or ``None`` when it published none — which is the case
            for a spec that named a pre-existing slug. Carried on the handle
            rather than left with the starter because it is the only way a
            caller can later tell the harness' own seed apart from the manifest
            AE published over it at submit; ``BaseE2ETest._supersedes`` is the
            reader, and its ``None`` branch is exactly this case ("nothing was
            seeded, so nothing can have been superseded").
    """

    workflow_slug: str
    run_id: str
    seed_version: int | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class SeededWorkflow:
    """An AE workflow with a published version, ready to be submitted against.

    What :func:`~application_sdk.testing.harness.starters.publish_seed_version`
    hands back — the half of :class:`AERunHandle` that exists before there is a
    run. A pair rather than a bare slug because the two fields are only
    meaningful together: comparing what AE later serves against
    :attr:`seed_version` is the whole deployed-manifest identity check, and a
    caller that kept only the slug could not tell the harness' own seed apart
    from the manifest the tenant published over it.

    Attributes:
        slug: AE's slug for the workflow.
        seed_version: The version number this publish created, or ``None`` when
            nothing was published — which is the pre-existing-slug path, where
            there is no seed for the tenant to have superseded.
    """

    slug: str
    seed_version: int | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class SubmitRetry:
    """How hard the AE submit tries before it gives up.

    A typed pair rather than the ``dict[str, int]`` the lifted code threaded
    through ``**kwargs``. Two reasons, and the second is the one that matters:
    a mapping of ints is checked by nobody, so a rename on either side fails at
    runtime inside a non-idempotent write; and the sizing is a *decision* about
    what the harness is waiting for, which deserves a name.

    :meth:`for_cold_start` is the only way to derive one. There is no second
    derivation of the budget here — the arithmetic stays in
    :func:`~application_sdk.testing.harness.automation_engine.cold_start_submit_kwargs`,
    which carries the reasoning about why widening the submit's *existing* retry
    loop is the only safe shape for a write that must not be re-entered.

    Attributes:
        retries: Retries on top of the initial attempt.
        sleep_seconds: Fixed gap between attempts, before any ``retry_after``
            the origin names.
    """

    retries: int
    sleep_seconds: int

    @classmethod
    def for_cold_start(
        cls, *, timeout_seconds: int, poll_interval_seconds: int
    ) -> SubmitRetry | None:
        """Size the submit's retry to a tenant-app cold start.

        The AE submit is the only tenant-facing probe of the installed app pod
        on the connector CI path, because the runner has no ``kubectl`` route
        into the tenant vcluster. A pod minutes from serving arrives as a
        generic retryable 5xx, so the budget has to cover a cold start rather
        than transient AE overload.

        Args:
            timeout_seconds: Total cold-start budget. Zero or negative answers
                ``None``, which leaves the submit's own defaults in place.
            poll_interval_seconds: Gap between submit attempts.

        Returns:
            The sizing, or ``None`` when the cold-start budget is disabled.

        Raises:
            MissingHarnessClassAttrError: When *timeout_seconds* is positive but
                *poll_interval_seconds* is not — the retry count divides by the
                interval, so a zero would crash rather than gate the submit.
        """
        sizing = cold_start_submit_kwargs(timeout_seconds, poll_interval_seconds)
        if not sizing:
            return None
        return cls(
            retries=sizing["retries"], sleep_seconds=sizing["retry_sleep_seconds"]
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class AEWorkflowSpec:
    """A workflow to start through the Automation Engine.

    Four steps' worth of input in one value: create the AE workflow, seed a DAG
    version under it, publish that version, submit a run against it. They are
    one spec rather than four calls because they are one *decision* — a caller
    that seeded a DAG and did not submit has left a published version on the
    tenant that nothing will ever run.

    What is deliberately not here is how the seed DAG was *built*. Resolving a
    manifest's mustache tokens, choosing the extract task queue, falling back to
    a legacy graph — those are the connector suite's, and they are what makes
    ``BaseE2ETest`` a base class rather than a function. This spec takes the
    graph as a value, which is also what lets a runtime scenario supply one that
    never came from a ``manifest.json`` at all.

    Attributes:
        name: AE workflow name. The create endpoint is idempotent on it, so
            re-running under the same name reuses the workflow rather than
            accumulating one per run.
        seed_dag: The DAG to publish as this workflow's seed version. Sent
            verbatim: AE's submit is what makes Heracles fetch the tenant pod's
            own manifest and publish it *over* this one, so the seed's job is to
            make the workflow submittable, not to be the graph that runs.
        payload: The AE submit body, as
            :func:`application_sdk.testing.e2e.payload.build_ae_payload` builds
            it.
        description: Free text on the AE workflow.
        slug: An AE slug that already exists. Non-empty **skips create, seed and
            publish entirely** and submits against it — the escape hatch for a
            suite pointed at a workflow someone else maintains. Not a
            convenience: seeding over such a workflow would replace a DAG this
            run does not own.
        version: Explicit seed version number, or ``None`` to mint one. See
            :meth:`~application_sdk.testing.harness.identity.Minter.seed_version`
            for why the minted value is a bare clock reading.
        submit_retry: How hard the submit tries. ``None`` leaves
            :meth:`~application_sdk.testing.harness.automation_engine.AEClient.submit_workflow`'s
            own defaults, which are sized for transient AE overload rather than
            for a pod cold start.
    """

    name: str
    seed_dag: Mapping[str, object] = field(default_factory=dict)
    payload: Mapping[str, object] = field(default_factory=dict)
    description: str = ""
    slug: str = ""
    version: int | None = None
    submit_retry: SubmitRetry | None = None


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
