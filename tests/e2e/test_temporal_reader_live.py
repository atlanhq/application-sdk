"""Live-frontend smoke test for the Temporal reader.

FND-247's acceptance criterion is that both readers work, and unit tests with a
scripted client cannot establish it. What they cannot check is that a real
frontend is reachable through a ``kubectl port-forward`` tunnel at all, that the
wire fields this backend reads are the ones a real frontend populates — a poller
with a ``deployment_options.build_id`` only exists where Worker Deployment
versioning is actually in use — and that a queue name referring to nothing
answers **empty** while an unreachable frontend **raises**. Those two answering
differently is the entire point of the read, and it is only observable here.

So it lives as an explicit, opt-in check rather than as prose in a PR
description. Requires VPN plus a logged-in ``kubectl`` context, which is why it
is ``e2e``-marked and skipped without a queue to point at::

    E2E_TEMPORAL_QUEUE=atlan-hello-world-default \\
    E2E_TEMPORAL_FRONTEND_NAMESPACE=temporal \\
        uv run pytest tests/e2e/test_temporal_reader_live.py -m e2e -v

``E2E_TEMPORAL_FRONTEND_SERVICE`` and ``E2E_TEMPORAL_FRONTEND_PORT`` override the
Service the tunnel targets; ``E2E_TEMPORAL_NAMESPACE`` overrides the Temporal
namespace (not the Kubernetes one — they are different things and the variables
are named apart for that reason). Point ``E2E_TEMPORAL_ADDRESS`` at a frontend
directly to skip the tunnel entirely, which is what a driver already inside the
cluster would do.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest

from application_sdk.testing.harness.cluster import ServiceTarget
from application_sdk.testing.harness.temporal import (
    PollerInfo,
    TaskQueueType,
    TemporalReadFailedError,
    TemporalServiceReader,
    WorkflowNotFoundError,
    frontend_connection,
    port_forwarded_connection,
    stale_version_pollers,
)

pytestmark = pytest.mark.e2e

_QUEUE_ENV = "E2E_TEMPORAL_QUEUE"


@pytest.fixture(scope="module")
def queue() -> str:
    value = os.environ.get(_QUEUE_ENV)
    if not value:
        pytest.skip(f"set {_QUEUE_ENV} to a task queue a worker is polling")
    return value


@pytest.fixture(scope="module")
def temporal_namespace() -> str:
    return os.environ.get("E2E_TEMPORAL_NAMESPACE", "default")


@pytest.fixture
async def reader(temporal_namespace: str) -> AsyncIterator[TemporalServiceReader]:
    """A reader over whichever connection the environment describes.

    Both factories are exercised by this one fixture on purpose: which one a run
    uses is the only difference between an out-of-cluster driver and an in-cluster
    one, and that is exactly the seam FND-248 arrives through.
    """
    address = os.environ.get("E2E_TEMPORAL_ADDRESS")

    async def _connect():
        if address:
            return await frontend_connection(
                address=address, namespace=temporal_namespace
            )
        return await port_forwarded_connection(
            target=ServiceTarget(
                namespace=os.environ.get("E2E_TEMPORAL_FRONTEND_NAMESPACE", "temporal"),
                service=os.environ.get(
                    "E2E_TEMPORAL_FRONTEND_SERVICE", "temporal-frontend"
                ),
                port=int(os.environ.get("E2E_TEMPORAL_FRONTEND_PORT", "7233")),
            ),
            namespace=temporal_namespace,
        )

    built = TemporalServiceReader(connect=_connect)
    try:
        yield built
    finally:
        await built.aclose()


async def test_a_real_worker_is_polling_both_halves_of_its_queue(
    reader: TemporalServiceReader, queue: str, temporal_namespace: str
) -> None:
    """Every field the backend claims to read is present on a real poller."""
    pollers = await reader.task_queue_pollers(queue, namespace=temporal_namespace)

    assert pollers, f"nothing is polling {queue} — point at a queue that has a worker"
    for entry in pollers:
        assert isinstance(entry, PollerInfo)
        assert entry.identity, "a real poller always reports an identity"
        assert entry.last_access.tzinfo is not None
        # A versioned worker reports its Worker Deployment, not just a build id:
        # the pair is what a TemporalWorkerDeployment's intended version can be
        # matched against.
        if entry.build_id is not None:
            assert entry.deployment_name is not None

    # Both halves, which is what makes a half-polling worker visible.
    seen = {entry.task_queue_type for entry in pollers}
    assert TaskQueueType.WORKFLOW in seen, (
        "no workflow-task poller: the queue has an activity poller only, which is "
        "the half-polling shape this read exists to surface"
    )


async def test_the_intended_version_is_the_one_holding_the_queue(
    reader: TemporalServiceReader, queue: str, temporal_namespace: str
) -> None:
    """The precondition gate's second half, against a real frontend.

    The intended version comes from the ``TemporalWorkerDeployment`` in a real
    scenario; here the majority build id stands in for it, which still exercises
    the predicate against real poller shapes and still fails if two builds are
    both holding the queue.
    """
    pollers = await reader.task_queue_pollers(queue, namespace=temporal_namespace)
    build_ids = {entry.build_id for entry in pollers}
    if build_ids == {None}:
        pytest.skip("this deployment does not use Worker Deployment versioning")

    current = max(
        (b for b in build_ids if b is not None),
        key=lambda b: sum(1 for entry in pollers if entry.build_id == b),
    )
    stale = stale_version_pollers(pollers, current_build_id=current)

    assert not stale, (
        "more than one build is polling this queue: "
        f"{sorted({entry.build_id for entry in stale}, key=str)} alongside {current}"
    )


async def test_a_queue_name_that_refers_to_nothing_answers_empty(
    reader: TemporalServiceReader, temporal_namespace: str
) -> None:
    """The largest uncaught regression class, and the only read that catches it:
    a well-formed queue name that nothing polls."""
    pollers = await reader.task_queue_pollers(
        "fnd247-no-such-queue", namespace=temporal_namespace
    )

    assert pollers == []


async def test_a_namespace_nobody_has_raises_rather_than_answering_empty(
    reader: TemporalServiceReader, queue: str
) -> None:
    """The other side of the same line, and the one that matters: empty is the
    finding this read delivers, so an unreadable frontend must not produce it."""
    with pytest.raises(TemporalReadFailedError) as raised:
        await reader.task_queue_pollers(queue, namespace="fnd247-no-such-namespace")

    assert raised.value.rpc_status in ("NOT_FOUND", "PERMISSION_DENIED")


async def test_a_workflow_nobody_started_is_absent_rather_than_an_error(
    reader: TemporalServiceReader,
) -> None:
    """The nullable read, so a wait for AE-dispatched work has a value to test."""
    assert await reader.find_workflow_status("fnd247-no-such-workflow") is None


async def test_the_strict_read_raises_not_found_for_the_same_workflow(
    reader: TemporalServiceReader,
) -> None:
    """And the strict twin fails at once rather than being absorbed by a wait."""
    with pytest.raises(WorkflowNotFoundError):
        await reader.workflow_status("fnd247-no-such-workflow")
