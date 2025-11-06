"""Unit tests for output interface."""

from typing import Any
import json
from unittest.mock import AsyncMock, mock_open, patch

import pandas as pd
import pytest

from application_sdk.common.dataframe_utils import is_empty_dataframe
from application_sdk.outputs import Output


def test_is_empty_dataframe_pandas():
    """Test is_empty_dataframe with pandas DataFrame."""
    # Test with empty DataFrame
    empty_df = pd.DataFrame()
    assert is_empty_dataframe(empty_df) is True

    # Test with non-empty DataFrame
    non_empty_df = pd.DataFrame({"col": [1, 2, 3]})
    assert is_empty_dataframe(non_empty_df) is False


@pytest.mark.asyncio
class TestOutput:
    class ConcreteOutput(Output):
        """Concrete implementation of Output for testing."""

        def __init__(self, output_path: str, output_prefix: str):
            self.output_path = output_path
            self.output_prefix = output_prefix
            self.total_record_count = 0
            self.chunk_count = 0
            self.partitions = []  # Initialize partitions attribute
            self.buffer_size = 5000
            self.max_file_size_bytes = 1024 * 1024 * 10  # 10MB
            self.current_buffer_size = 0
            self.current_buffer_size_bytes = 0

        async def write_dataframe(self, dataframe: pd.DataFrame):
            """Implement abstract method."""
            self.total_record_count += len(dataframe)
            self.chunk_count += 1

        async def write_daft_dataframe(self, dataframe: Any):  # type: ignore
            """Implement abstract method."""
            self.total_record_count += 1  # Mock implementation
            self.chunk_count += 1

    def setup_method(self):
        """Set up test fixtures."""
        self.output = self.ConcreteOutput("/test/path", "/test/prefix")

    @pytest.mark.asyncio
    async def test_write_batched_dataframe_sync(self):
        """Test write_batched_dataframe with sync generator."""

        def generate_dataframes():
            yield pd.DataFrame({"col": [1, 2]})
            yield pd.DataFrame({"col": [3, 4]})

        await self.output.write_batched_dataframe(generate_dataframes())
        assert self.output.total_record_count == 4
        assert self.output.chunk_count == 2

    @pytest.mark.asyncio
    async def test_write_batched_dataframe_async(self):
        """Test write_batched_dataframe with async generator."""

        async def generate_dataframes():
            yield pd.DataFrame({"col": [1, 2]})
            yield pd.DataFrame({"col": [3, 4]})

        await self.output.write_batched_dataframe(generate_dataframes())
        assert self.output.total_record_count == 4
        assert self.output.chunk_count == 2

    @pytest.mark.asyncio
    async def test_write_batched_dataframe_empty(self):
        """Test write_batched_dataframe with empty DataFrame."""

        def generate_empty_dataframes():
            yield pd.DataFrame()

        await self.output.write_batched_dataframe(generate_empty_dataframes())
        assert self.output.total_record_count == 0
        assert self.output.chunk_count == 0

    @pytest.mark.asyncio
    async def test_write_batched_dataframe_error(self):
        """Test write_batched_dataframe error handling."""

        def generate_error():
            yield pd.DataFrame({"col": [1]})
            raise Exception("Test error")

        with patch("application_sdk.outputs.logger.error") as mock_logger:
            # According to workspace rules, exceptions should be re-raised after logging
            with pytest.raises(Exception, match="Test error"):
                await self.output.write_batched_dataframe(generate_error())
            mock_logger.assert_called_once()
            assert "Error writing batched dataframe" in mock_logger.call_args[0][0]

    @pytest.mark.asyncio
    async def test_get_statistics_error(self):
        """Test get_statistics error handling."""
        with patch.object(self.output, "write_statistics") as mock_write:
            mock_write.side_effect = Exception("Test error")
            with pytest.raises(Exception):
                await self.output.get_statistics()

    @pytest.mark.asyncio
    async def test_write_statistics_success(self):
        """Test write_statistics successful case."""
        self.output.total_record_count = 100
        self.output.chunk_count = 5
        self.output.partitions = [1, 2, 1, 2, 1]

        # Mock the open function, orjson.dumps, and object store upload
        with patch("builtins.open", mock_open()) as mock_file, patch(
            "orjson.dumps",
            return_value=b'{"total_record_count": 100, "chunk_count": 5, "partitions": [1,2,1,2,1]}',
        ) as mock_orjson, patch(
            "application_sdk.outputs.get_object_store_prefix",
            return_value="path/statistics.json.ignore",
        ), patch(
            "application_sdk.services.objectstore.ObjectStore.upload_file",
            new_callable=AsyncMock,
        ) as mock_push:
            # Call the method
            stats = await self.output.write_statistics()

            # Assertions
            assert stats == {
                "total_record_count": 100,
                "chunk_count": 5,  # This is len(self.partitions) which is 5
                "partitions": [1, 2, 1, 2, 1],
            }
            mock_file.assert_called_once_with("/test/path/statistics.json.ignore", "w")
            mock_orjson.assert_called_once_with(
                {
                    "total_record_count": 100,
                    "chunk_count": 5,
                    "partitions": [1, 2, 1, 2, 1],
                }
            )
            # Verify the upload call
            mock_push.assert_awaited_once()
            upload_kwargs = mock_push.await_args.kwargs  # type: ignore[attr-defined]
            assert upload_kwargs["source"] == "/test/path/statistics.json.ignore"
            assert upload_kwargs["destination"] == "path/statistics.json.ignore"

    @pytest.mark.asyncio
    async def test_write_statistics_error(self):
        """Test write_statistics error handling."""
        with patch("pandas.DataFrame.to_json") as mock_to_json:
            mock_to_json.side_effect = Exception("Test error")

            with patch("application_sdk.outputs.logger.error") as mock_logger:
                result = await self.output.write_statistics()
                assert result is None
                mock_logger.assert_called_once()
                assert "Error writing statistics" in mock_logger.call_args[0][0]

    async def test__update_run_aggregate_skips_when_phase_unknown(self):
        """Skips aggregation when phase cannot be inferred from output_path."""
        # Ensure no 'raw' or 'transformed' in path so phase is None
        self.output.output_path = "/tmp/no-phase/path"
        stats = {"typename": "table", "total_record_count": 10}

        with patch(
            "application_sdk.services.objectstore.ObjectStore.download_file",
            new_callable=AsyncMock,
        ) as mock_dl, patch(
            "application_sdk.services.objectstore.ObjectStore.upload_file",
            new_callable=AsyncMock,
        ) as mock_ul, patch("builtins.open", mock_open()) as m:
            await self.output._update_run_aggregate("ignored", stats)
            mock_dl.assert_not_awaited()
            mock_ul.assert_not_awaited()
            # No reads/writes when phase is unknown
            assert m.call_count == 0

    async def test__update_run_aggregate_creates_new_aggregate_extract(self):
        """Creates a new aggregate structure and writes stats for Extract phase."""
        # Make phase inference return "Extract"
        self.output.output_path = "/tmp/run/raw/path"
        stats = {"typename": "table", "total_record_count": 7}

        with patch(
            "application_sdk.outputs.build_output_path", return_value="workflow/run"
        ), patch(
            "application_sdk.outputs.get_object_store_prefix",
            return_value="os://bucket/statistics.json.ignore",
        ), patch(
            "application_sdk.services.objectstore.ObjectStore.download_file",
            new_callable=AsyncMock,
        ) as mock_dl, patch(
            "application_sdk.services.objectstore.ObjectStore.upload_file",
            new_callable=AsyncMock,
        ) as mock_ul, patch("builtins.open", mock_open()) as m:
            # Simulate no existing aggregate in object store
            mock_dl.side_effect = Exception("not found")

            await self.output._update_run_aggregate("ignored", stats)

            handle = m()
            # One write with the aggregated payload
            write_calls = handle.write.call_args_list
            assert len(write_calls) == 1
            payload = write_calls[0].args[0]
            data = json.loads(payload)

            assert data["Extract"]["table"]["record_count"] == 7
            # Other phases should exist, even if empty
            assert "Transform" in data and "Publish" in data

            mock_ul.assert_awaited_once()

    async def test__update_run_aggregate_accumulates_existing_transform(self):
        """Accumulates total_record_count into existing Transform aggregate."""
        # Make phase inference return "Transform"
        self.output.output_path = "/tmp/run/transformed/path"
        stats = {"typename": "table", "total_record_count": 3}

        existing = {"Extract": {}, "Transform": {"table": {"record_count": 5}}, "Publish": {}}
        m = mock_open(read_data=json.dumps(existing))

        with patch(
            "application_sdk.outputs.build_output_path", return_value="workflow/run"
        ), patch(
            "application_sdk.outputs.get_object_store_prefix",
            return_value="os://bucket/statistics.json.ignore",
        ), patch(
            "application_sdk.services.objectstore.ObjectStore.download_file",
            new_callable=AsyncMock,
        ) as mock_dl, patch(
            "application_sdk.services.objectstore.ObjectStore.upload_file",
            new_callable=AsyncMock,
        ) as mock_ul, patch("builtins.open", m) as mo:
            await self.output._update_run_aggregate("ignored", stats)

            handle = mo()
            written = handle.write.call_args[0][0]
            data = json.loads(written)

            # 5 existing + 3 new
            assert data["Transform"]["table"]["record_count"] == 8
            mock_dl.assert_awaited_once()
            mock_ul.assert_awaited_once()

    async def test__update_run_aggregate_defaults_unknown_typename(self):
        """Uses 'unknown' typename when not provided in statistics."""
        self.output.output_path = "/tmp/run/raw/path"
        stats = {"total_record_count": 4}  # no 'typename'

        with patch(
            "application_sdk.outputs.build_output_path", return_value="workflow/run"
        ), patch(
            "application_sdk.outputs.get_object_store_prefix",
            return_value="os://bucket/statistics.json.ignore",
        ), patch(
            "application_sdk.services.objectstore.ObjectStore.download_file",
            new_callable=AsyncMock,
        ) as mock_dl, patch(
            "application_sdk.services.objectstore.ObjectStore.upload_file",
            new_callable=AsyncMock,
        ) as mock_ul, patch("builtins.open", mock_open()) as m:
            mock_dl.side_effect = Exception("not found")

            await self.output._update_run_aggregate("ignored", stats)

            payload = m().write.call_args[0][0]
            data = json.loads(payload)
            assert data["Extract"]["unknown"]["record_count"] == 4
            mock_ul.assert_awaited_once()
