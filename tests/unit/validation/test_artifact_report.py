"""Tests for the shared artifact-validation outcome surface (ADR-0020).

The two properties worth defending here are the ones a later change would break
quietly:

* **Two-tier bounding.** The scan is unbounded — every unit is examined and the
  scalar counts always describe the whole artifact — while the two *output*
  surfaces are capped by one shared constant, so the human report and the telemetry
  matrix can never drift apart.
* **No silent no-op.** Every hand-off resolves to exactly one outcome from the
  fixed vocabulary, the negatives included, and the matrix attribute is present
  even when empty so consumers never branch on its presence.
"""

from __future__ import annotations

import orjson
import pytest

from application_sdk.constants import (
    ARTIFACT_VALIDATION_MAX_ITEMS_PER_AXIS,
    ASSET_VALIDATION_MAX_ITEMS_PER_AXIS,
)
from application_sdk.validation.artifacts import (
    ARTIFACT_FIELD_TYPES,
    ARTIFACT_FIELD_TYPES_EXTENDED,
    ARTIFACT_VALIDATION_OUTCOMES,
    OUTCOME_ABSENT,
    OUTCOME_CLEAN,
    OUTCOME_FLAGGED,
    OUTCOME_NOT_DECLARED,
    OUTCOME_UNSUPPORTED,
    UNIT_COLUMN,
    UNIT_RECORD,
    ArtifactValidationFailure,
    ArtifactValidationReport,
    DeclaredField,
    FieldMapDeclaration,
    ModelDeclaration,
    artifact_validation_matrix_json,
)

CAP = ARTIFACT_VALIDATION_MAX_ITEMS_PER_AXIS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mismatch(index: int) -> ArtifactValidationFailure:
    """A type-mismatch row — the failure the capability exists to catch."""
    return ArtifactValidationFailure(
        kind="type_mismatch",
        field="START_TIME",
        expected="timestamp",
        actual="string",
        file="query_history.ndjson",
        line=index,
        errors=["declared timestamp, found string"],
    )


def _undecodable(index: int) -> ArtifactValidationFailure:
    return ArtifactValidationFailure(
        kind="undecodable",
        file="query_history.ndjson",
        line=index,
        errors=["not valid JSON"],
    )


def _scanned(
    *, failures: list[ArtifactValidationFailure], total: int
) -> ArtifactValidationReport:
    """A report from a scan that examined ``total`` records."""
    failing_lines = {f.line for f in failures}
    return ArtifactValidationReport(
        artifact_format="ndjson",
        schema_source="contract",
        unit=UNIT_RECORD,
        fields_declared=4,
        total=total,
        passed=total - len(failing_lines),
        failures=failures,
    )


# ---------------------------------------------------------------------------
# Outcome derivation
# ---------------------------------------------------------------------------


def test_clean_scan_is_clean_and_ok() -> None:
    report = _scanned(failures=[], total=1000)
    assert report.outcome == OUTCOME_CLEAN
    assert report.ok
    assert report.failed == 0
    assert report.undecodable == 0


def test_any_failure_flags_the_report() -> None:
    report = _scanned(failures=[_mismatch(7)], total=1000)
    assert report.outcome == OUTCOME_FLAGGED
    assert not report.ok
    assert report.failed == 1


def test_outcome_is_derived_not_stored_for_a_scan() -> None:
    """A derived outcome cannot disagree with the failure list it summarises."""
    report = _scanned(failures=[], total=10)
    assert report.outcome == OUTCOME_CLEAN
    report.failures.append(_mismatch(3))
    assert report.outcome == OUTCOME_FLAGGED


def test_not_declared_carries_the_boundary_axis() -> None:
    finding = ArtifactValidationReport.not_declared(boundary=True)
    informational = ArtifactValidationReport.not_declared(boundary=False)
    assert finding.outcome == informational.outcome == OUTCOME_NOT_DECLARED
    assert finding.boundary is True
    assert informational.boundary is False
    # Both emit; neither is silent.
    assert finding.ok and informational.ok


def test_unsupported_names_the_cell_that_cannot_check() -> None:
    report = ArtifactValidationReport.unsupported(
        artifact_format="parquet",
        schema_source="model",
        reason="a model carries no column mapping",
    )
    assert report.outcome == OUTCOME_UNSUPPORTED
    assert report.artifact_format == "parquet"
    assert report.schema_source == "model"


def test_absent_is_a_reported_outcome_not_an_exception() -> None:
    report = ArtifactValidationReport.absent(reason="artifact not found")
    assert report.outcome == OUTCOME_ABSENT
    assert report.reason == "artifact not found"


@pytest.mark.parametrize(
    "report",
    [
        _scanned(failures=[], total=1),
        _scanned(failures=[_mismatch(1)], total=1),
        ArtifactValidationReport.not_declared(boundary=True),
        ArtifactValidationReport.unsupported(
            artifact_format="parquet", schema_source="model", reason="r"
        ),
        ArtifactValidationReport.absent(reason="r"),
    ],
)
def test_every_report_resolves_to_a_vocabulary_outcome(
    report: ArtifactValidationReport,
) -> None:
    assert report.outcome in ARTIFACT_VALIDATION_OUTCOMES


def test_outcome_vocabulary_is_exactly_the_five_adr_values() -> None:
    assert ARTIFACT_VALIDATION_OUTCOMES == {
        "clean",
        "flagged",
        "not_declared",
        "unsupported",
        "absent",
    }


# ---------------------------------------------------------------------------
# Counts
# ---------------------------------------------------------------------------


def test_failed_counts_units_not_failure_rows() -> None:
    """One record breaking on two fields is one failed record, two matrix rows."""
    two_fields_one_record = [
        ArtifactValidationFailure(
            kind="type_mismatch", field="START_TIME", line=4, expected="timestamp"
        ),
        ArtifactValidationFailure(
            kind="missing", field="END_TIME", line=4, expected="timestamp"
        ),
    ]
    report = _scanned(failures=two_fields_one_record, total=10)
    assert report.failed == 1
    assert len(report.failures) == 2
    assert report.passed + report.failed == report.total


def test_undecodable_is_derived_from_the_failure_list() -> None:
    report = _scanned(
        failures=[_mismatch(1), _undecodable(2), _undecodable(3)], total=9
    )
    assert report.undecodable == 2


def test_the_scan_is_never_sampled() -> None:
    """Bounding is an output concern: the counts describe the whole artifact."""
    failures = [_mismatch(i) for i in range(1, CAP * 4 + 1)]
    report = _scanned(failures=failures, total=100_000)
    assert report.total == 100_000
    assert report.failed == CAP * 4
    assert len(report.failures) == CAP * 4


# ---------------------------------------------------------------------------
# Bounding — the human report
# ---------------------------------------------------------------------------


def test_format_report_lists_at_most_max_items() -> None:
    report = _scanned(failures=[_mismatch(i) for i in range(1, 101)], total=1000)
    listed = [
        line for line in report.format_report().splitlines() if "TYPE_MISMATCH" in line
    ]
    assert len(listed) == CAP


def test_format_report_headline_reflects_the_full_batch() -> None:
    report = _scanned(failures=[_mismatch(i) for i in range(1, 101)], total=1000)
    headline = report.format_report().splitlines()[0]
    assert "900/1000 records passed" in headline
    assert "100 failed" in headline


def test_overflow_is_split_by_kind() -> None:
    """A tail of undecodable records must not be miscounted as type mismatches."""
    failures = [_mismatch(i) for i in range(1, CAP + 6)]
    failures += [_undecodable(i) for i in range(CAP + 6, CAP + 11)]
    report = _scanned(failures=failures, total=1000)
    overflow = [
        line for line in report.format_report().splitlines() if "... and" in line
    ]
    assert overflow == [
        "  ... and 5 more type_mismatch",
        "  ... and 5 more undecodable",
    ]


def test_no_overflow_line_when_everything_fits() -> None:
    report = _scanned(failures=[_mismatch(1)], total=10)
    assert "... and" not in report.format_report()


def test_format_report_renders_the_declared_and_found_types() -> None:
    report = _scanned(failures=[_mismatch(7)], total=10)
    body = report.format_report()
    assert "START_TIME" in body
    assert "declared timestamp, found string" in body
    assert "(query_history.ndjson:7)" in body


def test_non_scan_outcomes_render_a_single_line() -> None:
    boundary = ArtifactValidationReport.not_declared(boundary=True).format_report()
    internal = ArtifactValidationReport.not_declared(boundary=False).format_report()
    assert boundary.splitlines() == [
        "Artifact validation: not_declared (boundary) — no artifact schema declared"
    ]
    assert "(internal)" in internal


# ---------------------------------------------------------------------------
# Bounding — the telemetry matrix
# ---------------------------------------------------------------------------


def test_matrix_is_always_present_even_when_empty() -> None:
    """Consumers parse it unconditionally rather than branching on presence."""
    assert artifact_validation_matrix_json(_scanned(failures=[], total=10)) == "[]"
    assert (
        artifact_validation_matrix_json(
            ArtifactValidationReport.not_declared(boundary=True)
        )
        == "[]"
    )
    assert (
        artifact_validation_matrix_json(ArtifactValidationReport.absent(reason="r"))
        == "[]"
    )


def test_matrix_row_shape_is_fixed() -> None:
    rows = orjson.loads(
        artifact_validation_matrix_json(_scanned(failures=[_mismatch(7)], total=10))
    )
    assert rows == [
        {
            "kind": "type_mismatch",
            "field": "START_TIME",
            "expected": "timestamp",
            "actual": "string",
            "error": "declared timestamp, found string",
            "file": "query_history.ndjson",
            "line": 7,
        }
    ]


def test_matrix_is_bounded_to_max_items() -> None:
    report = _scanned(failures=[_mismatch(i) for i in range(1, 501)], total=10_000)
    assert len(orjson.loads(artifact_validation_matrix_json(report))) == CAP


def test_matrix_truncates_a_pathological_message() -> None:
    report = _scanned(
        failures=[
            ArtifactValidationFailure(kind="invalid", errors=["x" * 5000], line=1)
        ],
        total=1,
    )
    (row,) = orjson.loads(artifact_validation_matrix_json(report))
    assert len(row["error"]) == 300


def test_report_and_matrix_share_one_cap() -> None:
    """The whole point of the shared constant: these two can never drift."""
    report = _scanned(failures=[_mismatch(i) for i in range(1, 501)], total=10_000)
    listed = sum(
        1 for line in report.format_report().splitlines() if "TYPE_MISMATCH" in line
    )
    assert listed == len(orjson.loads(artifact_validation_matrix_json(report)))


def test_asset_cap_is_the_same_number_by_construction() -> None:
    """Asset validation is one cell of the wrapper; two editable 25s would drift."""
    assert ASSET_VALIDATION_MAX_ITEMS_PER_AXIS == ARTIFACT_VALIDATION_MAX_ITEMS_PER_AXIS


# ---------------------------------------------------------------------------
# Declarations and the type vocabulary
# ---------------------------------------------------------------------------


def test_field_map_declaration_counts_its_fields() -> None:
    declaration = FieldMapDeclaration(
        fields=(
            DeclaredField(path="START_TIME", type="timestamp"),
            DeclaredField(path="CREDITS", type="decimal"),
            DeclaredField(path="payload.rows", type="array", required=False),
        )
    )
    assert declaration.field_count == 3
    assert declaration.fields[2].required is False


def test_declared_field_defaults_to_presence_only() -> None:
    """``any`` lets a thin declaration assert presence without inventing a type."""
    assert DeclaredField(path="ID").type == "any"
    assert DeclaredField(path="ID").required is True


def test_model_declaration_enumerates_nothing() -> None:
    declaration = ModelDeclaration(model=dict)
    assert declaration.field_count == 0
    assert declaration.model is dict


def test_extended_vocabulary_layers_additively_on_the_floor() -> None:
    assert ARTIFACT_FIELD_TYPES < ARTIFACT_FIELD_TYPES_EXTENDED
    assert {"string", "timestamp", "any"} <= ARTIFACT_FIELD_TYPES
    assert "decimal" in ARTIFACT_FIELD_TYPES_EXTENDED
    assert "decimal" not in ARTIFACT_FIELD_TYPES


def test_unit_vocabulary() -> None:
    assert (UNIT_RECORD, UNIT_COLUMN) == ("record", "column")
