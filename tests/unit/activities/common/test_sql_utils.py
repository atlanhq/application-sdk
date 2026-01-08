"""Unit tests for SQL utilities module."""

from unittest.mock import AsyncMock, Mock

import pandas as pd
import pytest

from application_sdk.activities.common import sql_utils
from application_sdk.activities.common.models import ActivityStatistics
from application_sdk.io.parquet import ParquetFileWriter


class TestFinalizeMultidbResults:
    """Test cases for finalize_multidb_results function."""

    @pytest.mark.asyncio
    async def test_finalize_multidb_results_concatenate_with_typename_passed(
        self,
    ):
        """Test that finalize_multidb_results passes typename to setup_parquet_output_func when concatenating."""
        # Create mock dataframes
        df1 = pd.DataFrame({"id": [1, 2], "name": ["a", "b"]})
        df2 = pd.DataFrame({"id": [3, 4], "name": ["c", "d"]})

        async def async_df_generator():
            yield df1
            yield df2

        dataframe_list = [async_df_generator()]

        # Mock setup_parquet_output_func to verify it's called with typename
        mock_setup_func = Mock()
        mock_parquet_writer = AsyncMock(spec=ParquetFileWriter)
        mock_stats = ActivityStatistics(
            total_record_count=4, chunk_count=1, typename="database"
        )
        mock_parquet_writer.close.return_value = mock_stats
        mock_setup_func.return_value = mock_parquet_writer

        result = await sql_utils.finalize_multidb_results(
            write_to_file=False,
            concatenate=True,
            return_dataframe=False,
            parquet_output=None,
            dataframe_list=dataframe_list,
            setup_parquet_output_func=mock_setup_func,
            output_path="/test/path",
            typename="database",
        )

        # Verify setup_parquet_output_func was called with correct arguments including typename
        mock_setup_func.assert_called_once_with("/test/path", True, "database")
        assert result == mock_stats
        mock_parquet_writer.write.assert_called_once()
        mock_parquet_writer.close.assert_called_once()
