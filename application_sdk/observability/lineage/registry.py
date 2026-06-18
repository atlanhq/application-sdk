"""Reason-code registries and category resolution.

A registry maps connector-specific :class:`ReasonCode` codes to the shared
:class:`ReasonCategory`. Connectors own their own registry (in the connector
repo) so a taxonomy change does not force an SDK release. The SDK ships only the
cross-cutting registries: :data:`ARS_REASON_CODES` (the connector-agnostic
publish/ARS boundary) and the seed :data:`TABLEAU_REASON_CODES` (the reference
connector, ported verbatim from the argo framework).

Governance: ``record_missing_reason`` accepts any string verbatim. The
emit/reduce layers call :func:`get_reason_category` and WARN (never raise) on an
unregistered code, bucketing it into :attr:`ReasonCategory.UNINSTRUMENTED_PATH`
for the metric rollup. One code maps to exactly one category.
"""

from __future__ import annotations

import logging
from typing import Dict, Iterable, Mapping, Optional, Union

from application_sdk.observability.lineage.types import ReasonCategory, ReasonCode

LOGGER = logging.getLogger(__name__)


class ReasonCodeRegistry:
    """A validated mapping of reason code → :class:`ReasonCode`.

    Thin wrapper over a plain dict that adds category resolution and a
    drift-warning lookup. Construct from a code dict, or merge several.
    """

    def __init__(self, codes: Optional[Mapping[str, ReasonCode]] = None) -> None:
        self._codes: Dict[str, ReasonCode] = {}
        if codes:
            self.merge(codes)

    @classmethod
    def from_codes(cls, codes: Iterable[ReasonCode]) -> "ReasonCodeRegistry":
        return cls({rc.code: rc for rc in codes})

    def merge(self, codes: Mapping[str, ReasonCode]) -> "ReasonCodeRegistry":
        for key, rc in codes.items():
            if key != rc.code:
                raise ValueError(
                    f"Registry key {key!r} does not match ReasonCode.code {rc.code!r}"
                )
            if not isinstance(rc.category, ReasonCategory):
                raise ValueError(f"{key!r} maps to non-ReasonCategory {rc.category!r}")
            self._codes[key] = rc
        return self

    def get(self, code: str) -> Optional[ReasonCode]:
        return self._codes.get(code)

    def category(self, code: str) -> ReasonCategory:
        """Resolve *code* to its category, warning + defaulting on drift."""
        rc = self._codes.get(code)
        if rc is not None:
            return rc.category
        LOGGER.warning(
            "Unregistered lineage reason code %r; bucketing as UNINSTRUMENTED_PATH",
            code,
        )
        return ReasonCategory.UNINSTRUMENTED_PATH

    def __contains__(self, code: object) -> bool:
        return code in self._codes

    def __len__(self) -> int:
        return len(self._codes)

    def as_dict(self) -> Dict[str, ReasonCode]:
        return dict(self._codes)


def get_reason_category(
    reason_code: str,
    reason_registry: Union[Mapping[str, ReasonCode], ReasonCodeRegistry, None] = None,
) -> Optional[ReasonCategory]:
    """Look up the category for a reason code string.

    Returns ``None`` (argo-compatible) when no registry is supplied or the code
    is unknown. For the warn-and-default behavior, use
    :meth:`ReasonCodeRegistry.category` instead.
    """
    if reason_registry is None:
        return None
    if isinstance(reason_registry, ReasonCodeRegistry):
        rc = reason_registry.get(reason_code)
        return rc.category if rc else None
    rc = reason_registry.get(reason_code)
    return rc.category if rc else None


# ---------------------------------------------------------------------------
# Shared publish/ARS-boundary registry (connector-agnostic — the resolver is)
# ---------------------------------------------------------------------------

ARS_REASON_CODES: Dict[str, ReasonCode] = {
    "ARS_EDGE_DROPPED": ReasonCode(
        code="ARS_EDGE_DROPPED",
        category=ReasonCategory.ASSET_NOT_FOUND,
        message="ARS edge had no cache match and noMatchAction=drop discarded it",
    ),
    "ARS_EDGE_NO_MATCH_TYPENAMES": ReasonCode(
        code="ARS_EDGE_NO_MATCH_TYPENAMES",
        category=ReasonCategory.MISSING_METADATA,
        message="ARS edge had empty matchTypeNames so no resolution was attempted",
    ),
    "ARS_EDGE_CASE_MISMATCH": ReasonCode(
        code="ARS_EDGE_CASE_MISMATCH",
        category=ReasonCategory.NO_SEARCHABLE_DIALECT,
        message="ARS edge components differ from cache only by identifier case",
    ),
    "ARS_PARENT_DROPPED_EMPTY_FIELD": ReasonCode(
        code="ARS_PARENT_DROPPED_EMPTY_FIELD",
        category=ReasonCategory.NO_UPSTREAM_DATA,
        message="Whole Process dropped because all its edges resolved to empty (arsNoNestedMatchAction=drop)",
    ),
    "ARS_PARENT_ALL_EDGES_DROPPED": ReasonCode(
        code="ARS_PARENT_ALL_EDGES_DROPPED",
        category=ReasonCategory.RELATIONSHIP_FALLBACK,
        message="Every edge of the parent Process was dropped at resolution",
    ),
    "ARS_MALFORMED_IDENTITY": ReasonCode(
        code="ARS_MALFORMED_IDENTITY",
        category=ReasonCategory.MISSING_METADATA,
        message="arsIdentity block was malformed and could not be routed",
    ),
    "ARS_ENTITY_UNROUTED": ReasonCode(
        code="ARS_ENTITY_UNROUTED",
        category=ReasonCategory.ASSET_NOT_FOUND,
        message="Entity-level arsIdentity (case a) found no routing target",
    ),
    "ARS_CACHE_NOT_DISCOVERED": ReasonCode(
        code="ARS_CACHE_NOT_DISCOVERED",
        category=ReasonCategory.CACHE_MISS,
        message="Upstream connector publish-cache was not discovered/materialized",
    ),
    "ARS_CONNECTOR_NOT_REFERENCED": ReasonCode(
        code="ARS_CONNECTOR_NOT_REFERENCED",
        category=ReasonCategory.CACHE_MISS,
        message="Referenced upstream connector is not present in the cache view",
    ),
    "ARS_ACTION_UNIMPLEMENTED": ReasonCode(
        code="ARS_ACTION_UNIMPLEMENTED",
        category=ReasonCategory.CONNECTOR_LIMITATION,
        message="Edge requested a noMatchAction (e.g. create_lineage) not implemented by the resolver",
    ),
    "ATLAS_REMOVE_REL_404": ReasonCode(
        code="ATLAS_REMOVE_REL_404",
        category=ReasonCategory.PROCESS_MAPPING_FAILURE,
        message="PHASE_TWO relationship removal returned 404 (entity already gone)",
    ),
    "DIFF_SYNCED_NO_PUBLISH": ReasonCode(
        code="DIFF_SYNCED_NO_PUBLISH",
        category=ReasonCategory.PROCESS_MAPPING_FAILURE,
        message="Diff marked the Process synced but it was never published",
    ),
}


# ---------------------------------------------------------------------------
# Tableau reason-code registry (seed / reference — ported verbatim from argo)
# ---------------------------------------------------------------------------

TABLEAU_REASON_CODES: Dict[str, ReasonCode] = {
    # --- Datasource reasons ---
    "DATASOURCE_MISSING_SITE_ID": ReasonCode(
        code="DATASOURCE_MISSING_SITE_ID",
        category=ReasonCategory.MISSING_METADATA,
        message="Datasource record has no site_id",
    ),
    "DATASOURCE_MISSING_PROJECT_ID": ReasonCode(
        code="DATASOURCE_MISSING_PROJECT_ID",
        category=ReasonCategory.MISSING_METADATA,
        message="Datasource record has no project_id",
    ),
    "DATASOURCE_MISSING_WORKBOOK_ID": ReasonCode(
        code="DATASOURCE_MISSING_WORKBOOK_ID",
        category=ReasonCategory.MISSING_METADATA,
        message="Non-embedded datasource has no workbook_id",
    ),
    "DATASOURCE_HAS_NO_UPSTREAM_TABLES": ReasonCode(
        code="DATASOURCE_HAS_NO_UPSTREAM_TABLES",
        category=ReasonCategory.NO_UPSTREAM_DATA,
        message="No upstream tables and no custom SQL table available",
    ),
    "CUSTOM_SQL_EMPTY_QUERY": ReasonCode(
        code="CUSTOM_SQL_EMPTY_QUERY",
        category=ReasonCategory.NO_UPSTREAM_DATA,
        message="Custom SQL table exists but query string is empty",
    ),
    "CUSTOM_SQL_PARSER_FAILURE": ReasonCode(
        code="CUSTOM_SQL_PARSER_FAILURE",
        category=ReasonCategory.PARSER_FAILURE,
        message="Custom SQL query parsing threw an exception",
    ),
    "UPSTREAM_TABLE_NAME_MISSING": ReasonCode(
        code="UPSTREAM_TABLE_NAME_MISSING",
        category=ReasonCategory.MISSING_METADATA,
        message="Upstream table found but name extraction failed",
    ),
    "NO_SEARCHABLE_DIALECT": ReasonCode(
        code="NO_SEARCHABLE_DIALECT",
        category=ReasonCategory.NO_SEARCHABLE_DIALECT,
        message="All upstream connectionType values are empty; no dialect for cache lookup",
    ),
    "CONNECTION_CACHE_MISS": ReasonCode(
        code="CONNECTION_CACHE_MISS",
        category=ReasonCategory.CACHE_MISS,
        message="Upstream tables exist but no matching SQL assets in connection cache",
    ),
    "DATASOURCE_UPSTREAM_DATASOURCE_NOT_FOUND_HAS_SQL_FALLBACK": ReasonCode(
        code="DATASOURCE_UPSTREAM_DATASOURCE_NOT_FOUND_HAS_SQL_FALLBACK",
        category=ReasonCategory.ASSET_NOT_FOUND,
        message="Upstream datasource missing but has custom SQL fallback",
    ),
    "DATASOURCE_UPSTREAM_DATASOURCE_NOT_FOUND_NO_SQL_FALLBACK": ReasonCode(
        code="DATASOURCE_UPSTREAM_DATASOURCE_NOT_FOUND_NO_SQL_FALLBACK",
        category=ReasonCategory.ASSET_NOT_FOUND,
        message="Upstream datasource missing with no alternative lineage path",
    ),
    # --- Field reasons ---
    "FIELD_MISSING_DATASOURCE_ID": ReasonCode(
        code="FIELD_MISSING_DATASOURCE_ID",
        category=ReasonCategory.MISSING_METADATA,
        message="Field has no datasource.id",
    ),
    "FIELD_MISSING_DATASOURCE_TABLES": ReasonCode(
        code="FIELD_MISSING_DATASOURCE_TABLES",
        category=ReasonCategory.NO_UPSTREAM_DATA,
        message="Field's datasource has no tables in mapping",
    ),
    "FIELD_MISSING_PARENT_DATASOURCE_PROCESS": ReasonCode(
        code="FIELD_MISSING_PARENT_DATASOURCE_PROCESS",
        category=ReasonCategory.PROCESS_MAPPING_FAILURE,
        message="Datasource-to-worksheet process not found for field",
    ),
    "FIELD_CONNECTION_CACHE_MISS": ReasonCode(
        code="FIELD_CONNECTION_CACHE_MISS",
        category=ReasonCategory.CACHE_MISS,
        message="Column search in connection cache returned no results",
    ),
    # --- Worksheet reasons ---
    "WORKSHEET_HAS_NO_UPSTREAM_DATASOURCES": ReasonCode(
        code="WORKSHEET_HAS_NO_UPSTREAM_DATASOURCES",
        category=ReasonCategory.NO_UPSTREAM_DATA,
        message="Worksheet upstreamDatasources is empty",
    ),
    "WORKSHEET_NO_VALID_UPSTREAM_DATASOURCES": ReasonCode(
        code="WORKSHEET_NO_VALID_UPSTREAM_DATASOURCES",
        category=ReasonCategory.ASSET_NOT_FOUND,
        message="All upstream datasource references were invalid",
    ),
    # --- Worksheet field reasons ---
    "WORKSHEET_FIELD_HAS_NO_UPSTREAM_DATASOURCE": ReasonCode(
        code="WORKSHEET_FIELD_HAS_NO_UPSTREAM_DATASOURCE",
        category=ReasonCategory.NO_UPSTREAM_DATA,
        message="Worksheet field has no upstreamDatasource",
    ),
    "WORKSHEET_FIELD_MISSING_UPSTREAM_DATASOURCE_ID": ReasonCode(
        code="WORKSHEET_FIELD_MISSING_UPSTREAM_DATASOURCE_ID",
        category=ReasonCategory.MISSING_METADATA,
        message="Worksheet field upstreamDatasource.id is missing",
    ),
    "WORKSHEET_FIELD_UPSTREAM_DATASOURCE_NOT_FOUND": ReasonCode(
        code="WORKSHEET_FIELD_UPSTREAM_DATASOURCE_NOT_FOUND",
        category=ReasonCategory.ASSET_NOT_FOUND,
        message="Upstream datasource ID not found in datasources_map",
    ),
    "WORKSHEET_FIELD_SOURCE_FIELD_NOT_FOUND": ReasonCode(
        code="WORKSHEET_FIELD_SOURCE_FIELD_NOT_FOUND",
        category=ReasonCategory.ASSET_NOT_FOUND,
        message="Worksheet field not found in datasource/calculated field maps",
    ),
    "MISSING_DATASOURCE_TO_WORKSHEET_PROCESS": ReasonCode(
        code="MISSING_DATASOURCE_TO_WORKSHEET_PROCESS",
        category=ReasonCategory.PROCESS_MAPPING_FAILURE,
        message="Datasource-to-worksheet process mapping is missing",
    ),
    # --- Dashboard reasons ---
    "DASHBOARD_HAS_NO_SHEETS": ReasonCode(
        code="DASHBOARD_HAS_NO_SHEETS",
        category=ReasonCategory.NO_UPSTREAM_DATA,
        message="Dashboard sheets is empty",
    ),
    "DASHBOARD_NO_VALID_UPSTREAM_WORKSHEETS": ReasonCode(
        code="DASHBOARD_NO_VALID_UPSTREAM_WORKSHEETS",
        category=ReasonCategory.ASSET_NOT_FOUND,
        message="All sheet references were invalid",
    ),
    "DASHBOARD_UNPUBLISHED_UPSTREAM_DISABLED": ReasonCode(
        code="DASHBOARD_UNPUBLISHED_UPSTREAM_DISABLED",
        category=ReasonCategory.CONNECTOR_LIMITATION,
        message="Dashboard not in unpublished_worksheets_upstream list",
    ),
    # --- Dashboard field reasons ---
    "DASHBOARD_FIELD_DASHBOARD_NOT_FOUND": ReasonCode(
        code="DASHBOARD_FIELD_DASHBOARD_NOT_FOUND",
        category=ReasonCategory.ASSET_NOT_FOUND,
        message="Dashboard field's dashboard ID not in dashboard map",
    ),
    "DASHBOARD_FIELD_NOT_FOUND": ReasonCode(
        code="DASHBOARD_FIELD_NOT_FOUND",
        category=ReasonCategory.ASSET_NOT_FOUND,
        message="Dashboard field ID not found in dashboard field map",
    ),
    "DASHBOARD_FIELD_UPSTREAM_DATASOURCE_NOT_FOUND": ReasonCode(
        code="DASHBOARD_FIELD_UPSTREAM_DATASOURCE_NOT_FOUND",
        category=ReasonCategory.ASSET_NOT_FOUND,
        message="Datasource referenced by dashboard field is missing",
    ),
    "MISSING_DATASOURCE_TO_DASHBOARD_PROCESS": ReasonCode(
        code="MISSING_DATASOURCE_TO_DASHBOARD_PROCESS",
        category=ReasonCategory.PROCESS_MAPPING_FAILURE,
        message="Datasource-to-dashboard process mapping is missing",
    ),
    "MISSING_WORKSHEET_TO_DASHBOARD_PROCESS": ReasonCode(
        code="MISSING_WORKSHEET_TO_DASHBOARD_PROCESS",
        category=ReasonCategory.PROCESS_MAPPING_FAILURE,
        message="Worksheet-to-dashboard process mapping is missing",
    ),
    # --- Relationship lineage ---
    "WORKBOOK_NO_EMBEDDED_DATASOURCE_RELATIONSHIP": ReasonCode(
        code="WORKBOOK_NO_EMBEDDED_DATASOURCE_RELATIONSHIP",
        category=ReasonCategory.RELATIONSHIP_FALLBACK,
        message="Workbook has no embedded datasource relationship lineage",
    ),
}


__all__ = [
    "ReasonCodeRegistry",
    "get_reason_category",
    "ARS_REASON_CODES",
    "TABLEAU_REASON_CODES",
]
