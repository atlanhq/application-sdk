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

import json
import os
import shutil
import tempfile
from typing import Dict, List, Set
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

    def extract_ids_from_store(self, store: Dict[str, List[str]]) -> Set[int]:
        """Extract all record IDs from mock object store."""
        ids: Set[int] = set()
        for path, content in store.items():
            if "statistics" in path:
                continue
            for line in content:
                try:
                    record = json.loads(line)
                    ids.add(record.get("id"))
                except Exception:
                    pass
        return ids

    @pytest.mark.asyncio
    async def test_pandas_writer_no_data_loss(self, temp_dir: str):
        """Verify pandas writer preserves all records across multiple write() calls."""
        object_store: Dict[str, List[str]] = {}

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

        object_store: Dict[str, List[str]] = {}

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

        uploaded_files: List[str] = []

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
    """Verify ParquetFileWriter preserves all data when DataFrames exceed buffer_size.

    Parquet's pq.write_table() overwrites the target file (unlike JSON which
    appends). Without incrementing chunk_part after each _flush_buffer call,
    _write_dataframe's buffer loop writes every sub-chunk to the same filename,
    silently losing all data except the last sub-chunk. See HYP-773.
    """

    @pytest.fixture
    def temp_dir(self):
        temp_path = tempfile.mkdtemp(prefix="parquet_integrity_")
        yield temp_path
        shutil.rmtree(temp_path, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_single_write_exceeding_buffer_size(self, temp_dir: str):
        """A single write() with rows > buffer_size must preserve all rows.

        This is the core HYP-773 regression test. With buffer_size=50 and
        120 rows, the writer creates 3 sub-chunks (50+50+20). Before the
        fix, all three wrote to chunk-0-part0.parquet and only the last
        20 rows survived.
        """
        from application_sdk.storage.formats.parquet import ParquetFileWriter

        with patch(
            "application_sdk.storage.formats._upload_file",
            new_callable=AsyncMock,
        ):
            writer = ParquetFileWriter(
                path=temp_dir,
                typename="test_entity",
                buffer_size=50,
                use_consolidation=False,
            )

            df = pd.DataFrame(
                {"id": list(range(120)), "value": [f"row_{i}" for i in range(120)]}
            )
            await writer.write(df)
            await writer.close()

            # Read back all parquet files written to disk
            import glob

            parquet_files = glob.glob(
                os.path.join(temp_dir, "test_entity", "**", "*.parquet"),
                recursive=True,
            )
            assert len(parquet_files) > 0, "No parquet files written"

            result_df = pd.concat(
                [pd.read_parquet(f) for f in parquet_files], ignore_index=True
            )
            assert len(result_df) == 120, (
                f"Expected 120 rows, got {len(result_df)}. "
                f"Data loss: {120 - len(result_df)} rows missing."
            )
            assert set(result_df["id"]) == set(range(120))

    @pytest.mark.asyncio
    async def test_multiple_writes_exceeding_buffer_size(self, temp_dir: str):
        """Multiple write() calls, each exceeding buffer_size, must preserve all rows."""
        from application_sdk.storage.formats.parquet import ParquetFileWriter

        with patch(
            "application_sdk.storage.formats._upload_file",
            new_callable=AsyncMock,
        ):
            writer = ParquetFileWriter(
                path=temp_dir,
                typename="test_entity",
                buffer_size=50,
                use_consolidation=False,
            )

            total_records = 0
            for batch_idx in range(3):
                start = batch_idx * 200
                df = pd.DataFrame(
                    {
                        "id": list(range(start, start + 200)),
                        "batch": [f"batch_{batch_idx}"] * 200,
                    }
                )
                await writer.write(df)
                total_records += 200

            await writer.close()

            import glob

            parquet_files = glob.glob(
                os.path.join(temp_dir, "test_entity", "**", "*.parquet"),
                recursive=True,
            )
            result_df = pd.concat(
                [pd.read_parquet(f) for f in parquet_files], ignore_index=True
            )
            assert len(result_df) == total_records, (
                f"Expected {total_records} rows, got {len(result_df)}. "
                f"Data loss: {total_records - len(result_df)} rows missing."
            )
            assert set(result_df["id"]) == set(range(total_records))
