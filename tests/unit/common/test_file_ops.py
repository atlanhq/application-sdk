"""Unit tests for SafeFileOps class.

Note: Empty path validation is tested in test_path.py via convert_to_extended_path.
These tests focus on SafeFileOps-specific behavior beyond stdlib wrappers.
"""

from pathlib import Path

import pytest

from application_sdk.common.file_ops import SafeFileOps


class TestSafeFileOpsUnlink:
    """Test suite for SafeFileOps.unlink - tests missing_ok parameter behavior."""

    def test_unlink_deletes_file(self, tmp_path: Path) -> None:
        """Test that unlink correctly deletes a file."""
        file_path = tmp_path / "test.txt"
        file_path.write_text("content")

        SafeFileOps.unlink(file_path)

        assert not file_path.exists()

    def test_unlink_with_missing_ok_true_ignores_missing_file(
        self, tmp_path: Path
    ) -> None:
        """Test that unlink with missing_ok=True doesn't raise for missing file."""
        SafeFileOps.unlink(tmp_path / "nonexistent.txt", missing_ok=True)

    def test_unlink_with_missing_ok_false_raises_for_missing_file(
        self, tmp_path: Path
    ) -> None:
        """Test that unlink with missing_ok=False raises for missing file."""
        with pytest.raises(FileNotFoundError):
            SafeFileOps.unlink(tmp_path / "nonexistent.txt", missing_ok=False)


class TestSafeFileOpsMakedirs:
    """Test suite for SafeFileOps.makedirs - tests exist_ok parameter behavior."""

    def test_makedirs_creates_nested_directories(self, tmp_path: Path) -> None:
        """Test that makedirs creates nested directory structure."""
        nested_path = tmp_path / "a" / "b" / "c"

        SafeFileOps.makedirs(nested_path)

        assert nested_path.exists()
        assert nested_path.is_dir()

    def test_makedirs_with_exist_ok_true_succeeds_for_existing_dir(
        self, tmp_path: Path
    ) -> None:
        """Test that makedirs with exist_ok=True doesn't raise for existing dir."""
        existing_dir = tmp_path / "existing"
        existing_dir.mkdir()

        SafeFileOps.makedirs(existing_dir, exist_ok=True)

    def test_makedirs_with_exist_ok_false_raises_for_existing_dir(
        self, tmp_path: Path
    ) -> None:
        """Test that makedirs with exist_ok=False raises for existing dir."""
        existing_dir = tmp_path / "existing"
        existing_dir.mkdir()

        with pytest.raises(FileExistsError):
            SafeFileOps.makedirs(existing_dir, exist_ok=False)


class TestSafeFileOpsRmtree:
    """Test suite for SafeFileOps.rmtree - tests ignore_errors parameter behavior."""

    def test_rmtree_removes_directory_tree(self, tmp_path: Path) -> None:
        """Test that rmtree removes entire directory tree."""
        dir_path = tmp_path / "tree"
        dir_path.mkdir()
        (dir_path / "subdir").mkdir()
        (dir_path / "file.txt").write_text("content")
        (dir_path / "subdir" / "nested.txt").write_text("nested")

        SafeFileOps.rmtree(dir_path)

        assert not dir_path.exists()

    def test_rmtree_with_ignore_errors_true_ignores_missing_dir(
        self, tmp_path: Path
    ) -> None:
        """Test that rmtree with ignore_errors=True doesn't raise for missing dir."""
        SafeFileOps.rmtree(tmp_path / "nonexistent", ignore_errors=True)


class TestSafeFileOpsOpen:
    """Test suite for SafeFileOps.open - tests context manager and encoding."""

    def test_open_as_context_manager(self, tmp_path: Path) -> None:
        """Test that open works as a context manager for read/write."""
        file_path = tmp_path / "test.txt"

        with SafeFileOps.open(file_path, "w") as f:
            f.write("hello world")

        with SafeFileOps.open(file_path, "r") as f:
            content = f.read()

        assert content == "hello world"

    def test_open_with_encoding(self, tmp_path: Path) -> None:
        """Test that open respects encoding parameter."""
        file_path = tmp_path / "test.txt"
        content = "hello 世界"

        with SafeFileOps.open(file_path, "w", encoding="utf-8") as f:
            f.write(content)

        with SafeFileOps.open(file_path, "r", encoding="utf-8") as f:
            read_content = f.read()

        assert read_content == content


class TestSafeFileOpsCopy:
    """Test suite for SafeFileOps.copy."""

    def test_copy_creates_file_copy(self, tmp_path: Path) -> None:
        """Test that copy creates a copy preserving original."""
        src = tmp_path / "source.txt"
        dst = tmp_path / "destination.txt"
        src.write_text("test content")

        SafeFileOps.copy(src, dst)

        assert dst.read_text() == "test content"
        assert src.exists()

    def test_copy_to_directory(self, tmp_path: Path) -> None:
        """Test that copy works when destination is a directory."""
        src = tmp_path / "source.txt"
        dst_dir = tmp_path / "dest_dir"
        dst_dir.mkdir()
        src.write_text("test content")

        SafeFileOps.copy(src, dst_dir)

        assert (dst_dir / "source.txt").read_text() == "test content"


class TestSafeFileOpsMove:
    """Test suite for SafeFileOps.move."""

    def test_move_moves_file(self, tmp_path: Path) -> None:
        """Test that move correctly moves a file."""
        src = tmp_path / "source.txt"
        dst = tmp_path / "destination.txt"
        src.write_text("test content")

        SafeFileOps.move(src, dst)

        assert not src.exists()
        assert dst.read_text() == "test content"

    def test_move_moves_directory(self, tmp_path: Path) -> None:
        """Test that move correctly moves a directory."""
        src_dir = tmp_path / "src_dir"
        dst_dir = tmp_path / "dst_dir"
        src_dir.mkdir()
        (src_dir / "file.txt").write_text("content")

        SafeFileOps.move(src_dir, dst_dir)

        assert not src_dir.exists()
        assert (dst_dir / "file.txt").read_text() == "content"
