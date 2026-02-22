"""Tests for incremental extraction helper functions.

Tests cover public functions with real business logic:
- extract_epoch_id_from_qualified_name: Format parsing with validation
- get_persistent_s3_prefix: S3 path construction from workflow args
- normalize_marker_timestamp: Nanosecond stripping from timestamps
- prepone_marker_timestamp: Datetime arithmetic for clock skew handling
- is_incremental_run: Multi-condition prerequisite check
- count_json_files_recursive: Recursive file counting
- copy_directory_parallel: Parallel file copy operations
"""

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from application_sdk.common.incremental.helpers import (
    copy_directory_parallel,
    count_json_files_recursive,
    extract_epoch_id_from_qualified_name,
    get_persistent_s3_prefix,
    is_incremental_run,
    normalize_marker_timestamp,
    prepone_marker_timestamp,
)

# ---------------------------------------------------------------------------
# extract_epoch_id_from_qualified_name
# ---------------------------------------------------------------------------


class TestExtractEpochId:
    """Tests for extract_epoch_id_from_qualified_name (format parsing)."""

    @pytest.mark.parametrize(
        "qualified_name, expected",
        [
            ("default/oracle/1764230875", "1764230875"),
            ("tenant1/clickhouse/999", "999"),
            ("a/b/c", "c"),
        ],
    )
    def test_valid_qualified_names(self, qualified_name, expected):
        """Correctly extracts last segment from well-formed qualified names."""
        assert extract_epoch_id_from_qualified_name(qualified_name) == expected

    def test_extra_segments_uses_last(self):
        """With more than 3 segments, last segment is returned."""
        assert extract_epoch_id_from_qualified_name("a/b/c/12345") == "12345"

    def test_non_numeric_id_still_returned(self):
        """Non-numeric epoch IDs are returned with a warning (not rejected)."""
        result = extract_epoch_id_from_qualified_name("tenant/conn/abc-def")
        assert result == "abc-def"

    def test_empty_string_raises(self):
        """Empty string raises ValueError."""
        with pytest.raises(ValueError, match="cannot be empty"):
            extract_epoch_id_from_qualified_name("")

    def test_too_few_segments_raises(self):
        """Fewer than 3 segments raises ValueError."""
        with pytest.raises(ValueError, match="Expected format"):
            extract_epoch_id_from_qualified_name("only/two")

    def test_single_segment_raises(self):
        """Single segment raises ValueError."""
        with pytest.raises(ValueError, match="Expected format"):
            extract_epoch_id_from_qualified_name("just-one")


# ---------------------------------------------------------------------------
# get_persistent_s3_prefix
# ---------------------------------------------------------------------------


class TestGetPersistentS3Prefix:
    """Tests for get_persistent_s3_prefix (S3 path construction)."""

    def _make_workflow_args(self, qualified_name, app_name=None):
        args = {
            "connection": {"connection_qualified_name": qualified_name},
        }
        if app_name:
            args["application_name"] = app_name
        return args

    def test_constructs_correct_prefix(self):
        """Constructs S3 prefix from connection qualified name and app name."""
        args = self._make_workflow_args("default/oracle/1764230875", app_name="oracle")
        result = get_persistent_s3_prefix(args)
        assert result == "persistent-artifacts/apps/oracle/connection/1764230875"

    def test_uses_env_app_name_as_fallback(self):
        """Falls back to ATLAN_APPLICATION_NAME env var when not in args."""
        args = self._make_workflow_args("tenant/ch/999")
        with patch.dict("os.environ", {"ATLAN_APPLICATION_NAME": "clickhouse"}):
            result = get_persistent_s3_prefix(args)
        assert "clickhouse" in result
        assert "999" in result

    def test_missing_qualified_name_raises(self):
        """Raises ValueError when connection_qualified_name is empty."""
        args = self._make_workflow_args("")
        with pytest.raises(ValueError):
            get_persistent_s3_prefix(args)


# ---------------------------------------------------------------------------
# normalize_marker_timestamp
# ---------------------------------------------------------------------------


class TestNormalizeMarkerTimestamp:
    """Tests for normalize_marker_timestamp (nanosecond stripping)."""

    @pytest.mark.parametrize(
        "input_marker, expected",
        [
            ("2025-01-15T10:30:00.123456789Z", "2025-01-15T10:30:00Z"),
            ("2025-01-15T10:30:00.123Z", "2025-01-15T10:30:00Z"),
            ("2025-01-15T10:30:00.1Z", "2025-01-15T10:30:00Z"),
            ("2025-01-15T10:30:00Z", "2025-01-15T10:30:00Z"),
        ],
    )
    def test_strips_nanoseconds(self, input_marker, expected):
        """Strips fractional seconds of any precision before trailing Z."""
        assert normalize_marker_timestamp(input_marker) == expected

    def test_no_z_suffix_unchanged(self):
        """Timestamps without trailing Z are not modified."""
        marker = "2025-01-15T10:30:00.123456789"
        assert normalize_marker_timestamp(marker) == marker


# ---------------------------------------------------------------------------
# prepone_marker_timestamp
# ---------------------------------------------------------------------------


class TestPreponeMarkerTimestamp:
    """Tests for prepone_marker_timestamp (datetime arithmetic)."""

    def test_moves_back_by_hours(self):
        """Moves timestamp back by the specified number of hours."""
        result = prepone_marker_timestamp("2025-01-15T10:30:00Z", 3)
        assert result == "2025-01-15T07:30:00Z"

    def test_crosses_midnight(self):
        """Handles preponing past midnight into the previous day."""
        result = prepone_marker_timestamp("2025-01-15T02:00:00Z", 5)
        assert result == "2025-01-14T21:00:00Z"

    def test_zero_hours_no_change(self):
        """Zero hours returns the same timestamp."""
        result = prepone_marker_timestamp("2025-01-15T10:30:00Z", 0)
        assert result == "2025-01-15T10:30:00Z"

    def test_crosses_month_boundary(self):
        """Handles preponing across month boundaries."""
        result = prepone_marker_timestamp("2025-02-01T01:00:00Z", 3)
        assert result == "2025-01-31T22:00:00Z"

    def test_invalid_format_raises(self):
        """Invalid timestamp format raises ValueError."""
        with pytest.raises(ValueError):
            prepone_marker_timestamp("not-a-timestamp", 1)


# ---------------------------------------------------------------------------
# is_incremental_run
# ---------------------------------------------------------------------------


class TestIsIncrementalRun:
    """Tests for is_incremental_run (multi-condition prerequisite check)."""

    def test_all_conditions_met(self):
        """Returns True when all three conditions are met."""
        args = {
            "metadata": {
                "incremental-extraction": True,
                "marker_timestamp": "2025-01-15T10:30:00Z",
                "current_state_available": True,
            }
        }
        assert is_incremental_run(args) is True

    def test_incremental_extraction_disabled(self):
        """Returns False when incremental_extraction is False."""
        args = {
            "metadata": {
                "incremental-extraction": False,
                "marker_timestamp": "2025-01-15T10:30:00Z",
                "current_state_available": True,
            }
        }
        assert is_incremental_run(args) is False

    def test_no_marker_timestamp(self):
        """Returns False when marker_timestamp is absent (first run)."""
        args = {
            "metadata": {
                "incremental-extraction": True,
                "current_state_available": True,
            }
        }
        assert is_incremental_run(args) is False

    def test_current_state_not_available(self):
        """Returns False when current_state_available is False."""
        args = {
            "metadata": {
                "incremental-extraction": True,
                "marker_timestamp": "2025-01-15T10:30:00Z",
                "current_state_available": False,
            }
        }
        assert is_incremental_run(args) is False

    def test_empty_metadata(self):
        """Returns False when metadata section is empty."""
        assert is_incremental_run({"metadata": {}}) is False

    def test_missing_metadata(self):
        """Returns False when metadata section is missing entirely."""
        assert is_incremental_run({}) is False


# ---------------------------------------------------------------------------
# count_json_files_recursive
# ---------------------------------------------------------------------------


class TestCountJsonFilesRecursive:
    """Tests for count_json_files_recursive (recursive file counting)."""

    def test_counts_json_files(self):
        """Counts .json files in directory tree."""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "a.json").write_text("{}")
            sub = root / "subdir"
            sub.mkdir()
            (sub / "b.json").write_text("{}")
            (sub / "c.json").write_text("{}")

            assert count_json_files_recursive(root) == 3

    def test_ignores_non_json_files(self):
        """Non-json files are not counted."""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "a.json").write_text("{}")
            (root / "b.parquet").write_text("data")
            (root / "c.txt").write_text("text")

            assert count_json_files_recursive(root) == 1

    def test_empty_directory(self):
        """Empty directory returns 0."""
        with tempfile.TemporaryDirectory() as temp_dir:
            assert count_json_files_recursive(Path(temp_dir)) == 0

    def test_nonexistent_directory(self):
        """Nonexistent directory returns 0 (not an error)."""
        assert count_json_files_recursive(Path("/nonexistent/path")) == 0


# ---------------------------------------------------------------------------
# copy_directory_parallel
# ---------------------------------------------------------------------------


class TestCopyDirectoryParallel:
    """Tests for copy_directory_parallel (parallel file copy)."""

    def test_copies_matching_files(self):
        """Copies files matching the pattern to destination."""
        with tempfile.TemporaryDirectory() as temp_dir:
            src = Path(temp_dir) / "src"
            dest = Path(temp_dir) / "dest"
            src.mkdir()

            (src / "a.json").write_text('{"key": "a"}')
            (src / "b.json").write_text('{"key": "b"}')
            (src / "skip.txt").write_text("not json")

            count = copy_directory_parallel(src, dest, pattern="*.json")

            assert count == 2
            assert (dest / "a.json").exists()
            assert (dest / "b.json").exists()
            assert not (dest / "skip.txt").exists()

    def test_creates_destination_directory(self):
        """Destination directory is created if it doesn't exist."""
        with tempfile.TemporaryDirectory() as temp_dir:
            src = Path(temp_dir) / "src"
            dest = Path(temp_dir) / "deep" / "nested" / "dest"
            src.mkdir()
            (src / "file.json").write_text("{}")

            copy_directory_parallel(src, dest)

            assert dest.exists()
            assert (dest / "file.json").exists()

    def test_nonexistent_source_returns_zero(self):
        """Returns 0 when source directory doesn't exist."""
        count = copy_directory_parallel(Path("/nonexistent"), Path("/also-nonexistent"))
        assert count == 0

    def test_empty_source_returns_zero(self):
        """Returns 0 when source has no matching files."""
        with tempfile.TemporaryDirectory() as temp_dir:
            src = Path(temp_dir) / "empty_src"
            src.mkdir()

            count = copy_directory_parallel(src, Path(temp_dir) / "dest")
            assert count == 0
