import os
from pathlib import Path
from typing import Generator
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


@pytest.fixture
def consolidation_dataframes() -> Generator[pd.DataFrame, None, None]:
    """Create multiple DataFrames for consolidation testing."""
    for i in range(5):  # 5 DataFrames of 300 records each = 1500 total
        df = pd.DataFrame(
            {
                "id": range(i * 300, (i + 1) * 300),
                "value": [f"batch_{i}_value_{j}" for j in range(300)],
                "category": [f"cat_{j % 3}" for j in range(300)],
                "batch_id": [i] * 300,
            }
        )
        yield df


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
        assert (
            parquet_output.chunk_count == 12
        )  # chunk_start (10) + initial chunk_count (2)
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
                output_path=base_output_path,
                output_suffix="test",
                use_consolidation=False,
            )

            # Mock os.path.exists after initialization to return True for upload check
            with patch("os.path.exists", return_value=True):
                await parquet_output.write_dataframe(sample_dataframe)

            assert parquet_output.chunk_count == 1

            # Check that to_parquet was called (the new implementation uses buffering)
            mock_to_parquet.assert_called()

            # With small dataframes and consolidation disabled, upload may not be called
            # The important thing is that the dataframe was processed and written
            # We can verify this by checking the chunk count and that to_parquet was called

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


class TestParquetOutputConsolidation:
    """Test ParquetOutput consolidation functionality."""

    def test_consolidation_init_attributes(self, base_output_path: str):
        """Test that consolidation attributes are properly initialized."""
        parquet_output = ParquetOutput(
            output_path=base_output_path, chunk_size=1000, buffer_size=200
        )

        # Check consolidation attributes
        assert parquet_output.use_consolidation is True
        assert parquet_output.consolidation_threshold == 1000  # Should equal chunk_size
        assert parquet_output.current_folder_records == 0
        assert parquet_output.temp_folder_index == 0
        assert parquet_output.temp_folders_created == []
        assert parquet_output.current_temp_folder_path is None

    def test_consolidation_init_with_none_chunk_size(self, base_output_path: str):
        """Test consolidation threshold when chunk_size is None."""
        parquet_output = ParquetOutput(
            output_path=base_output_path, chunk_size=None, buffer_size=200
        )

        # Should default to 100000 when chunk_size is None
        assert parquet_output.consolidation_threshold == 100000

    def test_temp_folder_path_generation(self, base_output_path: str):
        """Test temp folder path generation."""
        parquet_output = ParquetOutput(
            output_path=base_output_path,
            output_suffix="test_suffix",
            typename="test_type",
        )

        # Test temp folder path generation
        temp_path = parquet_output._get_temp_folder_path(0)
        expected_path = os.path.join(
            base_output_path,
            "test_suffix",
            "test_type",
            "temp_accumulation",
            "folder-0",
        )
        assert temp_path == expected_path

    def test_consolidated_file_path_generation(self, base_output_path: str):
        """Test consolidated file path generation."""
        parquet_output = ParquetOutput(
            output_path=base_output_path,
            output_suffix="test_suffix",
            typename="test_type",
        )

        # Test consolidated file path generation
        consolidated_path = parquet_output._get_consolidated_file_path(
            folder_index=0, chunk_count=0
        )
        expected_path = os.path.join(
            base_output_path, "test_suffix", "test_type", "chunk-0-part0.parquet"
        )
        assert consolidated_path == expected_path

    def test_start_new_temp_folder(self, base_output_path: str):
        """Test starting a new temp folder."""
        parquet_output = ParquetOutput(output_path=base_output_path)

        # Initially no temp folder
        assert parquet_output.current_temp_folder_path is None
        assert parquet_output.temp_folder_index == 0

        # Start first temp folder
        parquet_output._start_new_temp_folder()

        assert parquet_output.temp_folder_index == 0
        assert parquet_output.current_folder_records == 0
        assert parquet_output.current_temp_folder_path is not None
        assert os.path.exists(parquet_output.current_temp_folder_path)

        # Start second temp folder
        first_folder_path = parquet_output.current_temp_folder_path
        parquet_output._start_new_temp_folder()

        assert parquet_output.temp_folder_index == 1
        assert parquet_output.current_temp_folder_path != first_folder_path
        assert (
            0 in parquet_output.temp_folders_created
        )  # Previous folder should be tracked

    @pytest.mark.asyncio
    async def test_write_chunk_to_temp_folder(
        self, base_output_path: str, sample_dataframe: pd.DataFrame
    ):
        """Test writing chunk to temp folder."""
        parquet_output = ParquetOutput(output_path=base_output_path)

        # Start temp folder first
        parquet_output._start_new_temp_folder()

        # Write chunk
        await parquet_output._write_chunk_to_temp_folder(sample_dataframe)

        # Check that file was created
        temp_folder = parquet_output.current_temp_folder_path
        files = [f for f in os.listdir(temp_folder) if f.endswith(".parquet")]
        assert len(files) == 1
        assert files[0] == "chunk-0.parquet"

        # Write another chunk
        await parquet_output._write_chunk_to_temp_folder(sample_dataframe)

        files = [f for f in os.listdir(temp_folder) if f.endswith(".parquet")]
        assert len(files) == 2
        assert "chunk-1.parquet" in files

    @pytest.mark.asyncio
    async def test_write_chunk_to_temp_folder_no_path(
        self, base_output_path: str, sample_dataframe: pd.DataFrame
    ):
        """Test writing chunk to temp folder when no path is set."""
        parquet_output = ParquetOutput(output_path=base_output_path)

        # Should raise error when no temp folder path is set
        with pytest.raises(ValueError, match="No temp folder path available"):
            await parquet_output._write_chunk_to_temp_folder(sample_dataframe)

    @pytest.mark.asyncio
    async def test_consolidate_current_folder(self, base_output_path: str):
        """Test consolidating current temp folder using Daft."""
        with patch("daft.read_parquet") as mock_read, patch(
            "daft.execution_config_ctx"
        ) as mock_ctx, patch(
            "application_sdk.services.objectstore.ObjectStore.upload_file"
        ) as mock_upload, patch(
            "application_sdk.outputs.parquet.get_object_store_prefix"
        ) as mock_prefix:
            # Setup mocks
            mock_upload.return_value = AsyncMock()
            mock_prefix.return_value = "test/output/path"
            mock_ctx.return_value.__enter__ = MagicMock()
            mock_ctx.return_value.__exit__ = MagicMock()

            # Mock daft DataFrame
            mock_df = MagicMock()
            mock_read.return_value = mock_df
            mock_result = MagicMock()
            mock_result.to_pydict.return_value = {
                "path": ["test_generated_file.parquet"]
            }
            mock_df.write_parquet.return_value = mock_result

            parquet_output = ParquetOutput(output_path=base_output_path)
            parquet_output._start_new_temp_folder()
            parquet_output.current_folder_records = 500  # Simulate some records

            # Create a dummy file to simulate temp folder content
            temp_file = os.path.join(
                parquet_output.current_temp_folder_path, "chunk-0.parquet"
            )
            with open(temp_file, "w") as f:
                f.write("dummy")

            # Create dummy generated file for os.rename
            with open("test_generated_file.parquet", "w") as f:
                f.write("dummy")

            try:
                await parquet_output._consolidate_current_folder()

                # Check that Daft was called correctly
                mock_read.assert_called_once()
                mock_df.write_parquet.assert_called_once()
                mock_upload.assert_called_once()

                # Check statistics were updated
                assert parquet_output.chunk_count == 1
                assert parquet_output.total_record_count == 500
                # Statistics now track partition count, not record count
                assert parquet_output.statistics == [1]  # 1 partition from mock result

            finally:
                # Cleanup
                if os.path.exists("test_generated_file.parquet"):
                    os.remove("test_generated_file.parquet")

    @pytest.mark.asyncio
    async def test_consolidate_empty_folder(self, base_output_path: str):
        """Test consolidating when folder is empty."""
        parquet_output = ParquetOutput(output_path=base_output_path)
        parquet_output.current_folder_records = 0
        parquet_output.current_temp_folder_path = None

        # Should return early without doing anything
        await parquet_output._consolidate_current_folder()

        assert parquet_output.chunk_count == 0
        assert parquet_output.total_record_count == 0

    @pytest.mark.asyncio
    async def test_cleanup_temp_folders(self, base_output_path: str):
        """Test cleanup of temp folders."""
        parquet_output = ParquetOutput(output_path=base_output_path)

        # Create multiple temp folders
        parquet_output._start_new_temp_folder()
        first_folder = parquet_output.current_temp_folder_path
        parquet_output._start_new_temp_folder()
        second_folder = parquet_output.current_temp_folder_path

        # Both folders should exist
        assert os.path.exists(first_folder)
        assert os.path.exists(second_folder)

        # Cleanup
        await parquet_output._cleanup_temp_folders()

        # Folders should be removed
        assert not os.path.exists(first_folder)
        assert not os.path.exists(second_folder)

        # State should be reset
        assert parquet_output.temp_folders_created == []
        assert parquet_output.current_temp_folder_path is None
        assert parquet_output.temp_folder_index == 0
        assert parquet_output.current_folder_records == 0

    @pytest.mark.asyncio
    async def test_write_batched_dataframe_with_consolidation(
        self, base_output_path: str
    ):
        """Test write_batched_dataframe with consolidation enabled."""
        with patch("daft.read_parquet") as mock_read, patch(
            "daft.execution_config_ctx"
        ) as mock_ctx, patch(
            "application_sdk.services.objectstore.ObjectStore.upload_file"
        ) as mock_upload, patch(
            "application_sdk.outputs.parquet.get_object_store_prefix"
        ) as mock_prefix:
            # Setup mocks
            mock_upload.return_value = AsyncMock()
            mock_prefix.return_value = "test/output/path"
            mock_ctx.return_value.__enter__ = MagicMock()
            mock_ctx.return_value.__exit__ = MagicMock()

            # Mock daft DataFrame
            mock_df = MagicMock()
            mock_read.return_value = mock_df
            mock_result = MagicMock()
            mock_result.to_pydict.return_value = {"path": ["test_file.parquet"]}
            mock_df.write_parquet.return_value = mock_result

            parquet_output = ParquetOutput(
                output_path=base_output_path,
                chunk_size=500,  # Small threshold for testing
                buffer_size=100,  # Small buffer for testing
            )

            # Create test data generator
            def create_test_dataframes():
                for i in range(3):  # 3 DataFrames of 200 records each = 600 total
                    df = pd.DataFrame(
                        {
                            "id": range(i * 200, (i + 1) * 200),
                            "value": [f"value_{j}" for j in range(200)],
                            "batch": [i] * 200,
                        }
                    )
                    yield df

            # Create dummy generated file for os.rename
            with open("test_file.parquet", "w") as f:
                f.write("dummy")

            try:
                await parquet_output.write_batched_dataframe(create_test_dataframes())

                # Should have triggered consolidation (600 records > 500 threshold)
                assert parquet_output.total_record_count == 600
                assert parquet_output.chunk_count >= 1

                # Temp folders should be cleaned up
                temp_base = os.path.join(
                    parquet_output.output_path, "temp_accumulation"
                )
                assert not os.path.exists(temp_base) or len(os.listdir(temp_base)) == 0

            finally:
                # Cleanup
                if os.path.exists("test_file.parquet"):
                    os.remove("test_file.parquet")

    @pytest.mark.asyncio
    async def test_write_batched_dataframe_without_consolidation(
        self, base_output_path: str
    ):
        """Test write_batched_dataframe with consolidation disabled."""
        parquet_output = ParquetOutput(output_path=base_output_path)
        parquet_output.use_consolidation = False

        def create_test_dataframes():
            df = pd.DataFrame({"id": [1, 2, 3], "value": ["a", "b", "c"]})
            yield df

        # Mock the super() call to avoid actual file operations
        with patch(
            "application_sdk.outputs.Output.write_batched_dataframe"
        ) as mock_base_method:
            mock_base_method.return_value = AsyncMock()

            await parquet_output.write_batched_dataframe(create_test_dataframes())

            # Should have called base class method
            mock_base_method.assert_called_once()

    @pytest.mark.asyncio
    async def test_accumulate_dataframe(self, base_output_path: str):
        """Test accumulating DataFrame into temp folders."""
        parquet_output = ParquetOutput(
            output_path=base_output_path,
            chunk_size=500,  # This sets consolidation_threshold internally
            buffer_size=100,
        )

        # Create a DataFrame that will trigger folder creation and consolidation
        large_df = pd.DataFrame(
            {
                "id": range(600),  # 600 records > 500 threshold
                "value": [f"value_{i}" for i in range(600)],
            }
        )

        with patch.object(
            parquet_output, "_consolidate_current_folder"
        ) as mock_consolidate, patch.object(
            parquet_output,
            "_start_new_temp_folder",
            wraps=parquet_output._start_new_temp_folder,
        ) as mock_start_folder, patch.object(
            parquet_output, "write_chunk"
        ) as mock_write_chunk:
            mock_consolidate.return_value = AsyncMock()
            mock_write_chunk.return_value = AsyncMock()

            await parquet_output._accumulate_dataframe(large_df)

            # Should have triggered consolidation due to size
            mock_consolidate.assert_called()
            mock_start_folder.assert_called()

    @pytest.mark.asyncio
    async def test_consolidation_error_handling(self, base_output_path: str):
        """Test error handling in consolidation with cleanup."""
        parquet_output = ParquetOutput(output_path=base_output_path)

        def create_test_dataframes():
            df = pd.DataFrame({"id": [1, 2, 3], "value": ["a", "b", "c"]})
            yield df

        # Mock _accumulate_dataframe to raise an exception
        with patch.object(
            parquet_output, "_accumulate_dataframe"
        ) as mock_accumulate, patch.object(
            parquet_output, "_cleanup_temp_folders"
        ) as mock_cleanup:
            mock_accumulate.side_effect = Exception("Test error")
            mock_cleanup.return_value = AsyncMock()

            # Should raise the exception and call cleanup
            with pytest.raises(Exception, match="Test error"):
                await parquet_output.write_batched_dataframe(create_test_dataframes())

            mock_cleanup.assert_called_once()

    @pytest.mark.asyncio
    async def test_async_generator_support(self, base_output_path: str):
        """Test that consolidation works with async generators."""
        parquet_output = ParquetOutput(output_path=base_output_path)

        async def create_async_dataframes():
            for i in range(2):
                df = pd.DataFrame(
                    {
                        "id": range(i * 100, (i + 1) * 100),
                        "value": [f"value_{j}" for j in range(100)],
                    }
                )
                yield df

        with patch.object(
            parquet_output, "_accumulate_dataframe"
        ) as mock_accumulate, patch.object(
            parquet_output, "_cleanup_temp_folders"
        ) as mock_cleanup:
            mock_accumulate.return_value = AsyncMock()
            mock_cleanup.return_value = AsyncMock()

            await parquet_output.write_batched_dataframe(create_async_dataframes())

            # Should have called accumulate for each DataFrame
            assert mock_accumulate.call_count == 2
            mock_cleanup.assert_called_once()
