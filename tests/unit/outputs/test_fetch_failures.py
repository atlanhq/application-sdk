"""Unit tests for the fetch-failure capture path (FND-5294)."""

from __future__ import annotations

import asyncio
import contextvars
import threading
from pathlib import Path
from unittest import mock

import orjson
import pytest

import application_sdk.outputs.fetch_failures as fetch_failures
import application_sdk.storage as storage
from application_sdk._runtime.offload import run_in_thread
from application_sdk.outputs import (
    MAX_SKIPPED_RECORDS,
    FetchFailureReason,
    OutputCollector,
    current_outputs,
    record_fetch_failure,
)
from application_sdk.outputs.fetch_failures import (
    FETCH_FAILURES_LAYOUT,
    FETCH_FAILURES_METRIC,
    FETCH_FAILURES_UNPERSISTED_METRIC,
    FetchFailureSink,
    current_fetch_failures,
    persist_activity_fetch_failures,
    reason_metric_name,
)
from application_sdk.outputs.skipped import merge_run_log

_RUN_PREFIX = "artifacts/apps/a/workflows/wf/run"


@pytest.fixture
def collector():
    c = OutputCollector()
    token = current_outputs.set(c)
    yield c
    current_outputs.reset(token)


@pytest.fixture
def sink():
    s = FetchFailureSink()
    token = current_fetch_failures.set(s)
    yield s
    current_fetch_failures.reset(token)


@pytest.fixture
def store(tmp_path: Path):
    """An object-store double plus a fixed run prefix and part location."""
    objects: dict[str, bytes] = {}

    async def upload_file(key, local_path):
        objects[key] = Path(local_path).read_bytes()

    async def list_data_keys(prefix):
        return sorted(k for k in objects if k.startswith(prefix))

    async def download_file(key, local_path):
        Path(local_path).write_bytes(objects[key])

    with (
        mock.patch.multiple(
            storage,
            upload_file=upload_file,
            list_data_keys=list_data_keys,
            download_file=download_file,
        ),
        mock.patch(
            "application_sdk.execution._temporal.activity_utils.build_output_path",
            return_value=_RUN_PREFIX,
        ),
        mock.patch.object(
            fetch_failures,
            "_part_location",
            return_value=(tmp_path / "local" / "act-1.ndjson", "act-1.ndjson"),
        ),
    ):
        yield objects


_PART_KEY = f"{_RUN_PREFIX}/fetch-failures/parts/act-1.ndjson"


def _lines(raw: bytes) -> list[dict]:
    return [orjson.loads(line) for line in raw.splitlines()]


def _record(i: int = 0, reason=FetchFailureReason.NOT_FOUND) -> None:
    record_fetch_failure(
        reason,
        entity_type="Report",
        record_id=f"r{i}",
        endpoint="/reports/{id}",
        http_status=404,
        detail="gone",
    )


class TestRecordFetchFailure:
    async def test_line_shape_reaches_the_part(self, collector, sink, store):
        _record(1)
        await persist_activity_fetch_failures(sink)

        assert _lines(store[_PART_KEY]) == [
            {
                "record_id": "r1",
                "entity_type": "Report",
                "reason": "not_found",
                "endpoint": "/reports/{id}",
                "http_status": 404,
                "detail": "gone",
            }
        ]

    async def test_metrics_land_when_the_activity_returns(self, collector, sink, store):
        _record(1)
        _record(2)
        _record(3, FetchFailureReason.RATE_LIMITED)
        # Nothing touches the collector until persist: it is not thread-safe.
        assert collector.to_dict() == {}

        await persist_activity_fetch_failures(sink)

        assert collector.to_dict()["metrics"] == {
            FETCH_FAILURES_METRIC: 3,
            "fetch-failures-not-found": 2,
            "fetch-failures-rate-limited": 1,
        }

    def test_reason_metric_names_are_kebab_case(self):
        assert {reason_metric_name(r) for r in FetchFailureReason} == {
            "fetch-failures-source-unavailable",
            "fetch-failures-not-found",
            "fetch-failures-forbidden",
            "fetch-failures-rate-limited",
        }

    def test_free_string_reason_is_rejected(self, sink):
        with pytest.raises(TypeError, match="FetchFailureReason"):
            record_fetch_failure("not_found", entity_type="R", record_id="x")  # type: ignore[arg-type]
        assert sink.seen == 0

    def test_log_names_ids_never_detail_or_endpoint(self, sink):
        with mock.patch.object(fetch_failures, "_logger") as logger:
            record_fetch_failure(
                FetchFailureReason.FORBIDDEN,
                entity_type="Report",
                record_id="r1",
                endpoint="/secret-endpoint",
                detail="sensitive-detail",
            )
        (call,) = logger.warning.call_args_list
        rendered = repr(call)
        assert "r1" in rendered and "Report" in rendered and "forbidden" in rendered
        assert "sensitive-detail" not in rendered
        assert "/secret-endpoint" not in rendered

    async def test_cap_bounds_lines_and_logs_but_not_counts(
        self, collector, sink, store
    ):
        with (
            mock.patch.object(fetch_failures, "MAX_SKIPPED_RECORDS", 2),
            mock.patch.object(fetch_failures, "_logger") as logger,
        ):
            for i in range(5):
                _record(i)
            await persist_activity_fetch_failures(sink)

        assert len(_lines(store[_PART_KEY])) == 2
        assert collector.to_dict()["metrics"][FETCH_FAILURES_METRIC] == 5
        # Two per-failure warnings, then one "more than" notice.
        assert logger.warning.call_count == 3

    async def test_nothing_recorded_means_nothing_uploaded(
        self, collector, sink, store
    ):
        await persist_activity_fetch_failures(sink)
        assert store == {}
        assert collector.to_dict() == {}

    async def test_record_after_close_is_dropped_and_logged(
        self, collector, sink, store
    ):
        await persist_activity_fetch_failures(sink)
        with mock.patch.object(fetch_failures, "_logger") as logger:
            _record(9)
        assert "after its activity returned" in logger.warning.call_args.args[0]
        assert sink.seen == 0


class TestSharedAcrossTasksAndThreads:
    """The acceptance case: one activity sink reached from every place a fetch runs."""

    async def test_gather_child_tasks_share_the_activity_sink(
        self, collector, sink, store
    ):
        async def fetch(i: int) -> int | None:
            await asyncio.sleep(0)
            if i % 2:
                _record(i)
                return None
            return i

        results = await asyncio.gather(*(fetch(i) for i in range(10)))

        assert [r for r in results if r is not None] == [0, 2, 4, 6, 8]
        await persist_activity_fetch_failures(sink)
        assert collector.to_dict()["metrics"][FETCH_FAILURES_METRIC] == 5
        assert sorted(line["record_id"] for line in _lines(store[_PART_KEY])) == [
            "r1",
            "r3",
            "r5",
            "r7",
            "r9",
        ]

    async def test_task_group_child_tasks_share_the_activity_sink(
        self, collector, sink, store
    ):
        async def fetch(i: int) -> None:
            _record(i, FetchFailureReason.SOURCE_UNAVAILABLE)

        async with asyncio.TaskGroup() as tg:
            for i in range(3):
                tg.create_task(fetch(i))

        await persist_activity_fetch_failures(sink)
        assert collector.to_dict()["metrics"]["fetch-failures-source-unavailable"] == 3

    async def test_sync_function_in_run_in_thread_records_into_the_sink(
        self, collector, sink, store
    ):
        recorded_on: list[int] = []

        def blocking_fetch(i: int) -> None:
            recorded_on.append(threading.get_ident())
            _record(i, FetchFailureReason.FORBIDDEN)

        await asyncio.gather(*(run_in_thread(blocking_fetch, i) for i in range(4)))

        assert threading.get_ident() not in recorded_on
        await persist_activity_fetch_failures(sink)
        assert collector.to_dict()["metrics"] == {
            FETCH_FAILURES_METRIC: 4,
            "fetch-failures-forbidden": 4,
        }
        assert len(_lines(store[_PART_KEY])) == 4

    def test_concurrent_threads_lose_no_counts(self, sink):
        per_thread = 500
        barrier = threading.Barrier(8)

        def worker() -> None:
            barrier.wait()
            for i in range(per_thread):
                _record(i)

        with mock.patch.object(fetch_failures, "_logger"):
            # Each thread runs in its own copy of the context, as run_in_thread
            # does, so all eight see the one activity sink.
            threads = [
                threading.Thread(target=contextvars.copy_context().run, args=(worker,))
                for _ in range(8)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        snapshot = sink.close()
        assert snapshot.seen == 8 * per_thread
        assert snapshot.by_reason == {FetchFailureReason.NOT_FOUND: 8 * per_thread}
        assert len(snapshot.lines) == min(8 * per_thread, MAX_SKIPPED_RECORDS)


class TestPersistFailsOpen:
    async def test_upload_failure_is_logged_and_counted(
        self, collector, sink, tmp_path
    ):
        _record(1)
        _record(2)
        with (
            mock.patch(
                "application_sdk.execution._temporal.activity_utils.build_output_path",
                return_value=_RUN_PREFIX,
            ),
            mock.patch.object(
                fetch_failures,
                "_part_location",
                return_value=(tmp_path / "a.ndjson", "a.ndjson"),
            ),
            mock.patch.object(
                storage, "upload_file", new_callable=mock.AsyncMock, side_effect=OSError
            ),
            mock.patch.object(fetch_failures, "_logger") as logger,
        ):
            await persist_activity_fetch_failures(sink)

        logger.error.assert_called_once()
        metrics = collector.to_dict()["metrics"]
        # The counts still land; the lost lines are accounted for.
        assert metrics[FETCH_FAILURES_METRIC] == 2
        assert metrics[FETCH_FAILURES_UNPERSISTED_METRIC] == 2


class TestMerge:
    async def test_parts_merge_into_one_artifact(self, collector, store, tmp_path):
        store[f"{_RUN_PREFIX}/fetch-failures/parts/a.ndjson"] = b'{"n":1}\n'
        store[f"{_RUN_PREFIX}/fetch-failures/parts/b.ndjson"] = b'{"n":2}\n{"n":3}\n'

        result = await merge_run_log(_RUN_PREFIX, str(tmp_path), FETCH_FAILURES_LAYOUT)

        key = f"{_RUN_PREFIX}/fetch-failures.ndjson"
        assert (result.key, result.kept) == (key, 3)
        assert _lines(store[key]) == [{"n": 1}, {"n": 2}, {"n": 3}]
        assert collector.to_dict()["artifacts"] == {
            "fetch-failures": {"path": key, "display_name": "Fetch failures"}
        }

    async def test_no_parts_adds_no_artifact(self, collector, store, tmp_path):
        result = await merge_run_log(_RUN_PREFIX, str(tmp_path), FETCH_FAILURES_LAYOUT)
        assert result.key is None
        assert collector.to_dict() == {}
