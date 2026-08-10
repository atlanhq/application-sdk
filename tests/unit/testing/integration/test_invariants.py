"""Tests for the generic transform-output invariants.

These assert properties that must hold of a connector's ``transformed/`` output
for any source or tenant — the cheapest way to turn "the workflow ran" into "the
workflow produced well-formed output".
"""

from __future__ import annotations

import json
from pathlib import Path

from application_sdk.testing.integration.invariants import (
    AttributeNotNull,
    NonEmptyOutput,
    QualifiedNamePrefix,
    RequiredAttributes,
    UniqueQualifiedName,
    check_invariants,
    load_entities,
)


def _entity(type_name: str, qualified_name: str, **attributes) -> dict:
    return {
        "typeName": type_name,
        "attributes": {"qualifiedName": qualified_name, **attributes},
    }


# --------------------------------------------------------------------------- #
# Individual invariants
# --------------------------------------------------------------------------- #


def test_unique_qualified_name_passes_when_unique():
    entities = [_entity("Table", "db/s/t1"), _entity("Table", "db/s/t2")]
    assert UniqueQualifiedName().check(entities) == []


def test_unique_qualified_name_flags_duplicates():
    entities = [_entity("Table", "db/s/t1"), _entity("Table", "db/s/t1")]
    violations = UniqueQualifiedName().check(entities)
    assert len(violations) == 1
    assert "db/s/t1" in violations[0]


def test_unique_qualified_name_ignores_same_qn_across_types():
    # Same qualifiedName under different typeNames is not a duplicate.
    entities = [_entity("Table", "db/s/x"), _entity("View", "db/s/x")]
    assert UniqueQualifiedName().check(entities) == []


def test_non_empty_output():
    assert NonEmptyOutput().check([]) != []
    assert NonEmptyOutput().check([_entity("Table", "db/s/t")]) == []


def test_non_empty_output_per_type():
    entities = [_entity("Table", "db/s/t")]
    assert NonEmptyOutput(type_name="Column").check(entities) != []
    assert NonEmptyOutput(type_name="Table").check(entities) == []


def test_required_attributes_defaults_to_qualified_name():
    good = [_entity("Table", "db/s/t")]
    assert RequiredAttributes().check(good) == []

    missing_qn = [{"typeName": "Table", "attributes": {"name": "t"}}]
    assert RequiredAttributes().check(missing_qn) != []


def test_required_attributes_flags_missing_type_name():
    entities = [{"attributes": {"qualifiedName": "db/s/t"}}]
    violations = RequiredAttributes().check(entities)
    assert any("typeName" in v for v in violations)


def test_required_attributes_custom_set():
    entities = [_entity("Table", "db/s/t")]  # has no connectionQualifiedName
    violations = RequiredAttributes("connectionQualifiedName").check(entities)
    assert len(violations) == 1


def test_qualified_name_prefix():
    entities = [_entity("Table", "default/pg/1/db/s/t"), _entity("Table", "wrong/x")]
    violations = QualifiedNamePrefix("default/pg/1/").check(entities)
    assert len(violations) == 1
    assert "wrong/x" in violations[0]


def test_attribute_not_null_only_checks_its_type():
    entities = [
        _entity("Schema", "db/s1", tableCount=3),
        _entity("Schema", "db/s2", tableCount=None),
        _entity("Table", "db/s1/t"),  # different type, ignored
    ]
    violations = AttributeNotNull("Schema", "tableCount").check(entities)
    assert len(violations) == 1
    assert "db/s2" in violations[0]


# --------------------------------------------------------------------------- #
# Loading + running
# --------------------------------------------------------------------------- #


def _write_ndjson(path: Path, entities: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(e) for e in entities), encoding="utf-8")


def test_load_entities_reads_ndjson(tmp_path):
    root = tmp_path / "transformed"
    _write_ndjson(root / "Table" / "chunk-0.json", [_entity("Table", "db/s/t1")])
    _write_ndjson(
        root / "Column" / "chunk-0.json",
        [_entity("Column", "db/s/t1/c1"), _entity("Column", "db/s/t1/c2")],
    )
    entities = load_entities(str(root))
    assert len(entities) == 3
    assert {e["typeName"] for e in entities} == {"Table", "Column"}


def test_load_entities_accepts_entities_envelope(tmp_path):
    root = tmp_path / "transformed"
    path = root / "Table" / "chunk-0.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"entities": [_entity("Table", "db/s/t1")]}), encoding="utf-8"
    )
    assert len(load_entities(str(root))) == 1


def test_check_invariants_end_to_end_pass(tmp_path):
    root = tmp_path / "transformed"
    _write_ndjson(
        root / "Table" / "chunk-0.json",
        [_entity("Table", "db/s/t1"), _entity("Table", "db/s/t2")],
    )
    report = check_invariants(
        str(root), [UniqueQualifiedName(), NonEmptyOutput(), RequiredAttributes()]
    )
    assert report.ok
    assert report.total_entities == 2
    assert report.violation_count == 0


def test_check_invariants_end_to_end_fail(tmp_path):
    root = tmp_path / "transformed"
    _write_ndjson(
        root / "Table" / "chunk-0.json",
        [_entity("Table", "db/s/t1"), _entity("Table", "db/s/t1")],  # duplicate
    )
    report = check_invariants(str(root), [UniqueQualifiedName()])
    assert not report.ok
    assert report.violation_count == 1
    assert "FAIL" in report.format_report()


def test_check_invariants_missing_path_reports_violation(tmp_path):
    report = check_invariants(str(tmp_path / "does-not-exist"), [NonEmptyOutput()])
    assert not report.ok
    assert report.total_entities == 0
