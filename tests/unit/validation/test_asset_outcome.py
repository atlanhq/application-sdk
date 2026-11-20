"""``summarize_asset_validation``: the bounded outcome the isolated scan returns.

FND-4088. The upload hook runs the scan in a child process and used to pickle the
whole :class:`AssetArtifactReport` back into the worker — every failure twice, about
250 B per failing record. These tests pin the replacement: the child reduces the
report to the event's attribute map and the capped ``format_report()`` text, both
identical to what the worker used to build, and the result's size does not grow
with the number of failing records.
"""

from __future__ import annotations

import pickle
from pathlib import Path

from pyatlan_v9.model.assets import Asset, Database, Table

from application_sdk.validation import (
    ArtifactValidationReport,
    AssetBatchScope,
    AssetValidationOutcome,
    ModelSource,
    asset_validation_event_fields,
    summarize_asset_validation,
    validate_artifact,
    validate_assets_as_artifact,
)

APP = "test-app"
CONN = "default/snow/123"
CAP = 5
SCOPE = AssetBatchScope(upload_kind="upload", parts_validated=1, parts_not_local=0)


def _write_lines(base: Path, lines: list[bytes]) -> Path:
    out = base / "transformed" / "Table"
    out.mkdir(parents=True, exist_ok=True)
    (out / "entities.json").write_bytes(b"\n".join(lines) + b"\n")
    return base / "transformed"


def _failing_batch(base: Path, records: int) -> Path:
    """``records`` failures on both per-asset axes: half invalid, half undecodable."""
    invalid = Table.creator(name="T", schema_qualified_name=f"{CONN}/DB/S")
    invalid.qualified_name = None
    invalid_line = invalid.to_nested_bytes()
    return _write_lines(
        base,
        [invalid_line if i % 2 else b"{not json" for i in range(records)],
    )


def _summarize(target: Path) -> AssetValidationOutcome:
    return summarize_asset_validation(
        validate_artifact,
        str(target),
        ModelSource(model=Asset),
        app_name=APP,
        scope=SCOPE,
        max_items=CAP,
    )


def test_a_flagged_batch_carries_the_same_event_and_report_the_worker_built(
    tmp_path: Path,
) -> None:
    target = _failing_batch(tmp_path, 40)

    outcome = _summarize(target)

    full = validate_assets_as_artifact([str(target)])
    assert outcome.event_fields == asset_validation_event_fields(
        full.assets, app_name=APP, max_items=CAP, scope=SCOPE
    )
    assert outcome.flagged
    assert outcome.flagged_report == full.assets.format_report(max_items=CAP)
    assert outcome.event_fields["outcome"] == "flagged"
    assert outcome.event_fields["assets_total"] == 40


def test_a_clean_batch_has_counts_and_no_report_text(tmp_path: Path) -> None:
    target = _write_lines(
        tmp_path,
        [Database.creator(name="DB", connection_qualified_name=CONN).to_nested_bytes()],
    )

    outcome = _summarize(target)

    assert outcome.event_fields is not None
    assert outcome.event_fields["outcome"] == "clean"
    assert not outcome.flagged
    assert outcome.flagged_report == ""


def test_a_report_without_asset_counts_keeps_only_its_outcome_and_reason() -> None:
    outcome = summarize_asset_validation(
        _absent, "ignored", app_name=APP, scope=SCOPE, max_items=CAP
    )

    assert outcome == AssetValidationOutcome(
        event_fields=None, outcome="absent", reason="validator raised: RuntimeError"
    )
    assert not outcome.flagged


def test_the_outcome_does_not_grow_with_the_number_of_failing_records(
    tmp_path: Path,
) -> None:
    small = pickle.dumps(_summarize(_failing_batch(tmp_path / "small", 50)))
    large_target = _failing_batch(tmp_path / "large", 5_000)
    large = pickle.dumps(_summarize(large_target))

    # Counts gain digits and nothing else grows: the matrix and the report text
    # are capped per axis, and the summary aggregates by (kind, type, rule).
    assert len(large) - len(small) < 64
    # The test can tell: what crossed the process boundary before grows linearly.
    full = pickle.dumps(validate_assets_as_artifact([str(large_target)]))
    assert len(full) > 100 * len(large)


def test_the_outcome_does_not_grow_with_the_length_of_record_values(
    tmp_path: Path,
) -> None:
    # Rows are capped per axis, but a row's values come from the record: a huge
    # qualifiedName lands in the report line, the matrix row and the
    # connection-prefix message; a huge typeName lands in the summary key too.
    huge = 1_000_000
    long_qn = Table.creator(name="T", schema_qualified_name=f"{CONN}/DB/S")
    long_qn.qualified_name = "x" * huge
    long_type = b'{"typeName": "' + b"T" * huge + b'", "attributes": 5}'
    target = _write_lines(tmp_path, [long_qn.to_nested_bytes(), long_type])

    outcome = _summarize(target)

    assert outcome.flagged
    assert outcome.event_fields is not None
    assert outcome.event_fields["assets_total"] == 2
    assert outcome.event_fields["assets_passed"] == 0
    assert len(pickle.dumps(outcome)) < 16 * 1024


def _absent(_path: str) -> ArtifactValidationReport:
    return ArtifactValidationReport.absent(reason="validator raised: RuntimeError")
