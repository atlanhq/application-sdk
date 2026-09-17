"""Unit tests for application_sdk.contracts.types."""

from pathlib import Path
from typing import Annotated

import pytest
from pydantic import BaseModel, ValidationError

from application_sdk.contracts.types import (
    AssetArtifact,
    FileReference,
    MaxItems,
    StorageTier,
    asset_artifact_fields,
    asset_artifact_marker,
)

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


# =============================================================================
# StorageTier
# =============================================================================


class TestStorageTier:
    def test_string_values_are_lowercase(self) -> None:
        assert StorageTier.TRANSIENT.value == "transient"
        assert StorageTier.RETAINED.value == "retained"
        assert StorageTier.PERSISTENT.value == "persistent"

    def test_reconstruct_from_value(self) -> None:
        assert StorageTier("transient") is StorageTier.TRANSIENT
        assert StorageTier("retained") is StorageTier.RETAINED
        assert StorageTier("persistent") is StorageTier.PERSISTENT


# =============================================================================
# AssetArtifact — the one reader both enforcement points share (FND-1863)
# =============================================================================


class _Marked(BaseModel):
    transformed: Annotated[FileReference | None, AssetArtifact()] = None
    bounded_and_marked: Annotated[
        list[FileReference], MaxItems(10), AssetArtifact()
    ] = []
    plain: FileReference | None = None
    count: int = 0


class _Inherited(_Marked):
    extra: int = 0


class _NotAModel:
    transformed = None


class TestAssetArtifactMarker:
    """The marker, and the single reader the guard and the interceptor share.

    Two callers act on this answer and they have to act on the *same* one: a
    field the guard exempts from hand-declaration but the interceptor does not
    model-validate would be a boundary nothing checks at all.  These tests are
    on the reader for that reason — there is deliberately only one.
    """

    def test_reads_the_marker_off_the_annotation(self) -> None:
        assert isinstance(asset_artifact_marker(_Marked, "transformed"), AssetArtifact)

    def test_coexists_with_other_annotated_metadata(self) -> None:
        """``MaxItems`` and the marker sit side by side, as they do on the SDK's own field."""
        assert asset_artifact_marker(_Marked, "bounded_and_marked") is not None

    def test_an_unmarked_field_reads_as_none(self) -> None:
        assert asset_artifact_marker(_Marked, "plain") is None
        assert asset_artifact_marker(_Marked, "count") is None

    def test_an_unknown_field_reads_as_none(self) -> None:
        assert asset_artifact_marker(_Marked, "nope") is None

    def test_no_owner_reads_as_none(self) -> None:
        """A bare reference has no annotation, so it can carry no marker."""
        assert asset_artifact_marker(None, "transformed") is None

    def test_a_non_pydantic_class_reads_as_none(self) -> None:
        assert asset_artifact_marker(_NotAModel, "transformed") is None

    def test_a_subclass_inherits_the_marker(self) -> None:
        """``model_fields`` resolves the MRO — this is what exempts the fleet.

        Every SQL connector's output contract subclasses the SDK's, and none of
        them marks anything of its own.
        """
        assert asset_artifact_marker(_Inherited, "transformed") is not None

    def test_the_field_set_is_every_marked_field(self) -> None:
        assert asset_artifact_fields(_Marked) == frozenset(
            {"transformed", "bounded_and_marked"}
        )
        assert asset_artifact_fields(_Inherited) == frozenset(
            {"transformed", "bounded_and_marked"}
        )
        assert asset_artifact_fields(_NotAModel) == frozenset()

    def test_the_model_is_the_atlas_asset_backbone(self) -> None:
        """The marker names its own model, so nothing else has to hardcode it."""
        from pyatlan_v9.model.assets import Asset

        assert AssetArtifact.model() is Asset

    def test_the_sdks_own_transformed_files_field_is_marked(self) -> None:
        """The fleet-wide fact this exists for, asserted on the real contract.

        ``ExtractionOutput.transformed_files`` is declared by the SDK, populated
        by ``SqlApp.run()`` and written by ``SqlApp._transform_entity``.  If this
        marker were ever dropped, every SQL connector would be back to
        hand-authoring an envelope for it — an error at v4.0.
        """
        from application_sdk.templates.contracts.sql_metadata import ExtractionOutput

        assert "transformed_files" in asset_artifact_fields(ExtractionOutput)


class _RedeclaredPlain(_Marked):
    """Redeclares the marked field with no marker of its own.

    The shape a connector reaches for when it wants a narrower type or its own
    ``Field(...)``. Pydantic rebuilds the metadata tuple from the new annotation,
    so this subclass's own ``model_fields`` entry carries no ``AssetArtifact``.
    """

    transformed: FileReference | None = None


class _RedeclaredWithField(_Marked):
    transformed: Annotated[FileReference | None, MaxItems(1)] = None


class _RedeclaredKeepingMarker(_Marked):
    transformed: Annotated[FileReference | None, AssetArtifact()] = None


class TestMarkerSurvivesRedeclaration:
    """A subclass may narrow a marked field; it may not silently undeclare it.

    This is the path the fleet-wide claim rests on. A connector is told it can
    delete its hand-written envelope because it inherits the marker — so if
    redeclaring the field dropped the marker, that connector would land on
    ``not_declared`` on a public boundary, and at v4.0 on a hard error, having
    done exactly what it was told.
    """

    def test_pydantic_really_does_drop_the_metadata(self) -> None:
        """The premise, pinned: this is a fact about Pydantic, not a guess.

        If a future Pydantic starts carrying an inherited field's metadata
        through a redeclaration, the MRO walk becomes redundant rather than
        wrong — but the reason it exists should fail visibly instead of quietly
        becoming folklore.
        """
        own = _RedeclaredPlain.model_fields["transformed"].metadata
        assert not any(isinstance(m, AssetArtifact) for m in own)

    def test_a_bare_redeclaration_keeps_the_marker(self) -> None:
        assert asset_artifact_marker(_RedeclaredPlain, "transformed") is not None

    def test_a_redeclaration_with_other_metadata_keeps_the_marker(self) -> None:
        assert asset_artifact_marker(_RedeclaredWithField, "transformed") is not None

    def test_a_redeclaration_restating_the_marker_keeps_it(self) -> None:
        assert (
            asset_artifact_marker(_RedeclaredKeepingMarker, "transformed") is not None
        )

    def test_the_field_set_covers_a_redeclared_field(self) -> None:
        assert "transformed" in asset_artifact_fields(_RedeclaredPlain)

    def test_redeclaring_an_unmarked_field_stays_unmarked(self) -> None:
        """Stickiness is not contagious: only a marked ancestor confers it."""

        class _RedeclaredPlainNeighbour(_Marked):
            plain: FileReference | None = None

        assert asset_artifact_marker(_RedeclaredPlainNeighbour, "plain") is None
