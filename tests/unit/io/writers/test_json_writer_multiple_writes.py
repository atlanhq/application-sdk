"""Unit tests for JsonFileWriter multiple write() calls scenario.

This test module specifically tests the bug where chunk_part gets reset
to 0 on every write() call, causing files to be orphaned or overwritten
when processing data in batches across multiple write() calls.

The actual workflow pattern is:
    writer = JsonFileWriter(...)
    for batch in data_batches:
        await writer.write(batch)  # Multiple calls!
    await writer.close()
"""

import os
from typing import List
from unittest.mock import AsyncMock, patch

import pandas as pd
import pytest

from application_sdk.io.json import JsonFileWriter


class TestJsonWriterMultipleWrites:
    """Test suite for JsonFileWriter with multiple write() calls.

    These tests verify that the writer correctly handles multiple write()
    calls on the same instance, which is the standard pattern in workflows
    like transform_data and query_executor.
    """

    @pytest.fixture
    def temp_output_path(self, tmp_path) -> str:
        """Create a temporary directory for test output."""
        output_path = tmp_path / "test_output"
        output_path.mkdir(parents=True, exist_ok=True)
        return str(output_path)

    @pytest.fixture
    def mock_object_store(self):
        """Mock ObjectStore.upload_file to track uploads."""
        with patch(
            "application_sdk.services.objectstore.ObjectStore.upload_file",
            new_callable=AsyncMock,
        ) as mock_upload:
            yield mock_upload

    def create_test_dataframe(self, num_rows: int, start_id: int = 0) -> pd.DataFrame:
        """Create a test DataFrame with specified number of rows.

        Args:
            num_rows: Number of rows to create.
            start_id: Starting ID for the rows (to distinguish batches).

        Returns:
            A pandas DataFrame with test data.
        """
        return pd.DataFrame(
            {
                "id": range(start_id, start_id + num_rows),
                "name": [f"item_{i}" for i in range(start_id, start_id + num_rows)],
                "value": [i * 10 for i in range(start_id, start_id + num_rows)],
            }
        )

    @pytest.mark.asyncio
    async def test_single_write_works(
        self, temp_output_path: str, mock_object_store: AsyncMock
    ) -> None:
        """Test that a single write() call works correctly.

        This is the baseline test to ensure basic functionality works.
        """
        writer = JsonFileWriter(
            path=temp_output_path,
            typename="test_type",
            chunk_size=100,
            buffer_size=50,
        )

        df = self.create_test_dataframe(50)
        await writer.write(df)
        stats = await writer.close()

        assert stats.total_record_count == 50
        # Verify at least one upload happened
        assert mock_object_store.call_count >= 1

    @pytest.mark.asyncio
    async def test_multiple_writes_small_data_no_upload_during_write(
        self, temp_output_path: str, mock_object_store: AsyncMock
    ) -> None:
        """Test multiple write() calls where no upload triggers during write.

        When data is small enough to not trigger upload during write(),
        all data should accumulate and be uploaded on close().
        """
        writer = JsonFileWriter(
            path=temp_output_path,
            typename="test_type",
            chunk_size=1000,  # Large enough to not trigger during writes
            buffer_size=50,
        )

        # Three write() calls, simulating batched processing
        df1 = self.create_test_dataframe(100, start_id=0)
        df2 = self.create_test_dataframe(100, start_id=100)
        df3 = self.create_test_dataframe(100, start_id=200)

        await writer.write(df1)
        await writer.write(df2)
        await writer.write(df3)

        stats = await writer.close()

        assert stats.total_record_count == 300
        # Should have uploaded the accumulated file
        assert mock_object_store.call_count >= 1

    @pytest.mark.asyncio
    async def test_chunk_part_not_reset_between_writes(
        self, temp_output_path: str, mock_object_store: AsyncMock
    ) -> None:
        """Test that chunk_part is NOT reset to 0 between write() calls.

        This is the core bug test. The issue is in _write_dataframe():
            if self.chunk_start is None:
                self.chunk_part = 0  # BUG: This resets on EVERY write!

        Expected: chunk_part should only be initialized once in __init__,
        not reset on every write() call.
        """
        # Track chunk_part values at critical points
        chunk_part_values: List[int] = []

        writer = JsonFileWriter(
            path=temp_output_path,
            typename="test_type",
            chunk_size=50,  # Triggers upload at 50 records
            buffer_size=10,
        )

        # Patch _write_dataframe to capture chunk_part BEFORE the reset
        original_write_dataframe = writer._write_dataframe

        async def tracking_write_dataframe(dataframe, **kwargs):
            # Capture chunk_part BEFORE the reset line executes
            # (This captures after because we call original, but we can
            # check the value before calling)
            before_value = writer.chunk_part
            chunk_part_values.append(("before_write", before_value))
            result = await original_write_dataframe(dataframe, **kwargs)
            chunk_part_values.append(("after_write", writer.chunk_part))
            return result

        writer._write_dataframe = tracking_write_dataframe

        # Write 1: 60 records (triggers upload at 50)
        df1 = self.create_test_dataframe(60, start_id=0)
        await writer.write(df1)

        # Write 2: 60 records
        df2 = self.create_test_dataframe(60, start_id=60)
        await writer.write(df2)

        stats = await writer.close()

        print(f"chunk_part values: {chunk_part_values}")
        print(f"Total uploads: {mock_object_store.call_count}")

        assert stats.total_record_count == 120

        # The bug manifests as:
        # - before_write for write 2 should be the value from end of write 1
        # - But if reset happens, it will be 0
        #
        # Expected (no bug): [('before', 0), ('after', 1), ('before', 1), ('after', 2+)]
        # With bug: [('before', 0), ('after', 1), ('before', 0), ('after', 1)]

    @pytest.mark.asyncio
    async def test_chunk_part_preserved_across_writes(
        self, temp_output_path: str, mock_object_store: AsyncMock
    ) -> None:
        """Verify chunk_part is preserved across multiple write() calls.

        This directly tests that chunk_part doesn't reset to 0 when
        chunk_start is None (the default case).
        """
        writer = JsonFileWriter(
            path=temp_output_path,
            typename="test_type",
            chunk_size=30,  # Triggers upload at every 30 records
            buffer_size=10,
        )

        # Write 1: 40 records - should trigger upload at 30, chunk_part becomes 1
        df1 = self.create_test_dataframe(40, start_id=0)
        await writer.write(df1)

        chunk_part_after_write1 = writer.chunk_part
        print(f"chunk_part after write 1: {chunk_part_after_write1}")

        # Write 2: 40 records
        df2 = self.create_test_dataframe(40, start_id=40)
        await writer.write(df2)

        chunk_part_after_write2 = writer.chunk_part
        print(f"chunk_part after write 2: {chunk_part_after_write2}")

        # Write 3: 40 records
        df3 = self.create_test_dataframe(40, start_id=80)
        await writer.write(df3)

        stats = await writer.close()

        # Total: 120 records with chunk_size=30 = 4 chunks
        assert stats.total_record_count == 120

        # Verify uploads: should have ~4 uploads for 4 chunks
        print(f"Total upload calls: {mock_object_store.call_count}")

    @pytest.mark.asyncio
    async def test_files_not_orphaned_across_writes(
        self, temp_output_path: str, mock_object_store: AsyncMock
    ) -> None:
        """Verify that files created during one write() are not orphaned.

        The bug scenario:
        1. Write 1 creates file at chunk_part=1, upload triggered
        2. Write 1 continues writing to chunk_part=1 file
        3. Write 2 starts, chunk_part RESETS to 0
        4. File at chunk_part=1 from Write 1 is NEVER uploaded (orphaned)

        This test verifies all files are properly uploaded.
        """
        uploaded_files: List[str] = []

        async def track_uploads(source, **kwargs):
            uploaded_files.append(source)
            # Simulate file deletion after upload (default behavior)
            if os.path.exists(source):
                os.remove(source)

        mock_object_store.side_effect = track_uploads

        writer = JsonFileWriter(
            path=temp_output_path,
            typename="test_type",
            chunk_size=25,
            buffer_size=10,
        )

        # Write in multiple batches
        for batch_num in range(4):
            df = self.create_test_dataframe(30, start_id=batch_num * 30)
            await writer.write(df)

        stats = await writer.close()

        assert stats.total_record_count == 120

        # Use os.path to check for remaining json files (orphaned files)
        output_dir = os.path.join(temp_output_path, "test_type")
        if os.path.exists(output_dir):
            remaining_files = [f for f in os.listdir(output_dir) if f.endswith(".json")]
            print(f"Remaining files after close: {remaining_files}")
            # With retain_local_copy=False (default), no files should remain
            # If files remain, they were orphaned (never uploaded)

        print(f"Uploaded files: {uploaded_files}")
        print(f"Total uploads: {len(uploaded_files)}")

        # With 120 records and chunk_size=25, we expect ~5 chunks
        # All should be uploaded

    @pytest.mark.asyncio
    async def test_total_record_count_accumulates_across_writes(
        self, temp_output_path: str, mock_object_store: AsyncMock
    ) -> None:
        """Verify total_record_count correctly accumulates across writes.

        Unlike chunk_part (which has the bug), total_record_count should
        correctly accumulate across multiple write() calls.
        """
        writer = JsonFileWriter(
            path=temp_output_path,
            typename="test_type",
            chunk_size=100,
            buffer_size=50,
        )

        counts_after_each_write = []

        for i in range(5):
            df = self.create_test_dataframe(20, start_id=i * 20)
            await writer.write(df)
            counts_after_each_write.append(writer.total_record_count)

        stats = await writer.close()

        # Verify accumulation
        expected_counts = [20, 40, 60, 80, 100]
        assert counts_after_each_write == expected_counts
        assert stats.total_record_count == 100


class TestJsonWriterDaftMultipleWrites:
    """Test suite specifically for DAFT DataFrame multiple write() scenario.

    For DAFT dataframes, the behavior is different from pandas:
    - chunk_count is NOT incremented during write(), only on close()
    - All writes share the same chunk_count (0)
    - chunk_part controls file naming within that chunk
    - If chunk_part resets between writes, FILE NAME COLLISION OCCURS!

    This is the critical bug path.
    """

    @pytest.fixture
    def temp_output_path(self, tmp_path) -> str:
        """Create a temporary directory for test output."""
        output_path = tmp_path / "test_output"
        output_path.mkdir(parents=True, exist_ok=True)
        return str(output_path)

    @pytest.fixture
    def mock_object_store(self):
        """Mock ObjectStore.upload_file to track uploads."""
        with patch(
            "application_sdk.services.objectstore.ObjectStore.upload_file",
            new_callable=AsyncMock,
        ) as mock_upload:
            yield mock_upload

    def create_daft_dataframe(self, num_rows: int, start_id: int = 0):
        """Create a test daft DataFrame.

        Args:
            num_rows: Number of rows to create.
            start_id: Starting ID for the rows.

        Returns:
            A daft DataFrame with test data.
        """
        try:
            import daft

            data = {
                "id": list(range(start_id, start_id + num_rows)),
                "name": [f"item_{i}" for i in range(start_id, start_id + num_rows)],
                "value": [i * 10 for i in range(start_id, start_id + num_rows)],
            }
            return daft.from_pydict(data)
        except ImportError:
            pytest.skip("daft not installed")

    @pytest.mark.asyncio
    async def test_daft_multiple_writes_chunk_part_reset_bug(
        self, temp_output_path: str, mock_object_store: AsyncMock
    ) -> None:
        """Test the chunk_part reset bug with DAFT multiple write() calls.

        For DAFT:
        - Write 1: chunk_part=0, uploads part0, chunk_part=1, writes to part1
        - Write 2: chunk_part RESETS to 0 (BUG!), writes to part0 again

        This causes:
        1. File name collision (part0 reused)
        2. Data loss in object store (same path overwritten)
        3. Orphaned files (part1 from write 1 may never upload)
        """
        try:
            import daft  # noqa: F401

            from application_sdk.common.types import DataframeType
        except ImportError:
            pytest.skip("daft not installed")

        # Track all uploaded file paths
        uploaded_file_paths: List[str] = []

        async def track_upload(source, **kwargs):
            uploaded_file_paths.append(os.path.basename(source))
            # Simulate file deletion after upload
            if os.path.exists(source):
                os.remove(source)

        mock_object_store.side_effect = track_upload

        writer = JsonFileWriter(
            path=temp_output_path,
            typename="test_type",
            chunk_size=30,  # Small to trigger uploads
            buffer_size=10,
            dataframe_type=DataframeType.daft,
        )

        # Write 1: 40 rows - should trigger upload at 30, chunk_part becomes 1
        df1 = self.create_daft_dataframe(40, start_id=0)
        await writer.write(df1)

        chunk_part_after_write1 = writer.chunk_part
        chunk_count_after_write1 = writer.chunk_count
        print(
            f"After write 1: chunk_count={chunk_count_after_write1}, chunk_part={chunk_part_after_write1}"
        )

        # Write 2: 40 rows
        df2 = self.create_daft_dataframe(40, start_id=40)
        await writer.write(df2)

        chunk_part_after_write2 = writer.chunk_part
        chunk_count_after_write2 = writer.chunk_count
        print(
            f"After write 2: chunk_count={chunk_count_after_write2}, chunk_part={chunk_part_after_write2}"
        )

        stats = await writer.close()

        print(f"Uploaded files: {uploaded_file_paths}")
        print(f"Total uploads: {len(uploaded_file_paths)}")

        # For DAFT, chunk_count should stay 0 during writes
        assert (
            chunk_count_after_write1 == 0
        ), "chunk_count should not increment during daft write"
        assert (
            chunk_count_after_write2 == 0
        ), "chunk_count should not increment during daft write"

        # Verify total record count
        assert stats.total_record_count == 80

        # BUG CHECK: With 80 rows and chunk_size=30, we should have:
        # - Upload at 30 (part0)
        # - Upload at 60 (part1 or part0 again if bug)
        # - Final upload on close (part2 or part1 again if bug)
        #
        # If bug exists: part0 appears multiple times in uploaded_file_paths
        # If no bug: each part number appears once

        part0_count = sum(1 for f in uploaded_file_paths if "part0" in f)
        print(f"part0 upload count: {part0_count}")

        # This assertion will FAIL if the bug exists (part0 uploaded multiple times)
        # Expected: part0 should only be uploaded ONCE
        # With bug: part0 uploaded 2+ times (data loss due to overwrite)
        if part0_count > 1:
            print(
                "BUG DETECTED: chunk_part reset causes part0 to be uploaded multiple times!"
            )
            print(
                "This causes data loss - later uploads overwrite earlier ones in object store"
            )

    @pytest.mark.asyncio
    async def test_daft_chunk_count_only_increments_on_close(
        self, temp_output_path: str, mock_object_store: AsyncMock
    ) -> None:
        """Verify that for DAFT, chunk_count only increments on close().

        This is expected behavior, but combined with chunk_part reset,
        it causes the bug.
        """
        try:
            from application_sdk.common.types import DataframeType
        except ImportError:
            pytest.skip("daft not installed")

        writer = JsonFileWriter(
            path=temp_output_path,
            typename="test_type",
            chunk_size=100,
            buffer_size=50,
            dataframe_type=DataframeType.daft,
        )

        df1 = self.create_daft_dataframe(30, start_id=0)
        await writer.write(df1)
        assert writer.chunk_count == 0, "chunk_count should be 0 after first daft write"

        df2 = self.create_daft_dataframe(30, start_id=30)
        await writer.write(df2)
        assert (
            writer.chunk_count == 0
        ), "chunk_count should still be 0 after second daft write"

        await writer.close()

        # Only after close() should chunk_count increment
        assert writer.chunk_count == 1, "chunk_count should be 1 after close"


class TestJsonWriterChunkPartInitialization:
    """Tests specifically for chunk_part initialization behavior."""

    @pytest.fixture
    def temp_output_path(self, tmp_path) -> str:
        """Create a temporary directory for test output."""
        output_path = tmp_path / "test_output"
        output_path.mkdir(parents=True, exist_ok=True)
        return str(output_path)

    @pytest.fixture
    def mock_object_store(self):
        """Mock ObjectStore.upload_file."""
        with patch(
            "application_sdk.services.objectstore.ObjectStore.upload_file",
            new_callable=AsyncMock,
        ) as mock_upload:
            yield mock_upload

    def create_test_dataframe(self, num_rows: int, start_id: int = 0) -> pd.DataFrame:
        """Create a test DataFrame."""
        return pd.DataFrame(
            {
                "id": range(start_id, start_id + num_rows),
                "name": [f"item_{i}" for i in range(start_id, start_id + num_rows)],
            }
        )

    @pytest.mark.asyncio
    async def test_chunk_part_initialized_to_zero(
        self, temp_output_path: str, mock_object_store: AsyncMock
    ) -> None:
        """Verify chunk_part is initialized to 0 in constructor."""
        writer = JsonFileWriter(
            path=temp_output_path,
            typename="test_type",
        )

        assert writer.chunk_part == 0

    @pytest.mark.asyncio
    async def test_chunk_part_not_reset_when_chunk_start_provided(
        self, temp_output_path: str, mock_object_store: AsyncMock
    ) -> None:
        """When chunk_start is provided, chunk_part should not reset."""
        writer = JsonFileWriter(
            path=temp_output_path,
            typename="test_type",
            chunk_start=5,  # Providing chunk_start
            chunk_size=100,
            buffer_size=50,
        )

        df = self.create_test_dataframe(50)
        await writer.write(df)

        stats = await writer.close()
        assert stats.total_record_count == 50
        # chunk_part should be based on processing, not reset to 0
        # When chunk_start is provided, the reset doesn't happen
