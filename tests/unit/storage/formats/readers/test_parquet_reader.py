from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from hypothesis import HealthCheck, given, settings

from application_sdk.common.types import DataframeType
from application_sdk.storage.formats.parquet import ParquetFileReader
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
# Compat shim tests — DataframeType.daft
# ---------------------------------------------------------------------------


def test_dataframe_type_daft_emits_deprecation_warning() -> None:
    """Passing DataframeType.daft must emit DeprecationWarning and route to pandas."""
    with pytest.warns(DeprecationWarning, match="DataframeType.daft is deprecated"):
        reader = ParquetFileReader(
            path="/data/test.parquet",
            dataframe_type=DataframeType.daft,
        )
    assert reader.dataframe_type == DataframeType.pandas


# ---------------------------------------------------------------------------
# PyArrow-backed read tests (real files via tmp_path)
# ---------------------------------------------------------------------------


def _make_parquet_file(tmp_path: Path, name: str, rows: int = 10) -> str:
    """Write a small parquet file and return its path."""
    schema = pa.schema([("id", pa.int64()), ("value", pa.string())])
    table = pa.Table.from_pydict(
        {"id": list(range(rows)), "value": [f"v{i}" for i in range(rows)]},
        schema=schema,
    )
    path = tmp_path / name
    pq.write_table(table, path)
    return str(path)


@pytest.mark.asyncio
async def test_read_returns_pandas_dataframe(tmp_path: Path) -> None:
    """read() returns a pandas DataFrame built from pyarrow read_table."""
    import pandas as pd

    parquet_file = _make_parquet_file(tmp_path, "data.parquet", rows=5)

    async def dummy_download(path, file_extension, file_names=None):
        return [parquet_file]

    with patch(
        "application_sdk.storage.formats.parquet._download_files",
        side_effect=dummy_download,
    ):
        reader = ParquetFileReader(
            path=str(tmp_path), dataframe_type=DataframeType.pandas
        )
        result = await reader.read()

    assert isinstance(result, pd.DataFrame)
    assert len(result) == 5
    assert list(result.columns) == ["id", "value"]


@pytest.mark.asyncio
async def test_read_batches_streams_pyarrow_row_groups(tmp_path: Path) -> None:
    """read_batches() yields pandas DataFrames via pyarrow ParquetFile.iter_batches."""
    import pandas as pd

    parquet_file = _make_parquet_file(tmp_path, "data.parquet", rows=10)

    async def dummy_download(path, file_extension, file_names=None):
        return [parquet_file]

    with patch(
        "application_sdk.storage.formats.parquet._download_files",
        side_effect=dummy_download,
    ):
        reader = ParquetFileReader(
            path=str(tmp_path), chunk_size=4, dataframe_type=DataframeType.pandas
        )
        batches = reader.read_batches()
        chunks = [chunk async for chunk in batches]

    assert len(chunks) >= 1
    for chunk in chunks:
        assert isinstance(chunk, pd.DataFrame)
        assert list(chunk.columns) == ["id", "value"]
    total_rows = sum(len(c) for c in chunks)
    assert total_rows == 10


@pytest.mark.asyncio
async def test_read_batches_multiple_files(tmp_path: Path) -> None:
    """read_batches iterates over each file in the returned file list."""
    import pandas as pd

    f1 = _make_parquet_file(tmp_path, "f1.parquet", rows=3)
    f2 = _make_parquet_file(tmp_path, "f2.parquet", rows=4)

    async def dummy_download(path, file_extension, file_names=None):
        return [f1, f2]

    with patch(
        "application_sdk.storage.formats.parquet._download_files",
        side_effect=dummy_download,
    ):
        reader = ParquetFileReader(
            path=str(tmp_path),
            chunk_size=100000,
            dataframe_type=DataframeType.pandas,
        )
        batches = reader.read_batches()
        chunks = [chunk async for chunk in batches]

    total_rows = sum(len(c) for c in chunks)
    assert total_rows == 7
    for chunk in chunks:
        assert isinstance(chunk, pd.DataFrame)


@pytest.mark.asyncio
async def test_read_with_mocked_pandas(monkeypatch) -> None:
    """Verify that read delegates to pyarrow and returns a DataFrame."""
    path = "/data/test.parquet"

    async def dummy_download(path, file_extension, file_names=None):
        return [path]

    monkeypatch.setattr(
        "application_sdk.storage.formats.parquet._download_files",
        dummy_download,
        raising=False,
    )

    import pandas as pd

    class FakeTable:
        def to_pandas(self):
            return pd.DataFrame({"col": [1, 2, 3]})

    def fake_read_table(f, **kwargs):
        return FakeTable()

    def fake_concat_tables(tables, **kwargs):
        return tables[0]

    with (
        patch(
            "application_sdk.storage.formats.parquet.pq.read_table"
            if False
            else "pyarrow.parquet.read_table",
            side_effect=fake_read_table,
        ),
        patch("pyarrow.concat_tables", side_effect=fake_concat_tables),
    ):
        parquet_input = ParquetFileReader(path=path, chunk_size=100000)
        result = await parquet_input.read()

    assert result is not None


@pytest.mark.asyncio
async def test_read_with_input_prefix(tmp_path: Path) -> None:
    """Verify that read downloads multiple files and concatenates them."""
    import pandas as pd

    f1 = _make_parquet_file(tmp_path, "file1.parquet", rows=100)
    f2 = _make_parquet_file(tmp_path, "file2.parquet", rows=100)

    async def dummy_download(path, file_extension, file_names=None):
        return [f1, f2]

    with patch(
        "application_sdk.storage.formats.parquet._download_files",
        side_effect=dummy_download,
    ):
        parquet_input = ParquetFileReader(
            path=str(tmp_path), dataframe_type=DataframeType.pandas
        )
        result = await parquet_input.read()

    assert isinstance(result, pd.DataFrame)
    assert len(result) == 200


# ---------------------------------------------------------------------------
# Context Manager and Close Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_context_manager_calls_close(tmp_path: Path) -> None:
    """Verify that using async with calls close() on exit."""
    parquet_file = _make_parquet_file(tmp_path, "test.parquet", rows=2)

    async def dummy_download(path, file_extension, file_names=None):
        return [parquet_file]

    with patch(
        "application_sdk.storage.formats.parquet._download_files",
        side_effect=dummy_download,
    ):
        async with ParquetFileReader(
            path=str(tmp_path), dataframe_type=DataframeType.pandas
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
async def test_read_after_close_raises_error(tmp_path: Path) -> None:
    """Verify that reading after close raises ReaderClosedError."""
    parquet_file = _make_parquet_file(tmp_path, "test.parquet", rows=2)

    async def dummy_download(path, file_extension, file_names=None):
        return [parquet_file]

    with patch(
        "application_sdk.storage.formats.parquet._download_files",
        side_effect=dummy_download,
    ):
        path = str(tmp_path / "test.parquet")
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
async def test_cleanup_on_close_false_retains_files(tmp_path: Path) -> None:
    """Verify that setting cleanup_on_close=False retains downloaded files."""
    parquet_file = _make_parquet_file(tmp_path, "test.parquet", rows=2)

    downloaded_files = [parquet_file]

    async def dummy_download(path, file_extension, file_names=None):
        return downloaded_files

    with patch(
        "application_sdk.storage.formats.parquet._download_files",
        side_effect=dummy_download,
    ):
        path = str(tmp_path)
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

        reader._cleanup_downloaded_files = mock_cleanup  # type: ignore[method-assign]

        # Close should NOT call cleanup when cleanup_on_close=False
        await reader.close()

    assert not cleanup_called
    assert reader._is_closed


@pytest.mark.asyncio
async def test_cleanup_on_close_true_cleans_files(tmp_path: Path) -> None:
    """Verify that setting cleanup_on_close=True cleans up downloaded files."""
    parquet_file = _make_parquet_file(tmp_path, "test.parquet", rows=2)

    downloaded_files = [parquet_file]

    async def dummy_download(path, file_extension, file_names=None):
        return downloaded_files

    with patch(
        "application_sdk.storage.formats.parquet._download_files",
        side_effect=dummy_download,
    ):
        path = str(tmp_path)
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

        reader._cleanup_downloaded_files = mock_cleanup  # type: ignore[method-assign]

        # Close should call cleanup when cleanup_on_close=True
        await reader.close()

    assert cleanup_called
    assert reader._is_closed


@pytest.mark.asyncio
async def test_downloaded_files_tracked_on_read(tmp_path: Path) -> None:
    """Verify that downloaded files are tracked when read() is called."""
    parquet_file = _make_parquet_file(tmp_path, "test.parquet", rows=2)

    downloaded_files = [parquet_file]

    async def dummy_download(path, file_extension, file_names=None):
        return downloaded_files

    with patch(
        "application_sdk.storage.formats.parquet._download_files",
        side_effect=dummy_download,
    ):
        path = str(tmp_path / "test.parquet")
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
# Schema unification (promoting null -> large_string before the read) is
# applied via _build_unified_parquet_schema. These tests lock that in.
# ---------------------------------------------------------------------------


def _write_parquet(path: Path, schema: pa.Schema, rows: dict) -> str:
    table = pa.Table.from_pydict(rows, schema=schema)
    pq.write_table(table, path)
    return str(path)


# ---------------------------------------------------------------------------
# Schema-unification integration: mixed-schema multi-file reads (BLDX-837)
#
# _build_unified_parquet_schema was removed: pa.concat_tables with
# promote_options="permissive" already handles the null/string promotion
# correctly, so the separate schema-build step was dead code.
#
# These tests verify the read path itself preserves rows across schemas.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Multi-row-group parquet tests
#
# The parquet reader uses pq.ParquetFile.iter_batches(), which streams one
# row group at a time without merging across groups. These tests verify:
#   1. Data fidelity — all rows recovered, no gaps, no duplication
#   2. Bounded batches — each yielded batch never exceeds chunk_size rows
#   3. Streaming — row groups are NOT merged into one large batch
#
# The motivation: the daft.read_parquet() path retained arena memory for the
# entire file; iter_batches() releases each row group after processing.
# ---------------------------------------------------------------------------


def _make_multi_row_group_parquet(
    tmp_path: Path, name: str, rows_per_group: int, num_groups: int
) -> str:
    """Write a parquet file with a controlled number of row groups."""
    total_rows = rows_per_group * num_groups
    schema = pa.schema([("id", pa.int64()), ("value", pa.string())])
    table = pa.Table.from_pydict(
        {
            "id": list(range(total_rows)),
            "value": [f"v{i}" for i in range(total_rows)],
        },
        schema=schema,
    )
    path = tmp_path / name
    pq.write_table(table, path, row_group_size=rows_per_group)
    assert pq.ParquetFile(path).metadata.num_row_groups == num_groups
    return str(path)


@pytest.mark.asyncio
async def test_read_batches_multi_row_group_data_fidelity(tmp_path: Path) -> None:
    """All rows are recovered from a file with multiple row groups — no gaps, no duplication."""
    # 3 row groups × 4 rows = 12 rows
    parquet_file = _make_multi_row_group_parquet(
        tmp_path, "multi_rg.parquet", rows_per_group=4, num_groups=3
    )

    async def dummy_download(path, file_extension, file_names=None):
        return [parquet_file]

    with patch(
        "application_sdk.storage.formats.parquet._download_files",
        side_effect=dummy_download,
    ):
        reader = ParquetFileReader(
            path=str(tmp_path), chunk_size=4, dataframe_type=DataframeType.pandas
        )
        chunks = [chunk async for chunk in reader.read_batches()]

    total_rows = sum(len(c) for c in chunks)
    assert total_rows == 12

    all_ids = sorted(id_ for chunk in chunks for id_ in chunk["id"].tolist())
    assert all_ids == list(range(12)), "rows missing or duplicated across row groups"

    all_values = sorted(v for chunk in chunks for v in chunk["value"].tolist())
    assert all_values == sorted(f"v{i}" for i in range(12))


@pytest.mark.asyncio
async def test_read_batches_bounded_by_chunk_size(tmp_path: Path) -> None:
    """Each yielded batch contains at most chunk_size rows (bounded memory per batch)."""
    # 4 row groups × 100 rows = 400 rows; chunk_size=50 forces splits within each group
    parquet_file = _make_multi_row_group_parquet(
        tmp_path, "large_rg.parquet", rows_per_group=100, num_groups=4
    )

    async def dummy_download(path, file_extension, file_names=None):
        return [parquet_file]

    with patch(
        "application_sdk.storage.formats.parquet._download_files",
        side_effect=dummy_download,
    ):
        reader = ParquetFileReader(
            path=str(tmp_path), chunk_size=50, dataframe_type=DataframeType.pandas
        )
        chunks = [chunk async for chunk in reader.read_batches()]

    assert all(len(c) <= 50 for c in chunks), "a batch exceeded chunk_size"
    assert sum(len(c) for c in chunks) == 400


@pytest.mark.asyncio
async def test_read_batches_streams_in_multiple_batches(tmp_path: Path) -> None:
    """When chunk_size < total rows, multiple batches are yielded — not one large load.

    pyarrow iter_batches(batch_size=N) merges across row groups up to N rows per batch.
    With chunk_size smaller than the total file, the file is processed in pieces.
    """
    # 4 row groups × 100 rows = 400 rows; chunk_size=100 → at least 4 batches
    parquet_file = _make_multi_row_group_parquet(
        tmp_path, "stream.parquet", rows_per_group=100, num_groups=4
    )

    async def dummy_download(path, file_extension, file_names=None):
        return [parquet_file]

    with patch(
        "application_sdk.storage.formats.parquet._download_files",
        side_effect=dummy_download,
    ):
        reader = ParquetFileReader(
            path=str(tmp_path), chunk_size=100, dataframe_type=DataframeType.pandas
        )
        chunks = [chunk async for chunk in reader.read_batches()]

    assert len(chunks) > 1, "chunk_size < total rows must yield multiple batches"
    assert all(len(c) <= 100 for c in chunks), "a batch exceeded chunk_size"
    assert sum(len(c) for c in chunks) == 400


class TestReadNonBatchedSchemaUnification:
    """Integration tests: mixed-schema parquet reads must not drop rows.

    These test ParquetFileReader.read() end-to-end to protect the BLDX-837
    fix (null-typed column in one file + string-typed in another).
    """

    @pytest.mark.asyncio
    async def test_read_preserves_rows_mixed_null_and_string(self, tmp_path) -> None:
        """ParquetFileReader.read() must preserve all rows from files with mixed schemas."""
        f1 = _write_parquet(
            tmp_path / "f1.parquet",
            pa.schema([("id", pa.int64()), ("tag", pa.null())]),
            {"id": [1, 2], "tag": [None, None]},
        )
        f2 = _write_parquet(
            tmp_path / "f2.parquet",
            pa.schema([("id", pa.int64()), ("tag", pa.string())]),
            {"id": [3, 4], "tag": ["a", "b"]},
        )

        async def _download(_path, _ext, _names=None):
            return [f1, f2]

        with patch(
            "application_sdk.storage.formats.parquet._download_files",
            side_effect=_download,
        ):
            reader = ParquetFileReader(path=str(tmp_path))
            df = await reader.read()

        assert len(df) == 4
        assert set(df["id"].tolist()) == {1, 2, 3, 4}
        assert set(df["tag"].tolist()) == {None, "a", "b"}
