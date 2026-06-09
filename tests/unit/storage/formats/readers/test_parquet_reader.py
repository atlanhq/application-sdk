import os
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from hypothesis import HealthCheck, given, settings

from application_sdk.common.types import DataframeType
from application_sdk.storage.formats.parquet import (
    ParquetFileReader,
    _build_unified_daft_schema,
)
from application_sdk.storage.formats.utils import _download_files
from application_sdk.testing.hypothesis.strategies.inputs.parquet_input import (
    parquet_input_config_strategy,
)

_MOCK_STORE = MagicMock()
_FIXED_HEX = "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4"
_DL_ID = _FIXED_HEX[:12]
_EXPECTED_TMP = str(Path("./local/tmp/") / _DL_ID)

# Configure Hypothesis settings at the module level
settings.register_profile(
    "parquet_input_tests", suppress_health_check=[HealthCheck.function_scoped_fixture]
)
settings.load_profile("parquet_input_tests")


@given(config=parquet_input_config_strategy)
def test_init(config: dict[str, Any]) -> None:
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
    """Test that ParquetFileReader raises when single file path is combined with file_names."""
    from application_sdk.storage.formats.format_errors import (
        SingleFilePathWithFileNamesError,
    )

    with pytest.raises(SingleFilePathWithFileNamesError) as exc_info:
        ParquetFileReader(
            path="/data/test.parquet",
            file_names=["other.parquet"],
            dataframe_type=DataframeType.pandas,
        )
    assert exc_info.value.code == "INVALID_INPUT_FORMAT_SINGLE_PATH_WITH_FILE_NAMES"


@pytest.mark.asyncio
async def test_not_download_file_that_exists() -> None:
    """Test that no download occurs when a parquet file exists locally."""
    path = "/data/test.parquet"

    with (
        patch("os.path.isfile", return_value=True),
        patch(
            "application_sdk.storage.formats.utils._download_one",
            new_callable=AsyncMock,
        ) as mock_download_one,
    ):
        parquet_input = ParquetFileReader(
            path=path,
            chunk_size=100000,
            dataframe_type=DataframeType.pandas,
        )

        result = await _download_files(
            parquet_input.path, ".parquet", parquet_input.file_names
        )
        mock_download_one.assert_not_called()
        assert result == [path]


@pytest.mark.asyncio
async def test_download_file_invoked_for_missing_files() -> None:
    """Ensure that a download is triggered when no parquet files exist locally."""
    path = "/local/test.parquet"

    with (
        patch("os.path.isfile", return_value=False),
        patch("os.path.isdir", return_value=False),
        patch("glob.glob", return_value=[]),
        patch(
            "application_sdk.storage.formats.utils._resolve_store",
            return_value=_MOCK_STORE,
        ),
        patch(
            "application_sdk.storage.formats.utils._download_one",
            new_callable=AsyncMock,
            return_value=(True, "downloaded"),
        ) as mock_download_one,
        patch("uuid.uuid4") as mock_uuid4,
    ):
        mock_uuid4.return_value.hex = _FIXED_HEX
        parquet_input = ParquetFileReader(
            path=path, chunk_size=100000, dataframe_type=DataframeType.pandas
        )

        result = await _download_files(
            parquet_input.path, ".parquet", parquet_input.file_names
        )

        expected_local = Path(_EXPECTED_TMP) / "local/test.parquet"
        mock_download_one.assert_called_once_with(
            _MOCK_STORE,
            "local/test.parquet",
            expected_local,
            skip_if_exists=False,
        )
        assert result == [str(expected_local)]


# ---------------------------------------------------------------------------
# Base Class Download Files Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test__download_files_uses_base_class() -> None:
    """Test that ParquetFileReader uses the base class _download_files method."""
    path = "/data/test.parquet"
    parquet_input = ParquetFileReader(path=path, dataframe_type=DataframeType.pandas)

    with patch("os.path.isfile", return_value=True):
        result = await _download_files(
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

    def read_parquet(path):
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

    def concat(objs, ignore_index=None):
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

    # Mock _download_files to return the path
    async def dummy_download(path, file_extension, file_names=None):
        return [path]  # Return the path as a list of files

    # Mock the base Input class method since ParquetFileReader calls super()._download_files()
    monkeypatch.setattr(
        "application_sdk.storage.formats.parquet._download_files",
        dummy_download,
        raising=False,
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

    # Mock _download_files to return the path
    async def dummy_download(path, file_extension, file_names=None):
        return [path]  # Return the path as a list of files

    # Mock the base Input class method since ParquetFileReader calls super()._download_files()
    monkeypatch.setattr(
        "application_sdk.storage.formats.parquet._download_files",
        dummy_download,
        raising=False,
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

    # Mock _download_files to return the path
    async def dummy_download(path, file_extension, file_names=None):
        return [path]  # Return the path as a list of files

    # Mock the base Input class method since ParquetFileReader calls super()._download_files()
    monkeypatch.setattr(
        "application_sdk.storage.formats.parquet._download_files",
        dummy_download,
        raising=False,
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


def _install_dummy_daft(monkeypatch):
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

    def read_parquet(path, _chunk_size=None, **kwargs):
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

    # Mock _download_files to return a list of files
    async def dummy_download(path, file_extension, file_names=None):
        return [f"{path}/file1.parquet", f"{path}/file2.parquet"]

    # Mock the base Input class method since ParquetFileReader calls super()._download_files()
    monkeypatch.setattr(
        "application_sdk.storage.formats.parquet._download_files",
        dummy_download,
        raising=False,
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

    # Mock _download_files to return the specific files
    async def dummy_download(path, file_extension, file_names=None):
        return (
            [os.path.join(path, fn).replace(os.path.sep, "/") for fn in file_names]
            if file_names
            else []
        )

    # Mock the base Input class method since ParquetFileReader calls super()._download_files()
    monkeypatch.setattr(
        "application_sdk.storage.formats.parquet._download_files",
        dummy_download,
        raising=False,
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

    # Mock _download_files to return a list of files
    async def dummy_download(path, file_extension, file_names=None):
        return [f"{path}/file1.parquet", f"{path}/file2.parquet"]

    # Mock the base Input class method since ParquetFileReader calls super()._download_files()
    monkeypatch.setattr(
        "application_sdk.storage.formats.parquet._download_files",
        dummy_download,
        raising=False,
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

    # Mock _download_files to return the specific files
    async def dummy_download(path, file_extension, file_names=None):
        return (
            [os.path.join(path, fn).replace(os.path.sep, "/") for fn in file_names]
            if file_names
            else []
        )

    # Mock the base Input class method since ParquetFileReader calls super()._download_files()
    monkeypatch.setattr(
        "application_sdk.storage.formats.parquet._download_files",
        dummy_download,
        raising=False,
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

    # Mock _download_files to return a list of files
    async def dummy_download(path, file_extension, file_names=None):
        return [f"{path}/file1.parquet", f"{path}/file2.parquet"]

    # Mock the base Input class method since ParquetFileReader calls super()._download_files()
    monkeypatch.setattr(
        "application_sdk.storage.formats.parquet._download_files",
        dummy_download,
        raising=False,
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

    # Mock _download_files to return a list of files
    async def dummy_download(path, file_extension, file_names=None):
        return [f"{path}/file1.parquet", f"{path}/file2.parquet"]

    # Mock the base Input class method since ParquetFileReader calls super()._download_files()
    monkeypatch.setattr(
        "application_sdk.storage.formats.parquet._download_files",
        dummy_download,
        raising=False,
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

    async def dummy_download(path, file_extension, file_names=None):
        return [path]

    monkeypatch.setattr(
        "application_sdk.storage.formats.parquet._download_files",
        dummy_download,
        raising=False,
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

    async def dummy_download(path, file_extension, file_names=None):
        return [path]

    monkeypatch.setattr(
        "application_sdk.storage.formats.parquet._download_files",
        dummy_download,
        raising=False,
    )

    path = "/data/test.parquet"
    reader = ParquetFileReader(path=path, dataframe_type=DataframeType.pandas)

    # Read should work before close
    await reader.read()

    # Close the reader
    await reader.close()

    # Read should raise after close
    from application_sdk.storage.formats.format_errors import ReaderClosedError

    with pytest.raises(ReaderClosedError) as exc_info:
        await reader.read()
    assert exc_info.value.code == "PRECONDITION_FORMAT_READER_CLOSED"


@pytest.mark.asyncio
async def test_read_batches_after_close_raises_error() -> None:
    """Verify that read_batches after close raises ReaderClosedError."""
    from application_sdk.storage.formats.format_errors import ReaderClosedError

    path = "/data/test.parquet"
    reader = ParquetFileReader(path=path, dataframe_type=DataframeType.pandas)

    # Close the reader
    await reader.close()

    # read_batches should raise after close
    with pytest.raises(ReaderClosedError) as exc_info:
        reader.read_batches()
    assert exc_info.value.code == "PRECONDITION_FORMAT_READER_CLOSED"


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

    async def dummy_download(path, file_extension, file_names=None):
        return downloaded_files

    monkeypatch.setattr(
        "application_sdk.storage.formats.parquet._download_files",
        dummy_download,
        raising=False,
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

    async def dummy_download(path, file_extension, file_names=None):
        return downloaded_files

    monkeypatch.setattr(
        "application_sdk.storage.formats.parquet._download_files",
        dummy_download,
        raising=False,
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

    async def dummy_download(path, file_extension, file_names=None):
        return downloaded_files

    monkeypatch.setattr(
        "application_sdk.storage.formats.parquet._download_files",
        dummy_download,
        raising=False,
    )

    path = "/data/test.parquet"
    reader = ParquetFileReader(path=path, dataframe_type=DataframeType.pandas)

    # Initially no downloaded files
    assert reader._downloaded_files == []

    # Read to trigger download
    await reader.read()

    # Files should now be tracked
    assert reader._downloaded_files == downloaded_files


# ---------------------------------------------------------------------------
# Schema-unification tests (BLDX-837)
#
# Multi-file parquet reads where early files have null-typed columns and
# later files have string-typed columns previously dropped rows silently.
# Schema unification (promoting null → large_string before daft reads) is
# now applied on both the non-batched and batched daft paths via
# _build_unified_daft_schema. These tests lock that in.
# ---------------------------------------------------------------------------


try:
    import daft  # noqa: F401 — availability probe for the test module

    _DAFT_AVAILABLE = True
except (
    BaseException
) as _daft_err:  # daft's Rust extension can panic on certain OTel envs
    _DAFT_AVAILABLE = False
    _DAFT_SKIP_REASON = f"daft unavailable: {_daft_err}"


def _write_parquet(path: Path, schema: pa.Schema, rows: dict) -> str:
    table = pa.Table.from_pydict(rows, schema=schema)
    pq.write_table(table, path)
    return str(path)


@pytest.mark.skipif(
    not _DAFT_AVAILABLE, reason="daft not importable in this environment"
)
class TestBuildUnifiedDaftSchema:
    """Unit tests for the _build_unified_daft_schema helper."""

    def test_promotes_null_field_to_large_string(self, tmp_path) -> None:
        import daft  # local — module-level import is guarded above

        f = _write_parquet(
            tmp_path / "nulls.parquet",
            pa.schema([("id", pa.int64()), ("remarks", pa.null())]),
            {"id": [1, 2], "remarks": [None, None]},
        )

        schema = _build_unified_daft_schema([f], daft)

        assert schema is not None
        assert schema["remarks"] == daft.DataType.from_arrow_type(pa.large_string())
        assert schema["id"] == daft.DataType.from_arrow_type(pa.int64())

    def test_unifies_mixed_null_and_string_across_files(self, tmp_path) -> None:
        import daft

        f1 = _write_parquet(
            tmp_path / "f1.parquet",
            pa.schema([("id", pa.int64()), ("extra", pa.null())]),
            {"id": [1, 2], "extra": [None, None]},
        )
        f2 = _write_parquet(
            tmp_path / "f2.parquet",
            pa.schema([("id", pa.int64()), ("extra", pa.string())]),
            {"id": [3, 4], "extra": ["foo", "bar"]},
        )

        schema = _build_unified_daft_schema([f1, f2], daft)

        assert schema is not None
        # f1 had null, f2 had string — unify_schemas promotes to string,
        # then the helper widens further to large_string. Either way the
        # downstream daft.read_parquet won't silently drop the f2 rows.
        assert schema["extra"] in (
            daft.DataType.from_arrow_type(pa.string()),
            daft.DataType.from_arrow_type(pa.large_string()),
        )

    def test_returns_none_on_invalid_input(self, tmp_path) -> None:
        """Bad inputs (nonexistent file, empty list) fall back to None so
        the caller passes ``schema=None`` to ``daft.read_parquet`` —
        preserving today's default-inference behaviour rather than raising.

        WARNING-level log emission is locked by the helper docstring and
        code review; ``caplog`` cannot assert it because the SDK logger
        is loguru-based (see tests/unit/storage/test_cloud.py:323)."""
        import daft

        # Nonexistent file → pq.read_schema raises FileNotFoundError (OSError).
        bogus = str(tmp_path / "does-not-exist.parquet")
        assert _build_unified_daft_schema([bogus], daft) is None

        # Empty list → pa.unify_schemas raises ArrowInvalid.
        assert _build_unified_daft_schema([], daft) is None


@pytest.mark.skipif(
    not _DAFT_AVAILABLE, reason="daft not importable in this environment"
)
class TestReadNonBatchedSchemaUnification:
    """End-to-end: non-batched _get_daft_dataframe must apply schema unification.

    Before this fix the non-batched reader called plain
    `daft.read_parquet(parquet_files)` with no schema arg — the same
    silent-data-loss bug PR #1186 closed for the batched path.
    """

    @pytest.mark.asyncio
    async def test_mixed_schema_files_do_not_drop_rows(
        self, tmp_path, monkeypatch
    ) -> None:
        f1 = _write_parquet(
            tmp_path / "f1.parquet",
            pa.schema([("id", pa.int64()), ("extra", pa.null())]),
            {"id": [1, 2], "extra": [None, None]},
        )
        f2 = _write_parquet(
            tmp_path / "f2.parquet",
            pa.schema([("id", pa.int64()), ("extra", pa.string())]),
            {"id": [3, 4], "extra": ["foo", "bar"]},
        )

        async def fake_download(path, file_extension, file_names=None):
            return [f1, f2]

        monkeypatch.setattr(
            "application_sdk.storage.formats.parquet._download_files",
            fake_download,
            raising=False,
        )

        reader = ParquetFileReader(
            path=str(tmp_path), dataframe_type=DataframeType.daft
        )
        df = await reader.read()

        result = df.to_pydict()
        assert sorted(result["id"]) == [1, 2, 3, 4]  # no rows dropped
        assert sorted(v for v in result["extra"] if v is not None) == [
            "bar",
            "foo",
        ]


@pytest.mark.asyncio
async def test_read_batches_empty_micropartition_yields_no_batches(monkeypatch) -> None:
    """Empty parquet input => zero MicroPartitions => count_rows() raises
    DaftCoreException ("Need at least 1 MicroPartition to perform concat").

    The batched daft reader should treat that as an empty result and yield no
    batches, instead of propagating the error and crashing the activity.
    """
    import sys
    import types

    dummy_daft = types.ModuleType("daft")
    dummy_exceptions = types.ModuleType("daft.exceptions")

    class DaftCoreException(Exception):
        pass

    dummy_exceptions.DaftCoreException = DaftCoreException  # type: ignore[attr-defined]
    dummy_daft.exceptions = dummy_exceptions  # type: ignore[attr-defined]

    class EmptyDaftDataFrame:
        def count_rows(self):
            raise DaftCoreException(
                "DaftError::ValueError Need at least 1 MicroPartition to perform concat"
            )

    dummy_daft.read_parquet = lambda path, **kwargs: EmptyDaftDataFrame()  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "daft", dummy_daft)
    monkeypatch.setitem(sys.modules, "daft.exceptions", dummy_exceptions)

    async def dummy_download(path, file_extension, file_names=None):
        return [f"{path}/empty.parquet"]

    monkeypatch.setattr(
        "application_sdk.storage.formats.parquet._download_files",
        dummy_download,
        raising=False,
    )
    monkeypatch.setattr(
        "application_sdk.storage.formats.parquet._build_unified_daft_schema",
        lambda files, daft: None,
        raising=False,
    )

    reader = ParquetFileReader(path="/tmp/empty", dataframe_type=DataframeType.daft)
    batches = [batch async for batch in reader._get_batched_daft_dataframe()]
    assert batches == []
