"""Tests for observability sinks: PartitionedJsonGzSink and the shared
_write_and_upload_json_gz primitive.

Design principle: each sink is tested directly (not via AtlanObservability),
which keeps tests fast, focused, and free of observability-instance boilerplate.
Integration of sink composition is covered by TestFlushRecordsSinkDispatch.
"""

from __future__ import annotations

import gzip
import os
from unittest.mock import AsyncMock, patch

import orjson
import pytest

from application_sdk.observability.sinks import (
    OBSERVABILITY_S3_PREFIX,
    ObservabilityRecordType,
    PartitionedJsonGzSink,
    _write_and_upload_json_gz,
    file_name_to_type,
)

# ---------------------------------------------------------------------------
# Constants patched for all tests in this module
# ---------------------------------------------------------------------------
_MODULE = "application_sdk.observability.sinks"
_DEPLOYMENT = "test-dep"
_APP = "test-app"

# 2024-03-07 23:30:00 UTC
_TS_NIGHT = 1709854200.0
# 2024-03-08 00:30:00 UTC  (one hour later — different hour bucket)
_TS_NEXT_HOUR = 1709857800.0


@pytest.fixture(autouse=True)
def patch_name_constants(monkeypatch):
    """Pin DEPLOYMENT_NAME / APPLICATION_NAME for deterministic filenames."""
    monkeypatch.setattr(f"{_MODULE}.DEPLOYMENT_NAME", _DEPLOYMENT)
    monkeypatch.setattr(f"{_MODULE}.APPLICATION_NAME", _APP)


@pytest.fixture
def log_record():
    return {
        "timestamp": _TS_NIGHT,
        "level": "INFO",
        "message": "hello",
        "logger_name": "test.logger",
        "extra": {
            "trace_id": "t-123",
            "span_id": "s-456",
            "exception_type": "",
            "exception_message": "",
            "exception_stacktrace": "",
        },
    }


@pytest.fixture
def two_records_different_hours(log_record):
    r2 = dict(log_record, timestamp=_TS_NEXT_HOUR, message="next hour")
    return [log_record, r2]


# ---------------------------------------------------------------------------
# _write_and_upload_json_gz
# ---------------------------------------------------------------------------


class TestWriteAndUploadJsonGz:
    @pytest.mark.asyncio
    async def test_writes_valid_ndjson_gzip_and_cleans_up(self, tmp_path):
        records = [{"a": 1}, {"b": "two"}]
        local_path = str(tmp_path / "out.json.gz")

        # Prevent deletion so we can inspect the file content.
        with patch(f"{_MODULE}.os.unlink") as mock_unlink:
            with patch(
                "application_sdk.services.objectstore.ObjectStore.upload_file",
                new_callable=AsyncMock,
            ):
                await _write_and_upload_json_gz(
                    records, local_path, "remote/key", ["store"]
                )

        mock_unlink.assert_called_once_with(local_path)

        with gzip.open(local_path, "rb") as f:
            lines = [line for line in f.read().splitlines() if line]
        assert [orjson.loads(line) for line in lines] == records

    @pytest.mark.asyncio
    async def test_file_deleted_even_on_upload_failure(self, tmp_path):
        local_path = str(tmp_path / "out.json.gz")
        with patch(
            "application_sdk.services.objectstore.ObjectStore.upload_file",
            new_callable=AsyncMock,
            side_effect=RuntimeError("S3 down"),
        ):
            with pytest.raises(RuntimeError, match="S3 down"):
                await _write_and_upload_json_gz(
                    [{"x": 1}], local_path, "key", ["store"]
                )

        assert not os.path.exists(local_path)

    @pytest.mark.asyncio
    async def test_uploads_to_every_store_name(self, tmp_path):
        local_path = str(tmp_path / "out.json.gz")
        with patch(
            "application_sdk.services.objectstore.ObjectStore.upload_file",
            new_callable=AsyncMock,
        ) as mock_upload:
            await _write_and_upload_json_gz(
                [{"x": 1}], local_path, "key", ["store-a", "store-b"]
            )

        stores = {c.kwargs["store_name"] for c in mock_upload.call_args_list}
        assert stores == {"store-a", "store-b"}
        assert mock_upload.call_count == 2

    @pytest.mark.asyncio
    async def test_uses_binary_mode_no_decode_roundtrip(self, tmp_path):
        """Verify bytes are written directly (wb), not via text-mode decode."""
        local_path = str(tmp_path / "out.json.gz")
        with patch(f"{_MODULE}.gzip.open", wraps=gzip.open) as mock_gzip:
            with patch(
                "application_sdk.services.objectstore.ObjectStore.upload_file",
                new_callable=AsyncMock,
            ):
                await _write_and_upload_json_gz(
                    [{"a": 1}], local_path, "key", ["store"]
                )
        mock_gzip.assert_called_once()
        assert mock_gzip.call_args[0][1] == "wb"


# ---------------------------------------------------------------------------
# PartitionedJsonGzSink
# ---------------------------------------------------------------------------


class TestPartitionedJsonGzSink:
    @pytest.fixture
    def sink(self, tmp_path):
        return PartitionedJsonGzSink(
            record_type=ObservabilityRecordType.LOGS,
            staging_root=str(tmp_path),
            store_names=["primary-store"],
        )

    @pytest.mark.asyncio
    async def test_empty_records_skips_upload(self, sink):
        with patch(f"{_MODULE}._write_and_upload_json_gz", new_callable=AsyncMock) as m:
            await sink.flush([])
        m.assert_not_called()

    @pytest.mark.asyncio
    async def test_partition_path_uses_sdr_logs_prefix(self, sink, log_record):
        """Verify path uses artifacts/apps/observability/sdr-logs/..."""
        with patch(f"{_MODULE}._write_and_upload_json_gz", new_callable=AsyncMock) as m:
            await sink.flush([log_record])

        local_path = m.call_args[0][1]
        assert OBSERVABILITY_S3_PREFIX in local_path
        assert "sdr-logs" in local_path

    @pytest.mark.asyncio
    async def test_partition_uses_record_timestamp_with_hour(self, sink, log_record):
        """2024-03-07 23:30 UTC → year=2024/month=03/day=07/hour=23."""
        with patch(f"{_MODULE}._write_and_upload_json_gz", new_callable=AsyncMock) as m:
            await sink.flush([log_record])

        local_path = m.call_args[0][1]
        assert "year=2024" in local_path
        assert "month=03" in local_path
        assert "day=07" in local_path
        assert "hour=23" in local_path

    @pytest.mark.asyncio
    async def test_records_spanning_hours_produce_separate_files(
        self, sink, two_records_different_hours
    ):
        """Records in different hours must never be merged into one file."""
        with patch(f"{_MODULE}._write_and_upload_json_gz", new_callable=AsyncMock) as m:
            await sink.flush(two_records_different_hours)

        assert m.call_count == 2
        paths = {c[0][1] for c in m.call_args_list}
        assert len(paths) == 2, "Each hour bucket must produce a distinct file"

    @pytest.mark.asyncio
    async def test_filename_is_lexi_sortable(self, sink, log_record):
        with patch(f"{_MODULE}._write_and_upload_json_gz", new_callable=AsyncMock) as m:
            await sink.flush([log_record])

        filename = os.path.basename(m.call_args[0][1])
        epoch_ns, *rest = filename.replace(".json.gz", "").split("_")
        assert epoch_ns.isdigit() and len(epoch_ns) >= 18
        assert _DEPLOYMENT in filename
        assert _APP in filename
        assert filename.endswith(".json.gz")

    @pytest.mark.asyncio
    async def test_uploads_to_all_store_names(self, tmp_path, log_record):
        """Dual upload: primary customer bucket + Atlan bucket."""
        sink = PartitionedJsonGzSink(
            record_type=ObservabilityRecordType.LOGS,
            staging_root=str(tmp_path),
            store_names=["store-a", "store-b"],
        )
        with patch(f"{_MODULE}._write_and_upload_json_gz", new_callable=AsyncMock) as m:
            await sink.flush([log_record])

        store_names_arg = m.call_args[0][3]
        assert store_names_arg == ["store-a", "store-b"]

    @pytest.mark.asyncio
    async def test_partition_error_is_isolated(self, sink, log_record):
        with patch(
            f"{_MODULE}._write_and_upload_json_gz",
            new_callable=AsyncMock,
            side_effect=RuntimeError("disk full"),
        ):
            await sink.flush([log_record])  # must not raise

    @pytest.mark.asyncio
    async def test_records_passed_through_unmodified(self, sink, log_record):
        """Records are written in raw OTel format; no field transformation."""
        with patch(f"{_MODULE}._write_and_upload_json_gz", new_callable=AsyncMock) as m:
            await sink.flush([log_record])

        written_records = m.call_args[0][0]
        assert written_records == [log_record]


# ---------------------------------------------------------------------------
# file_name_to_type helper
# ---------------------------------------------------------------------------


class TestFileNameToType:
    def test_known_log_filename(self):
        from application_sdk.constants import LOG_FILE_NAME

        assert file_name_to_type(LOG_FILE_NAME) == ObservabilityRecordType.LOGS

    def test_known_metrics_filename(self):
        from application_sdk.constants import METRICS_FILE_NAME

        assert file_name_to_type(METRICS_FILE_NAME) == ObservabilityRecordType.METRICS

    def test_known_traces_filename(self):
        from application_sdk.constants import TRACES_FILE_NAME

        assert file_name_to_type(TRACES_FILE_NAME) == ObservabilityRecordType.TRACES

    def test_unknown_filename_returns_other(self):
        assert file_name_to_type("unknown.parquet") == ObservabilityRecordType.OTHER


# ---------------------------------------------------------------------------
# Integration: AtlanObservability._flush_records dispatches to correct sinks
# ---------------------------------------------------------------------------


class TestFlushRecordsSinkDispatch:
    """Verify that _flush_records fans out to every configured sink."""

    def _make_instance(self, tmp_path, file_name="log.parquet"):
        from application_sdk.observability.observability import AtlanObservability

        class _Concrete(AtlanObservability):
            def process_record(self, record):
                return record

            def export_record(self, record):
                pass

        return _Concrete(
            batch_size=100,
            flush_interval=10,
            retention_days=30,
            cleanup_enabled=False,
            data_dir=str(tmp_path / "obs"),
            file_name=file_name,
        )

    @pytest.mark.asyncio
    async def test_single_sink_flushed(self, tmp_path):
        instance = self._make_instance(tmp_path)
        mock_sink = AsyncMock()
        instance._sinks = [mock_sink]

        records = [
            {
                "timestamp": _TS_NIGHT,
                "level": "INFO",
                "message": "hi",
                "logger_name": "",
                "extra": {},
            }
        ]
        with patch(
            "application_sdk.observability.observability.ENABLE_OBSERVABILITY_DAPR_SINK",
            True,
        ):
            await instance._flush_records(records)

        mock_sink.flush.assert_called_once_with(records)

    @pytest.mark.asyncio
    async def test_multiple_sinks_all_flushed(self, tmp_path):
        """When multiple sinks are configured, all receive records."""
        instance = self._make_instance(tmp_path)
        sink_a, sink_b = AsyncMock(), AsyncMock()
        instance._sinks = [sink_a, sink_b]

        records = [
            {
                "timestamp": _TS_NIGHT,
                "level": "INFO",
                "message": "hi",
                "logger_name": "",
                "extra": {},
            }
        ]
        with patch(
            "application_sdk.observability.observability.ENABLE_OBSERVABILITY_DAPR_SINK",
            True,
        ):
            await instance._flush_records(records)

        sink_a.flush.assert_called_once_with(records)
        sink_b.flush.assert_called_once_with(records)

    @pytest.mark.asyncio
    async def test_empty_records_skips_all_sinks(self, tmp_path):
        instance = self._make_instance(tmp_path)
        mock_sink = AsyncMock()
        instance._sinks = [mock_sink]

        with patch(
            "application_sdk.observability.observability.ENABLE_OBSERVABILITY_DAPR_SINK",
            True,
        ):
            await instance._flush_records([])

        mock_sink.flush.assert_not_called()

    @pytest.mark.asyncio
    async def test_dapr_sink_disabled_skips_all(self, tmp_path):
        instance = self._make_instance(tmp_path)
        mock_sink = AsyncMock()
        instance._sinks = [mock_sink]

        records = [
            {
                "timestamp": _TS_NIGHT,
                "level": "INFO",
                "message": "hi",
                "logger_name": "",
                "extra": {},
            }
        ]
        with patch(
            "application_sdk.observability.observability.ENABLE_OBSERVABILITY_DAPR_SINK",
            False,
        ):
            await instance._flush_records(records)

        mock_sink.flush.assert_not_called()
