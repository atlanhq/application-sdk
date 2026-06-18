"""The v2 artifact schemas (AssetRecord / CoverageSummary)."""

from application_sdk.observability.lineage import (
    AssetRecord,
    CoverageSummary,
    LineageStatus,
)
from application_sdk.observability.lineage.schema import (
    ASSET_SCHEMA_VERSION,
    COVERAGE_SCHEMA_VERSION,
)


def test_asset_record_serializes_to_camel_case():
    rec = AssetRecord(
        asset_type="TableauDatasource",
        asset_id="ds-1",
        qualified_name="default/tableau/site/ds-1",
        should_have_lineage=True,
        has_lineage=False,
        lineage_status=LineageStatus.MISSING_LINEAGE,
        reason="CONNECTION_CACHE_MISS",
        category="CACHE_MISS",
        connector_type="tableau",
    )
    dumped = rec.model_dump(by_alias=True, exclude_none=True)
    assert dumped["assetType"] == "TableauDatasource"
    assert dumped["shouldHaveLineage"] is True
    assert dumped["lineageStatus"] == "MISSING_LINEAGE"
    assert dumped["schemaVersion"] == ASSET_SCHEMA_VERSION


def test_asset_record_accepts_camel_or_snake_input():
    from_camel = AssetRecord.model_validate(
        {"assetType": "Sigma", "assetId": "e-1", "hasLineage": True}
    )
    assert from_camel.asset_type == "Sigma"
    assert from_camel.has_lineage is True


def test_coverage_summary_defaults():
    cov = CoverageSummary(connector_type="looker", workflow_id="wf-1")
    assert cov.schema_version == COVERAGE_SCHEMA_VERSION
    assert cov.observability_enabled is True
    assert cov.partials_missing == []
    dumped = cov.model_dump(by_alias=True)
    assert dumped["connectorType"] == "looker"
    assert "totalsByStageCategory" in dumped
