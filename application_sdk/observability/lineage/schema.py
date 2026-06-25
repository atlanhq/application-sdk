"""Pydantic models for the lineage-observability artifacts.

These are the v2 enriched contract written to the object store and consumed by
the reduce/stitch/telemetry layers and by reactive RCA tooling. They are a
superset of the argo per-asset / coverage shapes, adding run identity, the
``stage`` dimension, ``lineageStatus`` (incl. PARTIAL/PENDING), a category
rollup, and the ARS stitch key.

Models serialize to camelCase (``by_alias=True``) to match the argo JSON shape
and can be constructed from either snake_case or camelCase input.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from application_sdk.observability.lineage.identity import IDENTITY_SCHEMA_VERSION
from application_sdk.observability.lineage.types import LineageStatus, Stage

ASSET_SCHEMA_VERSION = 2
COVERAGE_SCHEMA_VERSION = 2


class _CamelModel(BaseModel):
    model_config = ConfigDict(
        populate_by_name=True, alias_generator=to_camel, extra="ignore"
    )


class ArsEdgeInfo(_CamelModel):
    """ARS stitch context attached to an ARS-bearing per-asset record."""

    direction: Optional[str] = None
    ordinal: Optional[int] = None
    components_hash: Optional[str] = None
    components: Optional[Dict[str, Any]] = None
    edge_intent: str = "required"
    resolution_status: Optional[str] = None


class AssetRecord(_CamelModel):
    """One per-asset coverage record (a line in ``*-assets.jsonl``)."""

    schema_version: int = ASSET_SCHEMA_VERSION
    identity_schema_version: int = IDENTITY_SCHEMA_VERSION
    run_id: str = ""
    workflow_id: str = ""
    tenant: str = ""
    connector_type: str = ""
    stage: str = Stage.TRANSFORM.value
    activity_id: str = ""
    asset_type: str
    asset_id: str
    qualified_name: Optional[str] = None
    should_have_lineage: bool = True
    lineage_status: LineageStatus = LineageStatus.MISSING_LINEAGE
    has_lineage: bool = False
    reason: Optional[str] = None
    category: Optional[str] = None
    reason_details: Optional[Dict[str, Any]] = None
    failed_paths: Optional[List[Dict[str, Any]]] = None
    ars: Optional[ArsEdgeInfo] = None
    created_at: Optional[str] = None


class CoverageSummary(_CamelModel):
    """The run-level coverage summary (``coverage.json``).

    A superset of the argo ``build_output()`` shape; the argo sub-keys
    (``totals``/``coverage``/``totalsByType``/…) are carried as nested dicts so a
    merged tracker output can be embedded verbatim.
    """

    schema_version: int = COVERAGE_SCHEMA_VERSION
    run_id: str = ""
    workflow_id: str = ""
    tenant: str = ""
    connector_type: str = ""
    stage: str = Stage.END_TO_END.value
    created_at: Optional[str] = None
    totals: Dict[str, int] = Field(default_factory=dict)
    coverage: Dict[str, float] = Field(default_factory=dict)
    totals_by_type: Dict[str, Any] = Field(default_factory=dict)
    totals_by_category: Dict[str, int] = Field(default_factory=dict)
    totals_by_stage_category: Dict[str, int] = Field(default_factory=dict)
    totals_by_reason: Dict[str, int] = Field(default_factory=dict)
    totals_by_type_and_reason: Dict[str, Any] = Field(default_factory=dict)
    totals_by_failed_path: Dict[str, int] = Field(default_factory=dict)
    partials_reduced: int = 0
    expected_partials: int = 0
    partials_missing: List[str] = Field(default_factory=list)
    observability_enabled: bool = True
    config: Dict[str, Any] = Field(default_factory=dict)


__all__ = [
    "ASSET_SCHEMA_VERSION",
    "COVERAGE_SCHEMA_VERSION",
    "ArsEdgeInfo",
    "AssetRecord",
    "CoverageSummary",
]
