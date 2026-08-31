"""Dispatching a workflow straight onto a Temporal task queue (FND-246).

**New work, not an extraction.** Nothing in this SDK or in
``atlanhq/app-runtime-test-suite`` started a workflow this way before, and the
Slack thread that scoped FND-224 was wrong about that in a way worth recording:
it described ``testing/e2e/workflows.run_workflow`` as "a helper that puts a
workflow straight onto a task queue". That function is an HTTP ``POST
/api/v1/workflows`` to the app's *handler Service* through an ephemeral
``kubectl port-forward`` — it never touches a queue, and the app's own handler
decides the type, the id and the queue. So this leaf of the shared tree was
unbuilt on both sides, which is why there is no original to run a differential
against and why this module's tests are written as claims rather than as a
comparison.

**Why it is first.** The runtime scenario doc puts it at the top explicitly:
*"Submit a workflow to the app's task queue and check it reaches Completed.
Everything else depends on this working, so it runs first."* Every scaling,
version-rollout and eviction scenario needs work on a queue before there is
anything to observe, and the two paths that already exist cannot supply it — the
AE submit needs a published DAG and an indexed slug, and the handler POST proves
the handler works rather than that the queue does.

**The mutation lives here, not on the reader.**
:class:`~application_sdk.testing.harness.temporal.TemporalReader` is read-only by
decision, so that no probe inside a bounded wait can change the thing it is
measuring. This module is the other side of that decision: it takes a connection
someone else opened and does exactly one thing with it.

**A** :class:`~application_sdk.testing.harness.temporal.TemporalConnection`
**, not a raw client.** FND-246 sketched the signature as ``client:
TemporalClient``, which predates the reader landing (FND-247) and does not
survive contact with it, for two reasons that point the same way. The harness
already has a type meaning "one loop's client plus whatever transport is holding
it up", built by the same two factories the reader is built over
(:func:`~application_sdk.testing.harness.temporal.frontend_connection`,
:func:`~application_sdk.testing.harness.temporal.port_forwarded_connection`); a
starter taking the bare client would be reaching *through* that type and then
re-deriving the namespace from the client and the address from nowhere, so its
failures could not name the frontend they went to. And it is what keeps the
engine out of this module's public signature — the coupling P007 is about — while
the reader's own factories keep owning how the frontend is reached.

What it deliberately does **not** take is a connection *factory*. The transport's
lifetime stays with whoever opened it: a port-forwarded connection holds a
``kubectl`` child process, and a one-shot function that opened its own would
either leak that child or close a tunnel the caller's reader is still reading
through. A caller that wants one tunnel for both hands the same connection to
this function and to the reader's ``connect`` — and then owns closing it, since
the reader's ``aclose`` would otherwise do it on the caller's behalf.

**No correlation memo, and that is a decision.** The production start path
stamps ``memo={"correlation_id": ...}`` so a run's logs are findable by that id.
Omitting it is safe — ``CorrelationContext.from_temporal_memo`` answers ``None``
for an absent memo and the app's inbound interceptor mints its own — so the only
thing lost is the harness *choosing* the id it will later grep for. That belongs
with evidence collection (child G, FND-243), where the id has a consumer;
stamping one here with nothing reading it would be a field on a handle that
means "someone might want this".
"""

from __future__ import annotations

import os

# P006 asks that ``temporalio`` stay behind ``execution/_temporal/``. Same
# exception, same reasoning, and the same hand-sorted layout as
# :mod:`application_sdk.testing.harness.temporal.client` — read the long note
# there for both. In short: the contract is about the *worker* adapter's
# workflow and activity API on a production path, and these two names are the
# error types an out-of-cluster test dispatch has to catch. The adapter does not
# offer a "start an arbitrary registered type on an arbitrary queue" verb and
# should not grow one to satisfy a lint rule — a harness-shaped start on a
# production path is a worse coupling than the one the rule prevents.
#
# The directives sit ABOVE each import rather than trailing it, and ``isort`` is
# off across the block: a trailing directive gets wrapped onto the name line by
# ``ruff-format`` and stops matching, and a re-sort that lifts an import out from
# under its own comment silently un-suppresses it.
# isort: off
# conformance: ignore[P006] harness control-plane dispatch — see the note above
from temporalio.exceptions import WorkflowAlreadyStartedError

# conformance: ignore[P006] harness control-plane dispatch — see the note above
from temporalio.service import RPCError
# isort: on

from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.testing.harness.identity import Minter
from application_sdk.testing.harness.starters._errors import (
    WorkflowStartConflictError,
    WorkflowStartFailedError,
)
from application_sdk.testing.harness.starters._specs import (
    QueueWorkflowSpec,
    WorkflowRunHandle,
)
from application_sdk.testing.harness.temporal import TemporalConnection

logger = get_logger(__name__)

__all__ = ["start_on_task_queue"]


async def start_on_task_queue(
    spec: QueueWorkflowSpec,
    *,
    connection: TemporalConnection,
    minter: Minter | None = None,
) -> WorkflowRunHandle:
    """Start a workflow by dispatching it directly onto a Temporal task queue.

    Deliberately shares no signature with
    :func:`~application_sdk.testing.harness.starters.start_via_automation_engine`
    or
    :func:`~application_sdk.testing.harness.starters.start_via_app_handler`:
    different inputs, different handles, nothing in the middle to widen.

    Args:
        spec: What to start, and where. Its queue name was already validated
            when it was constructed.
        connection: An open connection to the frontend, on *this* event loop — a
            ``temporalio`` client is bound to the loop that created it. Not
            closed here: see the module docstring on why the transport's lifetime
            stays with whoever opened it.
        minter: Supplies the unique half of a minted workflow id, when
            ``spec.workflow_id`` is ``None``. Injected for the reason
            :mod:`application_sdk.testing.harness.identity` exists at all: a
            name built from an unseeded clock is a name no test can predict, and
            the workflow id is what a scenario later describes, grades and (on a
            failed run) terminates. ``None`` builds one over the real clock.

    Returns:
        The run handle, carrying the workflow id, the run id of the execution
        this call started, and the queue it was dispatched on.

    Raises:
        WorkflowStartConflictError: If a run is already using this workflow id.
        WorkflowStartFailedError: If the dispatch failed, or succeeded without
            naming a run.
        BaseException: Anything that is not ``temporalio``'s own error type
            propagates unconverted. A ``TypeError`` from a wiring bug is a bug,
            and dressing it as a dependency failure would let a caller grading a
            scenario read it as "Temporal was down".

    Example:
        >>> connection = await port_forwarded_connection(  # doctest: +SKIP
        ...     target=frontend, namespace="default"
        ... )
        >>> handle = await start_on_task_queue(  # doctest: +SKIP
        ...     QueueWorkflowSpec.for_deployment(
        ...         workflow_type="HelloWorldWorkflow",
        ...         app_name="hello-world",
        ...         deployment_name="default",
        ...     ),
        ...     connection=connection,
        ... )
    """
    workflow_id = spec.workflow_id or _minted_workflow_id(spec.workflow_type, minter)
    try:
        started = await connection.client.start_workflow(
            spec.workflow_type,
            args=[dict(spec.argument)],
            id=workflow_id,
            task_queue=spec.task_queue,
        )
    # conformance: ignore[E004] re-raised immediately as a typed leaf carrying the id, the queue and the frontend; logging here would report the same failure twice
    except WorkflowAlreadyStartedError as error:
        raise WorkflowStartConflictError(
            message=(
                f"Workflow id {workflow_id!r} is already in use in namespace "
                f"{connection.namespace!r}, so nothing was dispatched onto "
                f"{spec.task_queue}. Let the starter mint an id, pass a "
                "different one, or terminate the run holding it"
            ),
            resource_identifier=workflow_id,
            workflow_id=workflow_id,
            existing_run_id=error.run_id,
            task_queue=spec.task_queue,
            temporal_namespace=connection.namespace,
            cause=error,
        ) from error
    except RPCError as error:
        raise WorkflowStartFailedError(
            message=(
                f"Could not dispatch {spec.workflow_type} as {workflow_id!r} "
                f"onto {spec.task_queue} ({error.status.name})"
            ),
            target=spec.task_queue,
            workflow_id=workflow_id,
            task_queue=spec.task_queue,
            temporal_namespace=connection.namespace,
            address=connection.address,
            rpc_status=error.status.name,
            cause=error,
        ) from error

    # `result_run_id`, never `run_id`: a handle from `start_workflow` leaves
    # `run_id` as None by documented design — it is only set by
    # `get_workflow_handle` — so reading it would make every started run
    # unidentifiable while the code looked right. `first_execution_run_id` is
    # the same value for a plain start and is read as a fallback rather than as
    # a second source of truth.
    run_id = started.result_run_id or started.first_execution_run_id
    if not run_id:
        raise WorkflowStartFailedError(
            message=(
                f"Temporal reported {workflow_id!r} started on "
                f"{spec.task_queue} but named no run. Without a run id every "
                "later read describes 'the latest run of this id', which is a "
                "different execution as soon as anything re-dispatches or "
                "continues-as-new"
            ),
            target=spec.task_queue,
            workflow_id=workflow_id,
            task_queue=spec.task_queue,
            temporal_namespace=connection.namespace,
            address=connection.address,
        )

    logger.info(
        "dispatched %s as %s run %s onto task queue %s in namespace %s",
        spec.workflow_type,
        workflow_id,
        run_id,
        spec.task_queue,
        connection.namespace,
    )
    return WorkflowRunHandle(
        workflow_id=started.id, run_id=run_id, task_queue=spec.task_queue
    )


def _minted_workflow_id(workflow_type: str, minter: Minter | None) -> str:
    """Mint a workflow id for a spec that did not carry one.

    The type is the prefix so a run is recognisable in the Temporal UI without
    cross-referencing anything, and
    :meth:`~application_sdk.testing.harness.identity.Minter.unique_suffix` is the
    unique half — the same "clock second plus six padded random digits" the
    ephemeral connection names use, chosen there because two parallel e2e legs
    landing in the same second must not collide. The collision here is worse
    than a name clash: two runs on one id means the second dispatch raises, and
    a scenario that re-read "the latest run" would grade the wrong execution.

    Only ``unique_suffix`` is used, so the ambient ``GITHUB_RUN_ID`` that
    :meth:`~application_sdk.testing.harness.identity.Minter.from_environment`
    reads has no effect on the result; the factory is used anyway so there is one
    construction path for a real-clock minter rather than two.
    """
    resolved = Minter.from_environment(os.environ) if minter is None else minter
    return f"{workflow_type}-{resolved.unique_suffix()}"
