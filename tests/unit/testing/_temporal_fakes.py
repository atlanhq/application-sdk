"""Test doubles for the ``temporalio`` Temporal reader (FND-247) and the
direct-to-task-queue starter (FND-246).

One fake serves both, and that is load-bearing rather than tidy: the starter's
own claim is that the run it dispatched is the run a later status read describes,
and a test that started against one double and read against another could not
check it — the two would agree by construction.

The doubles hand back **real protobuf messages and a real
``WorkflowExecutionDescription``**, for the reason
:mod:`tests.unit.testing._cluster_fakes` hands back real sanitized dicts: the
conversion under test is the production one, not a mock-shaped variant of it.
Every field-name assumption in ``client.py`` — ``deployment_options.build_id``
versus ``worker_version_capabilities.build_id``, ``last_access_time``,
``history_length`` — is therefore checked against the wire's own definitions
rather than against a stub that would agree with any spelling.

The narrowing needs a *real* ``RPCError`` for the same reason: the backend
converts only ``temporalio``'s error type and lets everything else through, so a
stub exception would let a test pass against a narrowing that had stopped
working.

The start hands back a **real** ``WorkflowHandle`` for the sharpest version of
that reasoning. ``run_id`` on a handle built by ``start_workflow`` is ``None`` by
documented design and ``result_run_id`` carries the started run — a stub handle
would happily expose whichever attribute the code under test read, so the bug
the real object catches (reading ``run_id`` and finding every started run
unidentifiable) is invisible against a mock.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from temporalio.api.common.v1 import WorkerVersionCapabilities
from temporalio.api.common.v1 import WorkflowExecution as WireWorkflowExecution
from temporalio.api.common.v1 import WorkflowType
from temporalio.api.deployment.v1 import WorkerDeploymentOptions
from temporalio.api.enums.v1 import WorkflowExecutionStatus as WireStatus
from temporalio.api.taskqueue.v1 import PollerInfo as WirePollerInfo
from temporalio.api.workflow.v1 import WorkflowExecutionInfo
from temporalio.api.workflowservice.v1 import (
    DescribeTaskQueueRequest,
    DescribeTaskQueueResponse,
    DescribeWorkflowExecutionResponse,
)
from temporalio.client import Client, WorkflowExecutionDescription, WorkflowHandle
from temporalio.converter import DataConverter
from temporalio.service import RPCError, RPCStatusCode

from application_sdk.testing.harness.temporal import (
    TemporalConnection,
    TemporalServiceReader,
)

__all__ = [
    "FakeTemporal",
    "RPCError",
    "RPCStatusCode",
    "connection_over",
    "describe_response",
    "started",
    "on_a_fresh_loop",
    "poller",
    "reader_over",
    "rpc_error",
    "workflow_description",
]

#: A ``BaseException`` in a fake's script means "the frontend answered with this".
Answer = object | BaseException


def _answer(value: Answer) -> Any:
    if isinstance(value, BaseException):
        raise value
    return value


def rpc_error(status: RPCStatusCode, message: str = "boom") -> RPCError:
    """A real ``RPCError`` carrying *status*.

    ``raw_grpc_status`` is empty because nothing under test reads it; the status
    is what the backend narrows on.
    """
    return RPCError(message, status, b"")


def poller(
    identity: str,
    *,
    build_id: str | None = None,
    deployment_name: str | None = None,
    legacy_build_id: str | None = None,
    last_access: datetime | None = None,
) -> WirePollerInfo:
    """One wire poller.

    Args:
        identity: The worker's self-reported identity.
        build_id: Build id on ``deployment_options`` — the Worker Deployment
            shape this SDK's worker actually sets.
        deployment_name: Deployment name on ``deployment_options``.
        legacy_build_id: Build id on ``worker_version_capabilities`` — the older
            shape, present so the fallback can be exercised without the new one.
        last_access: When Temporal last saw the poller. Left unset to exercise
            the epoch the wire renders for an absent timestamp.
    """
    entry = WirePollerInfo(identity=identity)
    if build_id is not None or deployment_name is not None:
        entry.deployment_options.CopyFrom(
            WorkerDeploymentOptions(
                build_id=build_id or "", deployment_name=deployment_name or ""
            )
        )
    if legacy_build_id is not None:
        entry.worker_version_capabilities.CopyFrom(
            WorkerVersionCapabilities(build_id=legacy_build_id, use_versioning=True)
        )
    if last_access is not None:
        entry.last_access_time.FromDatetime(last_access)
    return entry


def describe_response(*pollers: WirePollerInfo) -> DescribeTaskQueueResponse:
    """A ``DescribeTaskQueue`` answer carrying *pollers*."""
    return DescribeTaskQueueResponse(pollers=list(pollers))


async def workflow_description(
    *,
    workflow_id: str = "wf-1",
    run_id: str = "run-1",
    status: WireStatus.ValueType = WireStatus.WORKFLOW_EXECUTION_STATUS_RUNNING,
    task_queue: str = "atlan-hello-world-default",
    history_length: int = 42,
    started_at: datetime | None = None,
    closed_at: datetime | None = None,
    workflow_type: str = "MetadataExtraction",
) -> WorkflowExecutionDescription:
    """A real ``WorkflowExecutionDescription``, built from a real wire response.

    ``temporalio``'s own conversion runs, so the field names the backend reads
    are the ones the client actually publishes rather than the ones a stub was
    written to expose.
    """
    info = WorkflowExecutionInfo(
        execution=WireWorkflowExecution(workflow_id=workflow_id, run_id=run_id),
        type=WorkflowType(name=workflow_type),
        task_queue=task_queue,
        status=status,
        history_length=history_length,
    )
    if started_at is not None:
        info.start_time.FromDatetime(started_at)
    if closed_at is not None:
        info.close_time.FromDatetime(closed_at)
    return await WorkflowExecutionDescription._from_raw_description(
        DescribeWorkflowExecutionResponse(workflow_execution_info=info),
        "default",
        DataConverter.default,
    )


def started(
    *,
    workflow_id: str = "wf-1",
    result_run_id: str | None = "run-1",
    first_execution_run_id: str | None = None,
) -> _Start:
    """What a scripted ``start_workflow`` answers with.

    Args:
        workflow_id: The id the handle reports back, which the starter echoes
            onto its own handle rather than re-using the id it sent.
        result_run_id: The started run, where ``start_workflow`` actually puts
            it. ``None`` exercises the answer that names no run.
        first_execution_run_id: The fallback field, left unset by default so a
            reader of ``result_run_id`` is the only thing that passes.
    """
    return _Start(
        workflow_id=workflow_id,
        result_run_id=result_run_id,
        first_execution_run_id=first_execution_run_id,
    )


@dataclass(frozen=True)
class _Start:
    """A scripted start answer, turned into a real handle at call time."""

    workflow_id: str
    result_run_id: str | None
    first_execution_run_id: str | None

    def handle(self, client: Any) -> WorkflowHandle[Any, Any]:
        """A real ``WorkflowHandle``, shaped as ``start_workflow`` builds one."""
        return WorkflowHandle(
            client,
            self.workflow_id,
            result_run_id=self.result_run_id,
            first_execution_run_id=self.first_execution_run_id,
        )


class _FakeWorkflowService:
    """The one ``workflow_service`` verb the poller read calls."""

    def __init__(self, fake: FakeTemporal) -> None:
        self._fake = fake

    async def describe_task_queue(
        self, req: DescribeTaskQueueRequest, **kwargs: Any
    ) -> DescribeTaskQueueResponse:
        self._fake.calls.append(("describe_task_queue", req, kwargs))
        return _answer(self._fake.next_pollers())


class _FakeHandle:
    """The one handle verb the status read calls."""

    def __init__(
        self, fake: FakeTemporal, workflow_id: str, run_id: str | None
    ) -> None:
        self._fake = fake
        self._workflow_id = workflow_id
        self._run_id = run_id

    async def describe(self, **kwargs: Any) -> WorkflowExecutionDescription:
        self._fake.calls.append(("describe", (self._workflow_id, self._run_id), kwargs))
        return _answer(self._fake.next_description())


class _FakeClient:
    """Shaped like the parts of ``temporalio.client.Client`` a read or a start touches."""

    def __init__(self, fake: FakeTemporal, *, namespace: str = "default") -> None:
        self.workflow_service = _FakeWorkflowService(fake)
        self.namespace = namespace
        self._fake = fake

    def get_workflow_handle(
        self, workflow_id: str, *, run_id: str | None = None, **_: Any
    ) -> _FakeHandle:
        return _FakeHandle(self._fake, workflow_id, run_id)

    async def start_workflow(
        self, workflow: str, **kwargs: Any
    ) -> WorkflowHandle[Any, Any]:
        """Record the dispatch and answer with the next scripted start.

        Every keyword is recorded rather than only the ones asserted on, so a
        test can pin what the starter did *not* send as well as what it did — an
        ``id_reuse_policy`` or a ``memo`` appearing here is a behaviour change
        the recorded call makes visible.
        """
        self._fake.calls.append(("start_workflow", workflow, kwargs))
        answer = _answer(self._fake.next_start())
        if isinstance(answer, _Start):
            return answer.handle(self)
        raise AssertionError(f"scripted start answer is not a _Start: {answer!r}")


class FakeTemporal:
    """Scripted answers for one reader, plus a record of what was asked.

    Both scripts are *sequences* consumed one entry per call, so a test can say
    "the first read drops the connection, the second succeeds" — the shape the
    rebuild-once path needs — and the last entry repeats once the script runs out,
    so the common case stays a single value.

    Args:
        pollers: What each ``describe_task_queue`` answers with, in order. A
            ``BaseException`` is raised instead of returned.
        descriptions: What each ``describe`` answers with, in order.
        starts: What each ``start_workflow`` answers with, in order — a
            :func:`started` answer, or a ``BaseException`` to raise. Defaults to
            one plain successful start, so a test about the reads never has to
            mention the starter.
    """

    def __init__(
        self,
        *,
        pollers: Sequence[Answer] = (),
        descriptions: Sequence[Answer] = (),
        starts: Sequence[Answer] = (),
    ) -> None:
        self.calls: list[tuple[str, Any, dict[str, Any]]] = []
        self.connects: list[str] = []
        self.closes = 0
        self._pollers = list(pollers) or [describe_response()]
        self._descriptions = list(descriptions)
        self._starts = list(starts) or [started()]
        self._poller_index = 0
        self._description_index = 0
        self._start_index = 0

    # -- the connection factory --------------------------------------------

    def connect(
        self, address: str = "127.0.0.1:7233"
    ) -> Callable[[], Awaitable[TemporalConnection]]:
        """A ``connect`` factory that records every build and every close.

        The ``sleep(0)`` is load-bearing and must not be tidied away. Without a
        real suspension point inside the connect, an ``asyncio.gather`` over
        several first reads runs each one start-to-finish and no interleaving
        occurs — so a concurrency test built on this fake passes with the
        reader's lock *removed*, asserting only that a sequential program opens
        one connection. Verified by removal: with the yield, dropping the lock
        gives 5 connections; without it, 1 either way.

        It also has to precede the append, not follow it: the guard reads the
        state this records, so a yield after it proves nothing.
        """

        async def _build() -> TemporalConnection:
            await asyncio.sleep(0)
            self.connects.append(address)
            return TemporalConnection(
                client=_FakeClient(self),  # type: ignore[arg-type]
                namespace="default",
                address=address,
                close=self._close,
            )

        return _build

    async def _close(self) -> None:
        self.closes += 1

    # -- the scripts --------------------------------------------------------

    def next_pollers(self) -> Answer:
        value = self._pollers[min(self._poller_index, len(self._pollers) - 1)]
        self._poller_index += 1
        return value

    def next_start(self) -> Answer:
        value = self._starts[min(self._start_index, len(self._starts) - 1)]
        self._start_index += 1
        return value

    def next_description(self) -> Answer:
        if not self._descriptions:
            raise AssertionError("no `descriptions` were scripted for this fake")
        value = self._descriptions[
            min(self._description_index, len(self._descriptions) - 1)
        ]
        self._description_index += 1
        return value

    # -- what was asked -----------------------------------------------------

    def verbs(self) -> list[str]:
        """Every call made, in order."""
        return [name for name, _payload, _kwargs in self.calls]

    def requests(self) -> list[DescribeTaskQueueRequest]:
        """Every ``DescribeTaskQueue`` request, in order."""
        return [
            payload
            for name, payload, _kwargs in self.calls
            if name == "describe_task_queue"
        ]

    def kwargs_for(self, verb: str) -> dict[str, Any]:
        """The keyword arguments the reader passed to *verb*, first call."""
        for name, _payload, kwargs in self.calls:
            if name == verb:
                return kwargs
        raise AssertionError(f"{verb} was never called; saw {self.verbs()}")


def reader_over(fake: FakeTemporal, **kwargs: Any) -> TemporalServiceReader:
    """A reader wired to *fake* instead of to a real frontend."""
    return TemporalServiceReader(connect=fake.connect(), **kwargs)


def connection_over(
    fake: FakeTemporal,
    *,
    namespace: str = "default",
    address: str = "127.0.0.1:7233",
) -> TemporalConnection:
    """A :class:`TemporalConnection` wired to *fake* instead of to a frontend.

    A **real** ``TemporalConnection`` around a fake client, rather than a double
    for the connection itself: it is the type the starter takes, it is a frozen
    dataclass with no behaviour to fake, and building the real one is what keeps
    a test honest about which of its fields the starter actually reads.

    The ``cast`` is the honest spelling of the one thing that is not real. The
    field's type is ``temporalio``'s own ``Client``, so pyright is right that
    this is not one; a ``Protocol`` invented to make it one would be a second
    declaration of ``start_workflow``'s signature, free to drift from the
    engine's.
    """
    return TemporalConnection(
        client=cast(Client, _FakeClient(fake, namespace=namespace)),
        namespace=namespace,
        address=address,
        close=fake._close,
    )


def on_a_fresh_loop(work: Callable[[], Awaitable[Any]]) -> Any:
    """Run *work* on a loop of its own, then close it.

    For the loop-affinity tests, which need two genuinely different loops. The
    loop is closed in a ``finally`` so a failing assertion does not leave one
    behind for the next test to trip over.
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(work())
    finally:
        loop.close()
