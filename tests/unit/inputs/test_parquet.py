# Added os import for path manipulations used in new tests
import os
from typing import Any, Dict
from unittest.mock import patch

import pytest
from hypothesis import HealthCheck, given, settings

from application_sdk.inputs.parquet import ParquetInput
from application_sdk.test_utils.hypothesis.strategies.inputs.parquet_input import (
    file_names_strategy,
    input_prefix_strategy,
    parquet_input_config_strategy,
    safe_path_strategy,
)

# Configure Hypothesis settings at the module level
settings.register_profile(
    "parquet_input_tests", suppress_health_check=[HealthCheck.function_scoped_fixture]
)
settings.load_profile("parquet_input_tests")


@given(config=parquet_input_config_strategy)
def test_init(config: Dict[str, Any]) -> None:
    parquet_input = ParquetInput(
        path=config["path"],
        chunk_size=config["chunk_size"],
        input_prefix=config["input_prefix"],
        file_names=config["file_names"],
    )

    assert parquet_input.path == config["path"]
    assert parquet_input.chunk_size == config["chunk_size"]
    assert parquet_input.input_prefix == config["input_prefix"]
    assert parquet_input.file_names == config["file_names"]


@pytest.mark.asyncio
@given(
    path=safe_path_strategy,
    prefix=input_prefix_strategy,
    file_names=file_names_strategy,
)
async def test_not_download_file_that_exists(
    path: str, prefix: str, file_names: list[str]
) -> None:
    with patch("os.path.isdir", return_value=False), patch(
        "glob.glob"
    ) as mock_glob, patch(
        "application_sdk.services.objectstore.ObjectStore.download_file"
    ) as mock_download:
        # Mock existing parquet files
        mock_glob.return_value = ["existing.parquet"]

        parquet_input = ParquetInput(
            path=path, chunk_size=100000, input_prefix=prefix, file_names=file_names
        )

        await parquet_input.download_files(path)
        mock_download.assert_not_called()


@pytest.mark.asyncio
async def test_download_file_invoked_for_missing_files() -> None:
    """Ensure that a download is triggered when no parquet files exist and input_prefix is provided."""
    path = "/local/test.parquet"
    prefix = "remote"

    with patch("os.path.isdir", return_value=False), patch(
        "glob.glob", return_value=[]
    ), patch(
        "application_sdk.services.objectstore.ObjectStore.download_file"
    ) as mock_download:
        parquet_input = ParquetInput(
            path=path, chunk_size=100000, input_prefix=prefix, file_names=None
        )

        await parquet_input.download_files(path)

        # Should attempt to download the file
        mock_download.assert_called_once()
        # Verify call was made with keyword arguments
        args, kwargs = mock_download.call_args
        assert kwargs["destination"] == path


@pytest.mark.asyncio
async def test_download_file_not_invoked_when_file_present() -> None:
    """Ensure no download occurs when parquet files already exist."""
    path = "/local"

    with patch("os.path.isdir", return_value=True), patch(
        "glob.glob"
    ) as mock_glob, patch(
        "application_sdk.services.objectstore.ObjectStore.download_prefix"
    ) as mock_download:
        # Mock existing parquet files
        mock_glob.return_value = ["/local/exists.parquet"]

        parquet_input = ParquetInput(
            path=path, chunk_size=100000, input_prefix="remote", file_names=None
        )

        await parquet_input.download_files(path)

        mock_download.assert_not_called()


@pytest.mark.asyncio
async def test_download_file_error_when_no_files_and_no_prefix() -> None:
    """Ensure error is raised when no parquet files exist and no input prefix is provided."""
    path = "/local"

    with patch("os.path.isdir", return_value=True), patch("glob.glob", return_value=[]):
        parquet_input = ParquetInput(
            path=path, chunk_size=100000, input_prefix=None, file_names=None
        )

        with pytest.raises(ValueError, match="No parquet files found"):
            await parquet_input.download_files(path)


# ---------------------------------------------------------------------------
# Comprehensive Download Files Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_download_files_directory_path_calls_correct_method() -> None:
    """Test that directory paths call download_files_from_object_store."""
    path = "/local/directory"
    input_prefix = "remote/prefix"

    with patch("os.path.isdir", return_value=True), patch(
        "glob.glob", return_value=[]
    ), patch(
        "application_sdk.services.objectstore.ObjectStore.download_prefix"
    ) as mock_download_files, patch(
        "application_sdk.services.objectstore.ObjectStore.download_file"
    ) as mock_download_file:
        parquet_input = ParquetInput(input_prefix=input_prefix)

        await parquet_input.download_files(path)

        # Should call download_files_from_object_store for directories
        mock_download_files.assert_called_once()
        args, kwargs = mock_download_files.call_args
        assert kwargs["destination"] == path
        mock_download_file.assert_not_called()


@pytest.mark.asyncio
async def test_download_files_file_path_calls_correct_method() -> None:
    """Test that file paths call download_file_from_object_store."""
    path = "/local/file.parquet"
    input_prefix = "remote/prefix"

    with patch("os.path.isdir", return_value=False), patch(
        "glob.glob", return_value=[]
    ), patch(
        "application_sdk.services.objectstore.ObjectStore.download_prefix"
    ) as mock_download_files, patch(
        "application_sdk.services.objectstore.ObjectStore.download_file"
    ) as mock_download_file:
        parquet_input = ParquetInput(input_prefix=input_prefix)

        await parquet_input.download_files(path)

        # Should call download_file_from_object_store for files
        mock_download_file.assert_called_once()
        args, kwargs = mock_download_file.call_args
        assert kwargs["destination"] == path
        mock_download_files.assert_not_called()


@pytest.mark.asyncio
async def test_download_files_directory_with_existing_parquet_files() -> None:
    """Test that no download occurs when parquet files exist in directory."""
    path = "/local/directory"
    existing_files = [
        "/local/directory/file1.parquet",
        "/local/directory/file2.parquet",
    ]

    with patch("os.path.isdir", return_value=True), patch(
        "glob.glob"
    ) as mock_glob, patch(
        "application_sdk.services.objectstore.ObjectStore.download_prefix"
    ) as mock_download_files:
        mock_glob.return_value = existing_files

        parquet_input = ParquetInput(input_prefix="remote")
        result = await parquet_input.download_files(path)

        # Should check for files in directory
        mock_glob.assert_called_once_with(os.path.join("/local/directory", "*.parquet"))
        # Should not download since files exist
        mock_download_files.assert_not_called()
        assert result is None


@pytest.mark.asyncio
async def test_download_files_file_with_existing_parquet_file() -> None:
    """Test that no download occurs when specific parquet file exists."""
    path = "/local/specific.parquet"

    with patch("os.path.isdir", return_value=False), patch(
        "glob.glob"
    ) as mock_glob, patch(
        "application_sdk.services.objectstore.ObjectStore.download_file"
    ) as mock_download_file:
        mock_glob.return_value = [path]  # File exists

        parquet_input = ParquetInput(input_prefix="remote")
        result = await parquet_input.download_files(path)

        # Should check for specific file
        mock_glob.assert_called_once_with(path)
        # Should not download since file exists
        mock_download_file.assert_not_called()
        assert result is None


@pytest.mark.asyncio
async def test_download_files_with_logging() -> None:
    """Test that appropriate logging occurs during download."""
    path = "/local/file.parquet"
    input_prefix = "remote/prefix"

    with patch("os.path.isdir", return_value=False), patch(
        "glob.glob", return_value=[]
    ), patch("application_sdk.services.objectstore.ObjectStore.download_file"), patch(
        "application_sdk.inputs.parquet.logger"
    ) as mock_logger:
        parquet_input = ParquetInput(input_prefix=input_prefix)

        await parquet_input.download_files(path)

        # Should log the download operation
        mock_logger.info.assert_called_once_with(
            f"Reading file from object store: {path} from {input_prefix}"
        )


@pytest.mark.asyncio
async def test_download_files_error_propagation_from_object_store() -> None:
    """Test that errors from ObjectStore methods are properly propagated."""
    from application_sdk.common.error_codes import IOError as SDKIOError

    path = "/local/file.parquet"
    input_prefix = "remote/prefix"

    with patch("os.path.isdir", return_value=False), patch(
        "glob.glob", return_value=[]
    ), patch(
        "application_sdk.services.objectstore.ObjectStore.download_file",
        side_effect=SDKIOError("Download failed"),
    ):
        parquet_input = ParquetInput(input_prefix=input_prefix)

        with pytest.raises(SDKIOError, match="Download failed"):
            await parquet_input.download_files(path)


@pytest.mark.asyncio
async def test_download_files_directory_error_propagation() -> None:
    """Test that errors from directory downloads are properly propagated."""
    from application_sdk.common.error_codes import IOError as SDKIOError

    path = "/local/directory"
    input_prefix = "remote/prefix"

    with patch("os.path.isdir", return_value=True), patch(
        "glob.glob", return_value=[]
    ), patch(
        "application_sdk.services.objectstore.ObjectStore.download_prefix",
        side_effect=SDKIOError("Directory download failed"),
    ):
        parquet_input = ParquetInput(input_prefix=input_prefix)

        with pytest.raises(SDKIOError, match="Directory download failed"):
            await parquet_input.download_files(path)


@pytest.mark.asyncio
async def test_download_files_glob_patterns() -> None:
    """Test that correct glob patterns are used for different path types."""

    # Test directory glob pattern
    directory_path = "/data/parquet_files"
    with patch("os.path.isdir", return_value=True), patch(
        "glob.glob"
    ) as mock_glob, patch(
        "application_sdk.services.objectstore.ObjectStore.download_prefix"
    ):
        mock_glob.return_value = []

        parquet_input = ParquetInput(input_prefix="remote")
        await parquet_input.download_files(directory_path)

        # Should use *.parquet pattern for directories
        mock_glob.assert_called_once_with(
            os.path.join("/data/parquet_files", "*.parquet")
        )

    # Test file glob pattern
    file_path = "/data/specific_file.parquet"
    with patch("os.path.isdir", return_value=False), patch(
        "glob.glob"
    ) as mock_glob, patch(
        "application_sdk.services.objectstore.ObjectStore.download_file"
    ):
        mock_glob.return_value = []

        parquet_input = ParquetInput(input_prefix="remote")
        await parquet_input.download_files(file_path)

        # Should use direct path for files
        mock_glob.assert_called_once_with(file_path)


@pytest.mark.asyncio
async def test_download_files_with_various_file_extensions() -> None:
    """Test download behavior with different file patterns."""

    test_cases = [
        ("/data/file.parquet", False),  # Single parquet file
        ("/data/file.txt", False),  # Non-parquet file
        ("/data/", True),  # Directory
        ("/data/subdir/file.parquet", False),  # Nested file
    ]

    for path, is_dir in test_cases:
        with patch("os.path.isdir", return_value=is_dir), patch(
            "glob.glob", return_value=[]
        ), patch(
            "application_sdk.services.objectstore.ObjectStore.download_prefix"
        ) as mock_download_files, patch(
            "application_sdk.services.objectstore.ObjectStore.download_file"
        ) as mock_download_file:
            parquet_input = ParquetInput(input_prefix="remote")
            await parquet_input.download_files(path)

            if is_dir:
                mock_download_files.assert_called_once()
                args, kwargs = mock_download_files.call_args
                assert kwargs["destination"] == path
                mock_download_file.assert_not_called()
            else:
                mock_download_file.assert_called_once()
                args, kwargs = mock_download_file.call_args
                assert kwargs["destination"] == path
                mock_download_files.assert_not_called()


@pytest.mark.asyncio
async def test_download_files_return_value() -> None:
    """Test that download_files returns None as expected."""
    path = "/local/file.parquet"

    with patch("os.path.isdir", return_value=False), patch(
        "glob.glob", return_value=[]
    ), patch("application_sdk.services.objectstore.ObjectStore.download_file"):
        parquet_input = ParquetInput(input_prefix="remote")
        result = await parquet_input.download_files(path)

        # Method should return None
        assert result is None


@pytest.mark.asyncio
async def test_download_files_mixed_scenarios() -> None:
    """Test download_files with various combinations of conditions."""

    # Test 1: Directory with no files, has prefix - should download
    with patch("os.path.isdir", return_value=True), patch(
        "glob.glob", return_value=[]
    ), patch(
        "application_sdk.services.objectstore.ObjectStore.download_prefix"
    ) as mock_download:
        parquet_input = ParquetInput(input_prefix="remote")
        await parquet_input.download_files("/empty/dir")
        mock_download.assert_called_once()

    # Test 2: File with no matches, has prefix - should download
    with patch("os.path.isdir", return_value=False), patch(
        "glob.glob", return_value=[]
    ), patch(
        "application_sdk.services.objectstore.ObjectStore.download_file"
    ) as mock_download:
        parquet_input = ParquetInput(input_prefix="remote")
        await parquet_input.download_files("/missing/file.parquet")
        mock_download.assert_called_once()

    # Test 3: Directory with existing files - should not download
    with patch("os.path.isdir", return_value=True), patch(
        "glob.glob", return_value=["/dir/existing.parquet"]
    ), patch(
        "application_sdk.services.objectstore.ObjectStore.download_prefix"
    ) as mock_download:
        parquet_input = ParquetInput(input_prefix="remote")
        await parquet_input.download_files("/dir")
        mock_download.assert_not_called()


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

        class MockIloc:
            def __getitem__(self, slice_obj):
                return f"chunk-{slice_obj.start}-{slice_obj.stop}"

        return MockDataFrame()

    dummy_pandas.read_parquet = read_parquet  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "pandas", dummy_pandas)

    return call_log


@pytest.mark.asyncio
async def test_get_dataframe_with_mocked_pandas(monkeypatch) -> None:
    """Verify that get_dataframe calls pandas.read_parquet correctly."""

    path = "/data/test.parquet"
    call_log = _install_dummy_pandas(monkeypatch)

    parquet_input = ParquetInput(path=path, chunk_size=100000)

    result = await parquet_input.get_dataframe()

    # Should return the mock DataFrame
    assert hasattr(result, "data")
    assert len(result.data) == 100

    # Confirm read_parquet was invoked with correct path
    assert call_log == [{"path": path}]


@pytest.mark.asyncio
async def test_get_batched_dataframe_with_mocked_pandas(monkeypatch) -> None:
    """Verify that get_batched_dataframe streams chunks and respects chunk_size."""

    path = "/data/test.parquet"
    expected_chunksize = 30
    call_log = _install_dummy_pandas(monkeypatch)

    parquet_input = ParquetInput(path=path, chunk_size=expected_chunksize)

    chunks = [chunk async for chunk in parquet_input.get_batched_dataframe()]

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
async def test_get_batched_dataframe_no_chunk_size(monkeypatch) -> None:
    """Verify that get_batched_dataframe returns entire dataframe when no chunk_size is provided."""

    path = "/data/test.parquet"
    call_log = _install_dummy_pandas(monkeypatch)

    parquet_input = ParquetInput(path=path, chunk_size=None)

    chunks = [chunk async for chunk in parquet_input.get_batched_dataframe()]

    # Should yield the entire dataframe as one chunk
    assert len(chunks) == 1
    assert hasattr(chunks[0], "data")

    # Confirm read_parquet was invoked with correct path
    assert call_log == [{"path": path}]


@pytest.mark.asyncio
async def test_get_dataframe_with_input_prefix(monkeypatch) -> None:
    """Verify that get_dataframe downloads files when input_prefix is provided."""

    path = "/data/test.parquet"
    input_prefix = "remote"
    call_log = _install_dummy_pandas(monkeypatch)

    # Mock the OS and ObjectStore calls that download_files uses internally
    with patch("os.path.isdir", return_value=False), patch(
        "glob.glob", return_value=[]
    ), patch("application_sdk.services.objectstore.ObjectStore.download_file"):
        parquet_input = ParquetInput(path=path, input_prefix=input_prefix)

        result = await parquet_input.get_dataframe()

        # Should return the mock DataFrame
        assert hasattr(result, "data")

        # Confirm read_parquet was invoked with None (since download_files returns None and gets assigned to path)
        assert call_log == [{"path": None}]


# ---------------------------------------------------------------------------
# Daft-related helpers & tests
# ---------------------------------------------------------------------------


def _install_dummy_daft(monkeypatch):  # noqa: D401, ANN001
    import sys
    import types

    dummy_daft = types.ModuleType("daft")
    call_log: list[dict] = []

    def read_parquet(path):  # noqa: D401, ANN001
        call_log.append({"path": path})
        return f"daft_df:{path}"

    dummy_daft.read_parquet = read_parquet  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "daft", dummy_daft)

    return call_log


@pytest.mark.asyncio
async def test_get_daft_dataframe(monkeypatch) -> None:
    """Verify that get_daft_dataframe delegates to daft.read_parquet correctly."""

    call_log = _install_dummy_daft(monkeypatch)

    path = "/tmp/data"
    parquet_input = ParquetInput(path=path)

    result = await parquet_input.get_daft_dataframe()

    assert result == f"daft_df:{path}/*.parquet"
    assert call_log == [{"path": f"{path}/*.parquet"}]


@pytest.mark.asyncio
async def test_get_daft_dataframe_with_file_names(monkeypatch) -> None:
    """Verify that get_daft_dataframe works correctly with file_names parameter."""

    call_log = _install_dummy_daft(monkeypatch)

    path = "/tmp"
    file_names = ["dir/file1.parquet", "dir/file2.parquet"]

    parquet_input = ParquetInput(path=path, file_names=file_names)

    result = await parquet_input.get_daft_dataframe()

    expected_path = f"{path}/dir/*.parquet"
    assert result == f"daft_df:{expected_path}"
    assert call_log == [{"path": expected_path}]


@pytest.mark.asyncio
async def test_get_daft_dataframe_with_input_prefix(monkeypatch) -> None:
    """Verify that get_daft_dataframe downloads files when input_prefix is provided."""

    call_log = _install_dummy_daft(monkeypatch)

    # Mock the OS and ObjectStore calls that download_files uses internally
    with patch("os.path.isdir", return_value=True), patch(
        "glob.glob", return_value=[]
    ), patch("application_sdk.services.objectstore.ObjectStore.download_prefix"):
        path = "/tmp/data"
        input_prefix = "remote"

        parquet_input = ParquetInput(path=path, input_prefix=input_prefix)

        result = await parquet_input.get_daft_dataframe()

        assert result == f"daft_df:{path}/*.parquet"
        assert call_log == [{"path": f"{path}/*.parquet"}]


@pytest.mark.asyncio
async def test_get_batched_daft_dataframe_with_file_names(monkeypatch) -> None:
    """Ensure get_batched_daft_dataframe yields a frame per file when file_names provided."""

    call_log = _install_dummy_daft(monkeypatch)

    # Mock the OS and ObjectStore calls that download_files uses internally
    with patch("os.path.isdir", return_value=False), patch(
        "glob.glob", return_value=[]
    ), patch("application_sdk.services.objectstore.ObjectStore.download_file"):
        path = "/data"
        file_names = ["one.json", "two.json"]  # Note: .json extension gets replaced
        input_prefix = "remote"

        parquet_input = ParquetInput(
            path=path, file_names=file_names, input_prefix=input_prefix
        )

        frames = [frame async for frame in parquet_input.get_batched_daft_dataframe()]

        expected_frames = ["daft_df:/data/one.parquet", "daft_df:/data/two.parquet"]

        assert frames == expected_frames

        # Ensure a call was logged per file
        assert call_log == [
            {"path": "/data/one.parquet"},
            {"path": "/data/two.parquet"},
        ]


@pytest.mark.asyncio
async def test_get_batched_daft_dataframe_without_file_names(monkeypatch) -> None:
    """Ensure get_batched_daft_dataframe works with wildcard pattern when no file_names provided."""

    call_log = _install_dummy_daft(monkeypatch)

    # Mock the OS and ObjectStore calls that download_files uses internally
    with patch("os.path.isdir", return_value=True), patch(
        "glob.glob", return_value=[]
    ), patch("application_sdk.services.objectstore.ObjectStore.download_prefix"):
        path = "/data"
        input_prefix = "remote"

        parquet_input = ParquetInput(path=path, input_prefix=input_prefix)

        frames = [frame async for frame in parquet_input.get_batched_daft_dataframe()]

        expected_frames = ["daft_df:/data/*.parquet"]

        assert frames == expected_frames

        # Should have one call with wildcard pattern
        assert call_log == [{"path": "/data/*.parquet"}]


@pytest.mark.asyncio
async def test_get_batched_daft_dataframe_no_input_prefix(monkeypatch) -> None:
    """Ensure get_batched_daft_dataframe works without input_prefix (no download)."""

    call_log = _install_dummy_daft(monkeypatch)

    path = "/data"

    parquet_input = ParquetInput(path=path)

    frames = [frame async for frame in parquet_input.get_batched_daft_dataframe()]

    # When there's no input_prefix, the method doesn't yield anything
    expected_frames = []

    assert frames == expected_frames
    # No calls to daft.read_parquet should be made when there's no input_prefix
    assert call_log == []
