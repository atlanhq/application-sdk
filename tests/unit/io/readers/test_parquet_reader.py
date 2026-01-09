# Added os import for path manipulations used in new tests
import os
from typing import Any, Dict
from unittest.mock import patch

import pytest
from hypothesis import HealthCheck, given, settings

from application_sdk.common.types import DataframeType
from application_sdk.io.parquet import ParquetFileReader
from application_sdk.io.utils import download_files
from application_sdk.test_utils.hypothesis.strategies.inputs.parquet_input import (
    parquet_input_config_strategy,
)

# Configure Hypothesis settings at the module level
settings.register_profile(
    "parquet_input_tests", suppress_health_check=[HealthCheck.function_scoped_fixture]
)
settings.load_profile("parquet_input_tests")


@given(config=parquet_input_config_strategy)
def test_init(config: Dict[str, Any]) -> None:
    parquet_input = ParquetFileReader(
        path=config["path"],
        chunk_size=config["chunk_size"],
        file_names=config["file_names"],
        dataframe_type=DataframeType.pandas,
    )

    assert parquet_input.path == config["path"]
    assert parquet_input.chunk_size == config["chunk_size"]
    assert parquet_input.file_names == config["file_names"]


def test_init_single_file_with_file_names_raises_error() -> None:
    """Test that ParquetFileReader raises ValueError when single file path is combined with file_names."""
    with pytest.raises(ValueError, match="Cannot specify both a single file path"):
        ParquetFileReader(
            path="/data/test.parquet",
            file_names=["other.parquet"],
            dataframe_type=DataframeType.pandas,
        )


@pytest.mark.asyncio
async def test_not_download_file_that_exists() -> None:
    """Test that no download occurs when a parquet file exists locally."""
    path = "/data/test.parquet"  # Path with correct extension
    # Don't use file_names with single file path due to validation

    with patch("os.path.isfile", return_value=True), patch(
        "application_sdk.services.objectstore.ObjectStore.download_file"
    ) as mock_download:
        parquet_input = ParquetFileReader(
            path=path,
            chunk_size=100000,  # No file_names
            dataframe_type=DataframeType.pandas,
        )

        result = await download_files(
            parquet_input.path, ".parquet", parquet_input.file_names
        )
        mock_download.assert_not_called()
        assert result == [path]


@pytest.mark.asyncio
async def test_download_file_invoked_for_missing_files() -> None:
    """Ensure that a download is triggered when no parquet files exist locally."""
    path = "/local/test.parquet"

    with patch("os.path.isfile", side_effect=[False, True]), patch(
        "os.path.isdir", return_value=False
    ), patch("glob.glob", return_value=[]), patch(
        "application_sdk.services.objectstore.ObjectStore.download_file"
    ) as mock_download, patch(
        "application_sdk.activities.common.utils.get_object_store_prefix",
        return_value="local/test.parquet",
    ):
        parquet_input = ParquetFileReader(
            path=path, chunk_size=100000, dataframe_type=DataframeType.pandas
        )

        result = await download_files(
            parquet_input.path, ".parquet", parquet_input.file_names
        )

        # Should attempt to download the file
        mock_download.assert_called_once_with(
            source="local/test.parquet", destination="./local/tmp/local/test.parquet"
        )
        # Result should be the actual downloaded file path in temporary directory
        expected_path = "./local/tmp/local/test.parquet"
        assert result == [expected_path]


# ---------------------------------------------------------------------------
# Base Class Download Files Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_download_files_uses_base_class() -> None:
    """Test that ParquetFileReader uses the base class download_files method."""
    path = "/data/test.parquet"
    parquet_input = ParquetFileReader(path=path, dataframe_type=DataframeType.pandas)

    with patch("os.path.isfile", return_value=True):
        result = await download_files(
            parquet_input.path, ".parquet", parquet_input.file_names
        )

        assert result == [path]


# ---------------------------------------------------------------------------
# Pandas-related helpers & tests
# ---------------------------------------------------------------------------


# Helper to install dummy pandas module and capture read_parquet invocations
def _install_dummy_pandas(monkeypatch):
    """Install a dummy pandas module in sys.modules that tracks calls to read_parquet."""
    import sys
    import types

    dummy_pandas = types.ModuleType("pandas")
    call_log: list[dict] = []

    # Define MockIloc class once for reuse
    class MockIloc:
        def __getitem__(self, slice_obj):
            return f"chunk-{slice_obj.start}-{slice_obj.stop}"

    def read_parquet(path):  # noqa: D401, ANN001
        call_log.append({"path": path})

        # Return a mock DataFrame with length for chunking
        class MockDataFrame:
            def __init__(self):
                self.data = list(range(100))  # 100 rows for chunking tests

            def __len__(self):
                return len(self.data)

            @property
            def iloc(self):
                return MockIloc()

        return MockDataFrame()

    def concat(objs, ignore_index=None):  # noqa: D401, ANN001
        # Return a mock DataFrame that combines all input DataFrames
        class CombinedMockDataFrame:
            def __init__(self):
                # Combine data from all input DataFrames
                total_data = []
                for obj in objs:
                    if hasattr(obj, "data"):
                        total_data.extend(obj.data)
                    else:
                        total_data.extend(range(100))  # Default data
                self.data = total_data

            def __len__(self):
                return len(self.data)

            @property
            def iloc(self):
                return MockIloc()

        return CombinedMockDataFrame()

    dummy_pandas.read_parquet = read_parquet  # type: ignore[attr-defined]
    dummy_pandas.concat = concat  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "pandas", dummy_pandas)

    return call_log


@pytest.mark.asyncio
async def test_read_with_mocked_pandas(monkeypatch) -> None:
    """Verify that read calls pandas.read_parquet correctly."""

    path = "/data/test.parquet"
    call_log = _install_dummy_pandas(monkeypatch)

    # Mock download_files to return the path
    async def dummy_download(path, file_extension, file_names=None):  # noqa: D401, ANN001
        return [path]  # Return the path as a list of files

    # Mock the base Input class method since ParquetFileReader calls super().download_files()
    monkeypatch.setattr(
        "application_sdk.io.parquet.download_files", dummy_download, raising=False
    )

    parquet_input = ParquetFileReader(path=path, chunk_size=100000)

    result = await parquet_input.read()

    # Should return the mock DataFrame
    assert hasattr(result, "data")
    assert len(result.data) == 100

    # Confirm read_parquet was invoked with correct path
    assert call_log == [{"path": path}]


@pytest.mark.asyncio
async def test_read_batches_with_mocked_pandas(monkeypatch) -> None:
    """Verify that read_batches streams chunks and respects chunk_size."""

    path = "/data/test.parquet"
    expected_chunksize = 30
    call_log = _install_dummy_pandas(monkeypatch)

    # Mock download_files to return the path
    async def dummy_download(path, file_extension, file_names=None):  # noqa: D401, ANN001
        return [path]  # Return the path as a list of files

    # Mock the base Input class method since ParquetFileReader calls super().download_files()
    monkeypatch.setattr(
        "application_sdk.io.parquet.download_files", dummy_download, raising=False
    )

    parquet_input = ParquetFileReader(
        path=path, chunk_size=expected_chunksize, dataframe_type=DataframeType.pandas
    )

    batches = parquet_input.read_batches()
    chunks = [chunk async for chunk in batches]

    # With 100 rows and chunk_size=30, we should get 4 chunks
    expected_chunks = [
        "chunk-0-30",
        "chunk-30-60",
        "chunk-60-90",
        "chunk-90-120",  # Last chunk goes to end
    ]
    assert chunks == expected_chunks

    # Confirm read_parquet was invoked with correct path
    assert call_log == [{"path": path}]


@pytest.mark.asyncio
async def test_read_batches_with_chunk_size(monkeypatch) -> None:
    """Verify that read_batches chunks data properly with specified chunk_size."""

    path = "/data/test.parquet"
    call_log = _install_dummy_pandas(monkeypatch)

    # Mock download_files to return the path
    async def dummy_download(path, file_extension, file_names=None):  # noqa: D401, ANN001
        return [path]  # Return the path as a list of files

    # Mock the base Input class method since ParquetFileReader calls super().download_files()
    monkeypatch.setattr(
        "application_sdk.io.parquet.download_files", dummy_download, raising=False
    )

    parquet_input = ParquetFileReader(
        path=path, chunk_size=100, dataframe_type=DataframeType.pandas
    )

    batches = parquet_input.read_batches()
    chunks = [chunk async for chunk in batches]

    # With 100 rows and chunk_size=100, we should get 1 chunk
    assert len(chunks) == 1
    assert chunks[0] == "chunk-0-100"

    # Confirm read_parquet was invoked with correct path
    assert call_log == [{"path": path}]


# Test removed - input_prefix parameter no longer exists


# ---------------------------------------------------------------------------
# Daft-related helpers & tests
# ---------------------------------------------------------------------------


def _install_dummy_daft(monkeypatch):  # noqa: D401, ANN001
    import sys
    import types

    dummy_daft = types.ModuleType("daft")
    call_log: list[dict] = []

    class MockDaftDataFrame:
        def __init__(self, path):
            self.path = path
            # Simulate 100 rows total for chunking tests
            self._total_rows = 100
            self._offset = 0
            self._limit = None

        def count_rows(self):
            return self._total_rows

        def offset(self, offset_val):
            new_df = MockDaftDataFrame(self.path)
            new_df._total_rows = self._total_rows
            new_df._offset = offset_val
            new_df._limit = self._limit
            return new_df

        def limit(self, limit_val):
            new_df = MockDaftDataFrame(self.path)
            new_df._total_rows = self._total_rows
            new_df._offset = self._offset
            new_df._limit = limit_val
            return new_df

        def __str__(self):
            if isinstance(self.path, list):
                # For multiple files, return representation for first file
                return f"daft_df:{self.path[0] if self.path else 'unknown'}"
            return f"daft_df:{self.path}"

    def read_parquet(path, _chunk_size=None):  # noqa: D401, ANN001
        call_log.append({"path": path})
        if isinstance(path, list) and len(path) > 1:
            # For read_batches tests that need MockDaftDataFrame
            return MockDaftDataFrame(path)
        elif isinstance(path, list):
            # For read tests that expect simple string return
            return f"daft_df:{path}"
        return MockDaftDataFrame(path)

    dummy_daft.read_parquet = read_parquet  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "daft", dummy_daft)

    return call_log


@pytest.mark.asyncio
async def test_read(monkeypatch) -> None:
    """Verify that read delegates to pandas.read_parquet correctly."""

    call_log = _install_dummy_pandas(monkeypatch)

    # Mock download_files to return a list of files
    async def dummy_download(path, file_extension, file_names=None):  # noqa: D401, ANN001
        return [f"{path}/file1.parquet", f"{path}/file2.parquet"]

    # Mock the base Input class method since ParquetFileReader calls super().download_files()
    monkeypatch.setattr(
        "application_sdk.io.parquet.download_files", dummy_download, raising=False
    )

    path = "/tmp/data"
    parquet_input = ParquetFileReader(path=path, dataframe_type=DataframeType.pandas)

    result = await parquet_input.read()

    expected_files = ["/tmp/data/file1.parquet", "/tmp/data/file2.parquet"]
    # Since we have multiple files, pandas.concat returns a CombinedMockDataFrame object
    assert hasattr(
        result, "data"
    ), "Result should be a CombinedMockDataFrame with data attribute"
    assert len(result.data) == 200  # 100 rows from each file
    assert call_log == [{"path": expected_files[0]}, {"path": expected_files[1]}]


@pytest.mark.asyncio
async def test_read_with_file_names(monkeypatch) -> None:
    """Verify that read works correctly with file_names parameter."""

    call_log = _install_dummy_daft(monkeypatch)

    # Mock download_files to return the specific files
    async def dummy_download(path, file_extension, file_names=None):  # noqa: D401, ANN001
        return (
            [os.path.join(path, fn).replace(os.path.sep, "/") for fn in file_names]
            if file_names
            else []
        )

    # Mock the base Input class method since ParquetFileReader calls super().download_files()
    monkeypatch.setattr(
        "application_sdk.io.parquet.download_files", dummy_download, raising=False
    )

    path = "/tmp"
    file_names = ["dir/file1.parquet", "dir/file2.parquet"]

    parquet_input = ParquetFileReader(
        path=path, file_names=file_names, dataframe_type=DataframeType.daft
    )

    result = await parquet_input.read()

    expected_files = ["/tmp/dir/file1.parquet", "/tmp/dir/file2.parquet"]
    # Since we have multiple files, the mock returns a MockDaftDataFrame object
    assert hasattr(
        result, "path"
    ), "Result should be a MockDaftDataFrame with path attribute"
    assert result.path == expected_files
    assert call_log == [{"path": expected_files}]


@pytest.mark.asyncio
async def test_read_with_input_prefix(monkeypatch) -> None:
    """Verify that read downloads files when input_prefix is provided."""

    call_log = _install_dummy_pandas(monkeypatch)

    # Mock download_files to return a list of files
    async def dummy_download(path, file_extension, file_names=None):  # noqa: D401, ANN001
        return [f"{path}/file1.parquet", f"{path}/file2.parquet"]

    # Mock the base Input class method since ParquetFileReader calls super().download_files()
    monkeypatch.setattr(
        "application_sdk.io.parquet.download_files", dummy_download, raising=False
    )

    path = "/tmp/data"
    parquet_input = ParquetFileReader(path=path, dataframe_type=DataframeType.pandas)

    result = await parquet_input.read()

    expected_files = ["/tmp/data/file1.parquet", "/tmp/data/file2.parquet"]
    # Since we have multiple files, pandas.concat returns a CombinedMockDataFrame object
    assert hasattr(
        result, "data"
    ), "Result should be a CombinedMockDataFrame with data attribute"
    assert len(result.data) == 200  # 100 rows from each file
    assert call_log == [{"path": expected_files[0]}, {"path": expected_files[1]}]


@pytest.mark.asyncio
async def test_read_batches_with_file_names(monkeypatch) -> None:
    """Ensure read_batches yields chunks from combined files when file_names provided."""

    call_log = _install_dummy_daft(monkeypatch)

    # Mock download_files to return the specific files
    async def dummy_download(path, file_extension, file_names=None):  # noqa: D401, ANN001
        return (
            [os.path.join(path, fn).replace(os.path.sep, "/") for fn in file_names]
            if file_names
            else []
        )

    # Mock the base Input class method since ParquetFileReader calls super().download_files()
    monkeypatch.setattr(
        "application_sdk.io.parquet.download_files", dummy_download, raising=False
    )

    path = "/data"
    file_names = [
        "one.parquet",
        "two.parquet",
    ]
    parquet_input = ParquetFileReader(
        path=path,
        file_names=file_names,
        buffer_size=50,
        dataframe_type=DataframeType.daft,
    )

    batches = parquet_input.read_batches()
    frames = [frame async for frame in batches]

    # With 100 total rows and buffer_size=50, expect 2 chunks
    assert len(frames) == 2

    # Ensure daft.read_parquet was called with the file list
    assert call_log == [
        {"path": ["/data/one.parquet", "/data/two.parquet"]},
    ]


@pytest.mark.asyncio
async def test_read_batches_without_file_names(monkeypatch) -> None:
    """Ensure read_batches works with chunked processing when no file_names provided."""

    call_log = _install_dummy_daft(monkeypatch)

    # Mock download_files to return a list of files
    async def dummy_download(path, file_extension, file_names=None):  # noqa: D401, ANN001
        return [f"{path}/file1.parquet", f"{path}/file2.parquet"]

    # Mock the base Input class method since ParquetFileReader calls super().download_files()
    monkeypatch.setattr(
        "application_sdk.io.parquet.download_files", dummy_download, raising=False
    )

    path = "/data"
    parquet_input = ParquetFileReader(
        path=path, buffer_size=50, dataframe_type=DataframeType.daft
    )

    batches = parquet_input.read_batches()
    frames = [frame async for frame in batches]

    # With 100 total rows and buffer_size=50, expect 2 chunks
    assert len(frames) == 2

    # Should have one call with the file list
    assert call_log == [
        {"path": ["/data/file1.parquet", "/data/file2.parquet"]},
    ]


@pytest.mark.asyncio
async def test_read_batches_no_input_prefix(monkeypatch) -> None:
    """Ensure read_batches works with chunked processing without input_prefix."""

    call_log = _install_dummy_daft(monkeypatch)

    # Mock download_files to return a list of files
    async def dummy_download(path, file_extension, file_names=None):  # noqa: D401, ANN001
        return [f"{path}/file1.parquet", f"{path}/file2.parquet"]

    # Mock the base Input class method since ParquetFileReader calls super().download_files()
    monkeypatch.setattr(
        "application_sdk.io.parquet.download_files", dummy_download, raising=False
    )

    path = "/data"

    parquet_input = ParquetFileReader(
        path=path, buffer_size=50, dataframe_type=DataframeType.daft
    )

    batches = parquet_input.read_batches()
    frames = [frame async for frame in batches]

    # With 100 total rows and buffer_size=50, expect 2 chunks
    assert len(frames) == 2
    # Should have one call with the file list
    assert call_log == [
        {"path": ["/data/file1.parquet", "/data/file2.parquet"]},
    ]


# ---------------------------------------------------------------------------
# Context Manager and Close Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_context_manager_calls_close(monkeypatch) -> None:
    """Verify that using async with calls close() on exit."""
    _install_dummy_pandas(monkeypatch)

    async def dummy_download(path, file_extension, file_names=None):  # noqa: D401, ANN001
        return [path]

    monkeypatch.setattr(
        "application_sdk.io.parquet.download_files", dummy_download, raising=False
    )

    path = "/data/test.parquet"

    async with ParquetFileReader(
        path=path, dataframe_type=DataframeType.pandas
    ) as reader:
        await reader.read()
        assert not reader._is_closed

    # After exiting context, reader should be closed
    assert reader._is_closed


@pytest.mark.asyncio
async def test_close_is_idempotent() -> None:
    """Verify that calling close() multiple times is safe."""
    path = "/data/test.parquet"
    reader = ParquetFileReader(path=path, dataframe_type=DataframeType.pandas)

    # Close multiple times - should not raise
    await reader.close()
    assert reader._is_closed

    await reader.close()  # Should be a no-op
    assert reader._is_closed

    await reader.close()  # Should still be a no-op
    assert reader._is_closed


@pytest.mark.asyncio
async def test_read_after_close_raises_error(monkeypatch) -> None:
    """Verify that reading after close raises ValueError."""
    _install_dummy_pandas(monkeypatch)

    async def dummy_download(path, file_extension, file_names=None):  # noqa: D401, ANN001
        return [path]

    monkeypatch.setattr(
        "application_sdk.io.parquet.download_files", dummy_download, raising=False
    )

    path = "/data/test.parquet"
    reader = ParquetFileReader(path=path, dataframe_type=DataframeType.pandas)

    # Read should work before close
    await reader.read()

    # Close the reader
    await reader.close()

    # Read should raise after close
    with pytest.raises(ValueError, match="Cannot read from a closed reader"):
        await reader.read()


@pytest.mark.asyncio
async def test_read_batches_after_close_raises_error() -> None:
    """Verify that read_batches after close raises ValueError."""
    path = "/data/test.parquet"
    reader = ParquetFileReader(path=path, dataframe_type=DataframeType.pandas)

    # Close the reader
    await reader.close()

    # read_batches should raise after close
    with pytest.raises(ValueError, match="Cannot read from a closed reader"):
        reader.read_batches()


@pytest.mark.asyncio
async def test_cleanup_on_close_default_true() -> None:
    """Verify that cleanup_on_close defaults to True."""
    path = "/data/test.parquet"
    reader = ParquetFileReader(path=path, dataframe_type=DataframeType.pandas)

    assert reader.cleanup_on_close is True


@pytest.mark.asyncio
async def test_cleanup_on_close_false_retains_files(monkeypatch) -> None:
    """Verify that setting cleanup_on_close=False retains downloaded files."""
    _install_dummy_pandas(monkeypatch)

    downloaded_files = [
        "/tmp/downloaded/file1.parquet",
        "/tmp/downloaded/file2.parquet",
    ]

    async def dummy_download(path, file_extension, file_names=None):  # noqa: D401, ANN001
        return downloaded_files

    monkeypatch.setattr(
        "application_sdk.io.parquet.download_files", dummy_download, raising=False
    )

    path = "/data"
    reader = ParquetFileReader(
        path=path,
        dataframe_type=DataframeType.pandas,
        cleanup_on_close=False,
    )

    # Read to trigger download tracking
    await reader.read()

    # Verify files are tracked
    assert reader._downloaded_files == downloaded_files

    # Mock cleanup to track if it's called
    cleanup_called = False

    async def mock_cleanup():
        nonlocal cleanup_called
        cleanup_called = True

    monkeypatch.setattr(reader, "_cleanup_downloaded_files", mock_cleanup)

    # Close should NOT call cleanup when cleanup_on_close=False
    await reader.close()

    assert not cleanup_called
    assert reader._is_closed


@pytest.mark.asyncio
async def test_cleanup_on_close_true_cleans_files(monkeypatch) -> None:
    """Verify that setting cleanup_on_close=True cleans up downloaded files."""
    _install_dummy_pandas(monkeypatch)

    downloaded_files = [
        "/tmp/downloaded/file1.parquet",
        "/tmp/downloaded/file2.parquet",
    ]

    async def dummy_download(path, file_extension, file_names=None):  # noqa: D401, ANN001
        return downloaded_files

    monkeypatch.setattr(
        "application_sdk.io.parquet.download_files", dummy_download, raising=False
    )

    path = "/data"
    reader = ParquetFileReader(
        path=path,
        dataframe_type=DataframeType.pandas,
        cleanup_on_close=True,
    )

    # Read to trigger download tracking
    await reader.read()

    # Verify files are tracked
    assert reader._downloaded_files == downloaded_files

    # Mock cleanup to track if it's called
    cleanup_called = False

    async def mock_cleanup():
        nonlocal cleanup_called
        cleanup_called = True
        reader._downloaded_files.clear()

    monkeypatch.setattr(reader, "_cleanup_downloaded_files", mock_cleanup)

    # Close should call cleanup when cleanup_on_close=True
    await reader.close()

    assert cleanup_called
    assert reader._is_closed


@pytest.mark.asyncio
async def test_downloaded_files_tracked_on_read(monkeypatch) -> None:
    """Verify that downloaded files are tracked when read() is called."""
    _install_dummy_pandas(monkeypatch)

    downloaded_files = ["/tmp/downloaded/file1.parquet"]

    async def dummy_download(path, file_extension, file_names=None):  # noqa: D401, ANN001
        return downloaded_files

    monkeypatch.setattr(
        "application_sdk.io.parquet.download_files", dummy_download, raising=False
    )

    path = "/data/test.parquet"
    reader = ParquetFileReader(path=path, dataframe_type=DataframeType.pandas)

    # Initially no downloaded files
    assert reader._downloaded_files == []

    # Read to trigger download
    await reader.read()

    # Files should now be tracked
    assert reader._downloaded_files == downloaded_files
