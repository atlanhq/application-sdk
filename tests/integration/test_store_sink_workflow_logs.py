"""Workflow-side log lines must reach the object-store log export (FND-1936).

The object-store sink cannot write from Temporal's workflow loop, so
``AtlanObservability._flush_records`` refuses to there. It used to refuse by
returning — after its caller had already swapped the batch out of the buffer.
Any flush that happened to start in a workflow (``add_record`` crossing the
batch size or flush interval on a workflow-side log call, or ``flush_all()``
from ``on_complete``) silently discarded every record in that batch: the
workflow's own lines *and* whatever activities on the same worker had logged
since the last flush. ``App started`` — the line that carries the build
identity into a run's exported logs — was routinely among them.

This runs a real worker so the records come from the real workflow and
activity loggers. ``batch_size=1`` makes every log call start a flush, so each
workflow-side line deterministically takes the path that used to drop it; the
activity lines are the positive control, since they flush from the worker loop
either way.

Requires a running Temporal dev server (see conftest.py).
"""

from __future__ import annotations

import gzip
import os
from collections.abc import Iterator

import orjson
import pytest

from application_sdk.app.base import App
from application_sdk.app.context import AppContext
from application_sdk.app.task import task
from application_sdk.contracts.base import Input, Output
from application_sdk.execution.retry import NO_RETRY
from application_sdk.observability.logger_adaptor import AtlanLoggerAdapter, get_logger
from application_sdk.observability.observability import AtlanObservability

# ---------------------------------------------------------------------------
# App / Input / Output at module level (see test_core_execution.py header).
# ---------------------------------------------------------------------------


class SinkInput(Input):
    value: int = 0


class SinkOutput(Output):
    result: int = 0


class StoreSinkApp(App):
    @task
    async def add_one(self, input: SinkInput) -> SinkOutput:
        return SinkOutput(result=input.value + 1)

    async def run(self, input: SinkInput) -> SinkOutput:
        return await self.add_one(input)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def written_messages(monkeypatch) -> Iterator[list[str]]:
    """Messages of every log record the object-store sink actually writes.

    Integration runs switch the store sink off at import, so this wires one
    adapter's sink onto loguru by hand and captures its finished ``.json.gz``
    partitions at the upload step, which is the last point before the bytes
    leave the process.
    """
    written: list[str] = []
    sink = get_logger(__name__)
    assert isinstance(sink, AtlanLoggerAdapter)

    async def _capture(local_path: str, remote_key: str) -> None:
        try:
            with gzip.open(local_path, "rb") as f:
                written.extend(orjson.loads(line)["message"] for line in f)
        finally:
            os.unlink(local_path)

    monkeypatch.setattr(
        "application_sdk.observability.observability.ENABLE_OBSERVABILITY_STORE_SINK",
        True,
    )
    monkeypatch.setattr(sink, "_batch_size", 1)
    monkeypatch.setattr(sink, "_upload_and_delete", _capture)
    handler_id = sink.logger.add(sink.objectstore_sink)
    try:
        yield written
    finally:
        sink.logger.remove(handler_id)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_workflow_log_lines_reach_the_store_sink(
    run_worker, executor, reregister_app, written_messages
):
    """Every lifecycle line is written, whichever loop it was logged on."""
    reregister_app(StoreSinkApp)
    context = AppContext(app_name=StoreSinkApp._app_name, app_version="1.0.0")
    async with run_worker():
        result = await executor.execute(
            StoreSinkApp, SinkInput(value=1), context=context, retry_policy=NO_RETRY
        )
    assert result.result == 2

    # What a workflow-side flush handed back sits in the buffer; this is the
    # worker-loop flush the periodic task would otherwise perform.
    await AtlanObservability.flush_all()

    def written(prefix: str) -> list[str]:
        return [m for m in written_messages if m.startswith(prefix)]

    # Positive control: activity lines are logged off the workflow loop.
    assert written("activity.started"), written_messages
    assert written("activity.ended"), written_messages

    # Workflow-side lines, each of which started a flush on the workflow loop.
    assert written("workflow.started"), written_messages
    assert written("workflow.ended"), written_messages
    assert [m for m in written("App started") if "sdk=" in m], written_messages
    assert [m for m in written("App completed") if "sdk=" in m], written_messages
