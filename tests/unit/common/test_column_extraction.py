"""Unit tests for generic column extraction utilities.

Tests cover SDK-generic functions:
- get_backfill_tables: DuckDB-based state comparison logic
- get_transformed_dir: Path validation logic
"""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from application_sdk.common.incremental.column_extraction import (
    get_backfill_tables,
    get_transformed_dir,
)


class TestGetBackfillTables:
    """Tests for get_backfill_tables (DuckDB-based state comparison)."""

    def test_no_previous_state_returns_none(self):
        """Test that missing previous state returns None (full run scenario)."""
        with tempfile.TemporaryDirectory() as temp_dir:
            current_dir = Path(temp_dir) / "current"
            current_dir.mkdir()

            result = get_backfill_tables(current_dir, None)

            assert result is None

    def test_nonexistent_previous_returns_none(self):
        """Test that nonexistent previous state path returns None."""
        with tempfile.TemporaryDirectory() as temp_dir:
            current_dir = Path(temp_dir) / "current"
            current_dir.mkdir()

            result = get_backfill_tables(current_dir, Path("/nonexistent"))

            assert result is None

    @patch(
        "application_sdk.common.incremental.column_extraction.backfill."
        "DuckDBConnectionManager"
    )
    def test_duckdb_error_propagates(self, mock_manager):
        """DuckDB / runtime failures must propagate, not be swallowed.

        The bare ``except Exception`` was previously hiding programming bugs,
        disk failures, and SQL errors as silent ``None`` returns. After
        BLDX-1190 the catch is narrowed to ``ValueError`` only, so callers
        can distinguish "no previous state" (legitimate skip) from
        "infrastructure error" (real failure).
        """
        mock_manager.return_value.__enter__ = lambda s: (_ for _ in ()).throw(
            Exception("DuckDB error")
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            current_dir = Path(temp_dir) / "current"
            previous_dir = Path(temp_dir) / "previous"
            current_dir.mkdir()
            previous_dir.mkdir()

            with pytest.raises(Exception, match="DuckDB error"):
                get_backfill_tables(current_dir, previous_dir)

    @patch(
        "application_sdk.common.incremental.column_extraction.backfill."
        "_load_tables_to_duckdb"
    )
    @patch(
        "application_sdk.common.incremental.column_extraction.backfill."
        "DuckDBConnectionManager"
    )
    def test_no_transformed_tables_returns_none(self, mock_manager, mock_load_tables):
        """The ``ValueError("No transformed tables found...")`` skip path
        is the only swallowed case after BLDX-1190; still returns None."""
        # Simulate the legitimate skip: current_tables comes back as 0/None.
        mock_load_tables.return_value = 0
        mock_manager.return_value.__enter__.return_value.connection = MagicMock()

        with tempfile.TemporaryDirectory() as temp_dir:
            current_dir = Path(temp_dir) / "current"
            previous_dir = Path(temp_dir) / "previous"
            current_dir.mkdir()
            previous_dir.mkdir()

            result = get_backfill_tables(current_dir, previous_dir)

            assert result is None


class TestGetTransformedDir:
    """Tests for get_transformed_dir (path validation)."""

    def test_raises_without_output_path(self):
        """Test raises FileNotFoundError when output_path missing."""
        with pytest.raises(FileNotFoundError, match="No output_path"):
            get_transformed_dir({})

    def test_raises_for_nonexistent_dir(self):
        """Test raises FileNotFoundError when transformed dir doesn't exist."""
        with tempfile.TemporaryDirectory() as temp_dir:
            with pytest.raises(
                FileNotFoundError, match="Transformed directory not found"
            ):
                get_transformed_dir({"output_path": temp_dir})

    def test_raises_for_empty_dir(self):
        """Test raises FileNotFoundError when transformed dir exists but empty."""
        with tempfile.TemporaryDirectory() as temp_dir:
            (Path(temp_dir) / "transformed").mkdir()

            with pytest.raises(FileNotFoundError, match="empty"):
                get_transformed_dir({"output_path": temp_dir})

    def test_returns_path_when_valid(self):
        """Test returns correct path when transformed dir has JSON files."""
        with tempfile.TemporaryDirectory() as temp_dir:
            transformed_dir = Path(temp_dir) / "transformed"
            transformed_dir.mkdir()
            (transformed_dir / "test.json").write_text("{}")

            result = get_transformed_dir({"output_path": temp_dir})

            assert result == transformed_dir
            assert result.exists()
