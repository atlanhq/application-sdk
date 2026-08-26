"""Three ways to start a workflow. Deliberately no shared signature.

Different inputs, different handles, no common verb — this is the point, not an
oversight. A shared ``start(spec) -> handle`` would have to accept the union of
three unrelated specs and return the union of three unrelated handles, and every
new way to start work would widen both. As three functions, a base class simply
calls the one it wants, and a fourth way to start work (a cluster mutation, a
chaos injection) is a fourth function with nothing in the middle to widen.

**Only one of the three exists today.** ``testing/e2e/workflows.run_workflow`` is
:func:`start_via_app_handler`: an HTTP ``POST /api/v1/workflows`` to the app's
*handler Service* through an ephemeral ``kubectl port-forward``. It never touches
a Temporal task queue. :func:`start_via_automation_engine` is the AE submit,
lifting from ``testing/e2e/client.py``. :func:`start_on_task_queue` is **new work
on both sides** — nothing in either repo starts a workflow by dispatching
directly to a queue — and it is the runtime suite's *first* scenario, because
everything else depends on it working.

:func:`start_via_automation_engine` lands in child G on FND-224.
:func:`start_via_app_handler` moves with the cluster reader in child E.
:func:`start_on_task_queue` is a separate issue, outside FND-224.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from application_sdk.testing.harness._errors import HarnessNotBuiltError
from application_sdk.testing.harness.automation_engine import AERunHandle
from application_sdk.testing.harness.cluster import ClusterReader, ServiceTarget

__all__ = [
    "HttpRunHandle",
    "HttpWorkflowSpec",
    "QueueWorkflowSpec",
    "WorkflowRunHandle",
    "start_on_task_queue",
    "start_via_app_handler",
    "start_via_automation_engine",
]


@dataclass(frozen=True, slots=True, kw_only=True)
class QueueWorkflowSpec:
    """A workflow to dispatch straight onto a Temporal task queue.

    Attributes:
        workflow_type: Registered workflow type name.
        task_queue: Queue to dispatch on. Not derived here: the derivation is
            :func:`application_sdk.common.task_queue.derive_task_queue`, and
            re-deriving it in the harness would add a sixth independent
            derivation of the seam this project is trying to collapse.
        workflow_id: Explicit workflow ID. ``None`` mints one.
        argument: The workflow's single input argument.
    """

    workflow_type: str
    task_queue: str
    workflow_id: str | None = None
    argument: Mapping[str, object] = field(default_factory=dict)


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
        run_id: Temporal's run ID for this execution.
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


async def start_on_task_queue(spec: QueueWorkflowSpec) -> WorkflowRunHandle:
    """Start a workflow by dispatching it directly onto a Temporal task queue.

    Args:
        spec: What to start, and where.

    Returns:
        The run handle.

    Raises:
        HarnessNotBuiltError: Always — new work, tracked as a separate issue
            outside FND-224. Nothing in the SDK or the runtime suite starts a
            workflow this way today.
    """
    raise HarnessNotBuiltError(
        message="start_on_task_queue is not implemented yet",
        operation="start_on_task_queue",
        reason="new work on both sides, tracked as a separate issue outside FND-224",
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
