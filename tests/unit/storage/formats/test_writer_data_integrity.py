"""
Writer Data Integrity Tests - Prevents data loss regression in file writers.

This test suite verifies that writers maintain data integrity when multiple
write() calls are made and when DataFrames exceed buffer_size.

Guards against two classes of bugs:
1. JSON/daft writer: file name collisions across write() calls (fixed earlier)
2. Parquet writer: pq.write_table() overwrites within a single write() call
   when DataFrame exceeds buffer_size, since parquet cannot append (HYP-773)

Run with:
    uv run pytest tests/unit/storage/formats/test_writer_data_integrity.py -v -s
"""

import glob
import json
import os
import shutil
import tempfile
from unittest.mock import AsyncMock, patch

import pandas as pd
import pytest

from application_sdk.common.types import DataframeType
from application_sdk.storage.formats.json import JsonFileWriter


class TestWriterDataIntegrity:
    """Verify pandas and daft writers preserve all data across multiple write() calls."""

    @pytest.fixture
    def temp_dir(self):
        temp_path = tempfile.mkdtemp(prefix="writer_integrity_")
        yield temp_path
        shutil.rmtree(temp_path, ignore_errors=True)

    def extract_ids_from_store(self, store: dict[str, list[str]]) -> set[int]:
        """Extract all record IDs from mock object store."""
        ids: set[int] = set()
        for path, content in store.items():
            if "statistics" in path:
                continue
            for line in content:
                try:
                    record = json.loads(line)
                    ids.add(record.get("id"))
                except Exception:  # noqa: S110  # skip non-JSON lines; test assertions validate id completeness
                    pass
        return ids

    @pytest.mark.asyncio
    async def test_pandas_writer_no_data_loss(self, temp_dir: str):
        """Verify pandas writer preserves all records across multiple write() calls."""
        object_store: dict[str, list[str]] = {}

        async def mock_upload(key, local_path, **kwargs):
            if os.path.exists(local_path):
                with open(local_path, "r") as f:
                    object_store[key] = f.readlines()

        with patch(
            "application_sdk.storage.formats._upload_file",
            new_callable=AsyncMock,
            side_effect=mock_upload,
        ):
            writer = JsonFileWriter(
                path=temp_dir,
                typename="pandas_test",
                buffer_size=50,
                chunk_size=100,
                retain_local_copy=False,
                dataframe_type=DataframeType.pandas,
            )

            total_records = 0
            for batch in range(3):
                df = pd.DataFrame(
                    {
                        "id": list(range(batch * 120, (batch + 1) * 120)),
                        "batch": [f"batch_{batch}"] * 120,
                    }
                )
                await writer.write(df)
                total_records += 120

            await writer.close()

            ids_in_store = self.extract_ids_from_store(object_store)
            expected_ids = set(range(total_records))
            missing_ids = expected_ids - ids_in_store

            assert (
                not missing_ids
            ), f"PANDAS data loss: missing IDs {sorted(missing_ids)[:20]}"
            assert len(ids_in_store) == total_records

    @pytest.mark.asyncio
    async def test_daft_writer_no_data_loss(self, temp_dir: str):
        """Verify daft writer preserves all records across multiple write() calls."""
        pytest.importorskip("daft")
        from unittest.mock import AsyncMock, patch

        import daft

        from application_sdk.common.types import DataframeType
        from application_sdk.storage.formats.json import JsonFileWriter

        object_store: dict[str, list[str]] = {}

        async def mock_upload(key, local_path, **kwargs):
            if os.path.exists(local_path):
                with open(local_path, "r") as f:
                    object_store[key] = f.readlines()

        with patch(
            "application_sdk.storage.formats._upload_file",
            new_callable=AsyncMock,
            side_effect=mock_upload,
        ):
            writer = JsonFileWriter(
                path=temp_dir,
                typename="daft_test",
                buffer_size=50,
                chunk_size=100,
                retain_local_copy=False,
                dataframe_type=DataframeType.daft,
            )

            total_records = 0
            for batch in range(3):
                df = daft.from_pydict(
                    {
                        "id": list(range(batch * 120, (batch + 1) * 120)),
                        "batch": [f"batch_{batch}"] * 120,
                    }
                )
                await writer.write(df)
                total_records += 120

            await writer.close()

            ids_in_store = self.extract_ids_from_store(object_store)
            expected_ids = set(range(total_records))
            missing_ids = expected_ids - ids_in_store

            assert (
                not missing_ids
            ), f"DAFT data loss: missing IDs {sorted(missing_ids)[:20]}"
            assert len(ids_in_store) == total_records

    @pytest.mark.asyncio
    async def test_pandas_daft_chunk_count_consistency(self, temp_dir: str):
        """Verify pandas and daft writers have identical chunk_count after writes."""
        pytest.importorskip("daft")
        from unittest.mock import AsyncMock, patch

        import daft

        from application_sdk.common.types import DataframeType
        from application_sdk.storage.formats.json import JsonFileWriter

        async def mock_upload(
            source, destination=None, retain_local_copy=False, **kwargs
        ):
            if os.path.exists(source) and not retain_local_copy:
                os.remove(source)

        with patch(
            "application_sdk.storage.formats._upload_file",
            new_callable=AsyncMock,
            side_effect=mock_upload,
        ):
            pandas_writer = JsonFileWriter(
                path=os.path.join(temp_dir, "pandas"),
                typename="test",
                buffer_size=50,
                chunk_size=100,
                retain_local_copy=False,
                dataframe_type=DataframeType.pandas,
            )

            daft_writer = JsonFileWriter(
                path=os.path.join(temp_dir, "daft"),
                typename="test",
                buffer_size=50,
                chunk_size=100,
                retain_local_copy=False,
                dataframe_type=DataframeType.daft,
            )

            for batch in range(3):
                pdf = pd.DataFrame({"id": list(range(batch * 50, (batch + 1) * 50))})
                ddf = daft.from_pydict(
                    {"id": list(range(batch * 50, (batch + 1) * 50))}
                )

                await pandas_writer.write(pdf)
                await daft_writer.write(ddf)

                assert pandas_writer.chunk_count == daft_writer.chunk_count, (
                    f"chunk_count diverged after write {batch + 1}: "
                    f"pandas={pandas_writer.chunk_count}, daft={daft_writer.chunk_count}"
                )

            await pandas_writer.close()
            await daft_writer.close()

            assert pandas_writer.chunk_count == daft_writer.chunk_count
            assert len(pandas_writer.partitions) == len(daft_writer.partitions)

    @pytest.mark.asyncio
    async def test_no_duplicate_file_uploads(self, temp_dir: str):
        """Verify no duplicate file names are uploaded (original bug symptom)."""
        pytest.importorskip("daft")
        from collections import Counter
        from unittest.mock import AsyncMock, patch

        import daft

        from application_sdk.common.types import DataframeType
        from application_sdk.storage.formats.json import JsonFileWriter

        uploaded_files: list[str] = []

        async def mock_upload(
            source, destination=None, retain_local_copy=False, **kwargs
        ):
            filename = os.path.basename(source)
            uploaded_files.append(filename)
            if os.path.exists(source) and not retain_local_copy:
                os.remove(source)

        with patch(
            "application_sdk.storage.formats._upload_file",
            new_callable=AsyncMock,
            side_effect=mock_upload,
        ):
            writer = JsonFileWriter(
                path=temp_dir,
                typename="test",
                buffer_size=50,
                chunk_size=80,
                retain_local_copy=False,
                dataframe_type=DataframeType.daft,
            )

            for batch in range(3):
                df = daft.from_pydict(
                    {"id": list(range(batch * 100, (batch + 1) * 100))}
                )
                await writer.write(df)

            await writer.close()

            json_uploads = [
                f
                for f in uploaded_files
                if f.endswith(".json") and "statistics" not in f
            ]
            counts = Counter(json_uploads)
            duplicates = {f: c for f, c in counts.items() if c > 1}

            assert not duplicates, f"Duplicate file uploads detected: {duplicates}"


class TestParquetWriterDataIntegrity:
    """Verify ParquetFileWriter preserves all data both on disk AND through
    the new FileReference upload boundary.

    Parquet's pq.write_table() overwrites the target file (unlike JSON which
    appends). The _flush_buffer override in ParquetFileWriter ensures each
    sub-chunk gets a unique filename via chunk_part advancement. See HYP-773.

    Upload is deferred to close()'s returned WriterResult.files — these tests
    verify both the local-disk contract AND that persisting result.files via
    persist_file_reference produces a complete copy in the store. All tests
    use buffer_size=50.
    """

    BUFFER_SIZE = 50

    @pytest.fixture
    def temp_dir(self):
        temp_path = tempfile.mkdtemp(prefix="parquet_integrity_")
        yield temp_path
        shutil.rmtree(temp_path, ignore_errors=True)

    async def _run_write_scenario(
        self, temp_dir: str, row_count: int, num_writes: int = 1
    ) -> dict:
        """Run a write scenario, persist via FileReference, and return results.

        Verifies the full contract: writer leaves files on disk, close() hands
        back a WriterResult, and ``persist_file_reference(store, result.files)``
        uploads every parquet file (and statistics sidecar) — zero loss.
        """
        from application_sdk.storage.factory import create_memory_store
        from application_sdk.storage.formats.parquet import ParquetFileWriter
        from application_sdk.storage.reference import persist_file_reference

        writer = ParquetFileWriter(
            path=temp_dir,
            typename="test_entity",
            buffer_size=self.BUFFER_SIZE,
            use_consolidation=False,
        )

        rows_per_write = row_count // num_writes
        total = 0
        for batch_idx in range(num_writes):
            start = batch_idx * rows_per_write
            df = pd.DataFrame({"id": list(range(start, start + rows_per_write))})
            await writer.write(df)
            total += rows_per_write

        result = await writer.close()

        # The writer must surface an ephemeral FileReference scoped to its
        # own output directory — never to anything outside.
        assert result.files.is_durable is False
        assert result.files.local_path == writer.path
        assert result.files.local_path.startswith(temp_dir)

        # Disk: read back all parquet files from the writer-owned subdir.
        parquet_files = glob.glob(
            os.path.join(result.files.local_path, "**", "*.parquet"),
            recursive=True,
        )
        if parquet_files:
            disk_df = pd.concat(
                [pd.read_parquet(f) for f in parquet_files], ignore_index=True
            )
        else:
            disk_df = pd.DataFrame()

        # Object store: persist via the FileReference and read everything back.
        store = create_memory_store()
        durable = await persist_file_reference(store, result.files)
        assert durable.is_durable is True

        from application_sdk.storage.batch import list_keys

        store_keys = await list_keys(durable.storage_path, store, suffix=".parquet")
        # Download each parquet key and re-read to count rows.
        from application_sdk.storage.ops import _get_bytes

        store_ids: set[int] = set()
        store_rows = 0
        import io as _io

        for key in store_keys:
            data = await _get_bytes(key, store, normalize=False)
            df_back = pd.read_parquet(_io.BytesIO(data))
            store_rows += len(df_back)
            store_ids.update(df_back["id"].tolist())

        return {
            "disk_rows": len(disk_df),
            "disk_ids": set(disk_df["id"].tolist()) if len(disk_df) > 0 else set(),
            "store_rows": store_rows,
            "store_ids": store_ids,
            "expected": total,
        }

    def _assert_no_data_loss(self, r: dict, expected: int) -> None:
        """Assert zero data loss on disk AND in the object store."""
        expected_ids = set(range(expected))
        assert (
            r["disk_rows"] == expected
        ), f"Disk data loss: {r['disk_rows']}/{expected} rows"
        assert r["disk_ids"] == expected_ids, "Disk: missing IDs detected"
        assert (
            r["store_rows"] == expected
        ), f"Store data loss: {r['store_rows']}/{expected} rows"
        assert r["store_ids"] == expected_ids, "Store: missing IDs detected"

    # --- Single write() scenarios ---

    async def test_single_row(self, temp_dir: str):
        """1 row: minimal case, well under buffer_size."""
        r = await self._run_write_scenario(temp_dir, row_count=1)
        self._assert_no_data_loss(r, 1)

    async def test_rows_less_than_buffer_size(self, temp_dir: str):
        """30 rows < buffer_size=50: single sub-chunk, no splitting."""
        r = await self._run_write_scenario(temp_dir, row_count=30)
        self._assert_no_data_loss(r, 30)

    async def test_rows_equal_to_buffer_size(self, temp_dir: str):
        """50 rows == buffer_size=50: exactly one sub-chunk, boundary."""
        r = await self._run_write_scenario(temp_dir, row_count=50)
        self._assert_no_data_loss(r, 50)

    async def test_rows_just_over_buffer_size(self, temp_dir: str):
        """51 rows: 2 sub-chunks (50+1). First overwrite scenario."""
        r = await self._run_write_scenario(temp_dir, row_count=51)
        self._assert_no_data_loss(r, 51)

    async def test_rows_double_buffer_size(self, temp_dir: str):
        """100 rows == 2x buffer_size: exactly 2 sub-chunks (50+50)."""
        r = await self._run_write_scenario(temp_dir, row_count=100)
        self._assert_no_data_loss(r, 100)

    async def test_rows_exceeding_buffer_size(self, temp_dir: str):
        """120 rows: 3 sub-chunks (50+50+20). Core HYP-773 regression case.

        Before the fix, all three sub-chunks wrote to chunk-0-part0.parquet
        and only the last 20 rows survived (83% data loss).
        """
        r = await self._run_write_scenario(temp_dir, row_count=120)
        self._assert_no_data_loss(r, 120)

    async def test_rows_many_sub_chunks(self, temp_dir: str):
        """501 rows: 11 sub-chunks (10x50 + 1). Stress test for part numbering."""
        r = await self._run_write_scenario(temp_dir, row_count=501)
        self._assert_no_data_loss(r, 501)

    # --- Multiple write() scenarios ---

    async def test_multiple_small_writes(self, temp_dir: str):
        """5 writes of 30 rows each: all under buffer_size, tests chunk_count."""
        r = await self._run_write_scenario(temp_dir, row_count=150, num_writes=5)
        self._assert_no_data_loss(r, 150)

    async def test_multiple_writes_each_exceeding_buffer_size(self, temp_dir: str):
        """3 writes of 200 rows each: tests correctness across write() calls.

        Each write() produces 4 sub-chunks (50+50+50+50). Verifies data is
        preserved both within each write() and across write() calls.
        """
        r = await self._run_write_scenario(temp_dir, row_count=600, num_writes=3)
        self._assert_no_data_loss(r, 600)

    async def test_multiple_writes_mixed_sizes(self, temp_dir: str):
        """4 writes of 75 rows each: 2 sub-chunks per write (50+25).

        Tests that chunk_count advances correctly across write() calls
        when each write produces a partial last sub-chunk.
        """
        r = await self._run_write_scenario(temp_dir, row_count=300, num_writes=4)
        self._assert_no_data_loss(r, 300)
