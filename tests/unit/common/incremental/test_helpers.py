"""Tests for incremental extraction helper functions.

Tests cover public functions with real business logic:
- extract_epoch_id_from_qualified_name: Format parsing with validation
- get_persistent_s3_prefix: S3 path construction from workflow args
- normalize_marker_timestamp: Nanosecond stripping from timestamps
- prepone_marker_timestamp: Datetime arithmetic for clock skew handling
- count_json_files_recursive: Recursive file counting
- copy_directory_parallel: Parallel file copy operations
"""

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from application_sdk.common.incremental.helpers import (
    copy_directory_parallel,
    count_json_files_recursive,
    download_s3_prefix_with_structure,
    extract_epoch_id_from_qualified_name,
    get_persistent_s3_prefix,
    normalize_marker_timestamp,
    prepone_marker_timestamp,
)
from application_sdk.common.incremental.incremental_errors import (
    ConnectionQualifiedNameEmptyError,
    ConnectionQualifiedNameFormatError,
    ConnectionQualifiedNameMissingError,
)
from application_sdk.storage.ops import _put

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
        """Empty string raises ConnectionQualifiedNameEmptyError."""
        with pytest.raises(ConnectionQualifiedNameEmptyError):
            extract_epoch_id_from_qualified_name("")

    def test_too_few_segments_raises(self):
        """Fewer than 3 segments raises ConnectionQualifiedNameFormatError."""
        with pytest.raises(ConnectionQualifiedNameFormatError):
            extract_epoch_id_from_qualified_name("only/two")

    def test_single_segment_raises(self):
        """Single segment raises ConnectionQualifiedNameFormatError."""
        with pytest.raises(ConnectionQualifiedNameFormatError):
            extract_epoch_id_from_qualified_name("just-one")

    @pytest.mark.parametrize(
        "qualified_name",
        ["default/oracle/", "a/b/c/", "default/oracle//"],
    )
    def test_empty_last_segment_raises(self, qualified_name):
        """An empty last segment is rejected, not warned through.

        It passes the segment-count check but yields ".../connection/", a
        directory every such connection would share — so two connections would
        overwrite each other's marker and silently move each other's extraction
        window. Failing loudly beats that.
        """
        with pytest.raises(ConnectionQualifiedNameFormatError):
            extract_epoch_id_from_qualified_name(qualified_name)

    def test_named_last_segment_still_accepted(self):
        """A non-epoch *name* is still accepted — only an empty segment is not.

        This is the CONNECT-1136 contract: connections named after a workflow
        crawl fine, so the SDK must not start failing them. Guards the empty-
        segment rejection above against over-reaching into a strictness change.
        """
        assert extract_epoch_id_from_qualified_name("default/oracle/rppsfj") == "rppsfj"


# ---------------------------------------------------------------------------
# get_persistent_s3_prefix
# ---------------------------------------------------------------------------


class TestGetPersistentS3Prefix:
    """Tests for get_persistent_s3_prefix (S3 path construction)."""

    def test_constructs_correct_prefix(self):
        """Constructs S3 prefix from connection qualified name and app name."""
        result = get_persistent_s3_prefix("default/oracle/1764230875", "oracle")
        assert result == "persistent-artifacts/apps/oracle/connection/1764230875"

    def test_uses_env_app_name_as_fallback(self):
        """Falls back to ATLAN_APPLICATION_NAME env var when not in args."""
        with patch.dict("os.environ", {"ATLAN_APPLICATION_NAME": "clickhouse"}):
            result = get_persistent_s3_prefix("tenant/ch/999")
        assert "clickhouse" in result
        assert "999" in result

    def test_missing_qualified_name_raises(self):
        """Raises ConnectionQualifiedNameMissingError when connection_qualified_name is empty."""
        with pytest.raises(ConnectionQualifiedNameMissingError):
            get_persistent_s3_prefix("")


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


# ---------------------------------------------------------------------------
# download_s3_prefix_with_structure
# ---------------------------------------------------------------------------


class TestDownloadS3PrefixWithStructure:
    """Tests for the prefix-stripping download wrapper.

    Exercised against a real (in-memory) object store rather than a mocked
    download: the whole point of this helper is *where the bytes land*, which a
    mock asserting "was awaited" cannot see (FND-340).
    """

    async def test_strips_prefix_so_tree_lands_directly_in_destination(
        self, memory_store, tmp_path
    ) -> None:
        """``<prefix>/table/x.json`` → ``<dest>/table/x.json``, prefix not repeated."""
        prefix = "persistent-artifacts/apps/app/connection/1/current-state"
        await _put(
            f"{prefix}/table/chunk-0.json", b'{"a": 1}', memory_store, normalize=False
        )
        await _put(
            f"{prefix}/column/chunk-0.json", b'{"b": 2}', memory_store, normalize=False
        )

        dest = tmp_path / "current-state"
        await download_s3_prefix_with_structure(prefix, dest)

        assert (dest / "table" / "chunk-0.json").read_bytes() == b'{"a": 1}'
        assert (dest / "column" / "chunk-0.json").read_bytes() == b'{"b": 2}'
        # No second copy of the store prefix underneath the destination.
        assert not (dest / "persistent-artifacts").exists()

    async def test_empty_prefix_downloads_nothing(self, memory_store, tmp_path) -> None:
        """A prefix with no objects leaves the destination empty (no error)."""
        dest = tmp_path / "out"
        await download_s3_prefix_with_structure("nothing/here", dest)

        assert not dest.exists() or not any(dest.rglob("*"))

    async def test_forwards_concurrency_bound_to_download_prefix(
        self, tmp_path
    ) -> None:
        """The caller's concurrency bound reaches the underlying primitive.

        Bounding itself is ``download_prefix``'s contract (and its tests); what
        this wrapper owns is passing the value through along with
        ``strip_prefix=True``.
        """
        mock_download = AsyncMock()
        with patch(
            "application_sdk.common.incremental.helpers.download_prefix",
            mock_download,
        ):
            await download_s3_prefix_with_structure("p/", tmp_path, max_concurrency=7)

        mock_download.assert_awaited_once_with(
            prefix="p/",
            local_dir=tmp_path,
            strip_prefix=True,
            max_concurrency=7,
        )
