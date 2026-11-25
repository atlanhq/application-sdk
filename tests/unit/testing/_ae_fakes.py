"""A stand-in for the async ``AEClient``, and builders for what it answers.

The fourth harness seam's shared double, beside ``_atlas_fakes``,
``_cluster_fakes`` and ``_temporal_fakes``. Before it, fourteen hand-written
Automation Engine doubles each implemented the subset of the client one test
file happened to reach — so a new AE call from ``BaseE2ETest`` meant editing up
to seven of them, found one failing file at a time.

:class:`FakeAE` implements **every public method** ``AEClient`` has, with the
same parameter names and kinds. ``tests/unit/testing/test_ae_fakes.py`` pins
that, so a method added to the client fails one test here rather than an
``AttributeError`` in whichever suite first reaches it.

It records instead of asserting: each call lands in :attr:`FakeAE.calls` with
its arguments bound by name, and :meth:`FakeAE.calls_to` / :meth:`FakeAE.args`
read them back. A test then states what the run did ("the teardown submitted
the published version of ``slug-1``") rather than which mock was called.

Two methods are the real client's, not re-scripted: ``foreign_published_dag``
runs ``AEClient``'s own guard over this fake's scripted
:meth:`FakeAE.get_published_version`. The decision it makes (which node names
are foreign, and that an unreadable AE degrades to "unanswered") is what the
seed and teardown tests are about, and a second copy of it in a fake would only
ever agree with itself.

The builders at the bottom — :func:`node`, :func:`succeeded`, :func:`failed`,
:func:`stalled`, :func:`numbered` and :func:`run_result` — replace the 23
private ``_node`` / ``_succeeded`` / ``_result`` helpers that built the same
``DAGRunResult`` shapes file by file.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from application_sdk.testing.harness.automation_engine import (
    AEClient,
    DAGNodeResult,
    DAGNodeStatus,
    DAGRunResult,
    DAGRunStatus,
    PublishedVersion,
    RunLookup,
)

#: What :meth:`FakeAE.get_node_outputs` may be scripted with: one answer for
#: every read, an error to raise, or a function of ``(run_id, node_id)``.
NodeOutputs = (
    dict[str, Any]
    | BaseException
    | Callable[[str, str], dict[str, Any] | BaseException]
)


class FakeAE:
    """Async ``AEClient`` double: scripted reads, recorded writes.

    Args:
        *polls: What successive ``poll_native_status`` / ``get_native_status``
            reads return, in order. The last one repeats once the queue is down
            to it, so a test that only cares about the verdict scripts one. A
            ``BaseException`` in the queue is raised at its turn instead.
            Nothing scripted means every read answers :func:`succeeded` for an
            ``extract`` node.
        published: What ``get_published_version`` answers — a
            :class:`PublishedVersion`, ``None`` for "nothing published / not
            readable", or an exception to raise.
        node_outputs: What ``get_node_outputs`` answers. ``{}`` by default.
        listed: What ``probe_run_is_listed`` answers.
        run_lookup: What ``find_run_created_since`` answers.
        errors: Method name to the exception that method raises. The call is
            still recorded first, so a test can assert that it was attempted.

    Every ``submit_*`` mints ``run-<n>`` from one counter shared by both
    endpoints, and reports it through ``on_run_id`` the way the real client
    does — which is how ``BaseE2ETest`` tracks a run for teardown before the
    submit has returned. ``create_workflow`` mints ``slug-<n>``.
    """

    def __init__(
        self,
        *polls: DAGRunResult | BaseException,
        published: PublishedVersion | BaseException | None = None,
        node_outputs: NodeOutputs | None = None,
        listed: bool | None = None,
        run_lookup: RunLookup | None = None,
        errors: dict[str, BaseException] | None = None,
    ) -> None:
        self._polls: list[DAGRunResult | BaseException] = list(polls) or [
            succeeded("extract")
        ]
        self._published = published
        self._node_outputs: NodeOutputs = {} if node_outputs is None else node_outputs
        self._listed = listed
        self._run_lookup = RunLookup() if run_lookup is None else run_lookup
        self._errors = dict(errors or {})
        self._runs = 0
        self._slugs = 0
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.closed = False
        #: The version the last ``create_version`` was given — what a subclass
        #: that supersedes the seed publishes over.
        self.seed_version: int | None = None

    # -- recording -----------------------------------------------------------

    def _record(self, method: str, frame_locals: dict[str, Any]) -> None:
        """Record *method* with every argument it was called with, by name.

        Called first thing in each method, so ``locals()`` is exactly the bound
        arguments — defaults included, which is what lets a test pin a budget
        the caller passed *or* failed to pass.
        """
        arguments = {k: v for k, v in frame_locals.items() if k != "self"}
        self.calls.append((method, arguments))
        error = self._errors.get(method)
        if error is not None:
            raise error

    def calls_to(self, method: str) -> list[dict[str, Any]]:
        """The arguments of every call to *method*, in call order."""
        return [arguments for name, arguments in self.calls if name == method]

    def args(self, method: str, *names: str) -> list[Any]:
        """The named argument(s) of every call to *method*, in call order.

        One name gives a list of values; several give a list of tuples, so
        ``ae.args("get_node_outputs", "run_id", "node_id")`` reads as the pairs
        the reads were made with.
        """
        calls = self.calls_to(method)
        if len(names) == 1:
            return [arguments[names[0]] for arguments in calls]
        return [tuple(arguments[name] for name in names) for arguments in calls]

    @property
    def methods(self) -> list[str]:
        """The method name of every call, in order."""
        return [name for name, _arguments in self.calls]

    def _next_poll(self) -> DAGRunResult:
        reading = self._polls.pop(0) if len(self._polls) > 1 else self._polls[0]
        if isinstance(reading, BaseException):
            raise reading
        return reading

    def _mint_run(self, on_run_id: Callable[[str], None] | None) -> str:
        self._runs += 1
        run_id = f"run-{self._runs}"
        if on_run_id is not None:
            on_run_id(run_id)
        return run_id

    # -- the AEClient surface -----------------------------------------------

    async def aclose(self) -> None:
        self._record("aclose", locals())
        self.closed = True

    async def create_workflow(
        self,
        name: str,
        description: str = "",
        *,
        retries: int = 4,
        retry_sleep_seconds: int = 5,
    ) -> str:
        self._record("create_workflow", locals())
        self._slugs += 1
        return f"slug-{self._slugs}"

    async def slug_resolves(self, slug: str) -> bool | None:
        self._record("slug_resolves", locals())
        return True

    async def wait_for_slug(
        self,
        slug: str,
        *,
        timeout_seconds: int = 30,
        interval_seconds: int = 1,
    ) -> bool:
        self._record("wait_for_slug", locals())
        return True

    async def create_version(
        self,
        slug: str,
        version_payload: dict[str, Any],
        *,
        retries: int = 5,
        retry_sleep_seconds: int = 5,
    ) -> int:
        self._record("create_version", locals())
        self.seed_version = int(version_payload["version"])
        return self.seed_version

    async def publish_version(
        self,
        slug: str,
        version: int,
        *,
        retries: int = 5,
        retry_sleep_seconds: int = 5,
    ) -> None:
        self._record("publish_version", locals())

    async def get_published_version(self, slug: str) -> PublishedVersion | None:
        self._record("get_published_version", locals())
        if isinstance(self._published, BaseException):
            raise self._published
        return self._published

    foreign_published_dag = AEClient.foreign_published_dag

    async def find_run_created_since(
        self,
        slug: str,
        since: datetime,
        *,
        timeout_seconds: int = 90,
        interval_seconds: int = 10,
    ) -> RunLookup:
        self._record("find_run_created_since", locals())
        return self._run_lookup

    async def probe_run_is_listed(self, slug: str, run_id: str) -> bool | None:
        self._record("probe_run_is_listed", locals())
        return self._listed

    async def terminate_run(self, run_id: str) -> bool | None:
        self._record("terminate_run", locals())
        return True

    async def submit_workflow(
        self,
        payload: dict[str, Any],
        *,
        slug: str = "",
        retries: int = 4,
        retry_sleep_seconds: int = 5,
        on_run_id: Callable[[str], None] | None = None,
    ) -> str:
        self._record("submit_workflow", locals())
        return self._mint_run(on_run_id)

    async def submit_published_version(
        self,
        slug: str,
        *,
        retries: int = 5,
        retry_sleep_seconds: int = 12,
        on_run_id: Callable[[str], None] | None = None,
    ) -> str:
        self._record("submit_published_version", locals())
        return self._mint_run(on_run_id)

    async def get_native_status(self, run_id: str) -> DAGRunResult:
        self._record("get_native_status", locals())
        return self._next_poll()

    async def get_node_outputs(self, run_id: str, node_id: str) -> dict[str, Any]:
        self._record("get_node_outputs", locals())
        answer = self._node_outputs
        if callable(answer):
            answer = answer(run_id, node_id)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    async def poll_native_status(
        self,
        run_id: str,
        *,
        interval_seconds: int = 10,
        timeout_seconds: int = 600,
        max_transient_failures: int = 5,
        stall_grace_seconds: int | None = None,
        stall_task_queue: str = "",
        progress_stall_seconds: int | None = None,
    ) -> DAGRunResult:
        self._record("poll_native_status", locals())
        return self._next_poll()


# ---------------------------------------------------------------------------
# DAGRunResult / DAGNodeResult builders
# ---------------------------------------------------------------------------


def node(
    name: str,
    status: DAGNodeStatus = DAGNodeStatus.SUCCEEDED,
    *,
    error: str | None = None,
    started_at_ms: int | None = None,
    completed_at_ms: int | None = None,
) -> DAGNodeResult:
    """One ``native-status`` node row."""
    return DAGNodeResult(
        name=name,
        status=status,
        started_at_ms=started_at_ms,
        completed_at_ms=completed_at_ms,
        error_message=error,
    )


def run_result(
    status: DAGRunStatus,
    *nodes: DAGNodeResult,
    run_id: str = "r",
    workflow_slug: str = "s",
    timed_out_after_seconds: float | None = None,
    progress_stalled_after_seconds: float | None = None,
    seconds_since_last_progress: float | None = None,
) -> DAGRunResult:
    """A ``native-status`` reading with exactly the given nodes."""
    return DAGRunResult(
        run_id=run_id,
        workflow_slug=workflow_slug,
        status=status,
        nodes=list(nodes),
        timed_out_after_seconds=timed_out_after_seconds,
        progress_stalled_after_seconds=progress_stalled_after_seconds,
        seconds_since_last_progress=seconds_since_last_progress,
    )


def succeeded(*names: str, run_id: str = "r", workflow_slug: str = "s") -> DAGRunResult:
    """A succeeded run whose every named node succeeded.

    At least one name is required: AE's verdict is ``all_nodes_succeeded``,
    which is false for an empty node list, so a nameless "success" would fail
    the harness's own gate. A test that means "AE reported no nodes" says so
    with ``run_result(DAGRunStatus.SUCCEEDED)``.
    """
    if not names:
        raise ValueError("succeeded() needs at least one node name")
    return run_result(
        DAGRunStatus.SUCCEEDED,
        *(node(name) for name in names),
        run_id=run_id,
        workflow_slug=workflow_slug,
    )


def failed(*names: str, run_id: str = "r", workflow_slug: str = "s") -> DAGRunResult:
    """A failed run whose every named node failed."""
    return run_result(
        DAGRunStatus.FAILED,
        *(node(name, DAGNodeStatus.FAILED) for name in names),
        run_id=run_id,
        workflow_slug=workflow_slug,
    )


def stalled(
    *names: str,
    window: float = 600.0,
    since_progress: float | None = 612.0,
    run_id: str = "r",
    workflow_slug: str = "s",
) -> DAGRunResult:
    """The observation a ``DAGProgressStalledError`` carries: nodes still running.

    The progress watchdog's stop, not the ceiling's — ``timed_out_after_seconds``
    stays ``None`` so a diagnostic cannot mislabel the stall as a timeout.
    """
    return run_result(
        DAGRunStatus.RUNNING,
        *(node(name, DAGNodeStatus.RUNNING) for name in names),
        run_id=run_id,
        workflow_slug=workflow_slug,
        progress_stalled_after_seconds=window,
        seconds_since_last_progress=since_progress,
    )


def numbered(
    status: DAGRunStatus,
    *node_statuses: DAGNodeStatus,
    run_id: str = "r",
    workflow_slug: str = "s",
) -> DAGRunResult:
    """A reading with one node per status, named ``node0``, ``node1``, ….

    For tests about how statuses combine, where node names carry nothing.
    """
    return run_result(
        status,
        *(node(f"node{i}", s) for i, s in enumerate(node_statuses)),
        run_id=run_id,
        workflow_slug=workflow_slug,
    )


__all__ = [
    "FakeAE",
    "NodeOutputs",
    "failed",
    "node",
    "numbered",
    "run_result",
    "stalled",
    "succeeded",
]
