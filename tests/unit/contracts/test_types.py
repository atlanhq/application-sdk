"""Unit tests for application_sdk.contracts.types."""

from pathlib import Path

import pytest
from pydantic import ValidationError

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
        with pytest.raises((ValidationError, AttributeError, TypeError)):
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
        assert ref.local_path == str(Path("/nonexistent/file.json"))
        assert ref.is_durable is False

    def test_from_local_with_path_object(self, tmp_path: Path) -> None:
        f = tmp_path / "data.csv"
        f.write_text("col1,col2\n1,2\n")
        ref = FileReference.from_local(f)
        assert ref.local_path == str(f)
        assert ref.file_count == 1
        assert ref.is_durable is False

    def test_from_local_string_path(self, tmp_path: Path) -> None:
        f = tmp_path / "results.parquet"
        f.write_bytes(b"PAR1fake")
        ref = FileReference.from_local(str(f))
        assert ref.local_path == str(f)
        assert ref.file_count == 1
        assert ref.is_durable is False

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

    # ---- auto_materialize escape hatch (BLDX-1155) ---------------------

    def test_auto_materialize_defaults_to_true(self) -> None:
        ref = FileReference()
        assert ref.auto_materialize is True

    def test_auto_materialize_can_be_disabled(self) -> None:
        ref = FileReference(local_path="/tmp/x", auto_materialize=False)
        assert ref.auto_materialize is False

    def test_auto_materialize_round_trips_through_model_dump(self) -> None:
        ref = FileReference(local_path="/tmp/x", auto_materialize=False)
        dumped = ref.model_dump()
        restored = FileReference.model_validate(dumped)
        assert restored.auto_materialize is False

    # ---- from_local() directory file_count fix (BLDX-1155) ------------

    def test_from_local_single_file_counts_one(self, tmp_path: Path) -> None:
        f = tmp_path / "single.txt"
        f.write_text("hi")
        ref = FileReference.from_local(f)
        assert ref.file_count == 1

    def test_from_local_directory_counts_all_files(self, tmp_path: Path) -> None:
        d = tmp_path / "tree"
        d.mkdir()
        (d / "a.txt").write_text("a")
        (d / "b.txt").write_text("b")
        sub = d / "sub"
        sub.mkdir()
        (sub / "c.txt").write_text("c")
        ref = FileReference.from_local(d)
        assert ref.file_count == 3

    def test_from_local_empty_directory_counts_zero(self, tmp_path: Path) -> None:
        d = tmp_path / "empty"
        d.mkdir()
        ref = FileReference.from_local(d)
        assert ref.file_count == 0

    def test_from_local_nonexistent_keeps_default_count(self) -> None:
        # Does not stat the path eagerly for non-existent inputs.
        ref = FileReference.from_local("/does/not/exist")
        assert ref.file_count == 1

    def test_from_local_oserror_falls_back_to_one(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """OSError from safe_list_directory uses file_count=1 fallback."""
        import application_sdk.contracts.types as types_module

        monkeypatch.setattr(
            types_module,
            "safe_list_directory",
            lambda _: (_ for _ in ()).throw(OSError("sandbox")),
        )
        ref = FileReference.from_local(tmp_path)
        assert ref.file_count == 1

    # ---- rglob listing race ---------------------------------------------

    def test_from_local_finds_files_when_rglob_returns_empty(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Mock ``Path.rglob`` to return empty (cpython#146646 silent-
        swallow); ``file_count`` must still reflect the real tree."""
        d = tmp_path / "tree"
        d.mkdir()
        (d / "a.txt").write_text("a")
        (d / "b.txt").write_text("b")
        sub = d / "sub"
        sub.mkdir()
        (sub / "c.txt").write_text("c")

        # Inject the listing transient.
        # Regression guard: a future revert to Path.rglob would re-trigger this mock.
        monkeypatch.setattr(Path, "rglob", lambda self, pat: iter([]))

        ref = FileReference.from_local(d)

        # On main: file_count == 0 (the bug). After fix: 3.
        assert ref.file_count == 3


# =============================================================================
# UploadInput / DownloadInput — ref field symmetry
# =============================================================================


class TestUploadDownloadRefSymmetry:
    """UploadInput.ref must be symmetric with DownloadInput.ref (both optional)."""

    def test_upload_input_ref_defaults_to_none(self) -> None:
        from application_sdk.contracts.storage import UploadInput

        assert UploadInput().ref is None

    def test_upload_input_ref_accepts_file_reference(self) -> None:
        from application_sdk.contracts.storage import UploadInput

        ref = FileReference(local_path="/tmp/x.jsonl", storage_path="artifacts/x.jsonl")
        assert UploadInput(ref=ref).ref == ref

    def test_download_input_ref_defaults_to_none(self) -> None:
        from application_sdk.contracts.storage import DownloadInput

        assert DownloadInput().ref is None

    def test_upload_and_download_ref_fields_are_symmetric(self) -> None:
        from application_sdk.contracts.storage import DownloadInput, UploadInput

        ref = FileReference(local_path="/tmp/x.jsonl", storage_path="artifacts/x.jsonl")
        assert UploadInput(ref=ref).ref == DownloadInput(ref=ref).ref
