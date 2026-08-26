"""SYSAPPS-328: ``SegmentClient.flush()`` must not bridge event loops in a workflow.

The production failure is a *cross-loop bridge*. ``SegmentClient`` runs its
sender on a dedicated thread with its own event loop; ``flush()`` schedules the
drain there with ``run_coroutine_threadsafe`` and awaits the result via
``asyncio.wrap_future``. ``wrap_future`` resolves the awaited future from a
done-callback that first calls ``is_closed()`` on the *destination* loop.
Inside a workflow that destination is Temporal's deterministic workflow loop,
which raises ``NotImplementedError`` from ``is_closed()`` — so the callback dies
before resolving and the await burns the full 10 s timeout:

    ERROR exception calling callback for <Future ... state=finished returned NoneType>
      ...
      File ".../asyncio/futures.py", line 398, in _call_set_state
        if dest_loop.is_closed():
      NotImplementedError
    WARN  Segment queue flush timed out          # 10.1 s later

These tests run that path through a **real Temporal worker** on the embedded
dev server rather than patching the guard's predicate, so they fail if the
guard is removed for any reason — including one that a mocked predicate would
hide.

Queue contents are irrelevant to the defect: ``run_coroutine_threadsafe`` is
called regardless, and the break is in the bridge, not in the send. The
production log confirms it — ``state=finished returned NoneType`` means the
drain itself completed; only the acknowledgement back to the workflow failed.
``api_url`` therefore points at a closed local port, so any lifecycle events the
events interceptor happens to queue fail their POST locally and nothing leaves
the machine.

Requires a running Temporal dev server (see conftest.py).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator

import pytest
from temporalio import workflow

from application_sdk.app.base import App
from application_sdk.app.context import AppContext
from application_sdk.contracts.base import Input, Output
from application_sdk.execution.retry import NO_RETRY
from application_sdk.observability.segment_client import SegmentClient
from application_sdk.observability.utils import in_temporal_workflow

# Wall-clock ceiling for the bridged flush. The bug costs a fixed 10 s
# (``asyncio.wait_for(..., timeout=10.0)``); anything under this means the
# bridge was never taken.
_BRIDGE_TIMEOUT_SECONDS = 10.0
_FAST_SECONDS = 3.0

# Set by the ``segment_client`` fixture and read from workflow code. The
# workflow reaches this module object unchanged because conftest's
# ``run_worker`` passes ``tests`` through the Temporal sandbox.
_client: SegmentClient | None = None


# ---------------------------------------------------------------------------
# App / Input / Output at module level (see test_core_execution.py header).
# ---------------------------------------------------------------------------


class FlushInput(Input):
    pass


class FlushOutput(Output):
    #: Proves the flush really executed in workflow context — without this the
    #: test could pass because it silently ran somewhere else.
    ran_in_workflow: bool = False
    #: Measured on Temporal's deterministic workflow clock. ``asyncio.wait_for``
    #: inside a workflow is a Temporal *timer*, so the buggy path advances this
    #: by exactly the 10 s timeout while the guarded path leaves it at ~0.
    flush_seconds: float = 0.0


class SegmentFlushApp(App):
    async def run(self, input: FlushInput) -> FlushOutput:
        assert _client is not None, "segment_client fixture did not run"
        started = workflow.now()
        await _client.flush()
        return FlushOutput(
            ran_in_workflow=in_temporal_workflow(),
            flush_seconds=(workflow.now() - started).total_seconds(),
        )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def segment_client() -> Iterator[SegmentClient]:
    """A live ``SegmentClient`` with a real worker thread and event loop.

    ``api_url`` points at a closed local port so nothing reaches Segment from
    CI: any batch the worker does send fails its connection locally, which is
    irrelevant to the bridge under test.
    """
    global _client
    client = SegmentClient(
        write_key="sysapps-328-integration",
        api_url="http://127.0.0.1:1/v1/batch",
    )

    # Non-vacuity guards. ``flush()`` early-returns when the client is disabled
    # or its loop is not yet running, which would make every assertion below
    # pass for the wrong reason. ``_initialized_event`` is set *before*
    # ``run_until_complete`` starts the loop, so poll rather than assume.
    assert client.enabled, "client must be enabled or flush() is a no-op"
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if client._loop is not None and client._loop.is_running():
            break
        time.sleep(0.01)
    assert (
        client._loop is not None and client._loop.is_running()
    ), "worker loop must be running or flush() short-circuits before the bridge"

    _client = client
    try:
        yield client
    finally:
        _client = None
        client.close()


class _RecordCollector(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.NOTSET)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def captured_logs() -> Iterator[_RecordCollector]:
    """Collect every propagated record, including from the Segment worker thread.

    ``concurrent.futures`` logs the failing done-callback on whichever thread
    completes the future, so a thread-local or logger-scoped capture would miss
    it; attach to the root logger instead.
    """
    handler = _RecordCollector()
    root = logging.getLogger()
    previous_level = root.level
    root.setLevel(logging.DEBUG)
    root.addHandler(handler)
    try:
        yield handler
    finally:
        root.removeHandler(handler)
        root.setLevel(previous_level)


def _callback_failures(collector: _RecordCollector) -> list[str]:
    """Records matching the production ERROR (``is_closed`` NotImplementedError)."""
    hits = []
    for record in collector.records:
        message = record.getMessage()
        if "exception calling callback" in message:
            hits.append(message)
        elif record.exc_info and record.exc_info[0] is NotImplementedError:
            hits.append(message)
    return hits


def _flush_timeouts(collector: _RecordCollector) -> list[str]:
    """Records matching the production WARN emitted after the 10 s timeout."""
    return [
        record.getMessage()
        for record in collector.records
        if "Segment queue flush timed out" in record.getMessage()
    ]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_flush_in_workflow_does_not_bridge_or_stall(
    run_worker, executor, reregister_app, segment_client, captured_logs
):
    """SYSAPPS-328: flushing from workflow context is instant and silent.

    Fails with the guard reverted: the workflow reaches ``wrap_future``, the
    done-callback raises ``NotImplementedError`` from ``is_closed()``, and the
    await runs out the full 10 s timeout.
    """
    reregister_app(SegmentFlushApp)
    context = AppContext(app_name=SegmentFlushApp._app_name, app_version="1.0.0")

    started = time.monotonic()
    async with run_worker():
        result = await executor.execute(
            SegmentFlushApp,
            FlushInput(),
            context=context,
            retry_policy=NO_RETRY,
        )
    wall_seconds = time.monotonic() - started

    # Non-vacuity: the flush really did run inside a workflow.
    assert result.ran_in_workflow is True

    # The 10 s bridge timeout was never entered — measured on the workflow
    # clock, which a Temporal timer advances deterministically.
    assert result.flush_seconds < 1.0, (
        f"workflow clock advanced {result.flush_seconds}s during flush(); "
        f"the {_BRIDGE_TIMEOUT_SECONDS}s cross-loop bridge was taken"
    )
    assert wall_seconds < _BRIDGE_TIMEOUT_SECONDS

    # Neither production log line is emitted.
    assert _callback_failures(captured_logs) == []
    assert _flush_timeouts(captured_logs) == []


@pytest.mark.integration
async def test_flush_outside_workflow_still_bridges(segment_client, captured_logs):
    """The guard is scoped to workflow context — ordinary shutdown flushing works.

    This is the path ``main.py``'s SIGTERM handler and ``atexit`` take, and it
    must keep bridging to the worker loop.
    """
    assert in_temporal_workflow() is False

    started = time.monotonic()
    await segment_client.flush()
    elapsed = time.monotonic() - started

    assert (
        elapsed < _FAST_SECONDS
    ), f"non-workflow flush took {elapsed:.1f}s; the bridge should resolve immediately"
    assert _callback_failures(captured_logs) == []
    assert _flush_timeouts(captured_logs) == []
