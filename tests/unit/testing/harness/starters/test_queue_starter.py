"""Unit tests for the direct-to-task-queue starter (FND-246).

**These are claims, not a differential.** The other harness modules were lifted
from something — ``cluster`` retired ``testing/e2e/pods.py``, ``waiting`` came
out of ``poll_native_status`` — so their tests could compare against an original
on identical inputs. Nothing in this SDK or in ``atlanhq/app-runtime-test-suite``
dispatched a workflow onto a task queue before, so there is no original and no
authority to borrow. What that changes is where the risk sits: with no baseline,
the tests have to state what the dispatch *should* carry, and the two that carry
the most weight are the two a plausible implementation gets wrong.

The first is the run id. ``WorkflowHandle.run_id`` is ``None`` on a handle built
by ``start_workflow`` — documented, and only set by ``get_workflow_handle`` — so
a starter that read it would hand back an unidentifiable run while looking
entirely correct. The handles here are *real* ``WorkflowHandle`` objects for that
one reason.

The second is the queue name. Temporal accepts a dispatch to any well-formed
queue, so an unresolved ``atlan-{app_name}-{deployment_name}`` template fails by
silence — accepted, never claimed, nothing reported until a 24-hour backstop.
That is CONNECT-183, and a harness that reproduced it would spend a whole
scenario budget learning that a manifest was stale.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta

import pytest
from temporalio.api.enums.v1 import WorkflowExecutionStatus as WireStatus
from temporalio.exceptions import WorkflowAlreadyStartedError

from application_sdk.common.task_queue import derive_task_queue
from application_sdk.testing.harness import Budget, Settled, poll_until
from application_sdk.testing.harness._poll import fake_clock
from application_sdk.testing.harness.identity import Minter
from application_sdk.testing.harness.starters import (
    QueueWorkflowSpec,
    UnusableTaskQueueError,
    WorkflowRunHandle,
    WorkflowStartConflictError,
    WorkflowStartFailedError,
    start_on_task_queue,
)
from application_sdk.testing.harness.temporal import (
    WorkflowExecutionStatus,
    WorkflowStatus,
)
from tests.unit.testing._temporal_fakes import (
    FakeTemporal,
    RPCStatusCode,
    connection_over,
    reader_over,
    rpc_error,
    started,
    workflow_description,
)

_QUEUE = "atlan-hello-world-default"
_WIRE_COMPLETED = WireStatus.WORKFLOW_EXECUTION_STATUS_COMPLETED


def _minter(*, second: int = 1_700_000_000, random: int = 42) -> Minter:
    """A minter whose names are a function of its inputs, so they can be pinned."""
    return Minter(clock=lambda: second, randbelow=lambda _bound: random)


def _spec(
    *,
    workflow_type: str = "HelloWorldWorkflow",
    task_queue: str = _QUEUE,
    workflow_id: str | None = None,
    argument: Mapping[str, object] | None = None,
) -> QueueWorkflowSpec:
    return QueueWorkflowSpec(
        workflow_type=workflow_type,
        task_queue=task_queue,
        workflow_id=workflow_id,
        argument={} if argument is None else argument,
    )


# ---------------------------------------------------------------------------
# What reaches Temporal
# ---------------------------------------------------------------------------


async def test_the_dispatch_carries_the_spec_verbatim():
    fake = FakeTemporal()

    await start_on_task_queue(
        _spec(workflow_id="wf-explicit", argument={"credential_guid": "abc"}),
        connection=connection_over(fake),
    )

    verb, workflow, kwargs = fake.calls[0]
    assert verb == "start_workflow"
    assert workflow == "HelloWorldWorkflow"
    assert kwargs["id"] == "wf-explicit"
    assert kwargs["task_queue"] == _QUEUE


async def test_the_argument_arrives_as_the_single_positional_this_sdk_starts_with():
    """``args=[input_data]`` is what
    :mod:`application_sdk.execution._temporal.backend` dispatches on the
    production path, so a workflow's ``run`` takes one argument. A harness that
    spread the mapping into several would be exercising an arity no deployed app
    is ever started with, and would fail against every real app rather than
    against a bug."""
    fake = FakeTemporal()

    await start_on_task_queue(
        _spec(argument={"connection": {"connection_qualified_name": "default/x/1"}}),
        connection=connection_over(fake),
    )

    _verb, _workflow, kwargs = fake.calls[0]
    assert kwargs["args"] == [
        {"connection": {"connection_qualified_name": "default/x/1"}}
    ]


async def test_an_absent_argument_dispatches_an_empty_mapping_not_no_argument():
    """The arity must not depend on whether the caller had anything to say: a
    workflow whose signature takes one argument fails to be scheduled if it is
    dispatched with none, which would make "start with defaults" a different
    code path from "start with a config"."""
    fake = FakeTemporal()

    await start_on_task_queue(_spec(), connection=connection_over(fake))

    _verb, _workflow, kwargs = fake.calls[0]
    assert kwargs["args"] == [{}]


async def test_nothing_else_is_sent():
    """Pinned so a later addition — an ``id_reuse_policy``, a ``retry_policy``,
    a correlation ``memo`` — is a visible decision rather than a default that
    arrived with a refactor. The memo in particular is deliberately absent: see
    the module docstring on ``_queue``."""
    fake = FakeTemporal()

    await start_on_task_queue(_spec(), connection=connection_over(fake))

    _verb, _workflow, kwargs = fake.calls[0]
    assert set(kwargs) == {"args", "id", "task_queue"}


# ---------------------------------------------------------------------------
# The handle
# ---------------------------------------------------------------------------


async def test_the_run_id_is_the_started_run_not_the_handles_own_run_id():
    """The bug a real ``WorkflowHandle`` exists here to catch. ``start_workflow``
    leaves ``run_id`` unset by documented design and puts the started run on
    ``result_run_id``, so a starter reading the obvious attribute returns a
    handle that names no run — and every later read silently describes "the
    latest run of this id" instead."""
    fake = FakeTemporal(starts=[started(result_run_id="run-abc")])

    handle = await start_on_task_queue(_spec(), connection=connection_over(fake))

    assert handle.run_id == "run-abc"


async def test_the_first_execution_run_id_is_the_fallback():
    """Same value as ``result_run_id`` for a plain start, read second rather than
    as an independent source of truth."""
    fake = FakeTemporal(
        starts=[started(result_run_id=None, first_execution_run_id="run-first")]
    )

    handle = await start_on_task_queue(_spec(), connection=connection_over(fake))

    assert handle.run_id == "run-first"


async def test_a_start_that_names_no_run_is_a_failure_not_a_handle():
    """A handle whose ``run_id`` were empty would be worse than an error,
    because it reads like one that can name its run."""
    fake = FakeTemporal(
        starts=[started(result_run_id=None, first_execution_run_id=None)]
    )

    with pytest.raises(WorkflowStartFailedError) as caught:
        await start_on_task_queue(_spec(), connection=connection_over(fake))

    assert caught.value.rpc_status is None
    assert caught.value.task_queue == _QUEUE


async def test_the_handle_echoes_the_queue_it_dispatched_on():
    """So a scenario can assert the queue it *meant* to use is the queue it got —
    the check that catches an ``agent_spec()`` / worker mismatch at the start
    rather than three minutes into a wait."""
    fake = FakeTemporal()

    handle = await start_on_task_queue(_spec(), connection=connection_over(fake))

    assert handle == WorkflowRunHandle(
        workflow_id="wf-1", run_id="run-1", task_queue=_QUEUE
    )


async def test_the_workflow_id_comes_back_from_temporal_not_from_the_request():
    """They agree in practice; reading Temporal's answer is what makes a
    server-side normalisation visible rather than assumed away."""
    fake = FakeTemporal(starts=[started(workflow_id="wf-as-temporal-has-it")])

    handle = await start_on_task_queue(
        _spec(workflow_id="wf-as-sent"), connection=connection_over(fake)
    )

    assert handle.workflow_id == "wf-as-temporal-has-it"


# ---------------------------------------------------------------------------
# Minting an id
# ---------------------------------------------------------------------------


async def test_an_absent_id_is_minted_from_the_injected_minter():
    fake = FakeTemporal()

    await start_on_task_queue(
        _spec(),
        connection=connection_over(fake),
        minter=_minter(second=1_700_000_000, random=7),
    )

    _verb, _workflow, kwargs = fake.calls[0]
    assert kwargs["id"] == "HelloWorldWorkflow-1700000000000007"


async def test_an_explicit_id_is_used_verbatim_and_the_minter_is_not_consulted():
    fake = FakeTemporal()

    def _refuse(_bound: int) -> int:
        raise AssertionError("the minter must not be consulted for an explicit id")

    await start_on_task_queue(
        _spec(workflow_id="wf-mine"),
        connection=connection_over(fake),
        minter=Minter(clock=lambda: 0, randbelow=_refuse),
    )

    _verb, _workflow, kwargs = fake.calls[0]
    assert kwargs["id"] == "wf-mine"


async def test_two_minted_ids_in_the_same_second_do_not_collide():
    """The collision here is not a cosmetic name clash: two runs on one workflow
    id means the second dispatch raises, and a scenario re-reading "the latest
    run" would grade the wrong execution. The random half is what separates two
    parallel e2e legs whose setup lands in the same wall-clock second."""
    fake = FakeTemporal()
    randoms = iter((1, 2))

    minter = Minter(clock=lambda: 1_700_000_000, randbelow=lambda _b: next(randoms))
    for _ in range(2):
        await start_on_task_queue(
            _spec(), connection=connection_over(fake), minter=minter
        )

    ids = [kwargs["id"] for _verb, _workflow, kwargs in fake.calls]
    assert ids == [
        "HelloWorldWorkflow-1700000000000001",
        "HelloWorldWorkflow-1700000000000002",
    ]


async def test_a_minted_id_is_prefixed_with_the_workflow_type():
    """So a run is recognisable in the Temporal UI without cross-referencing the
    harness's own log."""
    fake = FakeTemporal()

    await start_on_task_queue(
        _spec(workflow_type="hello-world:sync"),
        connection=connection_over(fake),
        minter=_minter(),
    )

    _verb, _workflow, kwargs = fake.calls[0]
    assert kwargs["id"].startswith("hello-world:sync-")


# ---------------------------------------------------------------------------
# The queue name, refused before a byte leaves the process
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "queue",
    [
        "atlan-{app_name}-{deployment_name}",
        "atlan-hello-world-{deployment_name}",
        "{app_name}",
    ],
)
def test_an_unresolved_queue_template_is_refused_at_construction(queue: str):
    """The failure this removes is silence, so it has to happen before the
    dispatch — Temporal would accept every one of these."""
    with pytest.raises(UnusableTaskQueueError) as caught:
        QueueWorkflowSpec(workflow_type="w", task_queue=queue)

    assert caught.value.task_queue == queue
    assert not caught.value.effective_retryable


@pytest.mark.parametrize("queue", ["", "   "])
def test_a_blank_queue_is_refused(queue: str):
    with pytest.raises(UnusableTaskQueueError):
        QueueWorkflowSpec(workflow_type="w", task_queue=queue)


def test_the_refusal_names_the_token_so_a_stale_manifest_is_diagnosable():
    """``resolve_manifest_tokens`` leaves an un-derivable queue's template
    *visible* on purpose, precisely so it is greppable rather than filled with a
    manufactured name. A harness failure that swallowed the token would throw
    that away."""
    with pytest.raises(UnusableTaskQueueError) as caught:
        QueueWorkflowSpec(
            workflow_type="w", task_queue="atlan-{app_name}-{deployment_name}"
        )

    assert "{app_name}" in caught.value.message
    assert "{deployment_name}" in caught.value.message


@pytest.mark.parametrize(
    ("app_name", "deployment_name"),
    [
        ("hello-world", "default"),
        ("hello-world", ""),
        ("hello-world", None),
    ],
)
def test_for_deployment_takes_the_queue_from_the_canonical_derivation(
    app_name: str, deployment_name: str | None
):
    """Asserted *against* ``derive_task_queue`` rather than against a literal, so
    the test cannot pass while the harness has quietly grown a sixth independent
    derivation of the rule — which is the failure mode FND-195 collapsed and
    CONNECT-183 was caused by."""
    spec = QueueWorkflowSpec.for_deployment(
        workflow_type="w", app_name=app_name, deployment_name=deployment_name
    )

    assert spec.task_queue == derive_task_queue(app_name, deployment_name)


def test_for_deployment_does_not_pre_prefix_the_app_name():
    """DISTR-834 shipped ``atlan-atlan-dbt-production`` by prefixing an
    already-prefixed value. The prefix is the derivation's to add, and this pins
    that the constructor passes the bare name through."""
    spec = QueueWorkflowSpec.for_deployment(
        workflow_type="w", app_name="hello-world", deployment_name="default"
    )

    assert spec.task_queue == "atlan-hello-world-default"


@pytest.mark.parametrize("app_name", [None, "", "   "])
def test_for_deployment_refuses_when_no_queue_is_derivable(app_name: str | None):
    """``derive_task_queue`` answers ``None`` without an app name rather than
    manufacturing a segment, because ``atlan-default-prod`` looks entirely
    legitimate and no worker polls it. A dispatch has nothing to do with that
    answer but fail."""
    with pytest.raises(UnusableTaskQueueError) as caught:
        QueueWorkflowSpec.for_deployment(
            workflow_type="w", app_name=app_name, deployment_name="default"
        )

    assert caught.value.task_queue is None


def test_for_deployment_carries_the_rest_of_the_spec_through():
    spec = QueueWorkflowSpec.for_deployment(
        workflow_type="w",
        app_name="hello-world",
        deployment_name="default",
        workflow_id="wf-mine",
        argument={"k": "v"},
    )

    assert (spec.workflow_type, spec.workflow_id, dict(spec.argument)) == (
        "w",
        "wf-mine",
        {"k": "v"},
    )


# ---------------------------------------------------------------------------
# Failures
# ---------------------------------------------------------------------------


async def test_an_id_collision_is_its_own_non_retryable_leaf():
    """The one condition whose fix is caller-side — a different id, a minted
    one, or terminating the run holding it — so it must not arrive in the same
    class as an unreachable frontend."""
    fake = FakeTemporal(
        starts=[WorkflowAlreadyStartedError("wf-1", "HelloWorldWorkflow", run_id="r0")]
    )

    with pytest.raises(WorkflowStartConflictError) as caught:
        await start_on_task_queue(
            _spec(workflow_id="wf-1"), connection=connection_over(fake)
        )

    assert caught.value.existing_run_id == "r0"
    assert caught.value.workflow_id == "wf-1"
    assert caught.value.task_queue == _QUEUE
    assert not caught.value.effective_retryable


@pytest.mark.parametrize(
    "status",
    [
        RPCStatusCode.UNAVAILABLE,
        RPCStatusCode.DEADLINE_EXCEEDED,
        RPCStatusCode.INVALID_ARGUMENT,
        RPCStatusCode.PERMISSION_DENIED,
    ],
)
async def test_every_other_grpc_status_is_one_leaf_carrying_the_status(
    status: RPCStatusCode,
):
    """One classification rule, the reader's rule, with the status name on the
    leaf to tell a restarting frontend from a malformed request. See
    ``starters/_errors.py`` for why the more precise split is declined."""
    fake = FakeTemporal(starts=[rpc_error(status)])

    with pytest.raises(WorkflowStartFailedError) as caught:
        await start_on_task_queue(_spec(), connection=connection_over(fake))

    assert caught.value.rpc_status == status.name
    assert caught.value.task_queue == _QUEUE
    assert caught.value.temporal_namespace == "default"


async def test_a_wiring_bug_is_not_dressed_as_a_dependency_failure():
    """Only ``temporalio``'s own error types are converted. A ``TypeError`` from
    a bad call is a bug, and reporting it as ``DEPENDENCY_UNAVAILABLE`` would let
    a caller grading a scenario record it as "Temporal was down"."""
    fake = FakeTemporal(starts=[TypeError("start_workflow() got an unexpected kwarg")])

    with pytest.raises(TypeError):
        await start_on_task_queue(_spec(), connection=connection_over(fake))


async def test_the_failure_names_the_queue_the_namespace_and_the_frontend():
    """The three fields that answer the most common wedges in one step rather
    than from a gRPC message: a well-formed queue name that refers to nothing, a
    client bound to the wrong namespace, and a run pointed at the wrong frontend
    through a stale tunnel. The last of the three is the reason this takes a
    connection rather than a bare client — a client cannot name its address."""
    fake = FakeTemporal(starts=[rpc_error(RPCStatusCode.UNAVAILABLE)])

    with pytest.raises(WorkflowStartFailedError) as caught:
        await start_on_task_queue(
            _spec(),
            connection=connection_over(
                fake, namespace="prod", address="127.0.0.1:41231"
            ),
        )

    assert caught.value.temporal_namespace == "prod"
    assert caught.value.address == "127.0.0.1:41231"
    assert caught.value.target == _QUEUE


async def test_the_starter_does_not_close_the_connection_it_was_handed():
    """The transport's lifetime belongs to whoever opened it. A port-forwarded
    connection holds a ``kubectl`` child process, and a starter that tidied up
    after itself would tear down the tunnel the caller's reader is still reading
    through — which is exactly the composition the scenario below relies on."""
    fake = FakeTemporal()

    await start_on_task_queue(_spec(), connection=connection_over(fake))

    assert fake.closes == 0


# ---------------------------------------------------------------------------
# The other half of the runtime suite's first scenario
# ---------------------------------------------------------------------------


async def test_the_started_run_is_the_run_the_status_reader_describes():
    """*"Submit a workflow to the app's task queue and check it reaches
    Completed"* is one scenario, and it is only one if the two halves agree on
    which execution they are talking about. The starter and the reader run
    against the same double here for that reason — separate doubles would agree
    by construction and prove nothing.

    No new symbol carries this: the issue names the waiting primitive (FND-227)
    and the status reader (FND-247) as the "reaches Completed" half, and both
    already exist. What this pins is that the composition is available to a
    scenario author without a third thing in the middle."""
    fake = FakeTemporal(
        starts=[started(workflow_id="wf-1", result_run_id="run-1")],
        descriptions=[
            await workflow_description(run_id="run-1", history_length=3),
            await workflow_description(run_id="run-1", history_length=9),
            await workflow_description(
                run_id="run-1",
                status=_WIRE_COMPLETED,
                history_length=14,
            ),
        ],
    )
    handle = await start_on_task_queue(_spec(), connection=connection_over(fake))
    reader = reader_over(fake)

    async def probe() -> WorkflowStatus:
        return await reader.workflow_status(handle.workflow_id, run_id=handle.run_id)

    with fake_clock():
        outcome = await poll_until(
            probe,
            settled=lambda status: status.status is WorkflowExecutionStatus.COMPLETED,
            fingerprint=lambda status: str(status.history_length),
            budget=Budget(
                timeout=timedelta(seconds=60), poll_interval=timedelta(seconds=5)
            ),
            label=f"workflow {handle.workflow_id} reaching Completed",
        )

    assert isinstance(outcome, Settled)
    assert outcome.value.run_id == "run-1"
    assert outcome.value.task_queue == _QUEUE
    assert outcome.attempts == 3


async def test_the_status_read_is_pinned_to_the_run_that_was_started():
    """Not "the latest run of this id". The distinction is invisible on a healthy
    first run and decisive the moment anything re-dispatches or
    continues-as-new, which is exactly what the version-rollout scenarios do."""
    fake = FakeTemporal(
        starts=[started(result_run_id="run-1")],
        descriptions=[await workflow_description(run_id="run-1")],
    )
    handle = await start_on_task_queue(_spec(), connection=connection_over(fake))

    await reader_over(fake).workflow_status(handle.workflow_id, run_id=handle.run_id)

    describes = [payload for verb, payload, _kwargs in fake.calls if verb == "describe"]
    assert describes == [("wf-1", "run-1")]
