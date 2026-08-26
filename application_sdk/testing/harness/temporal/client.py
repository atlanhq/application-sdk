"""The ``temporalio`` backend behind :class:`TemporalReader`, and how it connects.

Neither read existed in this SDK before FND-247. The poller read is a lift from
``suite/ports/temporal.py`` in ``atlanhq/app-runtime-test-suite``, whose
``pollers()`` already worked against ``DescribeTaskQueue``; the re-expression on
the way across is what the rest of this docstring is about.

**Async, with no bridge inside it.** The lifted implementation runs its own event
loop on a dedicated thread and hands every call to it with
``run_coroutine_threadsafe``, because its scenario authors never write ``async``.
Under this SDK's async-everywhere rule (D1) that inverts: the reader is plain
``async`` and the sync composer reaches it through the one public bridge,
:func:`~application_sdk.testing.harness.bridge.run_sync` — which owns exactly one
loop per thread, so a suite that also reads Atlas or a cluster shares it instead
of standing up a second one. Unlike
:class:`~application_sdk.testing.harness.cluster.KubernetesReader` there is no
:func:`~application_sdk._runtime.offload.run_in_thread` here at all:
``temporalio`` is asyncio-native, so ``async`` is free rather than paid for.

**No extra to declare, and no lazy import either.** FND-247's amendment expected
``temporalio`` to be optional behind ``[workflows]``, with these readers
import-guarded accordingly. Both halves turn out not to apply. ``temporalio`` was
promoted to a *core* dependency in v3.1 and ``[workflows]`` is a
backwards-compatibility alias resolving to it, so there is no extra to name —
unlike the ``harness`` extra the cluster backend genuinely needs. And guarding it
lazily the way that backend guards ``kubernetes`` would buy nothing measurable:
``application_sdk.testing``'s own package ``__init__`` already imports
``temporalio.client``, ``temporalio.service`` and the ``temporalio.api`` protos
before any harness module is reached. So these imports are plain and at the top,
which is what lets the conversions below take the client's real types instead of
``Any``.

**A poller count is two reads, not one.** A task-queue name addresses a workflow
queue and an activity queue, they are separate ``DescribeTaskQueue`` calls, and a
worker polls both. :meth:`TemporalServiceReader.task_queue_pollers` reads both by
default and tags every poller with which one it came from
(:class:`~application_sdk.testing.harness.temporal.TaskQueueType`). The lifted
implementation read only the workflow queue and returned a bare count; both
choices lose the state worth catching, because a union count and a workflow-only
count disagree exactly when a worker process is half-polling — the poll loop that
died while the process stayed alive, which is invisible to both Temporal's own
"is there a worker" question and to Kubernetes' "is the pod ready" one.

**One connection per loop, rebuilt once on a dropped transport.** A
``temporalio`` ``Client`` is bound to the loop that created it, so the connection
is cached against the running loop — the same affinity
:class:`~application_sdk.testing.harness.cluster.KubernetesReader` honours per
*thread* for its own non-thread-safe client, for the same reason. And when the
connection is a ``kubectl port-forward`` tunnel, a tunnel that has died is
permanent: the client's own reconnect loop retries an address nothing is
listening on any more. So an ``UNAVAILABLE`` discards the cached connection and
the read is re-attempted once against a fresh one — the trade
:class:`~application_sdk.testing.harness.cluster.PortForward` already makes for
HTTP calls, and child F made for the AE pool: pooled on the happy path, fresh on
the retry.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import timedelta, timezone
from types import TracebackType
from typing import TypeVar

# P006 asks that ``temporalio`` stay behind ``execution/_temporal/`` so a future
# orchestration change lands in one place. That contract is about the *worker*
# adapter: the engine's workflow and activity API, on a production code path.
# These imports are neither. They are an out-of-cluster read of Temporal's
# control plane from test infrastructure — ``DescribeTaskQueue`` and
# ``DescribeWorkflowExecution`` — which the adapter does not offer and should not
# grow: putting a describe-a-task-queue client on a production path to satisfy a
# lint rule is a worse coupling than the one the rule prevents. Each import
# therefore carries the directive the rule prescribes for a deliberate exception.
#
# Two things about the layout are load-bearing rather than preference, and both
# were arrived at by watching the tidier version fail.
#
# The directives sit ABOVE each import, not trailing it. Trailing is the obvious
# way to write six near-identical comments, and it does not survive: appending
# one takes these lines past 88 characters, ``ruff-format`` then wraps the
# statement, and the directive lands on the wrapped *name* line instead of the
# statement's own — which the checker matches on. Two of the six came back as
# findings that way.
#
# ``isort`` is off across the block for the mirror-image reason: a directive
# binds to the line below it, and a re-sort that lifts an import out from under
# its own comment silently un-suppresses one line while stacking two directives
# over another. Sorted by hand here, in the order isort would have chosen anyway
# — it is only the comment handling being overridden, not isort's opinion.
# isort: off
# conformance: ignore[P006] harness control-plane read — see the note above
from temporalio.api.enums.v1 import TaskQueueType as WireTaskQueueType

# conformance: ignore[P006] harness control-plane read — see the note above
from temporalio.api.taskqueue.v1 import PollerInfo as WirePollerInfo

# conformance: ignore[P006] harness control-plane read — see the note above
from temporalio.api.taskqueue.v1 import TaskQueue

# conformance: ignore[P006] harness control-plane read — see the note above
from temporalio.api.workflowservice.v1 import DescribeTaskQueueRequest

# conformance: ignore[P006] harness control-plane read — see the note above
from temporalio.client import Client, WorkflowExecutionDescription

# conformance: ignore[P006] harness control-plane read — see the note above
from temporalio.service import RPCError
# isort: on

from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.testing.harness.cluster._portforward import PortForward
from application_sdk.testing.harness.cluster._states import ServiceTarget
from application_sdk.testing.harness.temporal._errors import (
    TemporalConnectFailedError,
    TemporalReaderLoopMismatchError,
    TemporalReadFailedError,
    WorkflowNotFoundError,
)
from application_sdk.testing.harness.temporal._states import (
    PollerInfo,
    TaskQueueType,
    WorkflowExecutionStatus,
    WorkflowStatus,
)

logger = get_logger(__name__)

T = TypeVar("T")

__all__ = [
    "TemporalConnection",
    "TemporalServiceReader",
    "frontend_connection",
    "port_forwarded_connection",
]

#: Default per-call bound on a Temporal read. Distinct from any enclosing wait's
#: budget, for the reason the cluster backend's is: one hung RPC must not
#: silently consume the whole window a poll was given.
_DEFAULT_REQUEST_TIMEOUT = timedelta(seconds=30)

#: Which halves of a task queue a poller read covers when the caller does not
#: say. Both of the two an SDK worker polls, and not ``NEXUS``: no app in this
#: fleet registers a Nexus handler, so reading it would be a third RPC per probe
#: that always answers empty — and an empty answer nobody asked for is one more
#: way for a report to read as a finding.
_DEFAULT_TASK_QUEUE_TYPES: tuple[TaskQueueType, ...] = (
    TaskQueueType.WORKFLOW,
    TaskQueueType.ACTIVITY,
)

#: The harness's queue-half vocabulary, as the wire's. Declared as a mapping
#: rather than derived from the member names so a rename on either side is a
#: type error here instead of a silent read of the wrong queue.
_WIRE_QUEUE_TYPES: dict[TaskQueueType, WireTaskQueueType.ValueType] = {
    TaskQueueType.WORKFLOW: WireTaskQueueType.TASK_QUEUE_TYPE_WORKFLOW,
    TaskQueueType.ACTIVITY: WireTaskQueueType.TASK_QUEUE_TYPE_ACTIVITY,
    TaskQueueType.NEXUS: WireTaskQueueType.TASK_QUEUE_TYPE_NEXUS,
}

#: gRPC statuses that mean "the transport is gone", as opposed to "the frontend
#: said no". These are the ones a rebuilt connection can plausibly fix, which is
#: what makes them the retry-once set rather than the retryable set.
_TRANSPORT_LOST = frozenset({"UNAVAILABLE", "UNKNOWN"})


# ---------------------------------------------------------------------------
# Connections
# ---------------------------------------------------------------------------


async def _noop() -> None:
    """Release nothing. The closer for a connection that owns no transport."""


@dataclass(frozen=True, slots=True, kw_only=True)
class TemporalConnection:
    """One loop's client, plus whatever transport is holding it up.

    Built by a factory — :func:`frontend_connection` or
    :func:`port_forwarded_connection` — so that *how the frontend is reached* is
    a parameter of :class:`TemporalServiceReader` rather than a subclass of it.
    Mirrors :class:`~application_sdk.testing.harness.cluster.KubernetesApis`, and
    for the same payoff: a driver that already runs inside the cluster is a third
    factory here, not a second reader.

    Attributes:
        client: The connected ``temporalio`` client.
        namespace: Temporal namespace the client is bound to. Handles are scoped
            to it, so it is the namespace
            :meth:`TemporalServiceReader.workflow_status` reads even though that
            method takes no namespace argument.
        address: ``host:port`` the client is pointed at, for a failure to name.
        close: Releases the transport behind the client — a ``kubectl
            port-forward`` tunnel, or nothing at all for a direct address.
            Awaited by :meth:`TemporalServiceReader.aclose`.
    """

    client: Client
    namespace: str
    address: str
    close: Callable[[], Awaitable[None]] = field(default=_noop)


async def frontend_connection(
    *,
    address: str,
    namespace: str,
    api_key: str | None = None,
    tls: bool = False,
) -> TemporalConnection:
    """Connect to a Temporal frontend at a known address.

    Args:
        address: ``host:port`` of the frontend.
        namespace: Temporal namespace to bind the client to.
        api_key: Bearer token, when the frontend expects one. Handed to the
            client, never logged and never carried on a failure — the address and
            the namespace are what a report needs, and a token in an error field
            would travel wherever that report does.
        tls: Whether to negotiate TLS.

    Returns:
        The connection. Its :attr:`~TemporalConnection.close` releases nothing:
        the caller supplied the address, so the caller owns whatever provides it.

    Raises:
        TemporalConnectFailedError: If the client could not be established.
    """
    try:
        client = await Client.connect(
            address, namespace=namespace, api_key=api_key, tls=tls
        )
    # conformance: ignore[E004] re-raised immediately as a typed leaf naming the address and namespace; logging here would report the same failure twice
    except Exception as error:
        raise TemporalConnectFailedError(
            message=(
                f"Could not connect to the Temporal frontend at {address} "
                f"for namespace {namespace!r}"
            ),
            target=address,
            address=address,
            temporal_namespace=namespace,
            cause=error,
        ) from error
    return TemporalConnection(client=client, namespace=namespace, address=address)


async def port_forwarded_connection(
    *,
    target: ServiceTarget,
    namespace: str,
    kube_context: str | None = None,
    timeout: timedelta = _DEFAULT_REQUEST_TIMEOUT,
) -> TemporalConnection:
    """Connect to an in-cluster Temporal frontend Service through a tunnel.

    How an out-of-cluster driver reaches the frontend at all. The tunnel is
    :class:`~application_sdk.testing.harness.cluster.PortForward` — the same one
    :meth:`~application_sdk.testing.harness.cluster.ClusterReader.http` uses,
    reached through its
    :meth:`~application_sdk.testing.harness.cluster.PortForward.address`
    accessor because ``temporalio`` speaks gRPC and needs the tunnel's near end
    rather than an HTTP session over it. Sharing it is the point: a second
    "shell out, pick a free port, wait for it to accept" is the divergence
    FND-224 exists to remove, and this one already kills the child by handle
    rather than by pattern, which is what keeps concurrent runs on one host from
    tearing down each other's tunnels.

    Args:
        target: The frontend ``Service`` and its gRPC port. No default: which
            Service serves a tenant's frontend is deployment configuration, and a
            wrong guess baked in here would surface as a connect timeout rather
            than as a missing setting.
        namespace: Temporal namespace to bind the client to.
        kube_context: Kubeconfig context to tunnel through. Pass the same one the
            cluster reader was built with whenever that is a named context:
            without it ``kubectl`` uses whichever context the kubeconfig marks
            current, so a suite reading pods from ``e2e-gcp`` would read *its*
            Temporal from whatever is current instead — both calls succeeding,
            nothing logged, and the only symptom a set of readings that cannot be
            reconciled with each other. ``None`` is right only when the reader
            did not name a context either.
        timeout: Bound on the tunnel's local port becoming reachable.

    Returns:
        The connection. Its :attr:`~TemporalConnection.close` terminates the
        tunnel, so :meth:`TemporalServiceReader.aclose` is not optional here —
        an unclosed tunnel leaks a ``kubectl`` process for the rest of the
        session.

    Raises:
        TemporalConnectFailedError: If the tunnel never opened, or the client
            could not be established over it.
    """
    session = PortForward(
        target.namespace,
        target.service,
        target.port,
        timeout=timeout.total_seconds(),
        kube_context=kube_context,
    )
    where = f"{target.namespace}/{target.service}:{target.port}" + (
        f" (context {kube_context})" if kube_context else ""
    )
    try:
        address = await session.address()
    # conformance: ignore[E004] re-raised immediately as a typed leaf naming the Service; logging here would report the same failure twice
    except Exception as error:
        await session.aclose()
        raise TemporalConnectFailedError(
            message=f"Could not open a tunnel to the Temporal frontend at {where}",
            target=where,
            temporal_namespace=namespace,
            cause=error,
        ) from error

    logger.debug(
        "tunnelled to the Temporal frontend at %s via %s for namespace %s",
        where,
        address,
        namespace,
    )
    try:
        connected = await frontend_connection(address=address, namespace=namespace)
    except BaseException:
        # The tunnel is already up; leaving it behind on a failed connect is the
        # leak this except exists to prevent.
        await session.aclose()
        raise
    return TemporalConnection(
        client=connected.client,
        namespace=namespace,
        address=address,
        close=session.aclose,
    )


@dataclass(slots=True)
class _Bound:
    """One event loop's connection, and the lock that builds it exactly once.

    The lock lives here rather than on the reader because an
    :class:`asyncio.Lock` binds to the loop it is first awaited on, so a reader
    reused across two loops needs two locks — the same reason the connection
    itself is per-loop.
    """

    loop: asyncio.AbstractEventLoop
    lock: asyncio.Lock
    connection: TemporalConnection | None = None
    #: Set while a connect is in flight, and the reason it exists: for the whole
    #: duration of ``await connect()`` the connection is still ``None``, so a
    #: fail-close that only tested :attr:`connection` would let another loop
    #: replace this bound mid-connect — after which this loop stores its client
    #: on a bound nothing references any more, leaking the client and, on the
    #: tunnelled path, a ``kubectl`` child. Claimed before the await, cleared in
    #: a ``finally`` so a *failed* connect releases the bound rather than
    #: poisoning the reader for every other loop.
    connecting: bool = False

    @property
    def in_use(self) -> bool:
        """Whether another loop taking this reader would strand something."""
        return self.connection is not None or self.connecting


class TemporalServiceReader:
    """Read Temporal state through the ``temporalio`` client.

    Satisfies :class:`~application_sdk.testing.harness.temporal.TemporalReader`,
    and offers two reads beyond it: :meth:`find_workflow_status`, where a missing
    execution is a value rather than a raise, and :meth:`task_queue_pollers`'
    ``task_queue_types`` narrowing.

    Args:
        connect: Factory building a connection. The seam that keeps "how is the
            frontend reached" out of the reader: an in-cluster driver is another
            factory, not a second reader class.
        request_timeout: Per-call bound on every read.

    Use it as an async context manager. That is not a style preference: a
    port-forwarded connection holds a ``kubectl`` child process, so a reader that
    is never closed leaks one for the rest of the session, and ``async with``
    makes the release a property of the block rather than of the caller
    remembering. :meth:`aclose` remains public for callers whose lifetime does
    not fit a block.

    Example:
        >>> async with TemporalServiceReader(  # doctest: +SKIP
        ...     connect=functools.partial(
        ...         frontend_connection, address="127.0.0.1:7233", namespace="default"
        ...     )
        ... ) as reader:
        ...     pollers = await reader.task_queue_pollers(
        ...         "atlan-hello-world-default", namespace="default"
        ...     )
    """

    def __init__(
        self,
        *,
        connect: Callable[[], Awaitable[TemporalConnection]],
        request_timeout: timedelta = _DEFAULT_REQUEST_TIMEOUT,
    ) -> None:
        self._connect = connect
        self._request_timeout = request_timeout
        self._bound: _Bound | None = None
        # Guards the read-modify-write of `_bound`. A `threading.Lock`, not an
        # `asyncio.Lock`, and that is forced rather than chosen: the whole point
        # of `_bound` is that two *event loops* are in play, and separate loops
        # run on separate threads — so an asyncio primitive cannot serialise
        # them, and two threads could each pass the in-use check and each
        # install a fresh bound. Held across no awaits, so it cannot deadlock.
        self._swap = threading.Lock()

    # -- the connection, per loop -------------------------------------------

    async def _connection(self) -> TemporalConnection:
        """This loop's connection, built on first use.

        A ``temporalio`` ``Client`` is bound to the loop that created it, so a
        reader handed between loops — a sync composer's
        :func:`~application_sdk.testing.harness.bridge.run_sync` loop and a
        native-async scenario's, say — must not share one. Honoured the way
        :class:`~application_sdk.testing.harness.cluster.KubernetesReader`
        honours thread affinity for its own non-shareable client.
        """
        bound = self._loop_bound()
        if bound.connection is not None:
            return bound.connection
        async with bound.lock:
            # Re-checked under the lock: two probes racing the first read on this
            # loop would otherwise open two connections, and the loser's would be
            # dropped without being closed — a leaked tunnel and a leaked client.
            if bound.connection is None:
                try:
                    bound.connection = await self._connect()
                finally:
                    # Cleared whether the connect succeeded or raised. On failure
                    # the bound goes back to idle and another loop may take the
                    # reader; leaving the claim set would poison it for every
                    # loop after one bad connect.
                    with self._swap:
                        bound.connecting = False
            return bound.connection

    def _loop_bound(self) -> _Bound:
        """Claim this loop's bound, or refuse if another loop holds a live one.

        Atomic across threads, which is the part that makes the refusal sound
        rather than probable. Two event loops mean two threads, so the
        read-modify-write of :attr:`_bound` is a genuine data race and no asyncio
        primitive can serialise it: without :attr:`_swap`, two threads could each
        find the bound idle and each install a fresh one, and the loser would go
        on to connect onto a bound nothing references.

        A **fresh** bound is claimed as ``connecting`` here, before it is
        published and therefore before any await. That closes the window the
        marker exists for: from this point no other loop can replace it, so the
        connect that follows cannot be stranded halfway.

        Rebinds freely while there is nothing to lose, and refuses once there is.
        A reader that has never connected, or that has been closed, rebinds
        without complaint — there is no leak in either case, and raising would
        only punish a legitimate handoff of an idle reader.

        Raises:
            TemporalReaderLoopMismatchError: If another loop holds a connection
                or is in the middle of opening one. Close it there, or — better —
                stop sharing one reader across loops;
                :func:`~application_sdk.testing.harness.bridge.run_sync` owns one
                loop per thread, so a suite reaching the reader through it never
                sees this.
        """
        running = asyncio.get_running_loop()
        with self._swap:
            bound = self._bound
            if bound is not None and bound.loop is running:
                return bound
            if bound is not None and bound.in_use:
                raise TemporalReaderLoopMismatchError(
                    message=(
                        "This Temporal reader is "
                        + (
                            f"already connected to {bound.connection.address}"
                            if bound.connection is not None
                            else "in the middle of connecting"
                        )
                        + " on a different event loop. Rebinding would strand "
                        "that connection with nothing to close it, leaking the "
                        "kubectl child behind a port-forwarded tunnel — await "
                        "aclose() on the loop that opened it, or use one reader "
                        "per loop"
                    ),
                    address=(
                        bound.connection.address
                        if bound.connection is not None
                        else None
                    ),
                )
            if bound is not None:
                logger.debug(
                    "rebinding an idle Temporal reader to a new event loop — "
                    "nothing to close, so nothing is lost"
                )
            # Published already claimed, so the connect below cannot be displaced.
            fresh = _Bound(loop=running, lock=asyncio.Lock(), connecting=True)
            self._bound = fresh
            return fresh

    async def __aenter__(self) -> TemporalServiceReader:
        """Enter the block. Opens nothing.

        Deliberately not an eager connect: the reader's laziness is a property
        other code relies on (a fixture can build one for a scenario that then
        decides not to reach Temporal), and connecting here would turn
        ``async with`` into a network call for a block that may never read.
        """
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close on the way out, however the block ended.

        Returns ``None`` rather than a truthy value, so an exception raised
        inside the block propagates — teardown is not a reason to swallow the
        failure that triggered it.

        This is the structural half of the leak guard, and it is worth being
        precise about what it does and does not cover: it removes *forgetting* to
        close, which is the common case. It does not close the residue described
        on :meth:`aclose` — a read still running in another task can still
        resurrect a connection after the block exits — because that is a property
        of the read outliving the reader, not of who calls the close.
        """
        await self.aclose()

    async def aclose(self) -> None:
        """Release this loop's connection and whatever transport holds it up.

        Idempotent, and safe on a reader that never connected. Not optional for a
        port-forwarded connection: the tunnel is a ``kubectl`` child process —
        prefer ``async with`` on the reader, which makes that structural rather
        than remembered, and keep this for lifetimes that do not fit a block.

        **Waits for an in-flight connect rather than returning past it.** Testing
        ``connection is None`` and returning was wrong for the same reason the
        loop guard's version of that test was: the connection is ``None`` for the
        whole duration of ``await connect()``, so a teardown racing a first read
        — two tasks on one loop, which is exactly a fixture closing while a
        probe is still opening — returned "closed" and then had a client
        installed behind it, with nothing left to close it.

        :meth:`_connection` holds ``bound.lock`` across the connect, so acquiring
        it here means one of two things has already happened: the connect
        finished and there is a connection to close, or it failed and there is
        not. So the postcondition is "everything this reader had open is closed",
        rather than the weaker "nothing was open when I looked". Sequential use
        pays nothing: an uncontended :class:`asyncio.Lock` acquires without
        yielding.

        It is **not** "and nothing will be open afterwards", and the difference is
        deliberate rather than overlooked. A read already in flight holds its
        connection as a local — :meth:`_connection` hands it out before this
        method takes it away — so that read continues, fails against the closed
        client, and :meth:`_read`'s rebuild opens a *fresh* connection after
        teardown returned. Closing that residue needs ``aclose`` to be terminal
        (a flag that makes :meth:`_connection` refuse), which would also forbid
        the reuse-after-close that the rebuild itself depends on — a contract
        change, not a fix, and one for whoever decides whether a reader is
        single-use. Until then: do not tear a reader down while a read is still
        running on it. Sequential ``read`` then ``aclose`` has no residue at all.

        No deadlock, and it is worth saying why rather than leaving it to be
        rechecked: nothing on the connect path calls back into this method. The
        tunnelled factory closes its own
        :class:`~application_sdk.testing.harness.cluster.PortForward`, not the
        reader, and :meth:`_read`'s rebuild calls this only after
        :meth:`_connection` has returned and released the lock. That is the
        distinction from ``PortForward.aclose``, which genuinely cannot take its
        own open lock because ``_spawn`` calls it while holding one.

        Close on the loop that opened the connection. Awaiting a *contended*
        lock from another loop is not sound, which is the same constraint
        :meth:`_loop_bound` already enforces from the other side.
        """
        with self._swap:
            bound = self._bound
        if bound is None:
            return
        async with bound.lock:
            connection, bound.connection = bound.connection, None
            if connection is not None:
                await connection.close()

    # -- TemporalReader -----------------------------------------------------

    async def task_queue_pollers(
        self,
        queue: str,
        *,
        namespace: str,
        task_queue_types: Iterable[TaskQueueType] | None = None,
    ) -> Sequence[PollerInfo]:
        """Return the workers Temporal currently sees polling *queue*.

        One ``DescribeTaskQueue`` per requested type, in the order requested, so
        the returned sequence groups by type deterministically — a report and an
        assertion both read better for it, and it costs nothing that concurrency
        would buy back at this size.

        Args:
            queue: Task-queue name.
            namespace: Temporal namespace. Taken per-call rather than from the
                connection because ``DescribeTaskQueue`` carries it in the
                request, so one connection can read across namespaces.
            task_queue_types: Which halves of the queue to read. ``None`` selects
                the default — the workflow and activity queues, the two an SDK
                worker polls. An **empty** sequence is honoured as itself: it
                reads nothing and makes no RPC, rather than being taken for
                "unspecified".

        Returns:
            One :class:`PollerInfo` per poller per queue half. **Empty is a real
            answer**, and the one this read exists to deliver: it is the observed
            form of "no worker on this task queue", which ``testing.e2e``'s
            ``NoWorkerOnTaskQueueError`` currently infers from three minutes of
            silence.

        Raises:
            TemporalReadFailedError: If the read did not come back with data.
                Never an empty sequence for an unreadable frontend — see
                :mod:`application_sdk.testing.harness.temporal._errors` for why
                that matters more here than anywhere else in the harness.
        """
        pollers: list[PollerInfo] = []
        # `is None` rather than falsiness: an explicitly empty sequence means
        # "read no queue halves", and `or` would silently promote it to both.
        # A caller narrowing types from a config that happens to resolve empty
        # would then get the opposite of what it asked for, and get it quietly.
        wanted = (
            _DEFAULT_TASK_QUEUE_TYPES if task_queue_types is None else task_queue_types
        )
        for queue_type in tuple(wanted):
            request = DescribeTaskQueueRequest(
                namespace=namespace,
                task_queue=TaskQueue(name=queue),
                task_queue_type=_WIRE_QUEUE_TYPES[queue_type],
            )
            response = await self._read(
                lambda connection, request=request: (
                    connection.client.workflow_service.describe_task_queue(
                        request, timeout=self._request_timeout
                    )
                ),
                target=f"pollers on {queue_type.value.lower()} queue {queue}",
                namespace=namespace,
            )
            pollers.extend(
                _poller_info(entry, queue_type) for entry in response.pollers
            )
        return pollers

    async def workflow_status(
        self, workflow_id: str, *, run_id: str | None = None
    ) -> WorkflowStatus:
        """Return the state of one workflow execution.

        Args:
            workflow_id: Workflow ID to describe.
            run_id: Specific run, or ``None`` for the latest run of that ID.

        Returns:
            The execution's :class:`WorkflowStatus`.

        Raises:
            WorkflowNotFoundError: If there is no such execution. Not folded into
                the read failure: a wrong id does not recover, so a wait must
                fail on it at once rather than absorb it as a blip.
            TemporalReadFailedError: If the read did not come back with data.
        """
        found = await self.find_workflow_status(workflow_id, run_id=run_id)
        if found is None:
            connection = await self._connection()
            raise WorkflowNotFoundError(
                message=(
                    f"Temporal has no execution {workflow_id!r}"
                    + (f" run {run_id!r}" if run_id else "")
                    + f" in namespace {connection.namespace!r} — check the id and "
                    "the namespace the client is bound to before the worker"
                ),
                resource_identifier=workflow_id,
                workflow_id=workflow_id,
                run_id=run_id,
                temporal_namespace=connection.namespace,
            )
        return found

    # -- beyond the Protocol ------------------------------------------------

    async def find_workflow_status(
        self, workflow_id: str, *, run_id: str | None = None
    ) -> WorkflowStatus | None:
        """Return one execution's state, or ``None`` when there is no such execution.

        The nullable twin of :meth:`workflow_status`, and not merely a
        convenience: a wait for work that something *else* dispatches — the
        Automation Engine, in every connector run — starts before the execution
        exists, so "not there yet" is a reading its predicate has to be able to
        test. Raising through that window would make the wait's own
        ``NeverStarted`` verdict unreachable, since the first probe would end the
        wait.

        The same 404-is-an-answer narrowing as
        :meth:`~application_sdk.testing.harness.cluster.CustomResourceReader.crd_schema`,
        and drawn in the same place: only ``NOT_FOUND`` answers ``None``. A
        ``PERMISSION_DENIED`` from a narrowed token or a ``DEADLINE_EXCEEDED``
        over a VPN raises, because a false "it never started" cached from one of
        those is the negative no later read corrects.

        Args:
            workflow_id: Workflow ID to describe.
            run_id: Specific run, or ``None`` for the latest run of that ID.

        Returns:
            The execution's :class:`WorkflowStatus`, or ``None`` if Temporal has
            no execution under that ID.

        Raises:
            TemporalReadFailedError: On any failure that is not a clean
                ``NOT_FOUND``.
        """
        target = f"workflow {workflow_id}" + (f" run {run_id}" if run_id else "")
        try:
            description = await self._read(
                lambda connection: connection.client.get_workflow_handle(
                    workflow_id, run_id=run_id
                ).describe(rpc_timeout=self._request_timeout),
                target=target,
                namespace=None,
            )
        except TemporalReadFailedError as error:
            if error.rpc_status != "NOT_FOUND":
                raise
            logger.debug("%s does not exist in this namespace (NOT_FOUND)", target)
            return None
        return _workflow_status(description, workflow_id)

    # -- the read, with its one rebuild ------------------------------------

    async def _read(
        self,
        call: Callable[[TemporalConnection], Awaitable[T]],
        *,
        target: str,
        namespace: str | None,
    ) -> T:
        """Run one read, rebuilding the connection once if the transport is gone.

        Args:
            call: The read, given this loop's connection. Takes the connection
                rather than closing over it so the retry runs against the *new*
                one — a closure over the dead client would retry the same corpse.
            target: What was being read, as a noun phrase for the report.
            namespace: Namespace the read was scoped to, or ``None`` when the
                connection's own namespace is the answer.

        Returns:
            Whatever the read returned.

        Raises:
            TemporalReadFailedError: If the read failed, and a rebuilt connection
                did not fix it. Two consecutive transport failures are a frontend
                that is genuinely unreachable, not a stale tunnel.
        """
        connection = await self._connection()
        try:
            return await self._attempt(connection, call, target, namespace)
        except TemporalReadFailedError as error:
            if error.rpc_status not in _TRANSPORT_LOST:
                raise
            logger.warning(
                "the Temporal connection to %s dropped mid-read (%s) while "
                "reading %s — rebuilding it and re-attempting once",
                connection.address,
                error.rpc_status,
                target,
                exc_info=True,
            )
        await self.aclose()
        return await self._attempt(await self._connection(), call, target, namespace)

    async def _attempt(
        self,
        connection: TemporalConnection,
        call: Callable[[TemporalConnection], Awaitable[T]],
        target: str,
        namespace: str | None,
    ) -> T:
        """One read against one connection, with its errors typed.

        Only ``temporalio``'s own ``RPCError`` is converted. A ``TypeError`` from
        a wiring bug is a bug, and dressing it as ``DEPENDENCY_UNAVAILABLE``
        would let a bounded wait absorb it as a transient blip and spend its
        whole budget on it — the fail-open shape, one level up. ``RPCError``
        rather than ``Exception`` plus an ``isinstance`` makes that a property of
        the syntax rather than of a branch that can rot.
        """
        try:
            return await call(connection)
        except RPCError as error:
            status = error.status.name
            raise TemporalReadFailedError(
                message=f"Could not read {target} ({status})",
                target=target,
                rpc_status=status,
                address=connection.address,
                temporal_namespace=namespace or connection.namespace,
                cause=error,
            ) from error


# ---------------------------------------------------------------------------
# Wire objects -> typed states
# ---------------------------------------------------------------------------


def _poller_info(entry: WirePollerInfo, queue_type: TaskQueueType) -> PollerInfo:
    """One wire ``PollerInfo`` as the harness's own.

    The build id is read from ``deployment_options`` first and
    ``worker_version_capabilities`` second, because those are the new and old
    shapes of the same fact and this SDK's worker sets the new one
    (``WorkerDeploymentConfig``, from ``ATLAN_APP_DEPLOYMENT_NAME`` and
    ``ATLAN_APP_BUILD_ID``). Reading only the legacy field would report every
    versioned Atlan worker as unversioned, which
    :func:`~application_sdk.testing.harness.temporal.stale_version_pollers`
    would then read as *every* poller being stale.
    """
    build_id = _non_empty(entry.deployment_options.build_id) or _non_empty(
        entry.worker_version_capabilities.build_id
    )
    return PollerInfo(
        identity=entry.identity,
        last_access=entry.last_access_time.ToDatetime(tzinfo=timezone.utc),
        task_queue_type=queue_type,
        build_id=build_id,
        deployment_name=_non_empty(entry.deployment_options.deployment_name),
    )


def _workflow_status(
    description: WorkflowExecutionDescription, workflow_id: str
) -> WorkflowStatus:
    """One ``WorkflowExecutionDescription`` as a :class:`WorkflowStatus`."""
    return WorkflowStatus(
        workflow_id=description.id or workflow_id,
        run_id=description.run_id,
        status=_execution_status(description.status),
        task_queue=description.task_queue,
        history_length=description.history_length,
        started_at=description.start_time,
        closed_at=description.close_time,
    )


def _execution_status(value: object) -> WorkflowExecutionStatus:
    """A reported status as a :class:`WorkflowExecutionStatus`.

    ``temporalio`` surfaces the proto's ``UNSPECIFIED`` as ``None``, and a status
    this SDK has never heard of would arrive as an enum member with an unfamiliar
    name. Both read as
    :attr:`~application_sdk.testing.harness.temporal.WorkflowExecutionStatus.UNKNOWN`,
    the way an unrecognised pod phase reads as ``Unknown``: a classification, not
    a failure, so raising on it would turn a new upstream status into a harness
    crash.
    """
    name = getattr(value, "name", None)
    if isinstance(name, str):
        member = WorkflowExecutionStatus.__members__.get(name)
        if member is not None:
            return member
    if value is not None:
        logger.warning(
            "Unrecognised workflow execution status %r — reading it as %s",
            value,
            WorkflowExecutionStatus.UNKNOWN,
        )
    return WorkflowExecutionStatus.UNKNOWN


def _non_empty(value: str) -> str | None:
    """A protobuf string field, with its empty default read as absent.

    Protobuf has no null for a scalar, so an unset ``build_id`` arrives as
    ``""``. Carrying that through would make ``build_id == ""`` a build id, and
    an unversioned poller would compare unequal to every real build while
    claiming to have one.
    """
    return value or None
