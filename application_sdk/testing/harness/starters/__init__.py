"""Three ways to start a workflow. Deliberately no shared signature.

Different inputs, different handles, no common verb — this is the point, not an
oversight. A shared ``start(spec) -> handle`` would have to accept the union of
three unrelated specs and return the union of three unrelated handles, and every
new way to start work would widen both. As three functions, a base class simply
calls the one it wants, and a fourth way to start work (a cluster mutation, a
chaos injection) is a fourth function with nothing in the middle to widen.

**Two of the three are real.** :func:`start_on_task_queue` dispatches straight
onto a Temporal task queue (FND-246) — new work on both sides of this project,
not a lift: nothing in this SDK or in ``atlanhq/app-runtime-test-suite`` started
a workflow that way before, and it is the runtime suite's *first* scenario
because everything else depends on it working. See
:mod:`application_sdk.testing.harness.starters._queue` for the correction to the
record that scoping it turned on.

:func:`start_via_automation_engine` is the opposite kind of module: a plain
extraction (child G, FND-243) of ``BaseE2ETest._bootstrap_workflow`` plus the
submit half of ``run_full_dag``, so it *can* be pinned against the code it came
from. What it adds is nothing; what it removes is the last part of the AE path
that only existed inside a base class.

One is still a stub. ``testing/e2e/workflows.run_workflow`` is
:func:`start_via_app_handler`: an HTTP ``POST /api/v1/workflows`` to the app's
*handler Service* through an ephemeral ``kubectl port-forward``, which never
touches a task queue. It moves with the cluster reader in child E.

Module map:

``_specs``
    The specs and handles — three pairs, no shared base, plus the submit
    retry's typed sizing.
``_queue``
    :func:`start_on_task_queue`, and where its client comes from.
``_ae``
    :func:`start_via_automation_engine`, and the four-write sequence it owns.
``_errors``
    The three leaves a dispatch can raise.
"""

from __future__ import annotations

from application_sdk.testing.harness._errors import HarnessNotBuiltError
from application_sdk.testing.harness.cluster import ClusterReader
from application_sdk.testing.harness.starters._ae import (
    publish_seed_version,
    start_via_automation_engine,
)
from application_sdk.testing.harness.starters._errors import (
    UnusableTaskQueueError,
    WorkflowStartConflictError,
    WorkflowStartFailedError,
)
from application_sdk.testing.harness.starters._queue import start_on_task_queue
from application_sdk.testing.harness.starters._specs import (
    AERunHandle,
    AEWorkflowSpec,
    HttpRunHandle,
    HttpWorkflowSpec,
    QueueWorkflowSpec,
    SeededWorkflow,
    SubmitRetry,
    WorkflowRunHandle,
)

__all__ = [
    # Specs and handles
    "AERunHandle",
    "AEWorkflowSpec",
    "HttpRunHandle",
    "HttpWorkflowSpec",
    "QueueWorkflowSpec",
    "SeededWorkflow",
    "SubmitRetry",
    "WorkflowRunHandle",
    # The publish half of the AE sequence, for a caller whose payload needs the
    # slug this mints before it can be built.
    "publish_seed_version",
    # The three ways to start work
    "start_on_task_queue",
    "start_via_app_handler",
    "start_via_automation_engine",
    # Leaves
    "UnusableTaskQueueError",
    "WorkflowStartConflictError",
    "WorkflowStartFailedError",
]


async def start_via_app_handler(
    spec: HttpWorkflowSpec, *, reader: ClusterReader
) -> HttpRunHandle:
    """Start a workflow by POSTing to the app's own handler Service.

    Args:
        spec: What to start, and which Service to call.
        reader: Supplies the HTTP transport into the cluster. Taken as an
            argument rather than constructed here so the same call works from a
            driver outside the cluster (port-forward) and one inside it.

    Returns:
        The run handle.

    Raises:
        HarnessNotBuiltError: Always — this lifts from
            ``application_sdk.testing.e2e.workflows.run_workflow`` with the
            cluster reader in child E on FND-224.
    """
    raise HarnessNotBuiltError(
        message="start_via_app_handler is not implemented yet",
        operation="start_via_app_handler",
        reason="child E on FND-224, with the cluster reader",
        issue="FND-224",
        component="harness_starters",
    )
