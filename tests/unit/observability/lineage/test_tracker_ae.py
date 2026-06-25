"""The two additive tracker methods (emit_intent / success_keys) and that they
do not perturb the byte-stable argo build_output()."""

from application_sdk.observability.lineage import (
    IntentEdge,
    LineageObservabilityTracker,
    components_hash,
)


def _edge(entity_id="ds-1", **kw):
    return IntentEdge(
        entity_id=entity_id,
        entity_type="TableauDatasource",
        qualified_name=f"default/tableau/site/{entity_id}",
        direction="inputs",
        ordinal=0,
        components={
            "connectorType": "snowflake",
            "databaseName": "DB",
            "tableName": "T",
        },
        match_type_names=["Table"],
        **kw,
    )


def test_emit_intent_registers_asset_and_records_edge():
    t = LineageObservabilityTracker(connector_type="tableau")
    t.emit_intent(_edge())
    assert "TableauDatasource:ds-1" in t.asset_index
    assert len(t.intent_edges) == 1
    edge = t.intent_edges[0]
    assert edge["direction"] == "inputs"
    assert edge["identityHash"] == components_hash(
        {"connectorType": "snowflake", "databaseName": "DB", "tableName": "T"}
    )
    assert edge["edgeIntent"] == "required"


def test_emit_intent_does_not_mark_has_lineage():
    # ARS intent is PENDING, not a success — must not inflate withLineage.
    t = LineageObservabilityTracker(connector_type="tableau")
    t.emit_intent(_edge())
    assert t.totals["withLineage"] == 0
    assert t.asset_index["TableauDatasource:ds-1"]["hasLineage"] is False


def test_success_keys_returns_lineaged_assets_only():
    t = LineageObservabilityTracker(connector_type="tableau")
    t.register_asset("Ds", "a", should_have_lineage=True)
    t.mark_output_lineage("Ds", "a")
    t.register_asset("Ds", "b", should_have_lineage=True)
    t.record_missing_reason("Ds", "b", reason="CACHE_MISS")
    assert t.success_keys() == {"Ds:a"}


def test_additive_methods_do_not_change_build_output_shape():
    # build_output must remain the verbatim argo shape (no new top-level keys).
    t = LineageObservabilityTracker(connector_type="tableau")
    t.register_asset("Ds", "a", should_have_lineage=True)
    t.mark_output_lineage("Ds", "a")
    t.emit_intent(_edge())  # additive state only
    out = t.build_output()
    assert set(out.keys()) == {
        "totals",
        "coverage",
        "totalsByType",
        "totalsByReason",
        "totalsByTypeAndReason",
        "totalsByFailedPath",
        "config",
    }
