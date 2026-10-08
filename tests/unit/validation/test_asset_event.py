"""The shipped ``ASSET_VALIDATION_EVENT`` surface, pinned (FND-690).

FND-690 folded asset validation into the artifact wrapper as its NDJSON x
``ModelSource`` cell. That is a refactor behind a **stable event surface**: the
event's name and every one of its attribute keys are matched verbatim by dashboards,
the ``asset_validation_matrix`` drill-down and alert rules, and v3 has shipped
consumers of all of it.

So this file does not merely exercise the projection — it pins it, in the two ways
that catch the two ways it can break:

* **A golden payload.** The full attribute map for a known batch is compared against
  a literal, so a reworded key, a dropped key, a renamed matrix row key or a changed
  ``outcome`` value fails loudly rather than silently emptying a panel.
* **The allowlist.** Every key the projection emits must be in
  ``logger_adaptor._KNOWN_EXTRA_KEYS``, or ``_build_extra_dict`` drops it and the
  attribute never reaches OTLP at all. A test that asserted only the returned dict
  would pass while the row arrived empty in ClickHouse.

The cell's own behaviour — that it is the same walk, reported in the shared shape —
is in ``test_assets.py`` (the walk) and ``test_ndjson_validator.py`` (the dispatch).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from pyatlan_v9.model.assets import Column

from application_sdk.constants import ASSET_VALIDATION_MAX_ITEMS_PER_AXIS
from application_sdk.observability.events import ASSET_VALIDATION_EVENT
from application_sdk.observability.logger_adaptor import (
    _KNOWN_EXTRA_KEYS,
    ASSET_VALIDATION_MATRIX_KEY,
    ASSET_VALIDATION_SUMMARY_KEY,
)
from application_sdk.validation.assets import (
    ASSET_VALIDATION_MATRIX_ERROR_MAXLEN,
    AssetBatchScope,
    AssetValidationFailure,
    AssetValidationReport,
    ReferentialFailure,
    asset_validation_event_fields,
    asset_validation_matrix_json,
    asset_validation_summary_json,
    validate_asset,
    validate_transformed_dir,
)

APP = "test-app"
CONN = "default/snow/123"
SCHEMA_QN = f"{CONN}/DB/SCHEMA"
TABLE_QN = f"{SCHEMA_QN}/T1"


def _failure(
    i: int, *, deserialize_error: bool = False, error: str | None = None
) -> AssetValidationFailure:
    return AssetValidationFailure(
        file="entities.json",
        line=i,
        type_name="Table",
        qualified_name=f"{TABLE_QN}_{i}",
        errors=[error if error is not None else f"bad {i}"],
        deserialize_error=deserialize_error,
    )


def _orphan(i: int) -> ReferentialFailure:
    return ReferentialFailure(
        missing_type_name="Table",
        missing_qualified_name=f"{SCHEMA_QN}/T_MISSING_{i}",
        reference_count=1,
        file="entities.json",
        line=i,
        type_name="Column",
        qualified_name=f"{SCHEMA_QN}/T_MISSING_{i}/C1",
        relationship="table",
    )


# ---------------------------------------------------------------------------
# The event surface, pinned
# ---------------------------------------------------------------------------


def test_the_event_name_is_unchanged() -> None:
    """The log message body *is* the event name, and it is matched verbatim.

    Pinned as a literal rather than compared to the constant: comparing a constant
    to itself would pass through any rename, which is the whole failure this guards.
    """
    assert ASSET_VALIDATION_EVENT == "Transformed-asset validation outcome"


def test_the_full_attribute_map_is_byte_identical_for_a_known_batch() -> None:
    """The golden payload: every key, every value, and the matrix as an exact string.

    One invalid record, one undeserializable record, one orphan, over a batch of
    four. The literal below is frozen from what the **pre-wrapper** emitter produced
    for this input, so a diff here is a change to a shipped surface. The one
    deliberate changes since are additive: ``asset_validation_summary`` and
    ``assets_referential_check`` (FND-3495). A hand-built report has no
    ``decode_reason`` and did not come from the walk, hence the two ``unknown``s.

    This is the before-vs-after anchor for the whole fold-in. Its sibling in
    ``test_asset_cell.py`` pins that the wrapper path equals the direct path, which
    is a different property: that comparison runs both sides through this same
    projection, so only a frozen literal can catch the projection itself changing.
    """
    report = AssetValidationReport(
        total=4,
        passed=2,
        undeserializable=1,
        failures=[
            _failure(3, error="qualified_name is required"),
            _failure(4, deserialize_error=True, error="could not deserialize"),
        ],
        orphans=[_orphan(2)],
    )

    fields = asset_validation_event_fields(report, app_name=APP)

    assert fields == {
        "outcome": "flagged",
        "app_name": "test-app",
        "assets_total": 4,
        "assets_passed": 2,
        "assets_invalid": 1,
        "assets_orphaned": 1,
        "assets_undeserializable": 1,
        "asset_validation_matrix": (
            '[{"kind":"invalid","type_name":"Table",'
            '"qualified_name":"default/snow/123/DB/SCHEMA/T1_3",'
            '"error":"qualified_name is required","file":"entities.json","line":3},'
            '{"kind":"undeserializable","type_name":"Table",'
            '"qualified_name":"default/snow/123/DB/SCHEMA/T1_4",'
            '"error":"could not deserialize","file":"entities.json","line":4},'
            '{"kind":"orphan","type_name":"Table",'
            '"qualified_name":"default/snow/123/DB/SCHEMA/T_MISSING_2",'
            '"relationship":"table","reference_count":1,'
            '"file":"entities.json","line":2}]'
        ),
        "asset_validation_summary": (
            '[{"kind":"invalid","type_name":"Table",'
            '"detail":"required:qualified_name","count":1},'
            '{"kind":"orphan","type_name":"Table","detail":"table","count":1,'
            '"references":1},'
            '{"kind":"undeserializable","type_name":"Table",'
            '"detail":"decode:unknown","count":1}]'
        ),
        "assets_referential_check": "unknown",
    }


def test_a_clean_batch_emits_a_denominator_with_an_empty_matrix() -> None:
    """The clean row exists so flag-rate has a denominator, and the matrix is
    always present — ``"[]"`` rather than absent, so consumers never branch on it."""
    fields = asset_validation_event_fields(
        AssetValidationReport(total=3, passed=3), app_name=APP
    )

    assert fields["outcome"] == "clean"
    assert fields[ASSET_VALIDATION_MATRIX_KEY] == "[]"


def test_the_outcome_vocabulary_stays_two_valued() -> None:
    """This event shipped with ``clean``/``flagged`` and nothing else.

    The shared artifact report has five outcomes, and a scan with nothing to read is
    ``absent`` there — but widening the vocabulary of a field dashboards group by is
    a breaking change dressed as an improvement, so a zero-record batch reports
    ``clean`` here.
    """
    empty = asset_validation_event_fields(AssetValidationReport(), app_name=APP)

    assert empty["outcome"] == "clean"


def test_every_emitted_key_is_allowlisted_for_otlp() -> None:
    """The projection's core promise — these attributes reach ClickHouse — holds only
    while every key stays in ``_KNOWN_EXTRA_KEYS``. A key absent from it is dropped
    by ``_build_extra_dict`` and silently never reaches the exporter, so asserting
    the returned dict alone would pass while the row arrived empty."""
    emitted = set(
        asset_validation_event_fields(
            AssetValidationReport(),
            app_name=APP,
            scope=AssetBatchScope(
                upload_kind="upload", parts_validated=1, parts_not_local=0
            ),
        )
    )

    assert not emitted - _KNOWN_EXTRA_KEYS


def test_invalid_and_undeserializable_are_reported_disjointly() -> None:
    """``AssetValidationReport.failed`` counts undeserializable records too, so the
    event subtracts them out — matching the headline in ``format_report()``. Without
    this, a batch of pure decode failures would be double-counted as invalid assets
    as well."""
    report = AssetValidationReport(
        total=2,
        passed=0,
        undeserializable=2,
        failures=[_failure(i, deserialize_error=True) for i in range(2)],
    )

    fields = asset_validation_event_fields(report, app_name=APP)

    assert fields["assets_invalid"] == 0
    assert fields["assets_undeserializable"] == 2


# ---------------------------------------------------------------------------
# The bounded drill-down matrix
# ---------------------------------------------------------------------------


def test_matrix_is_bounded_to_max_rows_per_axis() -> None:
    """The matrix caps at the shared constant **per axis**, so a pathological batch
    cannot produce an unbounded LogAttributes value. The report still carries the
    true totals — only the drill-down sample is bounded."""
    cap = ASSET_VALIDATION_MAX_ITEMS_PER_AXIS
    n = cap + 5
    report = AssetValidationReport(
        total=2 * n,
        passed=0,
        failures=[_failure(i) for i in range(n)],
        orphans=[_orphan(i) for i in range(n)],
    )

    matrix = json.loads(asset_validation_matrix_json(report))

    assert len([r for r in matrix if r["kind"] == "invalid"]) == cap
    assert len([r for r in matrix if r["kind"] == "orphan"]) == cap
    assert len(matrix) == 2 * cap
    # Scalar totals are unbounded (full batch), only the matrix is sampled.
    assert report.failed == n
    assert len(report.orphans) == n


def test_matrix_marks_undeserializable_rows() -> None:
    """A failure carrying ``deserialize_error=True`` surfaces as
    ``kind="undeserializable"``, never ``"invalid"`` — so a dashboard can tell decode
    failures from per-asset ``.validate()`` failures."""
    report = AssetValidationReport(
        total=1, passed=0, failures=[_failure(0, deserialize_error=True)]
    )

    matrix = json.loads(asset_validation_matrix_json(report))

    assert [r["kind"] for r in matrix] == ["undeserializable"]


def test_matrix_truncates_long_error_to_maxlen() -> None:
    """Per-row error text is clipped so a single pathological ``.validate()`` message
    cannot bloat the ClickHouse attribute. Pin the length so a future refactor of the
    slice can't silently drop the guard."""
    maxlen = ASSET_VALIDATION_MATRIX_ERROR_MAXLEN
    report = AssetValidationReport(
        total=1, passed=0, failures=[_failure(0, error="x" * (maxlen + 50))]
    )

    matrix = json.loads(asset_validation_matrix_json(report))

    assert len(matrix[0]["error"]) == maxlen


@pytest.mark.parametrize("max_items", [0, 1, 3])
def test_the_matrix_cap_is_honoured_per_axis(max_items: int) -> None:
    """``max_items`` is passed through by the event projection, so the human report
    and the telemetry can be held to the same cap at one call site."""
    report = AssetValidationReport(
        total=10,
        passed=0,
        failures=[_failure(i) for i in range(5)],
        orphans=[_orphan(i) for i in range(5)],
    )

    fields = asset_validation_event_fields(report, app_name=APP, max_items=max_items)

    assert len(json.loads(fields[ASSET_VALIDATION_MATRIX_KEY])) == 2 * max_items


# ---------------------------------------------------------------------------
# The complete per-rule summary (FND-3495)
# ---------------------------------------------------------------------------


def _summary(report: AssetValidationReport, **kwargs: int) -> list[dict[str, object]]:
    return json.loads(asset_validation_summary_json(report, **kwargs))


def _real_column_errors(qualified_name: str) -> list[str]:
    """The errors a real pyatlan_v9 ``Column.validate()`` produces, captured through
    the SDK's own ``validate_asset`` — so the parser is tested against the bundle
    pyatlan actually raises, not a hand-written imitation of it."""
    errors = validate_asset(Column(name="C1", qualified_name=qualified_name))
    assert errors, "fixture must fail validation"
    return errors


def test_a_real_pyatlan_bundle_is_split_into_per_field_rules() -> None:
    """pyatlan_v9 raises one ``ValueError`` holding every message. The summary must
    see each check separately, keyed by field — that is the whole point of it."""
    errors = _real_column_errors("not-a-valid-column-qn")
    report = AssetValidationReport(
        total=1,
        failures=[
            AssetValidationFailure(
                file="f.json",
                line=1,
                type_name="Column",
                qualified_name="not-a-valid-column-qn",
                errors=errors,
            )
        ],
    )

    details = {row["detail"] for row in _summary(report)}

    assert "pattern:qualified_name" in details
    assert "required_for_creation:schema_qualified_name" in details
    assert (
        "one_of_required_for_creation:table|table_partition|view|materialised_view"
        in details
    )
    assert "other" not in details


def test_the_summary_never_carries_record_values() -> None:
    """The pattern message embeds the offending qualifiedName; the rule key must not,
    or the summary would be unbounded in cardinality and carry record data."""
    qn = "default/snow/123/SECRETISH_VALUE"
    report = AssetValidationReport(
        total=1,
        failures=[
            AssetValidationFailure(
                file="f.json",
                line=1,
                type_name="Column",
                qualified_name=qn,
                errors=_real_column_errors(qn),
            )
        ],
    )

    assert "SECRETISH_VALUE" not in asset_validation_summary_json(report)


def test_counts_cover_the_whole_batch_not_the_matrix_sample() -> None:
    """The matrix stops at the per-axis cap; the summary must count every failure,
    which is the gap it closes."""
    n = ASSET_VALIDATION_MAX_ITEMS_PER_AXIS * 4
    errors = _real_column_errors("bad")
    report = AssetValidationReport(
        total=n,
        failures=[
            AssetValidationFailure(
                file="f.json",
                line=i,
                type_name="Column",
                qualified_name="bad",
                errors=errors,
            )
            for i in range(n)
        ],
    )

    by_detail = {row["detail"]: row["count"] for row in _summary(report)}

    assert by_detail["pattern:qualified_name"] == n


def test_an_asset_counts_once_per_rule_and_unknown_text_is_other() -> None:
    report = AssetValidationReport(
        total=2,
        failures=[
            AssetValidationFailure(
                file="f.json",
                line=1,
                type_name="Table",
                qualified_name="a",
                errors=["name is required", "name is required", "boom"],
            ),
            AssetValidationFailure(
                file="f.json",
                line=2,
                type_name="Table",
                qualified_name="b",
                errors=[],
            ),
        ],
    )

    assert _summary(report) == [
        {"kind": "invalid", "type_name": "Table", "detail": "other", "count": 2},
        {
            "kind": "invalid",
            "type_name": "Table",
            "detail": "required:name",
            "count": 1,
        },
    ]


def test_orphans_count_missing_targets_and_sum_their_references() -> None:
    def orphan(i: int, refs: int) -> ReferentialFailure:
        return ReferentialFailure(
            missing_type_name="Table",
            missing_qualified_name=f"T{i}",
            reference_count=refs,
            file="f.json",
            line=i,
            type_name="Column",
            qualified_name=f"T{i}/C",
            relationship="table",
        )

    report = AssetValidationReport(total=5, orphans=[orphan(1, 3), orphan(2, 2)])

    assert _summary(report) == [
        {
            "kind": "orphan",
            "type_name": "Table",
            "detail": "table",
            "count": 2,
            "references": 5,
        },
    ]


def test_rows_past_the_cap_fold_into_one_truncated_row_that_keeps_the_total() -> None:
    report = AssetValidationReport(
        total=6,
        failures=[
            AssetValidationFailure(
                file="f.json",
                line=i,
                type_name=f"Type{i}",
                qualified_name="q",
                errors=["name is required"],
            )
            for i in range(6)
        ],
    )

    rows = _summary(report, max_rows=2)

    assert len(rows) == 3
    assert rows[-1] == {
        "kind": "truncated",
        "type_name": "",
        "detail": "",
        "count": 4,
        "references": 0,
    }
    assert sum(row["count"] for row in rows) == 6


def test_truncation_keeps_the_reference_total_of_dropped_orphan_groups() -> None:
    """Two invalid groups outrank one orphan group; with ``max_rows=2`` the orphan
    group is dropped, and its five references must still reach the truncated row."""

    def invalid(i: int) -> AssetValidationFailure:
        return AssetValidationFailure(
            file="f.json",
            line=i,
            type_name="Table",
            qualified_name="q",
            errors=["name is required", "qualified_name is required"],
        )

    report = AssetValidationReport(
        total=7,
        failures=[invalid(i) for i in range(2)],
        orphans=[
            ReferentialFailure(
                missing_type_name="Schema",
                missing_qualified_name="S1",
                reference_count=5,
                file="f.json",
                line=9,
                type_name="Table",
                qualified_name="S1/T",
                relationship="atlan_schema",
            )
        ],
    )

    rows = _summary(report, max_rows=2)

    assert [row["kind"] for row in rows] == ["invalid", "invalid", "truncated"]
    assert rows[-1]["count"] == 1
    assert rows[-1]["references"] == 5


def test_a_clean_batch_emits_an_empty_summary() -> None:
    fields = asset_validation_event_fields(
        AssetValidationReport(total=3, passed=3), app_name=APP
    )

    assert fields[ASSET_VALIDATION_SUMMARY_KEY] == "[]"


# ---------------------------------------------------------------------------
# Batch context and decode reasons (FND-3495)
# ---------------------------------------------------------------------------


def _write_ndjson(base: Path, lines: list[bytes]) -> Path:
    target = base / "transformed" / "Mixed"
    target.mkdir(parents=True)
    (target / "entities.json").write_bytes(b"\n".join(lines) + b"\n")
    return base / "transformed"


def test_the_batch_scope_is_carried_verbatim() -> None:
    fields = asset_validation_event_fields(
        AssetValidationReport(),
        app_name=APP,
        scope=AssetBatchScope(
            upload_kind="upload_refs", parts_validated=3, parts_not_local=1
        ),
    )

    assert fields["assets_upload_kind"] == "upload_refs"
    assert fields["assets_parts_validated"] == 3
    assert fields["assets_parts_not_local"] == 1


def test_without_a_scope_the_scope_keys_are_omitted_not_guessed() -> None:
    fields = asset_validation_event_fields(AssetValidationReport(), app_name=APP)

    assert not {"assets_upload_kind", "assets_parts_validated"} & set(fields)


def test_decode_failures_keep_their_type_and_a_value_free_reason(
    tmp_path: Path,
) -> None:
    """Through the real walk: a record the full decode rejects still attributes to
    its ``typeName``, and the reason names the failing field, not its value."""
    path = _write_ndjson(
        tmp_path,
        [
            b"{not json",
            b'{"typeName":"Table","attributes":{"name":"t","columnCount":"many"}}',
            b'{"typeName":"Table","attributes":{"name":"t","columnCount":"lots"}}',
        ],
    )

    report = validate_transformed_dir(path)
    rows = {(r["type_name"], r["detail"]): r["count"] for r in _summary(report)}

    assert rows == {
        ("Table", "decode:schema_mismatch:columnCount"): 2,
        ("", "decode:malformed_json"): 1,
    }
    assert "many" not in asset_validation_summary_json(report)


@pytest.mark.parametrize(
    ("check_referential_integrity", "expected"),
    [(True, "ran"), (False, "not_requested")],
)
def test_the_walk_records_whether_the_orphan_pass_ran(
    tmp_path: Path, check_referential_integrity: bool, expected: str
) -> None:
    """Zero orphans means "none found" only when the pass ran; the row says which."""
    path = _write_ndjson(tmp_path, [b'{"typeName":"Table","attributes":{}}'])

    report = validate_transformed_dir(
        path, check_referential_integrity=check_referential_integrity
    )

    assert report.referential_check == expected


def test_a_missing_spill_store_is_reported_as_skipped(tmp_path: Path) -> None:
    path = _write_ndjson(tmp_path, [b'{"typeName":"Table","attributes":{}}'])

    with patch(
        "application_sdk.validation.assets.SpillableDict", side_effect=ImportError
    ):
        report = validate_transformed_dir(path)

    assert report.referential_check == "skipped_unavailable"
