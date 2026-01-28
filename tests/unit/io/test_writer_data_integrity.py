"""
Writer Data Integrity Tests - Prevents data loss regression in JsonFileWriter.

This test suite verifies that both pandas and daft writers maintain data integrity
when multiple write() calls are made. It specifically guards against a bug where
the daft writer would lose data due to:
1. Not uploading files at the end of each write() call
2. Not incrementing chunk_count after each write() call
3. File name collisions causing overwrites in object store

The bug manifested when using retain_local_copy=False (production default):
- Write 1: uploads chunk-0-part0.json with records 0-99
- Write 2: resets chunk_part, creates NEW chunk-0-part0.json with records 100-199
- Result: records 0-99 are overwritten and lost

Run with:
    uv run pytest tests/unit/io/test_writer_data_integrity.py -v -s
"""

import json
import os
import shutil
import tempfile
from typing import Dict, List, Set

import pandas as pd
import pytest


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
        from unittest.mock import AsyncMock, patch

        from application_sdk.common.types import DataframeType
        from application_sdk.io.json import JsonFileWriter

        object_store: Dict[str, List[str]] = {}

        async def mock_upload(
            source, destination=None, retain_local_copy=False, **kwargs
        ):
            dest = destination or source
            if os.path.exists(source):
                with open(source, "r") as f:
                    object_store[dest] = f.readlines()
                if not retain_local_copy:
                    os.remove(source)

        with patch(
            "application_sdk.io.ObjectStore.upload_file",
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
        from application_sdk.io.json import JsonFileWriter

        object_store: Dict[str, List[str]] = {}

        async def mock_upload(
            source, destination=None, retain_local_copy=False, **kwargs
        ):
            dest = destination or source
            if os.path.exists(source):
                with open(source, "r") as f:
                    object_store[dest] = f.readlines()
                if not retain_local_copy:
                    os.remove(source)

        with patch(
            "application_sdk.io.ObjectStore.upload_file",
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
        from application_sdk.io.json import JsonFileWriter

        async def mock_upload(
            source, destination=None, retain_local_copy=False, **kwargs
        ):
            if os.path.exists(source) and not retain_local_copy:
                os.remove(source)

        with patch(
            "application_sdk.io.ObjectStore.upload_file",
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
        from application_sdk.io.json import JsonFileWriter

        uploaded_files: List[str] = []

        async def mock_upload(
            source, destination=None, retain_local_copy=False, **kwargs
        ):
            filename = os.path.basename(source)
            uploaded_files.append(filename)
            if os.path.exists(source) and not retain_local_copy:
                os.remove(source)

        with patch(
            "application_sdk.io.ObjectStore.upload_file",
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
