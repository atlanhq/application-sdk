"""Unit tests for output interface."""

from typing import Any
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

        # Mock the open function, orjson.dumps, and push_file_to_object_store
        with patch("builtins.open", mock_open()) as mock_file, patch(
            "orjson.dumps",
            return_value=b'{"total_record_count": 100, "chunk_count": 5}',
        ) as mock_orjson, patch(
            "application_sdk.outputs.ObjectStoreOutput.push_file_to_object_store",
            new_callable=AsyncMock,
        ) as mock_push:
            # Call the method
            stats = await self.output.write_statistics()

            # Assertions
            assert stats == {"total_record_count": 100, "chunk_count": 5}
            mock_file.assert_called_once_with("/test/path/statistics.json.ignore", "w")
            mock_orjson.assert_called_once_with(
                {"total_record_count": 100, "chunk_count": 5}
            )
            mock_push.assert_awaited_once_with(
                "/test/prefix", "/test/path/statistics.json.ignore"
            )

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
