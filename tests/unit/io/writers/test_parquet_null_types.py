"""Tests for parquet null-type handling (BLDX-837).

Verifies that:
- All-null columns are written as large_string, not null type
- Non-null columns are unaffected
- Empty DataFrames don't corrupt the schema
- Multiple files with mixed null/string columns can be read back without data loss
"""

from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from application_sdk.io.parquet import ParquetFileWriter


@pytest.fixture
def writer(tmp_path: Path) -> ParquetFileWriter:
    """Create a minimal writer for testing _write_chunk."""
    return ParquetFileWriter(path=str(tmp_path / "output"))


class TestWriteChunkNullTypes:
    """Tests for _write_chunk null-type casting."""

    @pytest.mark.asyncio
    async def test_all_null_column_written_as_large_string(
        self, tmp_path: Path, writer: ParquetFileWriter
    ):
        df = pd.DataFrame(
            {
                "name": ["alice", "bob", "charlie"],
                "missing_col": [None, None, None],
            }
        )
        out_file = str(tmp_path / "test.parquet")
        await writer._write_chunk(df, out_file)

        schema = pq.read_schema(out_file)
        assert not pa.types.is_null(schema.field("missing_col").type)
        assert pa.types.is_large_string(schema.field("missing_col").type)

    @pytest.mark.asyncio
    async def test_no_null_columns_uses_fast_path(
        self, tmp_path: Path, writer: ParquetFileWriter
    ):
        df = pd.DataFrame(
            {
                "name": ["alice", "bob"],
                "age": [25, 30],
            }
        )
        out_file = str(tmp_path / "test.parquet")
        await writer._write_chunk(df, out_file)

        table = pq.read_table(out_file)
        assert table.num_rows == 2
        assert table.column("name").to_pylist() == ["alice", "bob"]

    @pytest.mark.asyncio
    async def test_empty_dataframe_preserves_schema(
        self, tmp_path: Path, writer: ParquetFileWriter
    ):
        df = pd.DataFrame(
            {
                "name": pd.Series([], dtype="str"),
                "age": pd.Series([], dtype="int64"),
            }
        )
        out_file = str(tmp_path / "test.parquet")
        await writer._write_chunk(df, out_file)

        schema = pq.read_schema(out_file)
        # Empty DataFrame should NOT have columns cast to large_string
        assert not pa.types.is_large_string(schema.field("age").type)

    @pytest.mark.asyncio
    async def test_mixed_null_and_data_columns(
        self, tmp_path: Path, writer: ParquetFileWriter
    ):
        df = pd.DataFrame(
            {
                "id": [1, 2, 3],
                "name": ["a", "b", "c"],
                "optional": [None, None, None],
                "score": [1.0, 2.0, 3.0],
            }
        )
        out_file = str(tmp_path / "test.parquet")
        await writer._write_chunk(df, out_file)

        schema = pq.read_schema(out_file)
        assert pa.types.is_large_string(schema.field("optional").type)
        assert not pa.types.is_large_string(schema.field("id").type)
        assert not pa.types.is_large_string(schema.field("score").type)


class TestMultiFileNullTypeRoundTrip:
    """Verify files with mixed null/string types produce no data loss."""

    @pytest.mark.asyncio
    async def test_mixed_files_no_data_loss(
        self, tmp_path: Path, writer: ParquetFileWriter
    ):
        # File 1: all-null column
        df1 = pd.DataFrame({"id": [1, 2], "status": [None, None]})
        file1 = str(tmp_path / "chunk_0.parquet")
        await writer._write_chunk(df1, file1)

        # File 2: column has actual data
        df2 = pd.DataFrame({"id": [3, 4], "status": ["active", "inactive"]})
        file2 = str(tmp_path / "chunk_1.parquet")
        await writer._write_chunk(df2, file2)

        # Both should have compatible schemas (not null type)
        schema1 = pq.read_schema(file1)
        schema2 = pq.read_schema(file2)
        assert not pa.types.is_null(schema1.field("status").type)
        assert not pa.types.is_null(schema2.field("status").type)

        # Read back — no data loss
        combined = pd.concat(
            [pd.read_parquet(file1), pd.read_parquet(file2)], ignore_index=True
        )
        assert len(combined) == 4
        assert combined["id"].tolist() == [1, 2, 3, 4]
        assert pd.isna(combined["status"].iloc[0])
        assert pd.isna(combined["status"].iloc[1])
        assert combined["status"].iloc[2] == "active"
        assert combined["status"].iloc[3] == "inactive"
