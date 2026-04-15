"""Tests for current state writer utilities.

Tests cover public functions with real business logic:
- copy_non_column_entities: Entity iteration and parallel copy
- _copy_columns_from_transformed: Column file copy (no ancestral merge)
- cleanup_previous_state: Safe cleanup with exception handling
- prepare_current_state_directory: Idempotent directory cleanup
- prepare_previous_state: Conditional download with cleanup on failure
- download_transformed_data: Output path validation
"""

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from application_sdk.common.incremental.state.state_writer import (
    _copy_columns_from_transformed,
    cleanup_previous_state,
    copy_non_column_entities,
    download_transformed_data,
    prepare_current_state_directory,
    prepare_previous_state,
)

# ---------------------------------------------------------------------------
# copy_non_column_entities
# ---------------------------------------------------------------------------


class TestCopyNonColumnEntities:
    """Tests for copy_non_column_entities (entity iteration and parallel copy)."""

    def test_copies_table_schema_database(self):
        """Copies table, schema, and database entity files."""
        with tempfile.TemporaryDirectory() as temp_dir:
            transformed = Path(temp_dir) / "transformed"
            state = Path(temp_dir) / "current-state"

            # Create entity directories with files
            for entity in ["table", "schema", "database"]:
                entity_dir = transformed / entity
                entity_dir.mkdir(parents=True)
                (entity_dir / "chunk-0.json").write_text("{}")

            counts = copy_non_column_entities(transformed, state)

            assert counts["table"] == 1
            assert counts["schema"] == 1
            assert counts["database"] == 1
            assert (state / "table" / "chunk-0.json").exists()
            assert (state / "schema" / "chunk-0.json").exists()
            assert (state / "database" / "chunk-0.json").exists()

    def test_skips_missing_entity_dirs(self):
        """Skips entity types that don't have directories."""
        with tempfile.TemporaryDirectory() as temp_dir:
            transformed = Path(temp_dir) / "transformed"
            state = Path(temp_dir) / "current-state"

            # Only create table directory (no schema/database)
            table_dir = transformed / "table"
            table_dir.mkdir(parents=True)
            (table_dir / "chunk-0.json").write_text("{}")

            counts = copy_non_column_entities(transformed, state)

            assert counts.get("table") == 1
            assert "schema" not in counts
            assert "database" not in counts

    def test_does_not_copy_columns(self):
        """Column directory is NOT copied (handled separately by _copy_columns_from_transformed)."""
        with tempfile.TemporaryDirectory() as temp_dir:
            transformed = Path(temp_dir) / "transformed"
            state = Path(temp_dir) / "current-state"

            # Create column directory
            col_dir = transformed / "column"
            col_dir.mkdir(parents=True)
            (col_dir / "chunk-0.json").write_text("{}")

            counts = copy_non_column_entities(transformed, state)

            assert "column" not in counts
            assert not (state / "column").exists()


# ---------------------------------------------------------------------------
# _copy_columns_from_transformed
# ---------------------------------------------------------------------------


class TestCopyColumnsFromTransformed:
    """Tests for _copy_columns_from_transformed (lightweight column copy)."""

    def test_copies_column_files(self):
        """Copies column files from transformed to current-state."""
        with tempfile.TemporaryDirectory() as temp_dir:
            transformed = Path(temp_dir) / "transformed"
            state = Path(temp_dir) / "current-state"
            state.mkdir(parents=True)

            col_dir = transformed / "column"
            col_dir.mkdir(parents=True)
            (col_dir / "chunk-0.json").write_text("{}")
            (col_dir / "chunk-1.json").write_text("{}")

            count = _copy_columns_from_transformed(transformed, state)

            assert count == 2
            assert (state / "column" / "chunk-0.json").exists()
            assert (state / "column" / "chunk-1.json").exists()

    def test_no_column_dir_returns_zero(self):
        """Returns 0 when no column directory exists in transformed."""
        with tempfile.TemporaryDirectory() as temp_dir:
            transformed = Path(temp_dir) / "transformed"
            transformed.mkdir(parents=True)
            state = Path(temp_dir) / "current-state"
            state.mkdir(parents=True)

            count = _copy_columns_from_transformed(transformed, state)
            assert count == 0

    def test_empty_column_dir_returns_zero(self):
        """Returns 0 when column directory exists but is empty."""
        with tempfile.TemporaryDirectory() as temp_dir:
            transformed = Path(temp_dir) / "transformed"
            state = Path(temp_dir) / "current-state"
            state.mkdir(parents=True)

            col_dir = transformed / "column"
            col_dir.mkdir(parents=True)

            count = _copy_columns_from_transformed(transformed, state)
            assert count == 0


# ---------------------------------------------------------------------------
# cleanup_previous_state
# ---------------------------------------------------------------------------


class TestCleanupPreviousState:
    """Tests for cleanup_previous_state (safe cleanup)."""

    def test_removes_existing_directory(self):
        """Removes directory when it exists."""
        with tempfile.TemporaryDirectory() as temp_dir:
            prev_dir = Path(temp_dir) / "previous"
            prev_dir.mkdir()
            (prev_dir / "data.json").write_text("{}")

            cleanup_previous_state(prev_dir)

            assert not prev_dir.exists()

    def test_none_is_noop(self):
        """Passing None does nothing (no exception)."""
        cleanup_previous_state(None)

    def test_nonexistent_dir_is_noop(self):
        """Passing a nonexistent path does nothing."""
        cleanup_previous_state(Path("/nonexistent/dir"))

    def test_rmtree_failure_does_not_raise(self):
        """Cleanup failure is logged but does NOT raise (non-critical)."""
        with tempfile.TemporaryDirectory() as temp_dir:
            prev_dir = Path(temp_dir) / "previous"
            prev_dir.mkdir()

            with patch("shutil.rmtree", side_effect=PermissionError("denied")):
                # Should not raise
                cleanup_previous_state(prev_dir)


# ---------------------------------------------------------------------------
# prepare_current_state_directory
# ---------------------------------------------------------------------------


class TestPrepareCurrentStateDirectory:
    """Tests for prepare_current_state_directory (idempotent cleanup)."""

    def test_creates_fresh_directory(self):
        """Creates directory when it doesn't exist."""
        with tempfile.TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir) / "current-state"

            prepare_current_state_directory(state_dir)

            assert state_dir.exists()

    def test_clears_existing_directory(self):
        """Removes existing content and recreates an empty directory."""
        with tempfile.TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir) / "current-state"
            state_dir.mkdir()
            (state_dir / "old-data.json").write_text("{}")

            prepare_current_state_directory(state_dir)

            assert state_dir.exists()
            assert list(state_dir.iterdir()) == []


# ---------------------------------------------------------------------------
# prepare_previous_state
# ---------------------------------------------------------------------------


class TestPreparePreviousState:
    """Tests for prepare_previous_state (conditional download)."""

    def _make_workflow_args(self, qualified_name="t/c/123"):
        return {
            "connection": {"connection_qualified_name": qualified_name},
            "application_name": "oracle",
        }

    @pytest.mark.asyncio
    async def test_returns_none_when_not_available(self):
        """Returns None when current_state_available is False."""
        with tempfile.TemporaryDirectory() as temp_dir:
            result = await prepare_previous_state(
                self._make_workflow_args(),
                current_state_available=False,
                current_state_dir=Path(temp_dir) / "state",
            )

        assert result is None

    @pytest.mark.asyncio
    async def test_downloads_previous_state(self):
        """Downloads previous state when available."""
        with tempfile.TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir) / "current-state"
            state_dir.mkdir()

            with patch(
                "application_sdk.common.incremental.state.state_writer."
                "download_s3_prefix_with_structure",
                new_callable=AsyncMock,
            ):
                result = await prepare_previous_state(
                    self._make_workflow_args(),
                    current_state_available=True,
                    current_state_dir=state_dir,
                )

            assert result is not None
            assert result.exists()
            assert "previous" in result.name

    @pytest.mark.asyncio
    async def test_cleans_up_on_download_failure(self):
        """Cleans up temp directory if S3 download fails."""
        with tempfile.TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir) / "current-state"
            state_dir.mkdir()

            with patch(
                "application_sdk.common.incremental.state.state_writer."
                "download_s3_prefix_with_structure",
                new_callable=AsyncMock,
                side_effect=Exception("S3 failure"),
            ):
                with pytest.raises(Exception, match="S3 failure"):
                    await prepare_previous_state(
                        self._make_workflow_args(),
                        current_state_available=True,
                        current_state_dir=state_dir,
                    )

            # Temp dir should be cleaned up after failure
            expected_temp = state_dir.parent / f"{state_dir.name}.previous"
            assert not expected_temp.exists()


# ---------------------------------------------------------------------------
# download_transformed_data
# ---------------------------------------------------------------------------


class TestDownloadTransformedData:
    """Tests for download_transformed_data (path validation)."""

    @pytest.mark.asyncio
    async def test_empty_output_path_raises(self):
        """Empty output_path raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError, match="No output_path"):
            await download_transformed_data("")

    @pytest.mark.asyncio
    async def test_whitespace_output_path_raises(self):
        """Whitespace-only output_path raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError, match="No output_path"):
            await download_transformed_data("   ")

    @pytest.mark.asyncio
    async def test_downloads_from_s3(self):
        """Downloads transformed data from S3 to local path."""
        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch(
                    "application_sdk.common.incremental.state.state_writer.ObjectStore"
                ) as mock_store,
                patch(
                    "application_sdk.common.incremental.state.state_writer.get_object_store_prefix",
                    return_value="prefix/transformed",
                ),
            ):
                mock_store.download_prefix = AsyncMock()

                result = await download_transformed_data(temp_dir)

            assert result == Path(temp_dir) / "transformed"
            mock_store.download_prefix.assert_awaited_once()
