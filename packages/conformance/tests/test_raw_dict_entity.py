"""Tests for P058 RawDictEntity (FND-3929).

Exercised through the P-series ``scan_text`` so the real wiring (scope gate,
suppression handling) is covered, not just the detector.  The positive and
negative fixtures are the shapes real apps use: the Dataplex aspect-type
catalog as it was before it moved to pyatlan_v9, and the raw-dict sites in the
openapi and metabase reference apps that are not emitted entities.
"""

from __future__ import annotations

import textwrap

import pytest
from conformance.suite.checks.prescriptions import scan_text as p_scan
from conformance.suite.rules.prescriptions import RULES
from conformance.suite.schema.catalog import RuleScope
from conformance.suite.schema.disposition import EnforcementTier
from conformance.suite.schema.findings import Finding


def _p058(src: str, file: str = "app/connector.py") -> list[Finding]:
    return [f for f in p_scan(textwrap.dedent(src), file) if f.rule_id == "P058"]


def _unsuppressed(src: str, file: str = "app/connector.py") -> list[Finding]:
    return [f for f in _p058(src, file) if not f.suppressed]


def test_registered_as_warn_app_scoped_not_autofixable() -> None:
    rule = next(r for r in RULES if r.id == "P058")
    assert rule.name == "RawDictEntity"
    assert rule.tier is EnforcementTier.WARN
    assert rule.scope is RuleScope.APP
    assert not rule.autofixable


# ── Positive: emitted raw-dict entities ──────────────────────────────────────────

_DATAPLEX_ASPECT_TYPES = """
    def build_aspect_type_entities(aspect_types, tenant_id, conn_qn):
        entities = []
        for at in aspect_types:
            entities.append({
                "typeName": "GCPDataplexAspectType",
                "attributes": {
                    "tenantId": tenant_id,
                    "name": at["name"],
                    "qualifiedName": f"{conn_qn}/aspect-type/{at['name']}",
                    "connectionQualifiedName": conn_qn,
                },
            })
        return entities
"""


def test_fires_on_dataplex_aspect_type_append() -> None:
    findings = _p058(_DATAPLEX_ASPECT_TYPES)
    assert len(findings) == 1
    finding = findings[0]
    assert finding.line == 5
    assert "`GCPDataplexAspectType` entity built as a raw dict" in finding.message
    assert "pyatlan_v9.model.assets" in finding.message
    assert '"status": "ACTIVE"' in finding.message
    assert "ignore[P058]" in finding.message


@pytest.mark.parametrize(
    "src",
    [
        'out.insert(0, {"typeName": "Table", "attributes": {}})\n',
        'out.extend([{"typeName": "Table", "attributes": {}}])\n',
        'out.extend({"typeName": "Table", "attributes": a} for a in rows)\n',
        'line = entity_bytes({"typeName": "Table", "attributes": {}})\n',
        'line = sdk.entity_bytes({"typeName": "Table", "attributes": {}})\n',
        'out.append(dict(typeName="Table", attributes={}))\n',
        'def rows():\n    yield {"typeName": "Table", "attributes": {}}\n',
        'def map_table(r):\n    return {"typeName": "Table", "attributes": {}}\n',
        'async def map_table(r):\n    return {"typeName": "Table", "attributes": {}}\n',
    ],
    ids=[
        "insert",
        "extend-list",
        "extend-genexp",
        "entity-bytes",
        "entity-bytes-attr",
        "dict-call",
        "yield",
        "mapper-return",
        "async-mapper-return",
    ],
)
def test_fires_on_each_emit_site(src: str) -> None:
    assert len(_p058(src)) == 1


def test_fires_through_a_local_name() -> None:
    src = """
        def enrich(rows):
            out = []
            for r in rows:
                entity = {"typeName": "Column", "attributes": r}
                out.append(entity)
            return out
    """
    findings = _p058(src)
    assert [f.line for f in findings] == [5]
    assert "`Column` entity" in findings[0].message


def test_dynamic_type_name_is_still_flagged() -> None:
    src = 'out.append({"typeName": entity_type, "attributes": {}})\n'
    findings = _p058(src)
    assert len(findings) == 1
    assert findings[0].message.startswith("entity built as a raw dict")


def test_flags_status_bearing_dict_too() -> None:
    # A raw dict that already carries status still bypasses pyatlan_v9; the
    # sanctioned end state for an unmodelled type is a justified ignore.
    src = 'out.append({"typeName": "T", "status": "ACTIVE", "attributes": {}})\n'
    assert len(_p058(src)) == 1


def test_one_finding_per_dict_emitted_twice() -> None:
    src = """
        def f(out, log):
            e = {"typeName": "Table", "attributes": {}}
            out.append(e)
            log.append(e)
    """
    assert len(_p058(src)) == 1


# ── Negative: the same shape where it is not an emitted entity ─────────────────


def test_silent_on_openapi_connection_ref() -> None:
    # atlan-openapi-app app/connector.py — a ref validated into ConnectionRef.
    src = """
        connection = ConnectionRef.model_validate(
            {"typeName": "Connection", "attributes": {"qualifiedName": conn_qn}}
        )
    """
    assert _p058(src) == []


def test_silent_on_metabase_ars_ref_builder() -> None:
    # atlan-metabase-app app/lineage/ars_builder.py — a ref nested into a
    # Process's inputs, returned from a builder that is not a mapper.
    src = """
        def build_partial_table_ref(*, table_name, qn):
            return {
                "typeName": "Table",
                "attributes": {"name": table_name, "qualifiedName": qn},
            }

        def build_process(source_tables):
            inputs = [build_partial_table_ref(**t) for t in source_tables]
            return {"typeName": "Process", "status": "ACTIVE",
                    "attributes": {"inputs": inputs}}
    """
    assert _p058(src) == []


def test_silent_on_struct_stored_under_subscript() -> None:
    # Struct values share the entity shape; storing one in a map is not an emit.
    src = """
        details[aspect_id] = {
            "typeName": "AssetGCPDataplexAspectMetadata",
            "attributes": {"assetGCPDataplexAspectDisplayName": name},
        }
    """
    assert _p058(src) == []


def test_silent_on_struct_nested_in_appended_dict() -> None:
    src = """
        out.append({
            "name": n,
            "meta": {"typeName": "SomeStruct", "attributes": {}},
        })
    """
    assert _p058(src) == []


@pytest.mark.parametrize(
    "src",
    [
        'out.append({"typeName": "Table", "uniqueAttributes": {"qualifiedName": q}})\n',
        'out.append({"typeName": "Table"})\n',
        'out.append({"attributes": {}})\n',
        'search(where={"typeName": "Table", "attributes": {}})\n',
    ],
    ids=["unique-attributes-ref", "type-name-only", "attributes-only", "kwarg"],
)
def test_silent_on_non_entity_shapes(src: str) -> None:
    assert _p058(src) == []


def test_silent_on_return_from_non_mapper() -> None:
    src = 'def helper():\n    return {"typeName": "Table", "attributes": {}}\n'
    assert _p058(src) == []


def test_name_binding_does_not_leak_across_functions() -> None:
    src = """
        def a():
            e = {"typeName": "Table", "attributes": {}}
            return e

        def b(out, e):
            out.append(e)
    """
    assert _p058(src) == []


@pytest.mark.parametrize(
    "file",
    ["app/generated/models.py", "tests/test_x.py", "scripts/seed.py"],
)
def test_silent_outside_hand_written_app_code(file: str) -> None:
    assert _p058(_DATAPLEX_ASPECT_TYPES, file) == []


# ── Suppression ─────────────────────────────────────────────────────────────────


def test_inline_ignore_with_reason_suppresses() -> None:
    src = """
        # conformance: ignore[P058] GCPDataplexAspectType is not modelled in pyatlan_v9
        out.append({"typeName": "GCPDataplexAspectType", "status": "ACTIVE", "attributes": {}})
    """
    findings = _p058(src)
    assert len(findings) == 1
    assert findings[0].suppressed
    assert _unsuppressed(src) == []
