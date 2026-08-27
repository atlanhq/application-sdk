"""Three ways to start a workflow. Deliberately no shared signature.

Different inputs, different handles, no common verb — this is the point, not an
oversight. A shared ``start(spec) -> handle`` would have to accept the union of
three unrelated specs and return the union of three unrelated handles, and every
new way to start work would widen both. As three functions, a base class simply
calls the one it wants, and a fourth way to start work (a cluster mutation, a
chaos injection) is a fourth function with nothing in the middle to widen.

**One of the three is real.** :func:`start_on_task_queue` dispatches straight
onto a Temporal task queue (FND-246) — new work on both sides of this project,
not a lift: nothing in this SDK or in ``atlanhq/app-runtime-test-suite`` started
a workflow that way before, and it is the runtime suite's *first* scenario
because everything else depends on it working. See
:mod:`application_sdk.testing.harness.starters._queue` for the correction to the
record that scoping it turned on.

The other two are still stubs. ``testing/e2e/workflows.run_workflow`` is
:func:`start_via_app_handler`: an HTTP ``POST /api/v1/workflows`` to the app's
*handler Service* through an ephemeral ``kubectl port-forward``, which never
touches a task queue. :func:`start_via_automation_engine` is the AE submit,
lifting from ``testing/e2e/client.py``.

:func:`start_via_automation_engine` lands in child G on FND-224.
:func:`start_via_app_handler` moves with the cluster reader in child E.

Module map:

``_specs``
    The five specs and handles — three pairs, no shared base.
``_queue``
    :func:`start_on_task_queue`, and where its client comes from.
``_errors``
    The three leaves a dispatch can raise.
"""

from __future__ import annotations

from collections.abc import Mapping

from application_sdk.testing.harness._errors import HarnessNotBuiltError
from application_sdk.testing.harness.cluster import ClusterReader
from application_sdk.testing.harness.starters._errors import (
    UnusableTaskQueueError,
    WorkflowStartConflictError,
    WorkflowStartFailedError,
)
from application_sdk.testing.harness.starters._queue import start_on_task_queue
from application_sdk.testing.harness.starters._specs import (
    AERunHandle,
    HttpRunHandle,
    HttpWorkflowSpec,
    QueueWorkflowSpec,
    WorkflowRunHandle,
)

__all__ = [
    # Specs and handles
    "AERunHandle",
    "HttpRunHandle",
    "HttpWorkflowSpec",
    "QueueWorkflowSpec",
    "WorkflowRunHandle",
    # The three ways to start work
    "start_on_task_queue",
    "start_via_app_handler",
    "start_via_automation_engine",
    # Leaves
    "UnusableTaskQueueError",
    "WorkflowStartConflictError",
    "WorkflowStartFailedError",
]


async def start_via_automation_engine(spec: Mapping[str, object]) -> AERunHandle:
    """Start a run through the Automation Engine.

    Args:
        spec: The AE submit payload, as
            :func:`application_sdk.testing.e2e.payload.build_ae_payload` builds it.

    Returns:
        The AE run handle.

    Raises:
        HarnessNotBuiltError: Always — implementation is child G on FND-224.
    """
    raise HarnessNotBuiltError(
        message="start_via_automation_engine is not implemented yet",
        operation="start_via_automation_engine",
        reason="child G on FND-224",
        issue="FND-224",
        component="harness_starters",
    )


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
