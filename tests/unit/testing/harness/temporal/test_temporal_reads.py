"""Unit tests for the ``temporalio`` Temporal reader.

Two claims get most of the attention, because they are the two the issue is
about. The first is that an unreadable frontend never answers "no pollers": that
is the finding this read exists to deliver, so a failure that returned an empty
list would not lose a diagnosis, it would invent one. The second is that "no such
workflow" and "could not read" stay distinguishable by type, since a bounded wait
absorbs one and must fail at once on the other.
"""

from __future__ import annotations

import asyncio
import itertools
import threading
from datetime import datetime, timedelta, timezone

import pytest
from temporalio.client import Client

from application_sdk.testing.harness.temporal import (
    PollerInfo,
    TaskQueueType,
    TemporalConnectFailedError,
    TemporalConnection,
    TemporalReader,
    TemporalReaderLoopMismatchError,
    TemporalReadFailedError,
    TemporalServiceReader,
    WorkflowExecutionStatus,
    WorkflowNotFoundError,
    stale_version_pollers,
)
from application_sdk.testing.harness.temporal.client import frontend_connection
from tests.unit.testing._temporal_fakes import (
    FakeTemporal,
    RPCStatusCode,
    _FakeClient,
    describe_response,
    on_a_fresh_loop,
    poller,
    reader_over,
    rpc_error,
    workflow_description,
)

_QUEUE = "atlan-hello-world-default"


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_the_reader_satisfies_the_protocol():
    assert isinstance(reader_over(FakeTemporal()), TemporalReader)


def test_the_reader_satisfies_the_protocol_statically_too():
    """``isinstance`` on a ``runtime_checkable`` Protocol only checks that the
    methods exist, so it would pass a backend whose *signatures* had drifted. The
    annotation is what pyright checks — and the drift worth catching is the
    ``task_queue_types`` keyword the backend adds beyond the Protocol, which is
    only compatible while it stays optional."""
    reader: TemporalReader = reader_over(FakeTemporal())

    assert reader is not None


def test_a_plain_object_satisfies_the_protocol_by_shape():
    """No consumer has to inherit from it — the runtime suite's own reader will
    not."""

    class Fake:
        async def task_queue_pollers(self, queue, *, namespace): ...
        async def workflow_status(self, workflow_id, *, run_id=None): ...

    assert isinstance(Fake(), TemporalReader)


# ---------------------------------------------------------------------------
# The poller read
# ---------------------------------------------------------------------------


async def test_no_pollers_is_a_value_not_a_wait():
    """The whole point: zero pollers is an answer available on the first probe,
    where ``NoWorkerOnTaskQueueError`` currently infers it from three minutes of
    silence."""
    fake = FakeTemporal(pollers=[describe_response()])

    assert await reader_over(fake).task_queue_pollers(_QUEUE, namespace="default") == []


async def test_both_queue_halves_are_read_and_tagged():
    """A task-queue name addresses two queues and a worker polls both, so a
    single read answers a narrower question than "is anything polling"."""
    fake = FakeTemporal(pollers=[describe_response(poller("1@host"))])

    pollers = await reader_over(fake).task_queue_pollers(_QUEUE, namespace="default")

    assert [p.task_queue_type for p in pollers] == [
        TaskQueueType.WORKFLOW,
        TaskQueueType.ACTIVITY,
    ]
    assert len(fake.requests()) == 2


async def test_a_half_polling_worker_is_visible_rather_than_collapsed():
    """The zombie shape: the workflow poll loop died while the process — and its
    activity poll loop — stayed alive. A union count reports one poller and hides
    it; per-half tagging is what makes it a finding."""
    fake = FakeTemporal(
        pollers=[describe_response(), describe_response(poller("1@host"))]
    )

    pollers = await reader_over(fake).task_queue_pollers(_QUEUE, namespace="default")

    assert [p.task_queue_type for p in pollers] == [TaskQueueType.ACTIVITY]


async def test_a_caller_can_narrow_to_one_half():
    fake = FakeTemporal(pollers=[describe_response(poller("1@host"))])

    pollers = await reader_over(fake).task_queue_pollers(
        _QUEUE, namespace="default", task_queue_types=[TaskQueueType.WORKFLOW]
    )

    assert [p.task_queue_type for p in pollers] == [TaskQueueType.WORKFLOW]
    assert len(fake.requests()) == 1


async def test_an_empty_task_queue_types_reads_nothing_and_makes_no_rpc():
    """``[]`` is honoured as itself, not taken for "unspecified".

    ``tuple(task_queue_types or _DEFAULT_TASK_QUEUE_TYPES)`` promoted an empty
    sequence to *both* halves — the exact opposite of what was asked, silently.
    A caller narrowing types from a config that happens to resolve empty would
    get two RPCs and a poller list it did not ask for, and nothing would say so.
    """
    fake = FakeTemporal()

    pollers = await reader_over(fake).task_queue_pollers(
        _QUEUE, namespace="default", task_queue_types=[]
    )

    assert pollers == []
    assert fake.requests() == []
    # No read means no reason to connect either.
    assert fake.connects == []


async def test_the_request_carries_the_queue_and_the_namespace_asked_for():
    """``DescribeTaskQueue`` takes the namespace in the request, so one
    connection reads across namespaces — and a wrong namespace has to be visible
    in what was sent."""
    fake = FakeTemporal()

    await reader_over(fake).task_queue_pollers(_QUEUE, namespace="other-ns")

    request = fake.requests()[0]
    assert request.namespace == "other-ns"
    assert request.task_queue.name == _QUEUE


async def test_the_deployment_build_id_is_preferred_over_the_legacy_field():
    """This SDK's worker sets ``deployment_options``; reading only the legacy
    field would report every versioned Atlan worker as unversioned."""
    fake = FakeTemporal(
        pollers=[
            describe_response(
                poller(
                    "1@host",
                    build_id="sha-new",
                    deployment_name="hello-world",
                    legacy_build_id="sha-old",
                )
            )
        ]
    )

    pollers = await reader_over(fake).task_queue_pollers(
        _QUEUE, namespace="default", task_queue_types=[TaskQueueType.WORKFLOW]
    )

    assert pollers[0].build_id == "sha-new"
    assert pollers[0].deployment_name == "hello-world"


async def test_the_legacy_build_id_is_still_read_when_it_is_the_only_one():
    fake = FakeTemporal(
        pollers=[describe_response(poller("1@host", legacy_build_id="sha-old"))]
    )

    pollers = await reader_over(fake).task_queue_pollers(
        _QUEUE, namespace="default", task_queue_types=[TaskQueueType.WORKFLOW]
    )

    assert pollers[0].build_id == "sha-old"


async def test_an_unversioned_poller_reports_no_build_id_rather_than_an_empty_one():
    """Protobuf has no null for a scalar, so an unset ``build_id`` arrives as
    ``""`` — which would compare unequal to every real build while claiming to be
    one."""
    fake = FakeTemporal(pollers=[describe_response(poller("1@host"))])

    pollers = await reader_over(fake).task_queue_pollers(
        _QUEUE, namespace="default", task_queue_types=[TaskQueueType.WORKFLOW]
    )

    assert pollers[0].build_id is None
    assert pollers[0].deployment_name is None


async def test_last_access_comes_back_timezone_aware():
    """A naive value compared against an aware ``now()`` raises at the call site,
    hours from the conversion that produced it."""
    seen = datetime(2026, 8, 26, 12, 30, tzinfo=timezone.utc)
    fake = FakeTemporal(pollers=[describe_response(poller("1@host", last_access=seen))])

    pollers = await reader_over(fake).task_queue_pollers(
        _QUEUE, namespace="default", task_queue_types=[TaskQueueType.WORKFLOW]
    )

    assert pollers[0].last_access == seen
    assert pollers[0].last_access.tzinfo is not None


async def test_an_unreadable_frontend_raises_rather_than_reporting_no_pollers():
    """The C4 fix, at the one read where a fail-open would *fabricate* the
    finding rather than merely lose it."""
    fake = FakeTemporal(pollers=[rpc_error(RPCStatusCode.PERMISSION_DENIED)])

    with pytest.raises(TemporalReadFailedError) as raised:
        await reader_over(fake).task_queue_pollers(_QUEUE, namespace="default")

    assert raised.value.rpc_status == "PERMISSION_DENIED"
    assert raised.value.address == "127.0.0.1:7233"
    assert raised.value.temporal_namespace == "default"
    assert raised.value.effective_retryable is True


async def test_the_second_half_is_not_read_after_the_first_one_failed():
    """A partial answer is worse than none: half a poller list looks exactly like
    a half-polling worker, which is a real finding this must not manufacture."""
    fake = FakeTemporal(pollers=[rpc_error(RPCStatusCode.PERMISSION_DENIED)])

    with pytest.raises(TemporalReadFailedError):
        await reader_over(fake).task_queue_pollers(_QUEUE, namespace="default")

    assert len(fake.requests()) == 1


async def test_a_wiring_bug_is_not_dressed_up_as_a_dependency_failure():
    """A ``TypeError`` reported as ``DEPENDENCY_UNAVAILABLE`` would be absorbed by
    a wait's transient budget and burn the window instead of failing at once."""
    fake = FakeTemporal(pollers=[TypeError("bad signature")])

    with pytest.raises(TypeError):
        await reader_over(fake).task_queue_pollers(_QUEUE, namespace="default")


async def test_the_per_call_timeout_is_handed_to_the_rpc():
    """One hung call must not silently consume the whole budget a poll was
    given."""
    fake = FakeTemporal()

    await reader_over(fake, request_timeout=timedelta(seconds=7)).task_queue_pollers(
        _QUEUE, namespace="default"
    )

    assert fake.kwargs_for("describe_task_queue")["timeout"] == timedelta(seconds=7)


# ---------------------------------------------------------------------------
# The workflow status read
# ---------------------------------------------------------------------------


async def test_workflow_status_reports_what_temporal_says():
    started = datetime(2026, 8, 26, 9, 0, tzinfo=timezone.utc)
    fake = FakeTemporal(
        descriptions=[await workflow_description(started_at=started, history_length=17)]
    )

    status = await reader_over(fake).workflow_status("wf-1")

    assert status.workflow_id == "wf-1"
    assert status.run_id == "run-1"
    assert status.status is WorkflowExecutionStatus.RUNNING
    assert status.task_queue == _QUEUE
    assert status.history_length == 17
    assert status.started_at == started
    assert status.closed_at is None


async def test_history_length_is_the_progress_signal():
    """Status alone never changes between "running" and "finished", so a wait on
    it could only ever report ``Expired``. A growing history is what makes
    ``Stalled`` reachable."""
    fake = FakeTemporal(
        descriptions=[
            await workflow_description(history_length=3),
            await workflow_description(history_length=9),
        ]
    )
    reader = reader_over(fake)

    first = await reader.workflow_status("wf-1")
    second = await reader.workflow_status("wf-1")

    assert (first.history_length, second.history_length) == (3, 9)


async def test_the_run_id_asked_for_reaches_the_handle():
    fake = FakeTemporal(descriptions=[await workflow_description()])

    await reader_over(fake).workflow_status("wf-1", run_id="run-9")

    assert fake.calls[0][1] == ("wf-1", "run-9")


async def test_a_missing_execution_raises_not_found_and_names_the_namespace():
    """``NOT_FOUND`` and not ``DEPENDENCY_UNAVAILABLE``: a wrong id or a wrong
    namespace does not recover while a wait sleeps, so the wait must fail on it
    at once rather than spend its budget."""
    fake = FakeTemporal(descriptions=[rpc_error(RPCStatusCode.NOT_FOUND)])

    with pytest.raises(WorkflowNotFoundError) as raised:
        await reader_over(fake).workflow_status("wf-typo")

    assert raised.value.workflow_id == "wf-typo"
    assert raised.value.temporal_namespace == "default"
    assert raised.value.effective_retryable is False


async def test_find_workflow_status_answers_none_for_a_missing_execution():
    """The nullable twin. A wait for AE-dispatched work starts before the
    execution exists, so "not there yet" has to be a value its predicate can
    test."""
    fake = FakeTemporal(descriptions=[rpc_error(RPCStatusCode.NOT_FOUND)])

    assert await reader_over(fake).find_workflow_status("wf-1") is None


@pytest.mark.parametrize(
    "status",
    [
        RPCStatusCode.PERMISSION_DENIED,
        RPCStatusCode.UNAUTHENTICATED,
        RPCStatusCode.DEADLINE_EXCEEDED,
        RPCStatusCode.INTERNAL,
        RPCStatusCode.RESOURCE_EXHAUSTED,
    ],
)
async def test_only_a_clean_not_found_reads_as_absent(status: RPCStatusCode):
    """The narrowing, from the other side: a false "it never started" cached from
    a 403 or a timeout is the negative no later read corrects."""
    fake = FakeTemporal(descriptions=[rpc_error(status)])

    with pytest.raises(TemporalReadFailedError):
        await reader_over(fake).find_workflow_status("wf-1")


async def test_an_unspecified_status_reads_as_unknown_rather_than_crashing():
    """``temporalio`` surfaces the proto's ``UNSPECIFIED`` as ``None``; a new
    upstream status name would arrive the same way. Neither is worth a harness
    crash — the same call an unrecognised pod phase makes."""
    from temporalio.api.enums.v1 import WorkflowExecutionStatus as WireStatus

    fake = FakeTemporal(
        descriptions=[
            await workflow_description(
                status=WireStatus.WORKFLOW_EXECUTION_STATUS_UNSPECIFIED
            )
        ]
    )

    status = await reader_over(fake).workflow_status("wf-1")

    assert status.status is WorkflowExecutionStatus.UNKNOWN


async def test_a_terminal_status_carries_its_close_time():
    from temporalio.api.enums.v1 import WorkflowExecutionStatus as WireStatus

    closed = datetime(2026, 8, 26, 10, 0, tzinfo=timezone.utc)
    fake = FakeTemporal(
        descriptions=[
            await workflow_description(
                status=WireStatus.WORKFLOW_EXECUTION_STATUS_COMPLETED, closed_at=closed
            )
        ]
    )

    status = await reader_over(fake).workflow_status("wf-1")

    assert status.status is WorkflowExecutionStatus.COMPLETED
    assert status.closed_at == closed


# ---------------------------------------------------------------------------
# The connection: pooled, per loop, rebuilt once
# ---------------------------------------------------------------------------


async def test_one_connection_serves_repeated_probes():
    """The shape ``wait_for_workflow`` did *not* have before FND-241: a fresh
    connection per probe is a handshake per probe.

    Named for what it does — sequential probes — rather than "the whole wait".
    There is no :func:`poll_until` here; the loop stands in for one, and a name
    claiming a bounded wait would be describing a test that does not exercise it.
    """
    fake = FakeTemporal()
    reader = reader_over(fake)

    for _ in range(3):
        await reader.task_queue_pollers(_QUEUE, namespace="default")

    assert len(fake.connects) == 1


async def test_a_dropped_transport_is_rebuilt_once_and_the_read_re_attempted():
    """A ``kubectl port-forward`` tunnel that has died is permanent — the
    client's own reconnect loop retries an address nothing is listening on."""
    fake = FakeTemporal(
        pollers=[
            rpc_error(RPCStatusCode.UNAVAILABLE),
            describe_response(poller("1@host")),
        ]
    )

    pollers = await reader_over(fake).task_queue_pollers(
        _QUEUE, namespace="default", task_queue_types=[TaskQueueType.WORKFLOW]
    )

    assert [p.identity for p in pollers] == ["1@host"]
    assert len(fake.connects) == 2
    assert fake.closes == 1


async def test_two_consecutive_transport_failures_are_a_real_outage():
    """One rebuild, not a retry loop: the budget belongs to the enclosing wait."""
    fake = FakeTemporal(pollers=[rpc_error(RPCStatusCode.UNAVAILABLE)])

    with pytest.raises(TemporalReadFailedError) as raised:
        await reader_over(fake).task_queue_pollers(_QUEUE, namespace="default")

    assert raised.value.rpc_status == "UNAVAILABLE"
    assert len(fake.connects) == 2


async def test_a_non_transport_failure_is_not_retried():
    """A 403 does not get better with a new connection, and re-attempting it
    doubles the time to a failure that was already certain."""
    fake = FakeTemporal(pollers=[rpc_error(RPCStatusCode.PERMISSION_DENIED)])

    with pytest.raises(TemporalReadFailedError):
        await reader_over(fake).task_queue_pollers(_QUEUE, namespace="default")

    assert len(fake.connects) == 1


async def test_concurrent_first_reads_open_exactly_one_connection():
    """Two probes racing the first read would otherwise leak the loser's
    connection — and with it a ``kubectl`` child process.

    This is only a concurrency test because ``FakeTemporal.connect`` yields
    inside the connect. It did not, and this test passed with the reader's lock
    removed — asserting that a sequential program opens one connection. Verified
    by removal now: without the lock it reports 5.
    """
    fake = FakeTemporal()
    reader = reader_over(fake)

    await asyncio.gather(
        *(reader.task_queue_pollers(_QUEUE, namespace="default") for _ in range(5))
    )

    assert len(fake.connects) == 1


async def test_aclose_releases_the_transport_and_is_idempotent():
    """Not optional for a tunnelled connection: it is a child process."""
    fake = FakeTemporal()
    reader = reader_over(fake)
    await reader.task_queue_pollers(_QUEUE, namespace="default")

    await reader.aclose()
    await reader.aclose()

    assert fake.closes == 1


async def test_aclose_on_a_reader_that_never_connected_is_a_no_op():
    fake = FakeTemporal()

    await reader_over(fake).aclose()

    assert fake.connects == []


def test_a_connected_reader_used_on_a_second_loop_raises_rather_than_leaking():
    """A ``temporalio`` client is bound to the loop that created it, so a reader
    cannot follow one across loops.

    It raises rather than rebinding, because rebinding drops the previous
    connection *without* awaiting its close — and that close is what reaps the
    ``kubectl`` child behind a port-forwarded connection. A leaked process is
    worse than a loud refusal, and the new loop cannot do the closing either.
    """
    fake = FakeTemporal()
    reader = reader_over(fake)

    on_a_fresh_loop(lambda: reader.task_queue_pollers(_QUEUE, namespace="default"))

    with pytest.raises(TemporalReaderLoopMismatchError) as raised:
        on_a_fresh_loop(lambda: reader.task_queue_pollers(_QUEUE, namespace="default"))

    assert raised.value.address == "127.0.0.1:7233"
    assert raised.value.effective_retryable is False
    # Not a silent rebuild: exactly one connection was ever made, and it is still
    # the reader's — still closeable from the loop that owns it.
    assert len(fake.connects) == 1
    assert fake.closes == 0


def test_an_unconnected_reader_rebinds_to_a_new_loop_without_complaint():
    """Nothing to lose, so nothing to report.

    Raising here would punish a legitimate handoff — a reader built by a fixture
    on one loop and first used on another leaks nothing, because it never opened
    anything.
    """
    fake = FakeTemporal()
    reader = reader_over(fake)

    on_a_fresh_loop(lambda: reader.task_queue_pollers(_QUEUE, namespace="default"))

    assert len(fake.connects) == 1


def test_a_closed_reader_may_be_reused_on_another_loop():
    """`aclose()` on the owning loop is the documented way to hand a reader on."""
    fake = FakeTemporal()
    reader = reader_over(fake)

    async def _use_then_close() -> None:
        await reader.task_queue_pollers(_QUEUE, namespace="default")
        await reader.aclose()

    on_a_fresh_loop(_use_then_close)
    on_a_fresh_loop(lambda: reader.task_queue_pollers(_QUEUE, namespace="default"))

    assert len(fake.connects) == 2
    assert fake.closes == 1


# ---------------------------------------------------------------------------
# Connecting
# ---------------------------------------------------------------------------


@pytest.fixture
def refusing_client(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    """Patch the real ``Client.connect`` to refuse, recording what it was given.

    Patched rather than pointed at an unroutable address: a unit test that
    resolves a hostname is a unit test that can hang on someone else's DNS. The
    patch is on ``temporalio.client.Client`` itself, which is the object this
    backend imported, so the call being wrapped is still the real one.
    """
    calls: list[dict[str, object]] = []

    async def _refuse(address: str, **kwargs: object) -> object:
        calls.append({"address": address, **kwargs})
        raise RuntimeError("failed to connect to all addresses")

    monkeypatch.setattr(Client, "connect", _refuse)
    return calls


async def test_a_failed_connect_names_the_address_and_the_namespace(
    refusing_client: list[dict[str, object]],
):
    """A run pointed at the wrong frontend has to say so, rather than leaving it
    to be inferred from a gRPC message."""
    with pytest.raises(TemporalConnectFailedError) as raised:
        await frontend_connection(address="frontend:7233", namespace="default")

    assert raised.value.address == "frontend:7233"
    assert raised.value.temporal_namespace == "default"
    assert raised.value.effective_retryable is True
    assert refusing_client[0]["address"] == "frontend:7233"


async def test_a_failed_connect_carries_no_credential(
    refusing_client: list[dict[str, object]],
):
    """The address and the namespace are what a report needs; a token in an error
    field travels wherever that report does."""
    with pytest.raises(TemporalConnectFailedError) as raised:
        await frontend_connection(
            address="frontend:7233", namespace="default", api_key="s3cret-token"
        )

    # The token did reach the client — it is only absent from the failure.
    assert refusing_client[0]["api_key"] == "s3cret-token"
    assert "s3cret" not in str(raised.value)
    assert not any("s3cret" in str(value) for value in vars(raised.value).values())


async def test_a_successful_connect_owns_no_transport_to_release(
    monkeypatch: pytest.MonkeyPatch,
):
    """The caller supplied the address, so the caller owns whatever provides it —
    and closing the connection must therefore be safe and do nothing."""

    async def _accept(address: str, **kwargs: object) -> object:
        return object()

    monkeypatch.setattr(Client, "connect", _accept)

    connection = await frontend_connection(address="frontend:7233", namespace="ns")

    assert (connection.address, connection.namespace) == ("frontend:7233", "ns")
    assert await connection.close() is None


async def test_a_tunnel_that_never_opens_fails_as_a_connect_and_leaves_nothing_behind(
    monkeypatch: pytest.MonkeyPatch,
):
    """An unclosed tunnel leaks a ``kubectl`` process for the rest of the
    session, so the failure path has to release it."""
    from application_sdk.testing.harness.cluster import ServiceTarget
    from application_sdk.testing.harness.temporal.client import (
        port_forwarded_connection,
    )

    closed: list[bool] = []

    async def _never_opens(self) -> str:
        raise TimeoutError("port 40000 did not become ready")

    async def _record_close(self) -> None:
        closed.append(True)

    monkeypatch.setattr(
        "application_sdk.testing.harness.cluster._portforward.PortForward.address",
        _never_opens,
    )
    monkeypatch.setattr(
        "application_sdk.testing.harness.cluster._portforward.PortForward.aclose",
        _record_close,
    )

    with pytest.raises(TemporalConnectFailedError) as raised:
        await port_forwarded_connection(
            target=ServiceTarget(
                namespace="temporal", service="temporal-frontend", port=7233
            ),
            namespace="default",
        )

    assert "temporal/temporal-frontend:7233" in str(raised.value)
    assert closed == [True]


async def test_a_tunnel_that_opens_but_cannot_connect_is_still_released(
    monkeypatch: pytest.MonkeyPatch,
):
    """The other half of the same leak: the tunnel is up by the time the client
    fails."""
    from application_sdk.testing.harness.cluster import ServiceTarget
    from application_sdk.testing.harness.temporal.client import (
        port_forwarded_connection,
    )

    closed: list[bool] = []

    async def _opens(self) -> str:
        return "127.0.0.1:40001"

    async def _record_close(self) -> None:
        closed.append(True)

    monkeypatch.setattr(
        "application_sdk.testing.harness.cluster._portforward.PortForward.address",
        _opens,
    )
    monkeypatch.setattr(
        "application_sdk.testing.harness.cluster._portforward.PortForward.aclose",
        _record_close,
    )

    with pytest.raises(TemporalConnectFailedError):
        await port_forwarded_connection(
            target=ServiceTarget(
                namespace="temporal", service="temporal-frontend", port=7233
            ),
            namespace="default",
        )

    assert closed == [True]


async def test_a_tunnelled_connection_closes_the_tunnel_it_owns(
    monkeypatch: pytest.MonkeyPatch,
):
    from application_sdk.testing.harness.cluster import ServiceTarget
    from application_sdk.testing.harness.temporal import client as backend

    closed: list[bool] = []

    async def _opens(self) -> str:
        return "127.0.0.1:40002"

    async def _record_close(self) -> None:
        closed.append(True)

    async def _connect(*, address: str, namespace: str) -> object:
        return backend.TemporalConnection(
            client=object(),  # type: ignore[arg-type]
            namespace=namespace,
            address=address,
        )

    monkeypatch.setattr(
        "application_sdk.testing.harness.cluster._portforward.PortForward.address",
        _opens,
    )
    monkeypatch.setattr(
        "application_sdk.testing.harness.cluster._portforward.PortForward.aclose",
        _record_close,
    )
    monkeypatch.setattr(backend, "frontend_connection", _connect)

    connection = await backend.port_forwarded_connection(
        target=ServiceTarget(
            namespace="temporal", service="temporal-frontend", port=7233
        ),
        namespace="default",
    )
    assert connection.address == "127.0.0.1:40002"

    await connection.close()

    assert closed == [True]


def test_constructing_a_reader_does_not_connect():
    """Nothing is opened until something is read, so a fixture can build one
    against a frontend the test then decides not to reach."""
    TemporalServiceReader(connect=_unused)  # should not raise


def _unused():  # pragma: no cover — the constructor never calls its factory
    raise AssertionError("the factory is not called until the first read")


# ---------------------------------------------------------------------------
# stale_version_pollers
# ---------------------------------------------------------------------------


def _p(build_id: str | None) -> PollerInfo:
    return PollerInfo(
        identity="1@host",
        last_access=datetime(2026, 8, 26, tzinfo=timezone.utc),
        build_id=build_id,
    )


def test_an_unversioned_poller_counts_as_stale():
    """The ``hasUnversionedPoller`` trap: ``p.build_id and p.build_id != current``
    skips exactly the workers whose deployment config did not take, so the gate
    passes because it could not see the offender."""
    assert stale_version_pollers([_p(None)], current_build_id="sha-new") == (_p(None),)


def test_an_older_build_still_holding_the_queue_counts_as_stale():
    stale = stale_version_pollers(
        [_p("sha-old"), _p("sha-new")], current_build_id="sha-new"
    )

    assert [p.build_id for p in stale] == ["sha-old"]


def test_the_passing_state_is_empty():
    assert stale_version_pollers([_p("sha-new")], current_build_id="sha-new") == ()


def test_no_current_build_means_versioning_is_not_in_use_here():
    """Reporting every poller as stale would red every unversioned
    deployment."""
    assert stale_version_pollers([_p(None), _p("x")], current_build_id=None) == ()


def test_a_status_name_this_sdk_has_never_heard_of_reads_as_unknown():
    """Exercised against the classifier directly, because there is no wire value
    for it: a status Temporal has not shipped yet cannot be put in a protobuf
    enum. The point is that a *future* one is a classification rather than a
    harness crash, which is only checkable this way."""
    from enum import Enum

    from application_sdk.testing.harness.temporal.client import _execution_status

    class Later(Enum):
        SOME_NEW_STATUS = 99

    assert _execution_status(Later.SOME_NEW_STATUS) is WorkflowExecutionStatus.UNKNOWN


async def test_the_kube_context_reaches_the_tunnel(
    monkeypatch: pytest.MonkeyPatch,
):
    """FND-241 pinned the context onto ``kubectl port-forward`` because a reader
    built for one cluster was tunnelling into another, both calls succeeding and
    nothing logged. This factory builds its *own* ``PortForward``, so it has to
    thread the context too or it reintroduces exactly that split — Temporal read
    through whichever context is current while the pods came from a named one.

    Asserted on what the tunnel was constructed with, not on a round trip: a
    tunnel that reaches the right cluster because it also happens to be the
    current context passes either way, which is the state the bug hides in.
    """
    from application_sdk.testing.harness.cluster import ServiceTarget
    from application_sdk.testing.harness.cluster._portforward import PortForward
    from application_sdk.testing.harness.temporal import client as backend

    built: list[str | None] = []

    async def _spawn(self: PortForward) -> int:
        built.append(self.kube_context)
        self._local_port = 40003
        return 40003

    async def _connect(*, address: str, namespace: str) -> object:
        return backend.TemporalConnection(
            client=object(),  # type: ignore[arg-type]
            namespace=namespace,
            address=address,
        )

    monkeypatch.setattr(PortForward, "_spawn", _spawn)
    monkeypatch.setattr(backend, "frontend_connection", _connect)

    connection = await backend.port_forwarded_connection(
        target=ServiceTarget(
            namespace="temporal", service="temporal-frontend", port=7233
        ),
        namespace="default",
        kube_context="e2e-gcp",
    )

    assert built == ["e2e-gcp"]
    # And the context is named in the failure surface, so a run against the
    # wrong cluster says so rather than leaving it to be inferred.
    assert connection.address == "127.0.0.1:40003"


async def test_no_kube_context_leaves_the_argv_unpinned(
    monkeypatch: pytest.MonkeyPatch,
):
    """``None`` must stay "whatever is current" rather than becoming a literal —
    a `--context None` would fail every tunnel for a caller that never named
    one."""
    from application_sdk.testing.harness.cluster._portforward import kubectl_argv

    assert "--context" not in kubectl_argv("port-forward", kube_context=None)


def test_a_second_loop_is_refused_while_the_first_is_still_connecting():
    """The in-flight window, which the `connection is not None` check missed.

    For the whole duration of `await connect()` the connection is still `None`,
    so a fail-close that tested only that field let a second loop replace
    `_bound` mid-connect — after which the first loop stored its client on an
    orphaned bound, leaking a `temporalio` client and, on the tunnelled path, a
    `kubectl` child. Sequential reuse was already refused; this is the concurrent
    first-connect hole.

    Two real threads and two real loops, because that is the only way the race
    exists: separate loops run on separate threads, so this is a genuine data
    race that no asyncio primitive would serialise. Gated on `Event`s rather
    than sleeps so it cannot pass by timing luck.
    """
    connect_entered = threading.Event()
    release_connect = threading.Event()
    fake = FakeTemporal()
    inner = fake.connect()
    attempts = itertools.count()

    async def _slow_connect():
        # Only the FIRST connect blocks. If the refusal is missing, the second
        # loop therefore *succeeds* — so the failure is the leak itself (two
        # connections, one stranded on an orphaned bound) rather than a hang,
        # which would be both the wrong symptom and timing-dependent.
        if next(attempts) == 0:
            connect_entered.set()
            # Threaded, not awaited, so the other loop runs while this one sits
            # inside `await self._connect()`.
            await asyncio.get_running_loop().run_in_executor(
                None, release_connect.wait, 10
            )
        return await inner()

    reader = TemporalServiceReader(connect=_slow_connect)
    outcomes: dict[str, object] = {}

    def _first() -> None:
        try:
            outcomes["first"] = on_a_fresh_loop(
                lambda: reader.task_queue_pollers(_QUEUE, namespace="default")
            )
        except BaseException as exc:  # pragma: no cover - reported via outcomes
            outcomes["first"] = exc

    def _second() -> None:
        try:
            outcomes["second"] = on_a_fresh_loop(
                lambda: reader.task_queue_pollers(_QUEUE, namespace="default")
            )
        except BaseException as exc:
            outcomes["second"] = exc

    first = threading.Thread(target=_first, name="loop-a")
    first.start()
    assert connect_entered.wait(10), "the first loop never entered connect()"

    # The first loop is now inside `await connect()` with connection still None.
    second = threading.Thread(target=_second, name="loop-b")
    second.start()
    second.join(10)
    assert not second.is_alive(), "the second loop never returned"

    release_connect.set()
    first.join(10)
    assert not first.is_alive(), "the first loop never finished"

    # The leak assertion leads, because it is the defect: exactly one connection
    # was ever opened, so nothing is stranded on a bound that lost its reference.
    assert len(fake.connects) == 1, (
        f"{len(fake.connects)} connections opened — the first loop's is stranded "
        "on an orphaned bound and nothing will ever close it"
    )
    assert isinstance(outcomes["second"], TemporalReaderLoopMismatchError)
    # The refusal names the state it found rather than a stale address.
    assert "in the middle of connecting" in str(outcomes["second"])
    assert outcomes["first"] == []
    assert fake.closes == 0


async def test_aclose_waits_for_an_in_flight_connect_rather_than_returning_past_it():
    """Two tasks on one loop: a first read racing a fixture teardown.

    `aclose` used to test `connection is None` and return — but the connection is
    `None` for the whole duration of `await connect()`, so teardown reported
    "closed" and the read then installed a client behind it with nothing left to
    close it. Sequential read-then-close was always fine; this is the hole.

    Asserted on both halves, because either alone is passable by accident: that
    `aclose` does not complete while the connect is in flight (the ordering), and
    that the connection is closed exactly once when it does (the leak).
    """
    started = asyncio.Event()
    release = asyncio.Event()
    fake = FakeTemporal()
    inner = fake.connect()

    async def _slow_connect():
        started.set()
        await release.wait()
        return await inner()

    reader = TemporalServiceReader(connect=_slow_connect)

    read = asyncio.create_task(reader.task_queue_pollers(_QUEUE, namespace="default"))
    await started.wait()

    closing = asyncio.create_task(reader.aclose())
    # Generous yielding: if `aclose` returns past the in-flight connect it needs
    # only one scheduler turn to finish, so this cannot pass by not looking.
    for _ in range(10):
        await asyncio.sleep(0)
    assert not closing.done(), (
        "aclose() returned while a connect was still in flight — the client it "
        "installs afterwards will never be closed"
    )

    release.set()
    assert await read == []
    await closing

    assert len(fake.connects) == 1
    assert fake.closes == 1, (
        f"connection closed {fake.closes} times; the caller of aclose() believes "
        "teardown completed"
    )


async def test_aclose_is_still_cheap_and_idempotent_when_nothing_is_in_flight():
    """The waiting must not cost the ordinary path anything.

    An uncontended `asyncio.Lock` acquires without yielding, so sequential
    teardown is unchanged — and a second `aclose` still closes nothing twice.
    """
    fake = FakeTemporal()
    reader = reader_over(fake)
    await reader.task_queue_pollers(_QUEUE, namespace="default")

    await reader.aclose()
    await reader.aclose()

    assert fake.closes == 1


async def test_a_failed_connect_leaves_the_reader_usable_on_its_own_loop():
    """A connect that raised must not wedge the reader it was opening on.

    Renamed and re-scoped twice. It was `..._finds_nothing_to_close` and asserted
    only that `aclose` returned — it survived a mutation to `aclose` untouched,
    because absence-of-exception was the whole check.

    My first rewrite claimed the reconnect proved the `connecting` claim had been
    released. It does not: :meth:`_loop_bound` returns early on a loop match and
    never consults the claim, so a same-loop reconnect succeeds whether or not
    the claim was cleared. Mutating the releasing `finally` away leaves this test
    green. The claim only gates *other* loops — which is what
    `test_a_closed_reader_may_be_reused_on_another_loop` covers, and that one does
    go red on the mutation.

    So what this pins is the narrower true thing: a failed connect, followed by
    teardown, leaves the reader working on its own loop. Worth having, and worth
    not dressing up as more.
    """
    attempts = itertools.count()
    connects: list[int] = []

    async def _connect_failing_once() -> TemporalConnection:
        n = next(attempts)
        connects.append(n)
        if n == 0:
            raise RuntimeError("frontend unreachable")
        return TemporalConnection(
            client=_FakeClient(FakeTemporal()),  # type: ignore[arg-type]
            namespace="default",
            address="127.0.0.1:7233",
        )

    reader = TemporalServiceReader(connect=_connect_failing_once)

    with pytest.raises(RuntimeError, match="frontend unreachable"):
        await reader.task_queue_pollers(_QUEUE, namespace="default")
    assert connects == [0]

    # Teardown over a released claim: no work, no hang.
    await reader.aclose()

    await reader.task_queue_pollers(
        _QUEUE, namespace="default", task_queue_types=[TaskQueueType.WORKFLOW]
    )
    assert connects == [0, 1], "a failed connect wedged the reader on its own loop"


async def test_aclose_is_not_terminal_and_a_later_read_reconnects():
    """The sequential reuse contract: closing does not retire the reader.

    Load-bearing rather than incidental — `_read`'s transport-lost rebuild is
    built on exactly this (`aclose` then reconnect), so making `aclose` terminal
    would break the retry path. Anyone tempted to close the resurrection residue
    that way has to break this test first, which is the point of pinning it
    separately from the residue itself.
    """
    fake = FakeTemporal(pollers=[describe_response(poller("1@host"))])
    reader = reader_over(fake)

    await reader.task_queue_pollers(
        _QUEUE, namespace="default", task_queue_types=[TaskQueueType.WORKFLOW]
    )
    await reader.aclose()
    assert fake.closes == 1
    connects_at_teardown = len(fake.connects)

    await reader.task_queue_pollers(
        _QUEUE, namespace="default", task_queue_types=[TaskQueueType.WORKFLOW]
    )

    assert len(fake.connects) == connects_at_teardown + 1


async def test_a_read_in_flight_at_teardown_resurrects_a_connection_afterwards():
    """The residue `aclose` does NOT close, with the read genuinely overlapping.

    `_connection` hands the connection out as a local and releases `bound.lock`
    before the RPC, so a read past that point is unprotected: `aclose` acquires
    an uncontended lock, closes, and **returns** while the read is still running.
    The read then fails against the closed client, and `_read`'s rebuild opens a
    *fresh* connection after teardown reported success. On the tunnelled path
    that is a `kubectl` child outliving the fixture that closed it.

    Gated so the overlap is real rather than asserted: the first RPC parks on an
    event until teardown has completed. A version that awaited a *finished* read
    before closing would prove only that `aclose` is not terminal — which is a
    different property, pinned separately above.

    A record of current behaviour, not an endorsement. Closing it needs a
    terminal `aclose`, which would break the sibling test and `_read`'s rebuild
    with it — a contract change, not a fix.
    """
    in_flight = asyncio.Event()
    release = asyncio.Event()
    attempts = itertools.count()
    connects: list[str] = []
    closes: list[str] = []

    class _Service:
        async def describe_task_queue(self, req: object, **kwargs: object) -> object:
            if next(attempts) == 0:
                in_flight.set()
                await release.wait()
                # The closed client's RPC surfaces as a lost transport, which is
                # what sends `_read` down its rebuild path.
                raise rpc_error(RPCStatusCode.UNAVAILABLE)
            return describe_response(poller("1@host"))

    class _Client:
        def __init__(self) -> None:
            self.workflow_service = _Service()

    async def _connect() -> TemporalConnection:
        address = f"127.0.0.1:7233#{len(connects)}"
        connects.append(address)

        async def _close() -> None:
            closes.append(address)

        return TemporalConnection(
            client=_Client(),  # type: ignore[arg-type]
            namespace="default",
            address=address,
            close=_close,
        )

    reader = TemporalServiceReader(connect=_connect)
    read = asyncio.create_task(
        reader.task_queue_pollers(
            _QUEUE, namespace="default", task_queue_types=[TaskQueueType.WORKFLOW]
        )
    )
    await in_flight.wait()

    # Teardown with the read genuinely past `_connection()` and mid-RPC.
    await reader.aclose()
    assert closes == [connects[0]], "teardown did not close the live connection"

    release.set()
    pollers = await read

    # The read completed by opening a second connection — after aclose returned.
    assert [p.identity for p in pollers] == ["1@host"]
    assert len(connects) == 2, (
        "the in-flight read did not reconnect; if aclose is now terminal, update "
        "its docstring — it currently documents this residue as open"
    )
    assert closes == [connects[0]], (
        f"connection {connects[1]} was opened after teardown and never closed — "
        "this is the documented residue, not a new leak"
    )


async def test_async_with_closes_the_reader_on_block_exit():
    """The structural half of the leak guard.

    `PortForward` has never produced the never-closed leak across seven review
    rounds because its acquire and release are one construct; `_connection` and
    `aclose` are separate methods and produced it twice. This makes the caller's
    side one construct too, so forgetting to close stops being possible rather
    than being warned about.
    """
    fake = FakeTemporal()

    async with TemporalServiceReader(connect=fake.connect()) as reader:
        await reader.task_queue_pollers(_QUEUE, namespace="default")
        assert fake.closes == 0

    assert fake.closes == 1


async def test_async_with_closes_on_an_exception_and_re_raises_it():
    """Teardown must not swallow the failure that triggered it.

    `__aexit__` returns `None`, not a truthy value — an exception inside the
    block propagates while the connection is still released.
    """
    fake = FakeTemporal()

    with pytest.raises(RuntimeError, match="scenario failed"):
        async with TemporalServiceReader(connect=fake.connect()) as reader:
            await reader.task_queue_pollers(_QUEUE, namespace="default")
            raise RuntimeError("scenario failed")

    assert fake.closes == 1


async def test_entering_the_block_opens_nothing():
    """`__aenter__` is not an eager connect.

    A fixture may build a reader for a scenario that then decides not to reach
    Temporal, and the laziness is pinned elsewhere as its own property — an
    eager connect here would turn `async with` into a network call for a block
    that never reads.
    """
    fake = FakeTemporal()

    async with TemporalServiceReader(connect=fake.connect()):
        assert fake.connects == []

    assert fake.connects == []
    assert fake.closes == 0
