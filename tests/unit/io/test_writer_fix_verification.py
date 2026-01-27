"""Verify the DAFT writer fix: no data loss, matching pandas behavior.

Run with:
    uv run pytest tests/unit/io/test_writer_fix_verification.py -v -s
"""

import json
import os
import shutil
import tempfile
from typing import Dict, List, Set

import pandas as pd
import pytest


class TestWriterFixVerification:
    """Verify both pandas and daft writers have consistent behavior and no data loss."""

    @pytest.fixture
    def temp_dir(self):
        temp_path = tempfile.mkdtemp(prefix="writer_fix_test_")
        yield temp_path
        shutil.rmtree(temp_path, ignore_errors=True)

    def get_ids_from_object_store(self, store: Dict[str, List[str]]) -> Set[int]:
        """Extract all IDs from mock object store."""
        ids = set()
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
    async def test_pandas_no_data_loss(self, temp_dir: str):
        """Verify pandas writer has no data loss with production settings."""
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

            for batch in range(3):
                df = pd.DataFrame(
                    {
                        "id": list(range(batch * 120, (batch + 1) * 120)),
                        "batch": [f"batch_{batch}"] * 120,
                    }
                )
                await writer.write(df)

            stats = await writer.close()

            ids_in_store = self.get_ids_from_object_store(object_store)
            expected_ids = set(range(360))

            print("\n[PANDAS] Records written: 360")
            print(f"[PANDAS] Records in object store: {len(ids_in_store)}")
            print(
                f"[PANDAS] chunk_count: {writer.chunk_count}, partitions: {writer.partitions}"
            )

            assert (
                ids_in_store == expected_ids
            ), f"Missing IDs: {expected_ids - ids_in_store}"
            print("[PANDAS] PASS - No data loss")

            return {
                "chunk_count": writer.chunk_count,
                "partitions": writer.partitions,
                "total_record_count": stats.total_record_count,
            }

    @pytest.mark.asyncio
    async def test_daft_no_data_loss(self, temp_dir: str):
        """Verify daft writer has no data loss with production settings."""
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

            for batch in range(3):
                df = daft.from_pydict(
                    {
                        "id": list(range(batch * 120, (batch + 1) * 120)),
                        "batch": [f"batch_{batch}"] * 120,
                    }
                )
                await writer.write(df)

            stats = await writer.close()

            ids_in_store = self.get_ids_from_object_store(object_store)
            expected_ids = set(range(360))

            print("\n[DAFT] Records written: 360")
            print(f"[DAFT] Records in object store: {len(ids_in_store)}")
            print(
                f"[DAFT] chunk_count: {writer.chunk_count}, partitions: {writer.partitions}"
            )

            assert (
                ids_in_store == expected_ids
            ), f"Missing IDs: {expected_ids - ids_in_store}"
            print("[DAFT] PASS - No data loss")

            return {
                "chunk_count": writer.chunk_count,
                "partitions": writer.partitions,
                "total_record_count": stats.total_record_count,
            }

    @pytest.mark.asyncio
    async def test_pandas_and_daft_behavior_match(self, temp_dir: str):
        """Verify pandas and daft have identical chunk_count behavior."""
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

            print(f"\n{'='*60}")
            print("COMPARING PANDAS vs DAFT BEHAVIOR")
            print(f"{'='*60}")

            pandas_states = []
            daft_states = []

            for batch in range(3):
                pdf = pd.DataFrame({"id": list(range(batch * 50, (batch + 1) * 50))})
                ddf = daft.from_pydict(
                    {"id": list(range(batch * 50, (batch + 1) * 50))}
                )

                await pandas_writer.write(pdf)
                await daft_writer.write(ddf)

                pandas_states.append(
                    (pandas_writer.chunk_count, pandas_writer.chunk_part)
                )
                daft_states.append((daft_writer.chunk_count, daft_writer.chunk_part))

                print(f"\nAfter write {batch + 1}:")
                print(
                    f"  PANDAS: chunk_count={pandas_writer.chunk_count}, chunk_part={pandas_writer.chunk_part}"
                )
                print(
                    f"  DAFT:   chunk_count={daft_writer.chunk_count}, chunk_part={daft_writer.chunk_part}"
                )

            await pandas_writer.close()
            await daft_writer.close()

            print("\nAfter close:")
            print(
                f"  PANDAS: chunk_count={pandas_writer.chunk_count}, partitions={pandas_writer.partitions}"
            )
            print(
                f"  DAFT:   chunk_count={daft_writer.chunk_count}, partitions={daft_writer.partitions}"
            )

            assert (
                pandas_writer.chunk_count == daft_writer.chunk_count
            ), f"chunk_count mismatch: pandas={pandas_writer.chunk_count}, daft={daft_writer.chunk_count}"

            assert (
                len(pandas_writer.partitions) == len(daft_writer.partitions)
            ), f"partitions length mismatch: pandas={len(pandas_writer.partitions)}, daft={len(daft_writer.partitions)}"

            print("\nPASS - PANDAS and DAFT behavior match!")

    @pytest.mark.asyncio
    async def test_no_file_name_collisions(self, temp_dir: str):
        """Verify no duplicate file uploads (the original bug)."""
        pytest.importorskip("daft")
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

            print(f"\n{'='*60}")
            print("FILE UPLOAD ANALYSIS")
            print(f"{'='*60}")
            print(f"\nUploaded files: {json_uploads}")

            from collections import Counter

            counts = Counter(json_uploads)
            duplicates = {f: c for f, c in counts.items() if c > 1}

            if duplicates:
                print(f"\nFAIL - DUPLICATE UPLOADS DETECTED: {duplicates}")
                pytest.fail(f"Duplicate file uploads: {duplicates}")
            else:
                print("\nPASS - No duplicate file uploads (bug is fixed!)")

    @pytest.mark.asyncio
    async def test_comprehensive_data_integrity(self, temp_dir: str):
        """Full data integrity test with various batch sizes."""
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
                    content = f.readlines()
                if dest in object_store:
                    print(f"  WARNING: Overwriting {os.path.basename(dest)}")
                object_store[dest] = content
                if not retain_local_copy:
                    os.remove(source)

        with patch(
            "application_sdk.io.ObjectStore.upload_file",
            new_callable=AsyncMock,
            side_effect=mock_upload,
        ):
            writer = JsonFileWriter(
                path=temp_dir,
                typename="comprehensive",
                buffer_size=30,
                chunk_size=75,
                retain_local_copy=False,
                dataframe_type=DataframeType.daft,
            )

            print(f"\n{'='*60}")
            print("COMPREHENSIVE DATA INTEGRITY TEST")
            print("  buffer_size=30, chunk_size=75")
            print("  5 writes of varying sizes: 50, 80, 120, 45, 95 records")
            print(f"{'='*60}")

            batch_sizes = [50, 80, 120, 45, 95]
            current_id = 0

            for i, size in enumerate(batch_sizes):
                df = daft.from_pydict(
                    {
                        "id": list(range(current_id, current_id + size)),
                        "batch": [f"batch_{i}"] * size,
                    }
                )
                print(
                    f"\nWrite {i+1}: {size} records (IDs {current_id}-{current_id + size - 1})"
                )
                await writer.write(df)
                current_id += size

            stats = await writer.close()

            total_written = sum(batch_sizes)
            ids_in_store = self.get_ids_from_object_store(object_store)
            expected_ids = set(range(total_written))
            missing = expected_ids - ids_in_store

            print(f"\n{'='*60}")
            print("RESULTS")
            print(f"{'='*60}")
            print(f"  Total written: {total_written}")
            print(f"  In object store: {len(ids_in_store)}")
            print(f"  Stats total_record_count: {stats.total_record_count}")

            if missing:
                print(f"\nFAIL - DATA LOSS: Missing {len(missing)} records")
                print(f"   Missing IDs: {sorted(missing)[:20]}...")
                pytest.fail(f"Data loss: {len(missing)} records missing")
            else:
                print(f"\nPASS - All {total_written} records present - NO DATA LOSS!")
