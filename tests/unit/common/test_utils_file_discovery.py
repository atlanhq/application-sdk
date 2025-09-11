"""Tests for file discovery utilities in common/utils.py."""

import tempfile
from pathlib import Path

from application_sdk.activities.common.utils import find_local_files_by_extension


class TestFindFilesByExtension:
    """Test suite for find_local_files_by_extension utility function."""

    def test_single_file_exists(self):
        """Test finding a single file that exists."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_file_path = Path(tmp_dir) / "test.parquet"
            tmp_file_path.touch()

            result = find_local_files_by_extension(str(tmp_file_path), ".parquet")
            assert result == [str(tmp_file_path)]

    def test_single_file_wrong_extension(self):
        """Test single file with wrong extension returns empty."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_file_path = Path(tmp_dir) / "test.json"
            tmp_file_path.touch()

            result = find_local_files_by_extension(str(tmp_file_path), ".parquet")
            assert result == []

    def test_directory_with_matching_files(self):
        """Test directory containing files with matching extension."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            # Create test files
            file1 = Path(tmp_dir) / "test1.parquet"
            file2 = Path(tmp_dir) / "test2.parquet"
            file3 = Path(tmp_dir) / "test3.json"  # Different extension

            file1.touch()
            file2.touch()
            file3.touch()

            result = find_local_files_by_extension(tmp_dir, ".parquet")

            # Should find both parquet files
            assert len(result) == 2
            assert str(file1) in result
            assert str(file2) in result
            assert str(file3) not in result

    def test_directory_with_file_names_filter(self):
        """Test directory with file_names filter."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            # Create test files
            file1 = Path(tmp_dir) / "wanted.parquet"
            file2 = Path(tmp_dir) / "unwanted.parquet"

            file1.touch()
            file2.touch()

            result = find_local_files_by_extension(
                tmp_dir, ".parquet", file_names=["wanted.parquet"]
            )

            # Should only find the wanted file
            assert len(result) == 1
            assert str(file1) in result
            assert str(file2) not in result

    def test_directory_with_basename_matching(self):
        """Test that file_names filter works with basename matching."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            # Create nested directory structure
            subdir = Path(tmp_dir) / "subdir"
            subdir.mkdir()

            file1 = subdir / "target.parquet"
            file2 = Path(tmp_dir) / "other.parquet"

            file1.touch()
            file2.touch()

            result = find_local_files_by_extension(
                tmp_dir, ".parquet", file_names=["target.parquet"]
            )

            # Should find the file by basename even in subdirectory
            assert len(result) == 1
            assert str(file1) in result

    def test_nonexistent_path(self):
        """Test nonexistent path returns empty list."""
        result = find_local_files_by_extension("/nonexistent/path", ".parquet")
        assert result == []

    def test_empty_directory(self):
        """Test empty directory returns empty list."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            result = find_local_files_by_extension(tmp_dir, ".parquet")
            assert result == []

    def test_recursive_search(self):
        """Test that search is recursive in subdirectories."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            # Create nested structure
            subdir1 = Path(tmp_dir) / "sub1"
            subdir2 = subdir1 / "sub2"
            subdir1.mkdir()
            subdir2.mkdir()

            # Create files at different levels
            file1 = Path(tmp_dir) / "root.parquet"
            file2 = subdir1 / "level1.parquet"
            file3 = subdir2 / "level2.parquet"

            file1.touch()
            file2.touch()
            file3.touch()

            result = find_local_files_by_extension(tmp_dir, ".parquet")

            # Should find all files recursively
            assert len(result) == 3
            assert str(file1) in result
            assert str(file2) in result
            assert str(file3) in result

    def test_file_names_filter_multiple_matches(self):
        """Test file_names filter with multiple target files."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            # Create test files
            file1 = Path(tmp_dir) / "file1.parquet"
            file2 = Path(tmp_dir) / "file2.parquet"
            file3 = Path(tmp_dir) / "file3.parquet"

            file1.touch()
            file2.touch()
            file3.touch()

            result = find_local_files_by_extension(
                tmp_dir, ".parquet", file_names=["file1.parquet", "file3.parquet"]
            )

            # Should find both specified files
            assert len(result) == 2
            assert str(file1) in result
            assert str(file3) in result
            assert str(file2) not in result
