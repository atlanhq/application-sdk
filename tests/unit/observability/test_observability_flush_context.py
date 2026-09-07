"""The store buffer must never lose records to a flush it cannot perform.

Two callers hand records off the buffer: ``add_record`` when the batch or
interval trigger fires, and ``_flush_buffer``. Both used to swap the buffer out
first and decide afterwards whether the flush could run. Inside a Temporal
workflow loop ``_flush_records`` refuses the work, and in a thread with no
running loop the flush task cannot be scheduled at all; either way the swapped
batch was gone. Reproduced on a live worker: the preflight gate's ``blocked``
row and every row the activity emitted around it vanished from the object store
while the rows before and after survived, because a workflow-side log call
tripped the interval trigger on the shared buffer.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any
from unittest import mock

import pytest

from application_sdk.observability.observability import AtlanObservability

_OBS = "application_sdk.observability.observability"


class _Obs(AtlanObservability):
    def process_record(self, record: Any) -> dict[str, Any]:
        return record

    def export_record(self, record: Any) -> None:
        pass


@pytest.fixture
def obs(tmp_path):
    AtlanObservability._reset_for_testing()
    inst = _Obs(
        batch_size=2,
        flush_interval=60,
        retention_days=7,
        cleanup_enabled=False,
        data_dir=str(tmp_path),
        file_name="logs",
    )
    yield inst
    AtlanObservability._reset_for_testing()


def _rec(i: int) -> dict[str, Any]:
    return {"timestamp": 1.0 + i, "message": f"m-{i}"}


def _messages(records: list[dict[str, Any]]) -> list[str]:
    return [r["message"] for r in records]


class TestNoRecordIsLostToAnUnrunnableFlush:
    async def test_batch_trigger_inside_a_workflow_keeps_the_records(self, obs) -> None:
        with (
            mock.patch(f"{_OBS}.in_temporal_workflow", return_value=True),
            mock.patch.object(obs, "_flush_records", mock.AsyncMock()) as flush,
        ):
            for i in range(3):
                obs.add_record(_rec(i))
            await asyncio.sleep(0)
        flush.assert_not_awaited()
        assert _messages(obs._buffer) == ["m-0", "m-1", "m-2"]

    async def test_flush_buffer_inside_a_workflow_keeps_the_records(self, obs) -> None:
        obs._buffer.extend([_rec(0), _rec(1)])
        with (
            mock.patch(f"{_OBS}.in_temporal_workflow", return_value=True),
            mock.patch.object(obs, "_flush_records", mock.AsyncMock()) as flush,
        ):
            await obs._flush_buffer(force=True)
        flush.assert_not_awaited()
        assert _messages(obs._buffer) == ["m-0", "m-1"]

    async def test_records_deferred_in_a_workflow_flush_from_the_worker_loop(
        self, obs
    ) -> None:
        flushed: list[str] = []

        async def _capture(records):
            flushed.extend(_messages(records))

        with mock.patch.object(obs, "_flush_records", _capture):
            with mock.patch(f"{_OBS}.in_temporal_workflow", return_value=True):
                obs.add_record(_rec(0))
                obs.add_record(_rec(1))
                await obs._flush_buffer(force=True)
            await obs._flush_buffer(force=True)
        assert flushed == ["m-0", "m-1"]

    def test_batch_trigger_without_a_running_loop_keeps_the_records(self, obs) -> None:
        errors: list[BaseException] = []

        def _from_a_plain_thread() -> None:
            try:
                obs.add_record(_rec(0))
                obs.add_record(_rec(1))
            except BaseException as exc:
                errors.append(exc)

        worker = threading.Thread(target=_from_a_plain_thread)
        worker.start()
        worker.join()
        assert errors == []
        assert _messages(obs._buffer) == ["m-0", "m-1"]

    async def test_deferring_does_not_reset_the_interval(self, obs) -> None:
        obs._last_flush_time = 0.0
        with mock.patch(f"{_OBS}.in_temporal_workflow", return_value=True):
            obs.add_record(_rec(0))
        assert obs._last_flush_time == 0.0

    async def test_normal_trigger_still_flushes_exactly_once(self, obs) -> None:
        batches: list[list[str]] = []

        async def _capture(records):
            batches.append(_messages(records))

        with mock.patch.object(obs, "_flush_records", _capture):
            obs.add_record(_rec(0))
            obs.add_record(_rec(1))
            await asyncio.sleep(0)
        assert batches == [["m-0", "m-1"]]
        assert obs._buffer == []


class TestThePredicateNeverImportsTemporal:
    """``in_temporal_workflow`` runs on every buffered record, so it must not import.

    The worker's first records are logged while temporalio is still being
    imported on the main thread; an import from the flush thread at that moment
    raced it and crashed the boot on a partially initialised module.
    """

    def test_false_and_no_import_when_temporal_is_not_loaded(self) -> None:
        import sys

        from application_sdk.observability.utils import in_temporal_workflow

        with mock.patch.dict(sys.modules, {"temporalio.workflow": None}):
            assert in_temporal_workflow() is False

    def test_false_on_a_partially_initialised_module(self) -> None:
        import sys
        import types

        from application_sdk.observability.utils import in_temporal_workflow

        half_loaded = types.ModuleType("temporalio.workflow")
        with mock.patch.dict(sys.modules, {"temporalio.workflow": half_loaded}):
            assert in_temporal_workflow() is False

    def test_reads_the_loaded_module(self) -> None:
        import temporalio.workflow

        from application_sdk.observability.utils import in_temporal_workflow

        with mock.patch.object(temporalio.workflow, "in_workflow", return_value=True):
            assert in_temporal_workflow() is True
