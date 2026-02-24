"""Unit tests for generic column extraction utilities.

Tests cover SDK-generic functions:
- get_backfill_tables: DuckDB-based state comparison logic
- get_transformed_dir: Path validation logic
"""

import tempfile
from pathlib import Path
from unittest.mock import patch

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
    def test_duckdb_error_returns_none(self, mock_manager):
        """Test graceful fallback when DuckDB fails returns None."""
        mock_manager.return_value.__enter__ = lambda s: (_ for _ in ()).throw(
            Exception("DuckDB error")
        )

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
