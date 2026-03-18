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
        assert ref.size_bytes is None
        assert ref.checksum is None
        assert ref.content_type == "application/octet-stream"
        assert ref.is_durable is False
        assert ref.storage_key is None

    def test_is_frozen(self) -> None:
        ref = FileReference(local_path="/tmp/file.txt")
        with pytest.raises((AttributeError, TypeError)):
            ref.local_path = "/other/path"  # type: ignore[misc]

    def test_explicit_construction(self) -> None:
        ref = FileReference(
            local_path="/data/output.jsonl",
            size_bytes=1024,
            checksum="sha256:abc123",
            content_type="application/x-ndjson",
            is_durable=True,
            storage_key="artifacts/output.jsonl",
        )
        assert ref.local_path == "/data/output.jsonl"
        assert ref.size_bytes == 1024
        assert ref.checksum == "sha256:abc123"
        assert ref.content_type == "application/x-ndjson"
        assert ref.is_durable is True
        assert ref.storage_key == "artifacts/output.jsonl"

    def test_from_local_nonexistent_path(self) -> None:
        ref = FileReference.from_local("/nonexistent/file.json")
        assert ref.local_path == "/nonexistent/file.json"
        assert ref.size_bytes == 0
        assert ref.content_type == "application/json"
        assert ref.is_durable is False

    def test_from_local_with_path_object(self, tmp_path: Path) -> None:
        f = tmp_path / "data.csv"
        f.write_text("col1,col2\n1,2\n")
        ref = FileReference.from_local(f)
        assert ref.local_path == str(f)
        assert ref.size_bytes == f.stat().st_size
        assert ref.content_type == "text/csv"

    def test_from_local_content_type_override(self) -> None:
        ref = FileReference.from_local(
            "/tmp/data.jsonl", content_type="application/json"
        )
        assert ref.content_type == "application/json"

    def test_from_local_string_path(self, tmp_path: Path) -> None:
        f = tmp_path / "results.parquet"
        f.write_bytes(b"PAR1fake")
        ref = FileReference.from_local(str(f))
        assert ref.local_path == str(f)
        assert ref.content_type == "application/x-parquet"

    @pytest.mark.parametrize(
        "ext,expected_mime",
        [
            (".jsonl", "application/x-ndjson"),
            (".ndjson", "application/x-ndjson"),
            (".json", "application/json"),
            (".csv", "text/csv"),
            (".tsv", "text/tab-separated-values"),
            (".parquet", "application/x-parquet"),
            (".txt", "application/octet-stream"),
            (".bin", "application/octet-stream"),
        ],
    )
    def test_from_local_mime_detection(self, ext: str, expected_mime: str) -> None:
        ref = FileReference.from_local(f"/tmp/file{ext}")
        assert ref.content_type == expected_mime

    def test_equality(self) -> None:
        ref1 = FileReference(local_path="/tmp/a.json", size_bytes=100)
        ref2 = FileReference(local_path="/tmp/a.json", size_bytes=100)
        ref3 = FileReference(local_path="/tmp/b.json", size_bytes=100)
        assert ref1 == ref2
        assert ref1 != ref3

    def test_hashable(self) -> None:
        ref1 = FileReference(local_path="/tmp/a.json")
        ref2 = FileReference(local_path="/tmp/b.json")
        s = {ref1, ref2}
        assert len(s) == 2
