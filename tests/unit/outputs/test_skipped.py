"""Unit tests for the skipped-record capture path (FND-4881)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from unittest import mock

import orjson
import pytest
from pydantic import BaseModel

import application_sdk.outputs.skipped as skipped
import application_sdk.storage as storage
from application_sdk.outputs import (
    OutputCollector,
    SkipReason,
    current_outputs,
    skip_record,
)
from application_sdk.outputs.skipped import (
    SKIPPED_RECORDS_ARTIFACT,
    SKIPPED_RECORDS_FILE,
    SKIPPED_RECORDS_METRIC,
    SKIPPED_RECORDS_UNPERSISTED_METRIC,
    SkippedRecordSink,
    current_skipped_records,
    merge_run_skips,
    persist_activity_skips,
    reason_metric_name,
)


@pytest.fixture
def collector():
    c = OutputCollector()
    token = current_outputs.set(c)
    yield c
    current_outputs.reset(token)


@pytest.fixture
def sink(tmp_path: Path):
    s = SkippedRecordSink(tmp_path / "activity-1.ndjson")
    token = current_skipped_records.set(s)
    yield s
    current_skipped_records.reset(token)


def _lines(path: Path) -> list[dict]:
    return [orjson.loads(line) for line in path.read_bytes().splitlines()]


class _Row(BaseModel):
    name: str
    schema_name: str | None


class TestSkipRecord:
    async def test_writes_record_reason_and_ids(self, collector, sink):
        await skip_record(
            {"name": "t1", "schema": ""},
            SkipReason.BLANK_IDENTITY,
            record_id="t1",
            entity_type="Table",
            detail="schema is blank",
        )
        await sink.flush()

        assert _lines(sink.path) == [
            {
                "record_id": "t1",
                "entity_type": "Table",
                "reason": "blank_identity",
                "detail": "schema is blank",
                "record": {"name": "t1", "schema": ""},
            }
        ]

    async def test_pydantic_and_driver_types_serialise(self, collector, sink):
        await skip_record(
            _Row(name="t", schema_name=None),
            SkipReason.BLANK_IDENTITY,
            record_id="t",
            entity_type="Table",
        )
        await skip_record(
            {"size": Decimal("1.5"), 7: b"x"},
            SkipReason.MALFORMED_RECORD,
            record_id="r",
            entity_type="Column",
        )
        await sink.flush()

        first, second = _lines(sink.path)
        assert first["record"] == {"name": "t", "schema_name": None}
        assert second["record"] == {"size": "1.5", "7": "b'x'"}

    async def test_metrics_overall_and_per_reason(self, collector, sink):
        for reason in (
            SkipReason.BLANK_IDENTITY,
            SkipReason.BLANK_IDENTITY,
            SkipReason.UNSUPPORTED_TYPE,
        ):
            await skip_record({}, reason, record_id="x", entity_type="Table")

        metrics = collector.to_dict()["metrics"]
        assert metrics[SKIPPED_RECORDS_METRIC] == 3
        assert metrics["skipped-records-blank-identity"] == 2
        assert metrics["skipped-records-unsupported-type"] == 1

    def test_reason_metric_names_are_kebab_case(self):
        assert {reason_metric_name(r) for r in SkipReason} == {
            "skipped-records-blank-identity",
            "skipped-records-missing-required-field",
            "skipped-records-malformed-record",
            "skipped-records-unsupported-type",
        }

    async def test_free_string_reason_is_rejected(self, collector, sink):
        with pytest.raises(TypeError, match="SkipReason"):
            await skip_record({}, "blank", record_id="x", entity_type="Table")  # type: ignore[arg-type]
        assert not collector.has_data()
        assert sink.seen == 0

    async def test_log_names_ids_never_the_record(self, collector, sink):
        with mock.patch.object(skipped, "_logger") as logger:
            await skip_record(
                {"secret_column": "customer-value"},
                SkipReason.BLANK_IDENTITY,
                record_id="t1",
                entity_type="Table",
                detail="customer-value detail",
            )

        logger.warning.assert_called_once_with(
            "Skipped record %s (%s): %s", "t1", "Table", "blank_identity"
        )
        assert "customer-value" not in repr(logger.mock_calls)

    async def test_cap_bounds_file_and_logs_but_not_counts(self, collector, sink):
        with (
            mock.patch.object(skipped, "MAX_SKIPPED_RECORDS", 3),
            mock.patch.object(skipped, "_logger") as logger,
        ):
            for i in range(6):
                await skip_record(
                    {"i": i},
                    SkipReason.MALFORMED_RECORD,
                    record_id=str(i),
                    entity_type="Table",
                )
            await sink.flush()

        assert [line["record_id"] for line in _lines(sink.path)] == ["0", "1", "2"]
        assert (sink.seen, sink.kept) == (6, 3)
        assert collector.to_dict()["metrics"][SKIPPED_RECORDS_METRIC] == 6
        # 3 per-record warnings + one overflow notice, never one per skip.
        assert logger.warning.call_count == 4

    async def test_file_writes_go_off_the_event_loop(self, collector, sink):
        with (
            mock.patch.object(skipped, "_FLUSH_BYTES", 1),
            mock.patch(
                "application_sdk._runtime.offload.run_in_thread",
                new_callable=mock.AsyncMock,
            ) as run_in_thread,
        ):
            await skip_record(
                {}, SkipReason.BLANK_IDENTITY, record_id="x", entity_type="T"
            )

        run_in_thread.assert_awaited_once()
        assert run_in_thread.await_args.args[0] is skipped._append_lines

    async def test_local_write_failure_fails_open(self, collector, sink):
        """A full scratch disk never fails the activity (lens F-7f3f33)."""
        with (
            mock.patch.object(skipped, "_FLUSH_BYTES", 1),
            mock.patch(
                "application_sdk._runtime.offload.run_in_thread",
                new_callable=mock.AsyncMock,
                side_effect=OSError("No space left on device"),
            ),
            mock.patch.object(skipped, "_logger") as logger,
        ):
            await skip_record(
                {}, SkipReason.BLANK_IDENTITY, record_id="x", entity_type="T"
            )

        logger.error.assert_called_once()
        metrics = collector.to_dict()["metrics"]
        assert metrics[SKIPPED_RECORDS_METRIC] == 1
        assert metrics[SKIPPED_RECORDS_UNPERSISTED_METRIC] == 1
        assert (sink.seen, sink.kept) == (1, 0)

    async def test_no_skips_writes_nothing(self, collector, sink):
        await sink.flush()
        assert not sink.path.exists()
        assert not collector.has_data()


class TestPersistActivitySkips:
    @pytest.fixture(autouse=True)
    def _run_prefix(self):
        with mock.patch(
            "application_sdk.execution._temporal.activity_utils.build_output_path",
            return_value="artifacts/apps/a/workflows/wf/run",
        ):
            yield

    async def test_uploads_part_under_run_prefix(self, collector, sink):
        await skip_record({}, SkipReason.BLANK_IDENTITY, record_id="x", entity_type="T")
        with mock.patch.object(
            storage, "upload_file", new_callable=mock.AsyncMock
        ) as up:
            await persist_activity_skips(sink)

        up.assert_awaited_once_with(
            "artifacts/apps/a/workflows/wf/run/skipped-records/parts/activity-1.ndjson",
            sink.path,
        )
        assert sink.path.exists()

    async def test_nothing_kept_uploads_nothing(self, collector, sink):
        with mock.patch.object(
            storage, "upload_file", new_callable=mock.AsyncMock
        ) as up:
            await persist_activity_skips(sink)
        up.assert_not_awaited()

    async def test_upload_failure_fails_open_and_is_counted(self, collector, sink):
        for _ in range(2):
            await skip_record(
                {}, SkipReason.BLANK_IDENTITY, record_id="x", entity_type="T"
            )
        with (
            mock.patch.object(
                storage, "upload_file", new_callable=mock.AsyncMock, side_effect=OSError
            ),
            mock.patch.object(skipped, "_logger") as logger,
        ):
            await persist_activity_skips(sink)

        logger.error.assert_called_once()
        assert collector.to_dict()["metrics"][SKIPPED_RECORDS_UNPERSISTED_METRIC] == 2


class _PartialWriter:
    """A file whose write lands half the bytes, then fails like a full disk."""

    def __init__(self, fh, *, truncate_fails: bool = False):
        self._fh = fh
        self._truncate_fails = truncate_fails

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._fh.close()

    def tell(self):
        return self._fh.tell()

    def write(self, data):
        self._fh.write(data[: len(data) // 2])
        self._fh.flush()
        raise OSError(28, "No space left on device")

    def flush(self):
        self._fh.flush()

    def truncate(self, size):
        if self._truncate_fails:
            raise OSError(5, "I/O error")
        return self._fh.truncate(size)


class TestPartialAppend:
    """A failed append never leaves a partial NDJSON line (lens F-41d425)."""

    @staticmethod
    def _open_partial(path: Path, *, truncate_fails: bool = False):
        real_open = Path.open

        def fake_open(self, mode="r", *args, **kwargs):
            fh = real_open(self, mode, *args, **kwargs)
            if self == path and "a" in mode:
                return _PartialWriter(fh, truncate_fails=truncate_fails)
            return fh

        return mock.patch.object(Path, "open", fake_open)

    def test_failed_append_is_rolled_back(self, tmp_path):
        path = tmp_path / "part.ndjson"
        path.write_bytes(b'{"record_id":"a"}\n')
        with self._open_partial(path), pytest.raises(OSError):
            skipped._append_lines(path, b'{"record_id":"b"}\n')

        assert path.read_bytes() == b'{"record_id":"a"}\n'

    def test_unrollbackable_append_is_flagged(self, tmp_path):
        path = tmp_path / "part.ndjson"
        path.write_bytes(b'{"record_id":"a"}\n')
        with (
            self._open_partial(path, truncate_fails=True),
            pytest.raises(skipped._RollbackFailedError),
        ):
            skipped._append_lines(path, b'{"record_id":"b"}\n')

    async def test_partial_write_keeps_earlier_lines_valid(self, collector, sink):
        await skip_record({}, SkipReason.BLANK_IDENTITY, record_id="a", entity_type="T")
        await sink.flush()
        await skip_record({}, SkipReason.BLANK_IDENTITY, record_id="b", entity_type="T")
        with self._open_partial(sink.path), mock.patch.object(skipped, "_logger"):
            await sink.flush()

        assert [line["record_id"] for line in _lines(sink.path)] == ["a"]
        assert sink.kept == 1
        assert collector.numeric_metric(SKIPPED_RECORDS_UNPERSISTED_METRIC) == 1

    async def test_failed_rollback_drops_the_whole_file(self, collector, sink):
        await skip_record({}, SkipReason.BLANK_IDENTITY, record_id="a", entity_type="T")
        await sink.flush()
        await skip_record({}, SkipReason.BLANK_IDENTITY, record_id="b", entity_type="T")
        with (
            self._open_partial(sink.path, truncate_fails=True),
            mock.patch.object(skipped, "_logger"),
        ):
            await sink.flush()
        # Later skips are counted, never written to the untrusted file.
        await skip_record({}, SkipReason.BLANK_IDENTITY, record_id="c", entity_type="T")
        with mock.patch.object(
            storage, "upload_file", new_callable=mock.AsyncMock
        ) as up:
            await persist_activity_skips(sink)

        up.assert_not_awaited()
        assert sink.kept == 0
        assert collector.numeric_metric(SKIPPED_RECORDS_METRIC) == 3
        assert collector.numeric_metric(SKIPPED_RECORDS_UNPERSISTED_METRIC) == 3


class TestRetriedActivity:
    """A retry never inherits a failed attempt's lines (lens F-53be14)."""

    @staticmethod
    def _sink_for_attempt(attempt: int, scratch: Path) -> SkippedRecordSink:
        info = mock.MagicMock(activity_id="7", attempt=attempt)
        with (
            mock.patch("temporalio.activity.in_activity", return_value=True),
            mock.patch("temporalio.activity.info", return_value=info),
            mock.patch(
                "application_sdk.execution._temporal.activity_utils.run_temp_dir",
                return_value=str(scratch),
            ),
        ):
            return skipped._new_sink()

    async def test_each_attempt_writes_its_own_file_to_one_part(
        self, collector, tmp_path
    ):
        first = self._sink_for_attempt(1, tmp_path)
        token = current_skipped_records.set(first)
        try:
            await skip_record(
                {}, SkipReason.BLANK_IDENTITY, record_id="a", entity_type="T"
            )
            await first.flush()  # attempt 1 then fails before upload
        finally:
            current_skipped_records.reset(token)

        second = self._sink_for_attempt(2, tmp_path)
        token = current_skipped_records.set(second)
        try:
            await skip_record(
                {}, SkipReason.BLANK_IDENTITY, record_id="b", entity_type="T"
            )
            with (
                mock.patch(
                    "application_sdk.execution._temporal.activity_utils.build_output_path",
                    return_value="artifacts/run",
                ),
                mock.patch.object(
                    storage, "upload_file", new_callable=mock.AsyncMock
                ) as up,
            ):
                await persist_activity_skips(second)
        finally:
            current_skipped_records.reset(token)

        assert first.path != second.path
        up.assert_awaited_once_with(
            "artifacts/run/skipped-records/parts/7.ndjson", second.path
        )
        assert [line["record_id"] for line in _lines(second.path)] == ["b"]


class TestMergeRunSkips:
    PREFIX = "artifacts/apps/a/workflows/wf/run"

    def _store(self, parts: dict[str, bytes], uploads: dict[str, bytes]):
        async def list_data_keys(prefix):
            return [k for k in parts if k.startswith(prefix)]

        async def download_file(key, local_path):
            Path(local_path).write_bytes(parts[key])

        async def upload_file(key, local_path):
            uploads[key] = Path(local_path).read_bytes()

        return mock.patch.multiple(
            storage,
            list_data_keys=list_data_keys,
            download_file=download_file,
            upload_file=upload_file,
        )

    async def test_merges_parts_into_one_artifact(self, collector, tmp_path):
        parts_dir = f"{self.PREFIX}/skipped-records/parts"
        parts = {
            f"{parts_dir}/b.ndjson": b'{"record_id":"3"}\n',
            f"{parts_dir}/a.ndjson": b'{"record_id":"1"}\n{"record_id":"2"}\n',
        }
        uploads: dict[str, bytes] = {}
        with self._store(parts, uploads):
            result = await merge_run_skips(self.PREFIX, str(tmp_path))

        key = f"{self.PREFIX}/{SKIPPED_RECORDS_FILE}"
        assert result.key == key and result.kept == 3
        assert [orjson.loads(x)["record_id"] for x in uploads[key].splitlines()] == [
            "1",
            "2",
            "3",
        ]
        assert collector.to_dict()["artifacts"] == {
            SKIPPED_RECORDS_ARTIFACT: {"path": key, "display_name": "Skipped records"}
        }

    async def test_merge_is_capped(self, collector, tmp_path):
        parts_dir = f"{self.PREFIX}/skipped-records/parts"
        parts = {
            f"{parts_dir}/a.ndjson": b"1\n2\n",
            f"{parts_dir}/b.ndjson": b"3\n4\n",
        }
        uploads: dict[str, bytes] = {}
        with (
            self._store(parts, uploads),
            mock.patch.object(skipped, "MAX_SKIPPED_RECORDS", 3),
        ):
            result = await merge_run_skips(self.PREFIX, str(tmp_path))

        assert result.kept == 3
        assert uploads[result.key] == b"1\n2\n3\n"

    async def test_no_parts_means_no_artifact(self, collector, tmp_path):
        uploads: dict[str, bytes] = {}
        with self._store({}, uploads):
            result = await merge_run_skips(self.PREFIX, str(tmp_path))

        assert result.key is None
        assert uploads == {}
        assert not collector.has_data()
