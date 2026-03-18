"""Unit tests for application_sdk.contracts.types."""

from pathlib import Path

import pytest

from application_sdk.contracts.types import FileReference, MaxItems

# =============================================================================
# MaxItems
# =============================================================================


class TestMaxItems:
    def test_is_frozen_dataclass(self) -> None:
        mi = MaxItems(limit=100)
        assert mi.limit == 100
        with pytest.raises((AttributeError, TypeError)):
            mi.limit = 200  # type: ignore[misc]

    def test_equality(self) -> None:
        assert MaxItems(100) == MaxItems(100)
        assert MaxItems(100) != MaxItems(200)

    def test_hash(self) -> None:
        # Frozen dataclasses should be hashable
        s = {MaxItems(100), MaxItems(200), MaxItems(100)}
        assert len(s) == 2

    def test_zero_limit_allowed(self) -> None:
        mi = MaxItems(limit=0)
        assert mi.limit == 0

    def test_large_limit(self) -> None:
        mi = MaxItems(limit=1_000_000)
        assert mi.limit == 1_000_000


# =============================================================================
# FileReference
# =============================================================================


class TestFileReference:
    def test_default_field_values(self) -> None:
        ref = FileReference()
        assert ref.local_path is None
        assert ref.storage_path is None
        assert ref.is_durable is False
        assert ref.file_count == 1

    def test_is_frozen(self) -> None:
        ref = FileReference(local_path="/tmp/file.txt")
        with pytest.raises((AttributeError, TypeError)):
            ref.local_path = "/other/path"  # type: ignore[misc]

    def test_explicit_construction(self) -> None:
        ref = FileReference(
            local_path="/data/output.jsonl",
            storage_path="artifacts/output.jsonl",
            is_durable=True,
            file_count=5,
        )
        assert ref.local_path == "/data/output.jsonl"
        assert ref.storage_path == "artifacts/output.jsonl"
        assert ref.is_durable is True
        assert ref.file_count == 5

    def test_from_local_nonexistent_path(self) -> None:
        ref = FileReference.from_local("/nonexistent/file.json")
        assert ref.local_path == "/nonexistent/file.json"
        assert ref.is_durable is False

    def test_from_local_with_path_object(self, tmp_path: Path) -> None:
        f = tmp_path / "data.csv"
        f.write_text("col1,col2\n1,2\n")
        ref = FileReference.from_local(f)
        assert ref.local_path == str(f)

    def test_from_local_string_path(self, tmp_path: Path) -> None:
        f = tmp_path / "results.parquet"
        f.write_bytes(b"PAR1fake")
        ref = FileReference.from_local(str(f))
        assert ref.local_path == str(f)

    def test_equality(self) -> None:
        ref1 = FileReference(local_path="/tmp/a.json", file_count=1)
        ref2 = FileReference(local_path="/tmp/a.json", file_count=1)
        ref3 = FileReference(local_path="/tmp/b.json", file_count=1)
        assert ref1 == ref2
        assert ref1 != ref3

    def test_hashable(self) -> None:
        ref1 = FileReference(local_path="/tmp/a.json")
        ref2 = FileReference(local_path="/tmp/b.json")
        s = {ref1, ref2}
        assert len(s) == 2

    def test_file_count_default_is_one(self) -> None:
        ref = FileReference()
        assert ref.file_count == 1

    def test_file_count_directory_ref(self) -> None:
        ref = FileReference(
            local_path="/tmp/output",
            storage_path="artifacts/output/",
            is_durable=True,
            file_count=42,
        )
        assert ref.file_count == 42
