"""Unit tests for SDR (Self-Deployed Runtime) log export functionality."""

import gzip
import os
from unittest.mock import AsyncMock, patch

import orjson
import pytest

from application_sdk.observability.observability import AtlanObservability


class ConcreteObservability(AtlanObservability):
    """Concrete implementation for testing abstract AtlanObservability."""

    def process_record(self, record):
        return record

    def export_record(self, record):
        pass


@pytest.fixture
def observability_instance(tmp_path):
    """Create an observability instance for testing."""
    data_dir = str(tmp_path / "observability")
    os.makedirs(data_dir, exist_ok=True)
    return ConcreteObservability(
        batch_size=100,
        flush_interval=10,
        retention_days=30,
        cleanup_enabled=False,
        data_dir=data_dir,
        file_name="log.parquet",
    )


@pytest.fixture
def sample_log_records():
    """Sample log records for testing."""
    return [
        {
            "timestamp": 1709836800.123456,
            "level": "INFO",
            "message": "Test log message 1",
            "logger_name": "test.logger",
            "extra": {
                "trace_id": "trace-123",
                "span_id": "span-456",
                "exception_type": "",
                "exception_message": "",
                "exception_stacktrace": "",
            },
        },
        {
            "timestamp": 1709836801.654321,
            "level": "ERROR",
            "message": "Test error message",
            "logger_name": "test.logger.error",
            "extra": {
                "trace_id": "trace-789",
                "span_id": "span-012",
                "exception_type": "ValueError",
                "exception_message": "Invalid value",
                "exception_stacktrace": "Traceback...",
            },
        },
    ]


class TestFlushSdrRecords:
    """Tests for _flush_sdr_records method."""

    @pytest.mark.asyncio
    async def test_creates_json_gz_file(
        self, observability_instance, sample_log_records, tmp_path
    ):
        """Verify that SDR export creates a .json.gz file."""
        with (
            patch(
                "application_sdk.observability.observability.TEMPORARY_PATH",
                str(tmp_path),
            ),
            patch(
                "application_sdk.observability.observability.SDR_LOG_S3_PREFIX",
                "sdr-logs",
            ),
            patch(
                "application_sdk.observability.observability.DEPLOYMENT_NAME",
                "test-deployment",
            ),
            patch(
                "application_sdk.observability.observability.APPLICATION_NAME",
                "test-app",
            ),
            patch(
                "application_sdk.services.objectstore.ObjectStore.upload_file",
                new_callable=AsyncMock,
            ) as mock_upload,
        ):
            await observability_instance._flush_sdr_records(sample_log_records)

            # Find the created file
            sdr_logs_dir = tmp_path / "sdr-logs"
            assert sdr_logs_dir.exists(), "SDR logs directory should be created"

            # Find all .json.gz files
            json_gz_files = list(sdr_logs_dir.rglob("*.json.gz"))
            assert len(json_gz_files) == 1, "Should create exactly one .json.gz file"

            # Verify upload was called
            mock_upload.assert_called_once()
            call_args = mock_upload.call_args
            assert call_args[0][0].endswith(
                ".json.gz"
            ), "Local path should end with .json.gz"
            assert call_args[0][1].endswith(
                ".json.gz"
            ), "Remote key should end with .json.gz"

    @pytest.mark.asyncio
    async def test_file_is_valid_gzip(
        self, observability_instance, sample_log_records, tmp_path
    ):
        """Verify that the created file is valid gzip format."""
        with (
            patch(
                "application_sdk.observability.observability.TEMPORARY_PATH",
                str(tmp_path),
            ),
            patch(
                "application_sdk.observability.observability.SDR_LOG_S3_PREFIX",
                "sdr-logs",
            ),
            patch(
                "application_sdk.observability.observability.DEPLOYMENT_NAME",
                "test-deployment",
            ),
            patch(
                "application_sdk.observability.observability.APPLICATION_NAME",
                "test-app",
            ),
            patch(
                "application_sdk.services.objectstore.ObjectStore.upload_file",
                new_callable=AsyncMock,
            ),
        ):
            await observability_instance._flush_sdr_records(sample_log_records)

            # Find the created file
            sdr_logs_dir = tmp_path / "sdr-logs"
            json_gz_files = list(sdr_logs_dir.rglob("*.json.gz"))
            assert len(json_gz_files) == 1

            # Verify it's valid gzip
            with gzip.open(json_gz_files[0], "rt", encoding="utf-8") as f:
                content = f.read()
                assert len(content) > 0, "File should have content"

    @pytest.mark.asyncio
    async def test_file_contains_valid_json_lines(
        self, observability_instance, sample_log_records, tmp_path
    ):
        """Verify that each line in the file is valid JSON."""
        with (
            patch(
                "application_sdk.observability.observability.TEMPORARY_PATH",
                str(tmp_path),
            ),
            patch(
                "application_sdk.observability.observability.SDR_LOG_S3_PREFIX",
                "sdr-logs",
            ),
            patch(
                "application_sdk.observability.observability.DEPLOYMENT_NAME",
                "test-deployment",
            ),
            patch(
                "application_sdk.observability.observability.APPLICATION_NAME",
                "test-app",
            ),
            patch(
                "application_sdk.services.objectstore.ObjectStore.upload_file",
                new_callable=AsyncMock,
            ),
        ):
            await observability_instance._flush_sdr_records(sample_log_records)

            # Find the created file
            sdr_logs_dir = tmp_path / "sdr-logs"
            json_gz_files = list(sdr_logs_dir.rglob("*.json.gz"))

            # Read and parse each line
            with gzip.open(json_gz_files[0], "rt", encoding="utf-8") as f:
                lines = f.readlines()

            assert len(lines) == len(
                sample_log_records
            ), "Should have one line per record"

            for line in lines:
                parsed = orjson.loads(line.strip())
                assert isinstance(parsed, dict), "Each line should be a JSON object"

    @pytest.mark.asyncio
    async def test_schema_matches_iceberg_table(
        self, observability_instance, sample_log_records, tmp_path
    ):
        """Verify that JSON schema matches Iceberg workflow_logs table schema."""
        expected_fields = {
            "timestamp",
            "level",
            "message",
            "correlation_id",
            "app_name",
            "logger_name",
            "trace_id",
            "span_id",
            "exception_type",
            "exception_message",
            "exception_stacktrace",
        }

        with (
            patch(
                "application_sdk.observability.observability.TEMPORARY_PATH",
                str(tmp_path),
            ),
            patch(
                "application_sdk.observability.observability.SDR_LOG_S3_PREFIX",
                "sdr-logs",
            ),
            patch(
                "application_sdk.observability.observability.DEPLOYMENT_NAME",
                "test-deployment",
            ),
            patch(
                "application_sdk.observability.observability.APPLICATION_NAME",
                "test-app",
            ),
            patch(
                "application_sdk.services.objectstore.ObjectStore.upload_file",
                new_callable=AsyncMock,
            ),
        ):
            await observability_instance._flush_sdr_records(sample_log_records)

            # Find and read the file
            sdr_logs_dir = tmp_path / "sdr-logs"
            json_gz_files = list(sdr_logs_dir.rglob("*.json.gz"))

            with gzip.open(json_gz_files[0], "rt", encoding="utf-8") as f:
                first_line = f.readline()

            parsed = orjson.loads(first_line)
            assert (
                set(parsed.keys()) == expected_fields
            ), "Fields should match Iceberg schema"

    @pytest.mark.asyncio
    async def test_timestamp_converted_to_microseconds(
        self, observability_instance, sample_log_records, tmp_path
    ):
        """Verify that timestamp is converted from seconds to microseconds."""
        with (
            patch(
                "application_sdk.observability.observability.TEMPORARY_PATH",
                str(tmp_path),
            ),
            patch(
                "application_sdk.observability.observability.SDR_LOG_S3_PREFIX",
                "sdr-logs",
            ),
            patch(
                "application_sdk.observability.observability.DEPLOYMENT_NAME",
                "test-deployment",
            ),
            patch(
                "application_sdk.observability.observability.APPLICATION_NAME",
                "test-app",
            ),
            patch(
                "application_sdk.services.objectstore.ObjectStore.upload_file",
                new_callable=AsyncMock,
            ),
        ):
            await observability_instance._flush_sdr_records(sample_log_records)

            # Find and read the file
            sdr_logs_dir = tmp_path / "sdr-logs"
            json_gz_files = list(sdr_logs_dir.rglob("*.json.gz"))

            with gzip.open(json_gz_files[0], "rt", encoding="utf-8") as f:
                first_line = f.readline()

            parsed = orjson.loads(first_line)
            # Original timestamp: 1709836800.123456 seconds
            # Expected: 1709836800123456 microseconds
            expected_timestamp = int(sample_log_records[0]["timestamp"] * 1_000_000)
            assert parsed["timestamp"] == expected_timestamp

    @pytest.mark.asyncio
    async def test_hive_partition_path_format(
        self, observability_instance, sample_log_records, tmp_path
    ):
        """Verify that Hive partition path is correctly formatted."""
        with (
            patch(
                "application_sdk.observability.observability.TEMPORARY_PATH",
                str(tmp_path),
            ),
            patch(
                "application_sdk.observability.observability.SDR_LOG_S3_PREFIX",
                "sdr-logs",
            ),
            patch(
                "application_sdk.observability.observability.DEPLOYMENT_NAME",
                "test-deployment",
            ),
            patch(
                "application_sdk.observability.observability.APPLICATION_NAME",
                "test-app",
            ),
            patch(
                "application_sdk.services.objectstore.ObjectStore.upload_file",
                new_callable=AsyncMock,
            ),
        ):
            await observability_instance._flush_sdr_records(sample_log_records)

            # Find the created file
            sdr_logs_dir = tmp_path / "sdr-logs"
            json_gz_files = list(sdr_logs_dir.rglob("*.json.gz"))
            file_path = str(json_gz_files[0])

            # Verify Hive partitioning pattern
            assert "year=" in file_path, "Should have year partition"
            assert "month=" in file_path, "Should have month partition"
            assert "day=" in file_path, "Should have day partition"
            assert "hour=" in file_path, "Should have hour partition"

    @pytest.mark.asyncio
    async def test_filename_format_lexi_sortable(
        self, observability_instance, sample_log_records, tmp_path
    ):
        """Verify that filename is lexi-sortable with correct format."""
        with (
            patch(
                "application_sdk.observability.observability.TEMPORARY_PATH",
                str(tmp_path),
            ),
            patch(
                "application_sdk.observability.observability.SDR_LOG_S3_PREFIX",
                "sdr-logs",
            ),
            patch(
                "application_sdk.observability.observability.DEPLOYMENT_NAME",
                "test-deployment",
            ),
            patch(
                "application_sdk.observability.observability.APPLICATION_NAME",
                "test-app",
            ),
            patch(
                "application_sdk.services.objectstore.ObjectStore.upload_file",
                new_callable=AsyncMock,
            ),
        ):
            await observability_instance._flush_sdr_records(sample_log_records)

            # Find the created file
            sdr_logs_dir = tmp_path / "sdr-logs"
            json_gz_files = list(sdr_logs_dir.rglob("*.json.gz"))
            filename = json_gz_files[0].name

            # Expected format: {epoch_ns}_{deployment}_{app}.json.gz
            parts = filename.replace(".json.gz", "").split("_")
            assert len(parts) >= 3, "Filename should have at least 3 parts"

            # First part should be epoch nanoseconds (19 digits)
            epoch_ns = parts[0]
            assert epoch_ns.isdigit(), "First part should be numeric (epoch_ns)"
            assert len(epoch_ns) >= 18, "Epoch ns should be at least 18 digits"

            # Should contain deployment and app names
            assert "test-deployment" in filename, "Should contain deployment name"
            assert "test-app" in filename, "Should contain app name"
            assert filename.endswith(".json.gz"), "Should end with .json.gz"

    @pytest.mark.asyncio
    async def test_empty_records_list(self, observability_instance, tmp_path):
        """Verify handling of empty records list."""
        with (
            patch(
                "application_sdk.observability.observability.TEMPORARY_PATH",
                str(tmp_path),
            ),
            patch(
                "application_sdk.observability.observability.SDR_LOG_S3_PREFIX",
                "sdr-logs",
            ),
            patch(
                "application_sdk.observability.observability.DEPLOYMENT_NAME",
                "test-deployment",
            ),
            patch(
                "application_sdk.observability.observability.APPLICATION_NAME",
                "test-app",
            ),
            patch(
                "application_sdk.services.objectstore.ObjectStore.upload_file",
                new_callable=AsyncMock,
            ) as mock_upload,
        ):
            await observability_instance._flush_sdr_records([])

            # Should still create file (empty JSON Lines)
            sdr_logs_dir = tmp_path / "sdr-logs"
            json_gz_files = list(sdr_logs_dir.rglob("*.json.gz"))
            assert len(json_gz_files) == 1

            # File should be valid gzip with no content
            with gzip.open(json_gz_files[0], "rt", encoding="utf-8") as f:
                content = f.read()
            assert content == "", "Empty records should produce empty file"

            # Upload should still be called
            mock_upload.assert_called_once()

    @pytest.mark.asyncio
    async def test_upload_called_with_correct_store(
        self, observability_instance, sample_log_records, tmp_path
    ):
        """Verify that ObjectStore.upload_file is called with correct store name."""
        with (
            patch(
                "application_sdk.observability.observability.TEMPORARY_PATH",
                str(tmp_path),
            ),
            patch(
                "application_sdk.observability.observability.SDR_LOG_S3_PREFIX",
                "sdr-logs",
            ),
            patch(
                "application_sdk.observability.observability.DEPLOYMENT_NAME",
                "test-deployment",
            ),
            patch(
                "application_sdk.observability.observability.APPLICATION_NAME",
                "test-app",
            ),
            patch(
                "application_sdk.observability.observability.DEPLOYMENT_OBJECT_STORE_NAME",
                "test-store",
            ),
            patch(
                "application_sdk.services.objectstore.ObjectStore.upload_file",
                new_callable=AsyncMock,
            ) as mock_upload,
        ):
            await observability_instance._flush_sdr_records(sample_log_records)

            mock_upload.assert_called_once()
            call_kwargs = mock_upload.call_args[1]
            assert call_kwargs["store_name"] == "test-store"

    @pytest.mark.asyncio
    async def test_error_handling(
        self, observability_instance, sample_log_records, tmp_path
    ):
        """Verify that errors are logged but not re-raised."""
        with (
            patch(
                "application_sdk.observability.observability.TEMPORARY_PATH",
                str(tmp_path),
            ),
            patch(
                "application_sdk.observability.observability.SDR_LOG_S3_PREFIX",
                "sdr-logs",
            ),
            patch(
                "application_sdk.observability.observability.DEPLOYMENT_NAME",
                "test-deployment",
            ),
            patch(
                "application_sdk.observability.observability.APPLICATION_NAME",
                "test-app",
            ),
            patch(
                "application_sdk.services.objectstore.ObjectStore.upload_file",
                new_callable=AsyncMock,
                side_effect=Exception("Upload failed"),
            ),
            patch("logging.error") as mock_log_error,
        ):
            # Should not raise exception
            await observability_instance._flush_sdr_records(sample_log_records)

            # Should log the error
            mock_log_error.assert_called()
            call_args = str(mock_log_error.call_args)
            assert "Error flushing SDR records" in call_args

    @pytest.mark.asyncio
    async def test_app_name_in_records(
        self, observability_instance, sample_log_records, tmp_path
    ):
        """Verify that APPLICATION_NAME is included in each record."""
        test_app_name = "my-custom-app"
        with (
            patch(
                "application_sdk.observability.observability.TEMPORARY_PATH",
                str(tmp_path),
            ),
            patch(
                "application_sdk.observability.observability.SDR_LOG_S3_PREFIX",
                "sdr-logs",
            ),
            patch(
                "application_sdk.observability.observability.DEPLOYMENT_NAME",
                "test-deployment",
            ),
            patch(
                "application_sdk.observability.observability.APPLICATION_NAME",
                test_app_name,
            ),
            patch(
                "application_sdk.services.objectstore.ObjectStore.upload_file",
                new_callable=AsyncMock,
            ),
        ):
            await observability_instance._flush_sdr_records(sample_log_records)

            # Find and read the file
            sdr_logs_dir = tmp_path / "sdr-logs"
            json_gz_files = list(sdr_logs_dir.rglob("*.json.gz"))

            with gzip.open(json_gz_files[0], "rt", encoding="utf-8") as f:
                for line in f:
                    parsed = orjson.loads(line)
                    assert parsed["app_name"] == test_app_name
