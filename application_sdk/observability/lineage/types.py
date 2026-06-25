"""Core types for the lineage-observability framework.

These types are connector-agnostic and form the stable contract shared by all
BI connectors and the publish-app. The taxonomy split is load-bearing:

- :class:`ReasonCategory` is a **low-cardinality** aggregation bucket — the only
  reason-derived value ever emitted as a metric label. The set is frozen and
  stable across connectors.
- :class:`ReasonCode` is a **high-cardinality**, connector-specific code used for
  per-asset RCA artifacts only — never as a metric label.

The verbatim argo tracker (:mod:`application_sdk.observability.lineage.tracker`)
depends only on :class:`ObservabilityConfig` from this module; everything else
here is additive for the AE/distributed world (run identity, stage dimension,
lineage status, ARS intent edges).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List

from application_sdk.observability.lineage.identity import components_hash


class ReasonCategory(str, Enum):
    """Shared top-level categories for missing-lineage reasons.

    Every connector-specific reason code maps to exactly one category.
    Categories are stable across connectors and used for aggregation/dashboards.
    The set is frozen at 12 (the argo 10 + two AE additions); growing it is a
    deliberate, reviewed change because each member is a metric-label value.
    """

    # --- argo 10 (unchanged) ---
    PARSER_FAILURE = "PARSER_FAILURE"
    ASSET_NOT_FOUND = "ASSET_NOT_FOUND"
    CACHE_MISS = "CACHE_MISS"
    UNSUPPORTED_SOURCE_FEATURE = "UNSUPPORTED_SOURCE_FEATURE"
    MISSING_METADATA = "MISSING_METADATA"
    NO_UPSTREAM_DATA = "NO_UPSTREAM_DATA"
    NO_SEARCHABLE_DIALECT = "NO_SEARCHABLE_DIALECT"
    PROCESS_MAPPING_FAILURE = "PROCESS_MAPPING_FAILURE"
    CONNECTOR_LIMITATION = "CONNECTOR_LIMITATION"
    RELATIONSHIP_FALLBACK = "RELATIONSHIP_FALLBACK"
    # --- AE additions (the only two) ---
    OBSERVABILITY_DISABLED = "OBSERVABILITY_DISABLED"
    """Distinguishes a run with observability OFF from a genuine 0%-coverage run."""
    UNINSTRUMENTED_PATH = "UNINSTRUMENTED_PATH"
    """A code path with no tracker coverage; keeps it from masquerading as 100%."""


class Stage(str, Enum):
    """Where in the pipeline a lineage outcome was observed.

    A low-cardinality **dimension** (not a category): "where it died" is
    orthogonal to "why". One dashboard panel composes ``category x stage``.
    """

    TRANSFORM = "transform"
    PUBLISH = "publish"
    END_TO_END = "end_to_end"


class LineageStatus(str, Enum):
    """Per-asset lineage outcome.

    ``hasLineage`` (boolean, argo) is preserved on every record; ``LineageStatus``
    is the richer enum derived alongside it.
    """

    HAS_LINEAGE = "HAS_LINEAGE"
    """A real Process/ColumnProcess (or relationship lineage) was produced."""
    PARTIAL_LINEAGE = "PARTIAL_LINEAGE"
    """Has lineage AND at least one required edge was dropped (degraded-but-covered)."""
    MISSING_LINEAGE = "MISSING_LINEAGE"
    """Lineage was dropped/absent."""
    PENDING = "PENDING"
    """Transform emitted ARS intent; publish has not yet ruled (pre-stitch only)."""


@dataclass(frozen=True)
class ReasonCode:
    """A connector-specific reason code mapped to a shared category.

    Attributes:
        code: Unique string identifier (e.g., ``"CUSTOM_SQL_EMPTY_QUERY"``). This
            is what gets passed to ``record_missing_reason()``.
        category: The shared :class:`ReasonCategory` for aggregation.
        message: Human-readable description for support/debugging.
    """

    code: str
    category: ReasonCategory
    message: str


@dataclass
class ObservabilityConfig:
    """Configuration for lineage observability tracking.

    Attributes:
        enabled: Master switch. When ``False``, a NoOp tracker is used.
        log_successful_lineage: When ``True``, include assets WITH lineage in the
            per-asset output. When ``False`` (default), only missing-lineage
            assets appear in detailed output (bounded memory at scale).
        emit_intent: When ``True`` (default), record ARS intent edges so the
            publish-side stitch can reconcile end-to-end coverage.
        validate_reason_codes: When ``True`` (default), warn (never raise) on a
            reason code absent from the active registry.
        auto_derive: Opt-in auto-numerator safety net (off the critical path).
        fail_open: When ``True`` (default), any observability error degrades and
            logs but NEVER breaks lineage generation.
    """

    enabled: bool = True
    log_successful_lineage: bool = False
    emit_intent: bool = True
    validate_reason_codes: bool = True
    auto_derive: bool = False
    fail_open: bool = True


@dataclass(frozen=True)
class RunContext:
    """Run identity stamped onto every coverage artifact and metric.

    Sourced from the workflow input contract — crucially ``workflow_id`` is the
    STABLE workflow id (not the per-attempt run id), so partials are not orphaned
    on retry / continue-as-new.
    """

    connector_type: str
    workflow_id: str
    run_id: str = ""
    tenant: str = ""
    stage: str = Stage.TRANSFORM.value
    activity_id: str = ""


@dataclass(frozen=True)
class IntentEdge:
    """An ARS (BI→warehouse) lineage edge emitted unresolved at transform time.

    The connector cannot resolve the warehouse upstream itself; it stamps an
    ``arsIdentity`` (``components``) that the publish-app resolves later. The
    edge is recorded as ``PENDING`` (covered-but-pending, not a miss) and joined
    to the publish-side resolution outcome on
    ``(qualified_name, direction, ordinal, identity_hash)``.
    """

    entity_id: str
    entity_type: str
    qualified_name: str
    direction: str  # "inputs" | "outputs"
    ordinal: int  # derived from the SORTED components tuple, NOT emission order
    components: Dict[str, Any] = field(default_factory=dict)
    match_type_names: List[str] = field(default_factory=list)
    no_match_action: str = "drop"
    edge_intent: str = "required"  # "required" | "best_effort"

    @property
    def identity_hash(self) -> str:
        return components_hash(self.components)
