"""Lineage observability framework.

Central, reusable framework for tracking lineage coverage across Atlan BI
connectors and the publish-app. Records a structured reason whenever lineage is
skipped for a lineage-capable asset, yielding proactive coverage metrics and a
reactive per-asset RCA artifact.

This package is the **stable public import surface** for connector repos (which
import application-sdk as a dependency). The in-activity tracker is the argo
engine, ported verbatim; the AE additions (run identity, stage, lineage status,
ARS intent, identity hash, distributed reduce) wrap it without changing its
byte-stable ``build_output``.

Typical use inside a connector transform activity::

    from application_sdk.observability.lineage import create_tracker, ObservabilityConfig

    tracker = create_tracker("tableau", ObservabilityConfig(enabled=flag))
    tracker.register_asset("TableauDatasource", ds_id, qn, should_have_lineage=True)
    if not searchable_dialects:
        tracker.record_missing_reason("TableauDatasource", ds_id, "NO_SEARCHABLE_DIALECT")
    else:
        tracker.mark_output_lineage("TableauDatasource", ds_id, source="upstream_tables")
"""

from __future__ import annotations

from typing import Optional, Union

from application_sdk.observability.lineage.context import (
    get_lineage_tracker,
    reset_lineage_tracker,
    set_lineage_tracker,
)
from application_sdk.observability.lineage.identity import (
    IDENTITY_FIELDS,
    IDENTITY_SCHEMA_VERSION,
    canonical_identity_string,
    components_hash,
    stitch_key,
)
from application_sdk.observability.lineage.metrics import MissingLineageMetrics
from application_sdk.observability.lineage.noop_tracker import (
    NoOpLineageObservabilityTracker,
)
from application_sdk.observability.lineage.registry import (
    ReasonCodeRegistry,
    get_reason_category,
)
from application_sdk.observability.lineage.schema import (
    ArsEdgeInfo,
    AssetRecord,
    CoverageSummary,
)
from application_sdk.observability.lineage.tracker import LineageObservabilityTracker
from application_sdk.observability.lineage.types import (
    IntentEdge,
    LineageStatus,
    ObservabilityConfig,
    ReasonCategory,
    ReasonCode,
    RunContext,
    Stage,
)
# Backward-compatible aliases for the argo / ported-AE Tableau tracker.
LineageCoverageTracker = LineageObservabilityTracker
NoOpLineageCoverageTracker = NoOpLineageObservabilityTracker


def create_tracker(
    connector_type: str = "",
    config: Optional[ObservabilityConfig] = None,
    metrics: Optional[MissingLineageMetrics] = None,
    *,
    run_context: Optional[RunContext] = None,
    registry: Optional[ReasonCodeRegistry] = None,
) -> Union[LineageObservabilityTracker, NoOpLineageObservabilityTracker]:
    """Create the appropriate tracker and register it in the current context.

    Returns a :class:`NoOpLineageObservabilityTracker` when
    ``config.enabled is False`` (zero per-row cost). Otherwise returns a real
    tracker, attaching ``run_context``/``registry`` for the distributed reduce
    and telemetry layers, and registers it so :func:`get_lineage_tracker`
    returns it deeper in the call stack.

    The tracker writes nothing itself: call :meth:`build_output` for the coverage
    summary and :meth:`build_asset_details` for the per-asset records, then
    persist them with the connector's own output layer.
    """
    if config is not None and not config.enabled:
        noop = NoOpLineageObservabilityTracker()
        set_lineage_tracker(noop)
        return noop
    tracker = LineageObservabilityTracker(
        connector_type=connector_type,
        config=config,
        metrics=metrics,
    )
    tracker.run_context = run_context
    tracker.registry = registry
    set_lineage_tracker(tracker)
    return tracker


__all__ = [
    # trackers + factory
    "LineageObservabilityTracker",
    "NoOpLineageObservabilityTracker",
    "create_tracker",
    "LineageCoverageTracker",
    "NoOpLineageCoverageTracker",
    # context
    "get_lineage_tracker",
    "set_lineage_tracker",
    "reset_lineage_tracker",
    # types
    "ReasonCategory",
    "ReasonCode",
    "ObservabilityConfig",
    "LineageStatus",
    "Stage",
    "RunContext",
    "IntentEdge",
    # registry (machinery only — concrete subcode registries live in connectors)
    "ReasonCodeRegistry",
    "get_reason_category",
    # identity
    "components_hash",
    "canonical_identity_string",
    "stitch_key",
    "IDENTITY_SCHEMA_VERSION",
    "IDENTITY_FIELDS",
    # schema
    "AssetRecord",
    "CoverageSummary",
    "ArsEdgeInfo",
    # metrics protocol
    "MissingLineageMetrics",
]
