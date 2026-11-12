"""Tests for application_sdk.common.parquet_records: bounded-batch Parquet I/O."""

from __future__ import annotations

import os

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
import pytest

from application_sdk.common.parquet_records import (
    MAX_BATCH_ROWS,
    MIN_BATCH_ROWS,
    ParquetBatchWriter,
    iter_parquet_batches,
    parquet_batch_rows,
    rewrite_parquet,
)


def _write(path, rows: int, pad: int = 10):
    table = pa.table(
        {
            "id": list(range(rows)),
            "name": [f"n{i}" for i in range(rows)],
            "pad": ["x" * pad] * rows,
        }
    )
    pq.write_table(table, path)
    return table


class TestBatchSizing:
    def test_wide_rows_get_fewer_rows_per_batch(self, tmp_path):
        narrow, wide = tmp_path / "narrow.parquet", tmp_path / "wide.parquet"
        _write(narrow, 5_000, pad=10)
        _write(wide, 5_000, pad=5_000)

        narrow_rows = parquet_batch_rows(pq.ParquetFile(narrow), target_bytes=1_000_000)
        wide_rows = parquet_batch_rows(pq.ParquetFile(wide), target_bytes=1_000_000)

        assert wide_rows < narrow_rows

    def test_is_clamped_at_both_ends(self, tmp_path):
        path = tmp_path / "f.parquet"
        _write(path, 100)

        assert (
            parquet_batch_rows(pq.ParquetFile(path), target_bytes=1) == MIN_BATCH_ROWS
        )
        assert (
            parquet_batch_rows(pq.ParquetFile(path), target_bytes=10**12)
            == MAX_BATCH_ROWS
        )

    def test_empty_file(self, tmp_path):
        path = tmp_path / "empty.parquet"
        _write(path, 0)

        assert parquet_batch_rows(pq.ParquetFile(path)) == MIN_BATCH_ROWS


class TestIterParquetBatches:
    def test_yields_every_row_in_order_across_batches(self, tmp_path):
        path = tmp_path / "f.parquet"
        _write(path, 5_000)

        batches = list(iter_parquet_batches(str(path), target_bytes=1))

        assert len(batches) > 1
        assert [i for b in batches for i in b.column("id").to_pylist()] == list(
            range(5_000)
        )

    def test_reads_only_the_named_columns(self, tmp_path):
        path = tmp_path / "f.parquet"
        _write(path, 10)

        batch = next(iter_parquet_batches(str(path), columns=["id"]))

        assert batch.schema.names == ["id"]


class TestRewriteParquet:
    def test_filters_rows_and_matches_a_whole_table_rewrite(self, tmp_path):
        path = tmp_path / "f.parquet"
        table = _write(path, 5_000)

        counts = rewrite_parquet(
            str(path),
            lambda b: b.filter(pc.equal(pc.bit_wise_and(b.column("id"), 1), 0)),
            target_bytes=1,
        )

        assert counts == (5_000, 2_500)
        expected = table.filter(pc.equal(pc.bit_wise_and(table["id"], 1), 0))
        assert pq.read_table(path).equals(expected)

    def test_adds_a_column(self, tmp_path):
        path = tmp_path / "f.parquet"
        _write(path, 3_000)

        rewrite_parquet(
            str(path),
            lambda b: b.append_column(
                "even", pc.equal(pc.bit_wise_and(b.column("id"), 1), 0)
            ),
            target_bytes=1,
        )

        result = pq.read_table(path)
        assert result.schema.names == ["id", "name", "pad", "even"]
        assert result["even"].to_pylist() == [i % 2 == 0 for i in range(3_000)]

    def test_empty_file_is_left_untouched(self, tmp_path):
        path = tmp_path / "empty.parquet"
        _write(path, 0)
        before = path.read_bytes()

        assert rewrite_parquet(str(path), lambda b: b) == (0, 0)
        assert path.read_bytes() == before

    def test_all_rows_filtered_keeps_the_schema(self, tmp_path):
        path = tmp_path / "f.parquet"
        table = _write(path, 100)

        assert rewrite_parquet(str(path), lambda b: b.slice(0, 0)) == (100, 0)
        result = pq.read_table(path)
        assert result.num_rows == 0
        assert result.schema.equals(table.schema)

    def test_every_batch_dropped_keeps_the_schema(self, tmp_path):
        path = tmp_path / "f.parquet"
        table = _write(path, 100)

        assert rewrite_parquet(str(path), lambda b: None) == (100, 0)
        result = pq.read_table(path)
        assert result.num_rows == 0
        assert result.schema.equals(table.schema)

    def test_failure_leaves_the_original_and_no_temp_file(self, tmp_path):
        path = tmp_path / "f.parquet"
        table = _write(path, 5_000)
        calls = []

        def fail_on_second(batch):
            calls.append(batch.num_rows)
            if len(calls) == 2:
                raise RuntimeError("transform failed")
            return batch

        with pytest.raises(RuntimeError):
            rewrite_parquet(str(path), fail_on_second, target_bytes=1)

        assert pq.read_table(path).equals(table)
        assert os.listdir(tmp_path) == ["f.parquet"]

    def test_temp_file_is_never_globbed_as_parquet(self, tmp_path):
        """A reader listing ``*.parquet`` during a rewrite must not pick up the temp file."""
        path = tmp_path / "f.parquet"
        _write(path, 5_000)
        seen: list[str] = []

        def record_siblings(batch):
            # From the second batch on, the temp file exists beside the target.
            seen.extend(os.listdir(tmp_path))
            return batch

        rewrite_parquet(str(path), record_siblings, target_bytes=1)

        temp_files = {name for name in seen if name != "f.parquet"}
        assert len(temp_files) == 1
        assert not any(name.endswith(".parquet") for name in temp_files)


class TestParquetBatchWriter:
    SCHEMA = pa.schema([("qn", pa.string()), ("ddl", pa.string())])

    def test_rows_round_trip_across_flushes(self, tmp_path):
        path = tmp_path / "nested" / "out.parquet"

        with ParquetBatchWriter(str(path), self.SCHEMA, flush_bytes=200) as out:
            for i in range(100):
                out.write_row({"qn": f"q{i}", "ddl": "select 1"})

        table = pq.read_table(path)
        assert out.count == 100
        assert table["qn"].to_pylist() == [f"q{i}" for i in range(100)]
        assert pq.ParquetFile(path).metadata.num_row_groups > 1

    def test_no_rows_still_writes_a_file_with_the_schema(self, tmp_path):
        path = tmp_path / "out.parquet"

        with ParquetBatchWriter(str(path), self.SCHEMA):
            pass

        table = pq.read_table(path)
        assert table.num_rows == 0
        assert table.schema.equals(self.SCHEMA)

    def test_missing_column_is_null(self, tmp_path):
        path = tmp_path / "out.parquet"

        with ParquetBatchWriter(str(path), self.SCHEMA) as out:
            out.write_row({"qn": "q"})

        assert pq.read_table(path).to_pylist() == [{"qn": "q", "ddl": None}]

    def test_write_batch_keeps_order_with_buffered_rows(self, tmp_path):
        path = tmp_path / "out.parquet"

        with ParquetBatchWriter(str(path), self.SCHEMA) as out:
            out.write_row({"qn": "a", "ddl": "1"})
            out.write_batch(
                pa.record_batch(
                    {"qn": ["b", "c"], "ddl": ["2", "3"]}, schema=self.SCHEMA
                )
            )
            out.write_row({"qn": "d", "ddl": "4"})

        assert pq.read_table(path)["qn"].to_pylist() == ["a", "b", "c", "d"]
        assert out.count == 4

    def test_write_after_close_raises(self, tmp_path):
        out = ParquetBatchWriter(str(tmp_path / "out.parquet"), self.SCHEMA)
        out.close()

        with pytest.raises(ValueError):
            out.write_row({"qn": "q"})
