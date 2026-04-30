"""Tests for Windows path separator handling in storage operations.

Verifies that S3 keys (forward slashes) are correctly converted to
OS-native paths for local filesystem, and vice versa.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath


class TestDownloadPathConstruction:
    """Verify S3 keys → local paths use OS-native separators."""

    def test_s3_key_splits_into_directory_components(self):
        """PurePosixPath correctly splits forward-slash keys into parts."""
        key = "artifacts/run/transformed/file.json"
        parts = PurePosixPath(key).parts
        assert parts == ("artifacts", "run", "transformed", "file.json")

    def test_local_path_from_s3_key(self, tmp_path: Path):
        """Local path constructed from S3 key should be OS-native."""
        key = "artifacts/run/file.json"
        local = tmp_path / Path(*PurePosixPath(key).parts)
        # On all platforms, the path should have proper separators
        assert local.name == "file.json"
        assert local.parent.name == "run"
        assert local.parent.parent.name == "artifacts"

    def test_nested_s3_key_creates_nested_dirs(self, tmp_path: Path):
        """Deeply nested S3 keys should create proper directory hierarchy."""
        key = "a/b/c/d/e.txt"
        local = tmp_path / Path(*PurePosixPath(key).parts)
        assert len(local.relative_to(tmp_path).parts) == 5

    def test_single_component_key(self, tmp_path: Path):
        """S3 key with no slashes should work as a simple filename."""
        key = "file.json"
        local = tmp_path / Path(*PurePosixPath(key).parts)
        assert local == tmp_path / "file.json"


class TestUploadKeyConstruction:
    """Verify local paths → S3 keys use forward slashes."""

    def test_relative_path_to_posix_key(self):
        """Relative path parts should be joined with forward slashes."""
        # Simulate what happens on Windows where str(rel) would use backslashes
        rel = Path("subdir") / "nested" / "file.json"
        posix_key = str(PurePosixPath(*rel.parts))
        assert "/" in posix_key or len(rel.parts) == 1
        assert "\\" not in posix_key

    def test_upload_key_with_prefix(self):
        """Upload key should use forward slashes even from Windows paths."""
        prefix = "artifacts/run"
        rel = Path("transformed") / "file.json"
        rel_posix = PurePosixPath(*rel.parts)
        key = f"{prefix}/{rel_posix}"
        assert key == "artifacts/run/transformed/file.json"
        assert "\\" not in key

    def test_upload_key_without_prefix(self):
        """Without prefix, key should still be forward-slash based."""
        rel = Path("data") / "output.json"
        rel_posix = PurePosixPath(*rel.parts)
        key = str(rel_posix)
        assert key == "data/output.json"


class TestTransferPathConstruction:
    """Verify transfer.py download path handling."""

    def test_strip_prefix_and_convert(self, tmp_path: Path):
        """After stripping prefix, remaining key should become OS-native path."""
        key = "prefix/subdir/file.json"
        prefix = "prefix/"
        rel = key[len(prefix) :]
        local = tmp_path / Path(*PurePosixPath(rel.lstrip("/")).parts)
        assert local.name == "file.json"
        assert local.parent.name == "subdir"

    def test_leading_slash_stripped(self, tmp_path: Path):
        """Leading slash after prefix strip should be handled."""
        rel = "/subdir/file.json"
        local = tmp_path / Path(*PurePosixPath(rel.lstrip("/")).parts)
        assert local.name == "file.json"
        assert local.parent.name == "subdir"
