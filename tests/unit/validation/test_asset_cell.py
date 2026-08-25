"""The NDJSON x ``ModelSource`` cell: asset validation reached through the wrapper.

FND-690 folded ``validate_transformed_dir`` into the artifact wrapper. The claim
being tested is narrow and specific: **the same walk, reached a new way, reporting
the same findings in two vocabularies.** So the tests here are mostly equivalences
rather than behaviours — the behaviour of the walk itself is ``test_assets.py``'s
subject and is deliberately not re-asserted.

* The wrapper path and the direct path produce an **identical event payload** for the
  same input, which proves the wrapper *carries* the scan's own report rather than
  re-deriving one. Note what this does and does not pin: both sides call the same
  post-refactor projection, so it is wrapper == direct, not before == after. The
  before/after guarantee is the frozen literal in ``test_asset_event.py`` — that is
  where a reworded key or a changed value fails.
* The shared report's derived counts agree with the asset report's own, so a generic
  consumer and a dashboard cannot disagree about the same batch.
* Every outcome resolves through the wrapper — including the negatives — because a
  check that reports nothing is indistinguishable from a check that passed.

Fixtures are real pyatlan_v9 assets serialized with ``to_nested_bytes()``, the wire
shape connectors actually write, so the decode path is the production one.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from pyatlan_v9.model.assets import Asset, Column, Database, Schema, Table

from application_sdk.validation import (
    FORMAT_NDJSON,
    FORMAT_PARQUET,
    AssetArtifactReport,
    ModelSource,
    asset_validation_event_fields,
    validate_artifact,
    validate_assets_as_artifact,
    validate_transformed_dir,
)
from application_sdk.validation.artifacts import (
    OUTCOME_ABSENT,
    OUTCOME_CLEAN,
    OUTCOME_FLAGGED,
    OUTCOME_UNSUPPORTED,
    UNIT_RECORD,
    ModelDeclaration,
)

_HAS_ROCKSDICT = importlib.util.find_spec("rocksdict") is not None
requires_rocksdict = pytest.mark.skipif(
    not _HAS_ROCKSDICT,
    reason="referential-integrity pass needs rocksdict (the [storage] extra)",
)

APP = "test-app"
CONN = "default/snow/123"
DB_QN = f"{CONN}/DB"
SCHEMA_QN = f"{CONN}/DB/SCHEMA"
TABLE_QN = f"{SCHEMA_QN}/T1"


def _write(base: Path, entity: str, assets: list) -> None:
    out_dir = base / "transformed" / entity
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / "entities.json", "wb") as handle:
        for asset in assets:
            handle.write(asset.to_nested_bytes())
            handle.write(b"\n")


def _valid_hierarchy(base: Path) -> None:
    _write(
        base, "Database", [Database.creator(name="DB", connection_qualified_name=CONN)]
    )
    _write(
        base, "Schema", [Schema.creator(name="SCHEMA", database_qualified_name=DB_QN)]
    )
    _write(base, "Table", [Table.creator(name="T1", schema_qualified_name=SCHEMA_QN)])


def _invalid_table() -> Table:
    table = Table.creator(name="T1", schema_qualified_name=SCHEMA_QN)
    table.qualified_name = None
    return table


def _through_the_wrapper(path: Path) -> AssetArtifactReport:
    report = validate_artifact(path, ModelSource(model=Asset))
    assert isinstance(report, AssetArtifactReport), report
    return report


# ---------------------------------------------------------------------------
# The equivalence that makes this a refactor
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("flawed", [False, True], ids=["clean", "flagged"])
def test_the_wrapper_path_matches_the_direct_path(tmp_path: Path, flawed: bool) -> None:
    """Reaching the scan through the wrapper changes nothing about what is emitted.

    This is the invariant that makes ``AssetArtifactReport`` carrying ``.assets``
    worth the subclass: the wrapper hands the projection the scan's own report, so
    the two call paths cannot drift. Run over both outcomes, because the clean row is
    the denominator and is emitted just as unconditionally as the flagged one.

    **What this does not prove.** Both sides call the same post-refactor
    :func:`asset_validation_event_fields`, so a rename applied consistently would
    keep them equal. Before-vs-after is pinned separately, by the frozen literal in
    ``test_asset_event.py`` — the two tests are complements, not duplicates.
    """
    _valid_hierarchy(tmp_path)
    if flawed:
        _write(tmp_path, "Extra", [_invalid_table()])

    before = asset_validation_event_fields(
        validate_transformed_dir(tmp_path / "transformed"), app_name=APP
    )
    after = asset_validation_event_fields(
        _through_the_wrapper(tmp_path / "transformed").assets, app_name=APP
    )

    assert after == before
    assert after["outcome"] == ("flagged" if flawed else "clean")


def test_the_shared_counts_agree_with_the_asset_counts(tmp_path: Path) -> None:
    """One walk populates both vocabularies, so they cannot disagree about a batch.

    ``undecodable`` is the load-bearing one: the shared report *derives* it from the
    failure list rather than tracking it, so this pins that the projection's
    ``invalid``/``undecodable`` split lands on the same records the asset report
    counted as undeserializable.
    """
    _valid_hierarchy(tmp_path)
    _write(tmp_path, "Extra", [_invalid_table()])

    report = _through_the_wrapper(tmp_path / "transformed")

    assert report.total == report.assets.total == 4
    assert report.passed == report.assets.passed == 3
    assert report.undecodable == report.assets.undeserializable == 0
    # ``failed`` is a unit count: invalid + undecodable, and never the orphans.
    assert report.failed == 1


def test_an_undeserializable_record_lands_as_undecodable(tmp_path: Path) -> None:
    """The shared vocabulary's word for it, on the same records."""
    out_dir = tmp_path / "transformed" / "Junk"
    out_dir.mkdir(parents=True)
    (out_dir / "entities.json").write_bytes(b'{"not":"an asset"\n')

    report = _through_the_wrapper(tmp_path / "transformed")

    assert report.assets.undeserializable == 1
    assert report.undecodable == 1
    assert [f.kind for f in report.failures] == ["undecodable"]


@requires_rocksdict
def test_an_orphan_lands_as_a_missing_reference(tmp_path: Path) -> None:
    """An orphan is a ``missing`` failure naming the relationship it came through —
    and it does **not** move ``passed``. A record can be valid on its own terms and
    still point at a parent nobody emitted, which is why the referential pass is a
    second axis rather than a per-record verdict."""
    _valid_hierarchy(tmp_path)
    _write(
        tmp_path,
        "Column",
        [
            Column.creator(
                name="C1",
                parent_type=Table,
                parent_qualified_name=f"{SCHEMA_QN}/T_MISSING",
                order=1,
            )
        ],
    )

    report = _through_the_wrapper(tmp_path / "transformed")

    assert report.outcome == OUTCOME_FLAGGED
    assert report.total == report.passed == 4
    # Every unit passed its own check; the batch is still flagged.
    assert report.failed == 0
    orphans = [f for f in report.failures if f.kind == "missing"]
    assert len(orphans) == 1
    assert orphans[0].field == "table"
    assert "T_MISSING" in orphans[0].errors[0]


# ---------------------------------------------------------------------------
# Every path resolves to an outcome
# ---------------------------------------------------------------------------


def test_a_valid_batch_is_clean_and_names_its_cell(tmp_path: Path) -> None:
    """The wrapper stamps the cell, not the validator — so a dashboard reading the
    generic event knows which (format x source) produced the row."""
    _valid_hierarchy(tmp_path)

    report = _through_the_wrapper(tmp_path / "transformed")

    assert report.outcome == OUTCOME_CLEAN
    assert report.artifact_format == FORMAT_NDJSON
    assert report.schema_source == "model"
    assert report.unit == UNIT_RECORD
    # A model declares no enumerable field list at this layer.
    assert report.fields_declared == 0


def test_an_empty_batch_is_absent_not_clean(tmp_path: Path) -> None:
    """Zero records checked reported as ``clean`` is the exact failure this
    capability exists to remove. The legacy event still says ``clean``, because its
    outcome vocabulary shipped two-valued — both surfaces are honest, in their own
    vocabulary."""
    (tmp_path / "transformed").mkdir()

    report = _through_the_wrapper(tmp_path / "transformed")

    assert report.outcome == OUTCOME_ABSENT
    assert "no ndjson records" in report.reason
    assert asset_validation_event_fields(report.assets, app_name=APP)["outcome"] == (
        "clean"
    )


def test_parquet_times_model_is_still_unsupported(tmp_path: Path) -> None:
    """Folding in the NDJSON cell does not quietly claim the parquet one. A model
    carries no column mapping, so a footer diff has nothing to diff against."""
    report = validate_artifact(
        tmp_path, ModelSource(model=Asset, artifact_format=FORMAT_PARQUET)
    )

    assert report.outcome == OUTCOME_UNSUPPORTED
    assert not isinstance(report, AssetArtifactReport)


def test_an_undelegatable_model_is_unsupported(tmp_path: Path) -> None:
    """``ModelSource`` accepts any class with a callable ``validate``; this cell
    decodes through pyatlan_v9, so anything else is reported ``unsupported`` rather
    than scanned with the wrong decoder."""

    class _Whatever:
        def validate(self) -> None: ...

    report = validate_artifact(tmp_path, ModelSource(model=_Whatever))

    assert report.outcome == OUTCOME_UNSUPPORTED


def test_a_direct_caller_with_an_undelegatable_model_gets_a_report(
    tmp_path: Path,
) -> None:
    """Unreachable through the wrapper, which honours ``supports`` — guarded for the
    app that calls the cell itself, where a wrong-decoder scan would report a whole
    artifact as a data defect."""
    report = validate_assets_as_artifact(tmp_path, ModelDeclaration(model=dict))

    assert report.outcome == OUTCOME_ABSENT
    assert "delegates to pyatlan_v9 assets" in report.reason
    assert report.assets.total == 0


def test_the_referential_pass_can_be_turned_off_by_a_direct_caller(
    tmp_path: Path,
) -> None:
    """The wrapper path always runs it — extracts and transforms are full by design,
    so the batch is complete — but the knob survives for a caller validating a
    deliberately partial batch."""
    _write(
        tmp_path, "Table", [Table.creator(name="T1", schema_qualified_name=SCHEMA_QN)]
    )

    report = validate_assets_as_artifact(
        tmp_path / "transformed", check_referential_integrity=False
    )

    assert report.outcome == OUTCOME_CLEAN
    assert report.assets.orphans == []
