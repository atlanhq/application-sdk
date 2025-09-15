import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

from application_sdk.outputs.parquet import ParquetOutput


@pytest.fixture
def base_output_path(tmp_path: Path) -> str:
    """Create a temporary directory for tests."""
    return str(tmp_path / "test_output")


@pytest.fixture
def sample_dataframe() -> pd.DataFrame:
    """Create a sample pandas DataFrame for testing."""
    return pd.DataFrame(
        {
            "id": [1, 2, 3, 4, 5],
            "name": ["Alice", "Bob", "Charlie", "Diana", "Eve"],
            "age": [25, 30, 35, 28, 32],
            "department": ["engineering", "sales", "engineering", "marketing", "sales"],
            "year": [2023, 2023, 2024, 2024, 2023],
        }
    )


@pytest.fixture
def large_dataframe() -> pd.DataFrame:
    """Create a large pandas DataFrame for testing chunking."""
    data = {
        "id": list(range(1000)),
        "name": [f"user_{i}" for i in range(1000)],
        "value": [i * 10 for i in range(1000)],
        "category": [["A", "B", "C"][i % 3] for i in range(1000)],
    }
    return pd.DataFrame(data)


class TestParquetOutputInit:
    """Test ParquetOutput initialization."""

    def test_init_default_values(self, base_output_path: str):
        """Test ParquetOutput initialization with default values."""
        parquet_output = ParquetOutput(output_path=base_output_path)

        # The output path gets modified by adding suffix, so check it ends with the base path
        assert base_output_path in parquet_output.output_path
        assert parquet_output.output_suffix == ""
        assert parquet_output.output_prefix == ""
        assert parquet_output.typename is None

        assert parquet_output.chunk_size == 100000
        assert parquet_output.total_record_count == 0
        assert parquet_output.chunk_count == 0
        assert parquet_output.chunk_start is None
        assert parquet_output.start_marker is None
        assert parquet_output.end_marker is None
        # partition_cols was removed from the implementation

    def test_init_custom_values(self, base_output_path: str):
        """Test ParquetOutput initialization with custom values."""
        parquet_output = ParquetOutput(
            output_path=base_output_path,
            output_suffix="test_suffix",
            output_prefix="test_prefix",
            typename="test_table",
            chunk_size=50000,
            total_record_count=100,
            chunk_count=2,
            chunk_start=10,
            start_marker="start",
            end_marker="end",
        )

        assert parquet_output.output_suffix == "test_suffix"
        assert parquet_output.output_prefix == "test_prefix"
        assert parquet_output.typename == "test_table"

        assert parquet_output.chunk_size == 50000
        assert parquet_output.total_record_count == 100
        assert parquet_output.chunk_count == 2
        assert parquet_output.chunk_start == 10
        assert parquet_output.start_marker == "start"
        assert parquet_output.end_marker == "end"
        # partition_cols was removed from the implementation

    def test_init_creates_output_directory(self, base_output_path: str):
        """Test that initialization creates the output directory."""
        parquet_output = ParquetOutput(
            output_path=base_output_path,
            output_suffix="test_dir",
            typename="test_table",
        )

        expected_path = os.path.join(base_output_path, "test_dir", "test_table")
        assert os.path.exists(expected_path)
        assert parquet_output.output_path == expected_path


class TestParquetOutputPathGen:
    """Test ParquetOutput path generation."""

    def test_path_gen_with_markers(self, base_output_path: str):
        """Test path generation with start and end markers."""
        parquet_output = ParquetOutput(output_path=base_output_path)

        path = parquet_output.path_gen(start_marker="start_123", end_marker="end_456")

        assert path == "start_123_end_456.parquet"

    def test_path_gen_without_chunk_start(self, base_output_path: str):
        """Test path generation without chunk start."""
        parquet_output = ParquetOutput(output_path=base_output_path)

        path = parquet_output.path_gen(chunk_count=5)

        assert path == "5.parquet"

    def test_path_gen_with_chunk_start(self, base_output_path: str):
        """Test path generation with chunk start."""
        parquet_output = ParquetOutput(output_path=base_output_path)

        path = parquet_output.path_gen(chunk_start=10, chunk_count=3)

        assert path == "chunk-10-part3.parquet"


class TestParquetOutputWriteDataframe:
    """Test ParquetOutput pandas DataFrame writing."""

    @pytest.mark.asyncio
    async def test_write_empty_dataframe(self, base_output_path: str):
        """Test writing an empty DataFrame."""
        parquet_output = ParquetOutput(output_path=base_output_path)
        empty_df = pd.DataFrame()

        await parquet_output.write_dataframe(empty_df)

        assert parquet_output.chunk_count == 0
        assert parquet_output.total_record_count == 0

    @pytest.mark.asyncio
    async def test_write_dataframe_success(
        self, base_output_path: str, sample_dataframe: pd.DataFrame
    ):
        """Test successful DataFrame writing."""
        with patch(
            "application_sdk.services.objectstore.ObjectStore.upload_file"
        ) as mock_upload, patch(
            "pandas.DataFrame.to_parquet"
        ) as mock_to_parquet, patch(
            "application_sdk.outputs.parquet.get_object_store_prefix"
        ) as mock_prefix:
            mock_upload.return_value = AsyncMock()
            mock_prefix.return_value = "test/output/path"

            parquet_output = ParquetOutput(
                output_path=base_output_path, output_suffix="test"
            )

            await parquet_output.write_dataframe(sample_dataframe)

            assert parquet_output.chunk_count == 1

            # Check that to_parquet was called (the new implementation uses buffering)
            mock_to_parquet.assert_called()

            # Check that upload was called
            mock_upload.assert_called()

    @pytest.mark.asyncio
    async def test_write_dataframe_with_custom_path_gen(
        self, base_output_path: str, sample_dataframe: pd.DataFrame
    ):
        """Test DataFrame writing with custom path generation."""
        with patch(
            "application_sdk.services.objectstore.ObjectStore.upload_file"
        ) as mock_upload, patch(
            "pandas.DataFrame.to_parquet"
        ) as mock_to_parquet, patch(
            "application_sdk.outputs.parquet.get_object_store_prefix"
        ) as mock_prefix:
            mock_upload.return_value = AsyncMock()
            mock_prefix.return_value = "test/output/path"

            parquet_output = ParquetOutput(
                output_path=base_output_path,
                start_marker="test_start",
                end_marker="test_end",
            )

            await parquet_output.write_dataframe(sample_dataframe)

            # Check that to_parquet was called
            mock_to_parquet.assert_called()

            # The current implementation uses chunk-based naming even with markers
            # This is because the buffering system overrides the marker-based naming
            call_args = mock_to_parquet.call_args[0][
                0
            ]  # First positional argument (file path)
            assert "chunk-" in call_args and ".parquet" in call_args

    @pytest.mark.asyncio
    async def test_write_dataframe_error_handling(
        self, base_output_path: str, sample_dataframe: pd.DataFrame
    ):
        """Test error handling during DataFrame writing."""
        with patch("pandas.DataFrame.to_parquet") as mock_to_parquet:
            mock_to_parquet.side_effect = Exception("Test error")

            parquet_output = ParquetOutput(output_path=base_output_path)

            with pytest.raises(Exception, match="Test error"):
                await parquet_output.write_dataframe(sample_dataframe)


class TestParquetOutputWriteDaftDataframe:
    """Test ParquetOutput daft DataFrame writing."""

    @pytest.mark.asyncio
    async def test_write_daft_dataframe_empty(self, base_output_path: str):
        """Test writing an empty daft DataFrame."""
        with patch("daft.from_pydict") as mock_daft:
            mock_df = MagicMock()
            mock_df.count_rows.return_value = 0
            mock_daft.return_value = mock_df

            parquet_output = ParquetOutput(output_path=base_output_path)

            await parquet_output.write_daft_dataframe(mock_df)

            assert parquet_output.chunk_count == 0
            assert parquet_output.total_record_count == 0

    @pytest.mark.asyncio
    async def test_write_daft_dataframe_success(self, base_output_path: str):
        """Test successful daft DataFrame writing."""
        with patch("daft.execution_config_ctx") as mock_ctx, patch(
            "application_sdk.services.objectstore.ObjectStore.upload_prefix"
        ) as mock_upload, patch(
            "application_sdk.outputs.parquet.get_object_store_prefix"
        ) as mock_prefix:
            mock_upload.return_value = AsyncMock()
            mock_prefix.return_value = "test/output/path"
            mock_ctx.return_value.__enter__ = MagicMock()
            mock_ctx.return_value.__exit__ = MagicMock()

            # Mock daft DataFrame
            mock_df = MagicMock()
            mock_df.count_rows.return_value = 1000
            mock_df.write_parquet = MagicMock()

            parquet_output = ParquetOutput(
                output_path=base_output_path,
            )

            await parquet_output.write_daft_dataframe(mock_df)

            assert parquet_output.chunk_count == 1
            assert parquet_output.total_record_count == 1000

            # Check that daft write_parquet was called with correct parameters
            mock_df.write_parquet.assert_called_once_with(
                root_dir=parquet_output.output_path,
                write_mode="append",  # Uses method default value "append"
                partition_cols=[],  # Default empty list
            )

            # Check that upload_prefix was called
            mock_upload.assert_called_once()

    @pytest.mark.asyncio
    async def test_write_daft_dataframe_with_parameter_overrides(
        self, base_output_path: str
    ):
        """Test daft DataFrame writing with parameter overrides."""
        with patch("daft.execution_config_ctx") as mock_ctx, patch(
            "application_sdk.services.objectstore.ObjectStore.upload_prefix"
        ) as mock_upload, patch(
            "application_sdk.services.objectstore.ObjectStore.delete_prefix"
        ) as mock_delete, patch(
            "application_sdk.outputs.parquet.get_object_store_prefix"
        ) as mock_prefix:
            mock_upload.return_value = AsyncMock()
            mock_delete.return_value = AsyncMock()
            mock_prefix.return_value = "test/output/path"
            mock_ctx.return_value.__enter__ = MagicMock()
            mock_ctx.return_value.__exit__ = MagicMock()

            # Mock daft DataFrame
            mock_df = MagicMock()
            mock_df.count_rows.return_value = 500
            mock_df.write_parquet = MagicMock()

            parquet_output = ParquetOutput(
                output_path=base_output_path,
            )

            # Override parameters in method call
            await parquet_output.write_daft_dataframe(
                mock_df, partition_cols=["department", "year"], write_mode="overwrite"
            )

            # Check that overridden parameters were used
            mock_df.write_parquet.assert_called_once_with(
                root_dir=parquet_output.output_path,
                write_mode="overwrite",  # Overridden
                partition_cols=["department", "year"],  # Overridden
            )

            # Check that delete_prefix was called for overwrite mode
            mock_delete.assert_called_once_with(prefix="test/output/path")

    @pytest.mark.asyncio
    async def test_write_daft_dataframe_with_default_parameters(
        self, base_output_path: str
    ):
        """Test daft DataFrame writing with default parameters (uses method default write_mode='append')."""
        with patch("daft.execution_config_ctx") as mock_ctx, patch(
            "application_sdk.services.objectstore.ObjectStore.upload_prefix"
        ) as mock_upload, patch(
            "application_sdk.outputs.parquet.get_object_store_prefix"
        ) as mock_prefix:
            mock_upload.return_value = AsyncMock()
            mock_prefix.return_value = "test/output/path"
            mock_ctx.return_value.__enter__ = MagicMock()
            mock_ctx.return_value.__exit__ = MagicMock()

            # Mock daft DataFrame
            mock_df = MagicMock()
            mock_df.count_rows.return_value = 500
            mock_df.write_parquet = MagicMock()

            parquet_output = ParquetOutput(
                output_path=base_output_path,
            )

            # Use default parameters
            await parquet_output.write_daft_dataframe(mock_df)

            # Check that default method parameters were used
            mock_df.write_parquet.assert_called_once_with(
                root_dir=parquet_output.output_path,
                write_mode="append",  # Uses method default value "append"
                partition_cols=[],  # None converted to empty list
            )

    @pytest.mark.asyncio
    async def test_write_daft_dataframe_with_execution_configuration(
        self, base_output_path: str
    ):
        """Test that DAPR limit is properly configured."""
        with patch("daft.execution_config_ctx") as mock_ctx, patch(
            "application_sdk.services.objectstore.ObjectStore.upload_prefix"
        ) as mock_upload, patch(
            "application_sdk.outputs.parquet.get_object_store_prefix"
        ) as mock_prefix:
            mock_upload.return_value = AsyncMock()
            mock_prefix.return_value = "test/output/path"
            mock_ctx.return_value.__enter__ = MagicMock()
            mock_ctx.return_value.__exit__ = MagicMock()

            # Mock daft DataFrame
            mock_df = MagicMock()
            mock_df.count_rows.return_value = 1000
            mock_df.write_parquet = MagicMock()

            parquet_output = ParquetOutput(output_path=base_output_path)

            await parquet_output.write_daft_dataframe(mock_df)

            # Check that execution context was called (don't check exact value since DAPR_MAX_GRPC_MESSAGE_LENGTH is imported)
            mock_ctx.assert_called_once()
            # Verify the call was made with parquet_target_filesize parameter
            call_args = mock_ctx.call_args
            assert "parquet_target_filesize" in call_args.kwargs
            assert "default_morsel_size" in call_args.kwargs
            assert call_args.kwargs["parquet_target_filesize"] > 0
            assert call_args.kwargs["default_morsel_size"] > 0

    @pytest.mark.asyncio
    async def test_write_daft_dataframe_error_handling(self, base_output_path: str):
        """Test error handling during daft DataFrame writing."""
        # Test that count_rows error is properly handled
        mock_df = MagicMock()
        mock_df.count_rows.side_effect = Exception("Count rows error")

        parquet_output = ParquetOutput(output_path=base_output_path)

        with pytest.raises(Exception, match="Count rows error"):
            await parquet_output.write_daft_dataframe(mock_df)


class TestParquetOutputUtilityMethods:
    """Test ParquetOutput utility methods."""

    def test_get_full_path(self, base_output_path: str):
        """Test get_full_path method."""
        parquet_output = ParquetOutput(
            output_path=base_output_path,
            output_suffix="test_suffix",
            typename="test_table",
        )

        expected_path = os.path.join(base_output_path, "test_suffix", "test_table")
        assert parquet_output.get_full_path() == expected_path


class TestParquetOutputMetrics:
    """Test ParquetOutput metrics recording."""

    @pytest.mark.asyncio
    async def test_pandas_write_metrics(
        self, base_output_path: str, sample_dataframe: pd.DataFrame
    ):
        """Test that metrics are recorded for pandas DataFrame writes."""
        with patch(
            "application_sdk.services.objectstore.ObjectStore.upload_file"
        ) as mock_upload, patch(
            "application_sdk.outputs.parquet.get_metrics"
        ) as mock_get_metrics, patch(
            "application_sdk.outputs.parquet.get_object_store_prefix"
        ) as mock_prefix:
            mock_upload.return_value = AsyncMock()
            mock_prefix.return_value = "test/output/path"
            mock_metrics = MagicMock()
            mock_get_metrics.return_value = mock_metrics

            parquet_output = ParquetOutput(output_path=base_output_path)

            await parquet_output.write_dataframe(sample_dataframe)

            # Check that record metrics were called
            assert (
                mock_metrics.record_metric.call_count >= 2
            )  # At least records and chunks metrics

    @pytest.mark.asyncio
    async def test_daft_write_metrics(self, base_output_path: str):
        """Test that metrics are recorded for daft DataFrame writes."""
        with patch("daft.execution_config_ctx") as mock_ctx, patch(
            "application_sdk.services.objectstore.ObjectStore.upload_prefix"
        ) as mock_upload, patch(
            "application_sdk.outputs.parquet.get_metrics"
        ) as mock_get_metrics, patch(
            "application_sdk.outputs.parquet.get_object_store_prefix"
        ) as mock_prefix:
            mock_upload.return_value = AsyncMock()
            mock_prefix.return_value = "test/output/path"
            mock_ctx.return_value.__enter__ = MagicMock()
            mock_ctx.return_value.__exit__ = MagicMock()
            mock_metrics = MagicMock()
            mock_get_metrics.return_value = mock_metrics

            # Mock daft DataFrame
            mock_df = MagicMock()
            mock_df.count_rows.return_value = 1000
            mock_df.write_parquet = MagicMock()

            parquet_output = ParquetOutput(output_path=base_output_path)

            await parquet_output.write_daft_dataframe(mock_df)

            # Check that record metrics were called with correct labels
            assert (
                mock_metrics.record_metric.call_count >= 2
            )  # At least records and operations metrics

            # Verify that metrics include the correct write_mode
            calls = mock_metrics.record_metric.call_args_list
            for call in calls:
                labels = call[1]["labels"]
                assert labels["mode"] == "append"  # Uses method default "append"
                assert labels["type"] == "daft"
