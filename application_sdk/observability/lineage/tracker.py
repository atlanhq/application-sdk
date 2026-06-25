"""Lineage coverage tracker.

Ported VERBATIM from the argo lineage-observability framework
(``marketplace_scripts/lineage/observability/tracker.py``). The only changes are
the import roots (severed from ``marketplace_scripts``) and two ADDITIVE methods
(:meth:`emit_intent`, :meth:`success_keys`) that touch only new state — the argo
``build_output``/``_build_asset_output`` behavior is byte-identical and is locked
by the ported regression suite.

Thread safety: single-threaded execution path. Across Temporal activities / pods,
each activity owns its own tracker; partials are merged in a dedicated reduce step
(added in the distributed layer), never by sharing this object.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Set

from application_sdk.observability.lineage.metrics import MissingLineageMetrics
from application_sdk.observability.lineage.types import (
    IntentEdge,
    ObservabilityConfig,
    RunContext,
)

if TYPE_CHECKING:
    from application_sdk.observability.lineage.registry import ReasonCodeRegistry

LOGGER = logging.getLogger(__name__)


class LineageObservabilityTracker:
    """Tracks lineage coverage across assets during connector processing.

    Memory model: two in-memory dicts —
    - asset_index: lightweight state per asset (~100 bytes/asset)
    - asset_details: full details for assets with missing lineage (or all assets
      if log_successful_lineage=True)
    """

    def __init__(
        self,
        connector_type: str = "",
        config: Optional[ObservabilityConfig] = None,
        metrics: Optional[MissingLineageMetrics] = None,
        log_successful_lineage: bool = False,
    ):
        """
        Args:
            connector_type: Identifier for the connector (e.g., "tableau", "sigma").
            config: Observability configuration. If provided, overrides log_successful_lineage.
            metrics: Optional metrics sink. Called on each record_missing_reason().
            log_successful_lineage: Deprecated — use config.log_successful_lineage instead.
                Kept for backward compatibility with existing Tableau instantiation.

        The tracker is a pure in-memory aggregator: it computes coverage
        (:meth:`build_output`) and returns per-asset records
        (:meth:`build_asset_details`). It writes NO files — persistence is the
        connector's job (it already owns its object-store/output layer).
        """
        self.connector_type = connector_type
        if config is not None:
            self.log_successful_lineage = config.log_successful_lineage
        else:
            self.log_successful_lineage = log_successful_lineage
        self.metrics = metrics
        self.asset_index: Dict[str, Dict[str, Any]] = {}
        self.asset_details: Dict[str, Dict[str, Any]] = {}
        self.totals = {
            "totalAssets": 0,
            "shouldHaveLineage": 0,
            "withLineage": 0,
            "missingLineage": 0,
        }
        self.counts_by_type: Dict[str, Dict[str, int]] = {}
        self.counts_by_reason: Dict[str, int] = {}
        self.counts_by_type_reason: Dict[str, Dict[str, int]] = {}
        self.counts_by_failed_path: Dict[str, int] = {}
        # ADDITIVE (AE): ARS intent edges recorded for the publish-side stitch.
        # Does not participate in build_output(); read by the distributed writer.
        # This is also the per-entity "arsIdentity we sent to publish" debug log.
        self.intent_edges: List[Dict[str, Any]] = []
        # ADDITIVE (AE): ARS-dependent assets — bearers of BI->warehouse edges
        # whose Process is minted by the publish-app at resolution time, not by
        # the connector at transform. A SEPARATE bucket: NOT counted as
        # withLineage (the connector created no Process) and NOT counted as
        # missingLineage (the connector did emit the arsIdentity). Demoted out of
        # the shouldHaveLineage denominator so the build_output() coverage ratio
        # reflects native (BI->BI) lineage only.
        self.ars_dependent_keys: Set[str] = set()
        self.counts_ars_dependent_by_type: Dict[str, int] = {}
        # ADDITIVE (AE): run identity + active registry, attached by create_tracker
        # for the distributed writer / telemetry layers. Not used by build_output().
        self.run_context: Optional["RunContext"] = None
        self.registry: Optional["ReasonCodeRegistry"] = None

    @staticmethod
    def _key(asset_type: str, asset_id: str) -> str:
        return f"{asset_type}:{asset_id}"

    def register_asset(
        self,
        asset_type: str,
        asset_id: str,
        qualified_name: Optional[str] = None,
        should_have_lineage: bool = True,
    ):
        if not asset_type or not asset_id:
            LOGGER.warning(
                "Invalid asset registration skipped: asset_type=%s asset_id=%s",
                asset_type,
                asset_id,
            )
            return
        key = self._key(asset_type, asset_id)
        if key not in self.asset_index:
            self.asset_index[key] = {
                "assetType": asset_type,
                "assetId": asset_id,
                "qualifiedName": qualified_name,
                "shouldHaveLineage": should_have_lineage,
                "hasLineage": False,
                "missingRecorded": False,
            }
            self.totals["totalAssets"] += 1
            self.counts_by_type.setdefault(
                asset_type,
                {
                    "totalAssets": 0,
                    "shouldHaveLineage": 0,
                    "withLineage": 0,
                    "missingLineage": 0,
                },
            )
            self.counts_by_type[asset_type]["totalAssets"] += 1
            if should_have_lineage:
                self.totals["shouldHaveLineage"] += 1
                self.counts_by_type[asset_type]["shouldHaveLineage"] += 1
        else:
            state = self.asset_index[key]
            if qualified_name and not state.get("qualifiedName"):
                state["qualifiedName"] = qualified_name
                if key in self.asset_details and not self.asset_details[key].get(
                    "qualifiedName"
                ):
                    self.asset_details[key]["qualifiedName"] = qualified_name
            # Do NOT re-promote an ARS-dependent asset: emit_intent demoted it
            # out of the BI→BI denominator, and a later default-True
            # register_asset (e.g. from the backstop's record_missing_reason, or
            # a second intent edge) must not silently undo that. Only an explicit
            # native mark (_promote_from_ars_dependent) restores it.
            if (
                should_have_lineage
                and not state.get("shouldHaveLineage")
                and not state.get("arsDependent")
            ):
                state["shouldHaveLineage"] = True
                self.totals["shouldHaveLineage"] += 1
                self.counts_by_type[asset_type]["shouldHaveLineage"] += 1

    def _ensure_asset_details(self, key: str) -> Dict[str, Any]:
        if key not in self.asset_details:
            state = self.asset_index[key]
            self.asset_details[key] = {
                "assetType": state["assetType"],
                "assetId": state["assetId"],
                "qualifiedName": state.get("qualifiedName"),
                "shouldHaveLineage": state["shouldHaveLineage"],
                "hasLineage": state["hasLineage"],
            }
        return self.asset_details[key]

    def _clear_missing_state(self, key: str, asset_type: str) -> None:
        """Clear previously recorded missing lineage state and decrement counters."""
        state = self.asset_index.get(key)
        if not state or not state.get("missingRecorded"):
            return
        missing_reason = state.get("missingReason")
        state["missingRecorded"] = False
        state.pop("missingReason", None)
        self.totals["missingLineage"] = max(0, self.totals["missingLineage"] - 1)
        if asset_type in self.counts_by_type:
            self.counts_by_type[asset_type]["missingLineage"] = max(
                0, self.counts_by_type[asset_type]["missingLineage"] - 1
            )
        if missing_reason:
            new_reason_count = self.counts_by_reason.get(missing_reason, 0) - 1
            if new_reason_count <= 0:
                self.counts_by_reason.pop(missing_reason, None)
            else:
                self.counts_by_reason[missing_reason] = new_reason_count
            if asset_type in self.counts_by_type_reason:
                new_type_reason_count = (
                    self.counts_by_type_reason[asset_type].get(missing_reason, 0) - 1
                )
                if new_type_reason_count <= 0:
                    self.counts_by_type_reason[asset_type].pop(missing_reason, None)
                else:
                    self.counts_by_type_reason[asset_type][
                        missing_reason
                    ] = new_type_reason_count
                if not self.counts_by_type_reason[asset_type]:
                    self.counts_by_type_reason.pop(asset_type, None)
        if key in self.asset_details:
            self.asset_details[key].pop("reason", None)
            self.asset_details[key].pop("reasonDetails", None)

    def _promote_from_ars_dependent(self, key: str, asset_type: str) -> None:
        """A native (BI->BI) edge supersedes a prior ARS-dependent classification.

        Restores the asset to the shouldHaveLineage denominator and removes it
        from the ARS-dependent bucket before it is marked covered. No-op for the
        common case (asset was never ARS-dependent), so argo behavior is intact.
        """
        state = self.asset_index.get(key)
        if not state or not state.get("arsDependent"):
            return
        state["arsDependent"] = False
        self.ars_dependent_keys.discard(key)
        if asset_type in self.counts_ars_dependent_by_type:
            self.counts_ars_dependent_by_type[asset_type] = max(
                0, self.counts_ars_dependent_by_type[asset_type] - 1
            )
        if not state.get("shouldHaveLineage"):
            state["shouldHaveLineage"] = True
            self.totals["shouldHaveLineage"] += 1
            if asset_type in self.counts_by_type:
                self.counts_by_type[asset_type]["shouldHaveLineage"] += 1

    def mark_output_lineage(
        self,
        asset_type: str,
        asset_id: str,
        qualified_name: Optional[str] = None,
        source: str = "",
        source_details: Optional[Dict[str, Any]] = None,
    ):
        self.register_asset(asset_type, asset_id, qualified_name)
        key = self._key(asset_type, asset_id)
        state = self.asset_index.get(key)
        if not state:
            LOGGER.warning("Skipping mark_output_lineage for invalid asset: %s", key)
            return
        self._promote_from_ars_dependent(key, asset_type)
        self._clear_missing_state(key, asset_type)
        if not state["hasLineage"]:
            state["hasLineage"] = True
            self.totals["withLineage"] += 1
            self.counts_by_type[asset_type]["withLineage"] += 1
        if not self.log_successful_lineage:
            if key in self.asset_details and not state.get("missingRecorded"):
                self.asset_details.pop(key, None)
            return
        details_entry = self._ensure_asset_details(key)
        details_entry["hasLineage"] = True
        details_entry["lineageSource"] = source

        # Include failed path attempts in source details for diagnostic purposes
        self._apply_lineage_source_details(key, source_details)

    def mark_input_lineage(
        self,
        asset_type: str,
        asset_id: str,
        qualified_name: Optional[str] = None,
        source: str = "",
        source_details: Optional[Dict[str, Any]] = None,
    ):
        self.register_asset(asset_type, asset_id, qualified_name)
        key = self._key(asset_type, asset_id)
        # Mark that asset has input lineage (even if not output lineage)
        state = self.asset_index.get(key)
        if not state:
            LOGGER.warning("Skipping mark_input_lineage for invalid asset: %s", key)
            return
        if not state["hasLineage"]:
            # Only set lineage source if this is the first lineage detected
            if not state.get("lineageSource"):
                state["lineageSource"] = source
                if not self.log_successful_lineage:
                    return
                details_entry = self._ensure_asset_details(key)
                details_entry["lineageSource"] = source
                if source_details:
                    details_entry["lineageSourceDetails"] = source_details

    def record_failed_path_attempt(
        self,
        asset_type: str,
        asset_id: str,
        path: str,
        reason: str,
        details: Optional[Dict[str, Any]] = None,
        qualified_name: Optional[str] = None,
    ):
        """
        Record a diagnostic about a lineage path that was attempted but failed.
        This is for informational purposes and doesn't mark the asset as having missing lineage.
        Use this when a specific lineage path fails but other paths might succeed.
        """
        self.register_asset(asset_type, asset_id, qualified_name)
        key = self._key(asset_type, asset_id)
        state = self.asset_index.get(key)
        if not state:
            LOGGER.warning(
                "Skipping record_failed_path_attempt for invalid asset: %s", key
            )
            return

        failed_path_info = {
            "path": path,
            "reason": reason,
            "details": details or {},
        }
        path_reason = f"{path}:{reason}"
        self.counts_by_failed_path[path_reason] = (
            self.counts_by_failed_path.get(path_reason, 0) + 1
        )

        # Always store failedPaths for assets that don't have lineage yet.
        # record_failed_path_attempt is called *before* record_missing_reason,
        # so missingRecorded won't be set yet — gating on it would discard the
        # details that record_missing_reason's asset entry needs for debugging.
        # Only skip when the asset already has lineage (failed paths are noise).
        if state["hasLineage"] and not self.log_successful_lineage:
            return
        details_entry = self._ensure_asset_details(key)
        if "failedPaths" not in details_entry:
            details_entry["failedPaths"] = []
        details_entry["failedPaths"].append(failed_path_info)

    def record_missing_reason(
        self,
        asset_type: str,
        asset_id: str,
        reason: str,
        details: Optional[Dict[str, Any]] = None,
        qualified_name: Optional[str] = None,
    ):
        """
        Record the reason why an asset has NO lineage at all.
        This should only be called when we're certain no lineage paths will succeed.
        """
        self.register_asset(asset_type, asset_id, qualified_name)
        key = self._key(asset_type, asset_id)
        state = self.asset_index.get(key)
        if not state:
            LOGGER.warning("Skipping record_missing_reason for invalid asset: %s", key)
            return
        # Don't record missing reason if asset already has lineage
        if state["hasLineage"]:
            return
        # Don't record missing for an ARS-dependent asset — it's a separate
        # bucket (the arsIdentity was emitted to publish), neither created nor
        # missing. This is what makes the data-model backstop safe.
        if state.get("arsDependent"):
            return
        if state.get("missingRecorded"):
            return
        state["missingRecorded"] = True
        state["missingReason"] = reason
        self.totals["missingLineage"] += 1
        self.counts_by_type[asset_type]["missingLineage"] += 1
        self.counts_by_reason[reason] = self.counts_by_reason.get(reason, 0) + 1
        self.counts_by_type_reason.setdefault(asset_type, {})
        self.counts_by_type_reason[asset_type][reason] = (
            self.counts_by_type_reason[asset_type].get(reason, 0) + 1
        )
        details_entry = self._ensure_asset_details(key)
        details_entry["hasLineage"] = False
        details_entry["reason"] = reason
        if details:
            details_entry["reasonDetails"] = details
        if self.metrics:
            self.metrics.missing_lineage_event(reason)

    def apply_relationship_lineage(
        self,
        asset_type: str,
        asset_id: str,
        qualified_name: Optional[str],
        source_details: Dict[str, Any],
    ):
        self.register_asset(asset_type, asset_id, qualified_name)
        key = self._key(asset_type, asset_id)
        state = self.asset_index.get(key)
        if not state:
            LOGGER.warning(
                "Skipping apply_relationship_lineage for invalid asset: %s", key
            )
            return
        self._clear_missing_state(key, asset_type)
        if state["hasLineage"]:
            return
        state["hasLineage"] = True
        self.totals["withLineage"] += 1
        self.counts_by_type[asset_type]["withLineage"] += 1
        if not self.log_successful_lineage:
            if key in self.asset_details and not state.get("missingRecorded"):
                self.asset_details.pop(key, None)
            return
        details_entry = self._ensure_asset_details(key)
        details_entry["hasLineage"] = True
        details_entry["lineageSource"] = "relationship_lineage"

        # Include failed path attempts in source details for diagnostic purposes
        self._apply_lineage_source_details(key, source_details)

    def _build_asset_output(self, asset: Dict[str, Any]) -> Dict[str, Any]:
        asset_compact = {
            "assetType": asset["assetType"],
            "assetId": asset["assetId"],
            "qualifiedName": asset.get("qualifiedName"),
            "shouldHaveLineage": asset["shouldHaveLineage"],
            "hasLineage": asset["hasLineage"],
        }

        if not asset["hasLineage"]:
            if asset.get("reason"):
                asset_compact["reason"] = asset["reason"]
            if asset.get("reasonDetails"):
                asset_compact["reasonDetails"] = asset["reasonDetails"]

        if asset["hasLineage"]:
            if asset.get("lineageSource"):
                asset_compact["lineageSource"] = asset["lineageSource"]
            if asset.get("lineageSourceDetails"):
                source_details = asset["lineageSourceDetails"]
                if source_details.get("failedPathAttempts") or any(
                    v for k, v in source_details.items() if k != "failedPathAttempts"
                ):
                    asset_compact["lineageSourceDetails"] = source_details

        if asset.get("failedPaths"):
            asset_compact["failedPaths"] = asset["failedPaths"]

        return asset_compact

    def _apply_lineage_source_details(
        self,
        key: str,
        source_details: Optional[Dict[str, Any]],
    ) -> None:
        # Copy to avoid mutating the caller's dict when adding failedPathAttempts.
        source_details_dict = dict(source_details) if source_details else {}
        existing_failed_paths = self.asset_details.get(key, {}).get("failedPaths")
        if existing_failed_paths:
            source_details_dict["failedPathAttempts"] = existing_failed_paths

        if source_details_dict:
            self.asset_details[key]["lineageSourceDetails"] = source_details_dict

    def build_asset_details(self) -> List[Dict[str, Any]]:
        """Return the per-asset detail records for the connector to persist.

        These are the reactive RCA artifacts: one compact dict per asset that
        has a recorded miss (plus successes when ``log_successful_lineage`` is
        on, and ARS-dependent assets). The SDK ships no writer — the connector
        persists this list however it likes (its own ChunkedOutputHandler,
        object store, etc.).
        """
        return [self._build_asset_output(asset) for asset in self.asset_details.values()]

    def build_output(self) -> Dict[str, Any]:
        totals = self.totals
        totals_by_type_with_coverage: Dict[str, Dict[str, Any]] = {}
        for asset_type, counts in self.counts_by_type.items():
            should_have_coverage = (
                counts["withLineage"] / counts["shouldHaveLineage"]
                if counts["shouldHaveLineage"]
                else 0.0
            ) * 100
            total_coverage = (
                counts["withLineage"] / counts["totalAssets"]
                if counts["totalAssets"]
                else 0.0
            ) * 100
            totals_by_type_with_coverage[asset_type] = {
                **counts,
                "coverage": {
                    "totalLineageCoverage": total_coverage,
                    "shouldHaveLineageCoverage": should_have_coverage,
                },
            }

        should_have_coverage = (
            totals["withLineage"] / totals["shouldHaveLineage"]
            if totals["shouldHaveLineage"]
            else 0.0
        ) * 100
        total_coverage = (
            totals["withLineage"] / totals["totalAssets"]
            if totals["totalAssets"]
            else 0.0
        ) * 100

        return {
            "totals": totals,
            "coverage": {
                "totalLineageCoverage": total_coverage,
                "shouldHaveLineageCoverage": should_have_coverage,
            },
            "totalsByType": totals_by_type_with_coverage,
            "totalsByReason": self.counts_by_reason,
            "totalsByTypeAndReason": self.counts_by_type_reason,
            "totalsByFailedPath": self.counts_by_failed_path,
            "config": {
                "logSuccessfulLineage": self.log_successful_lineage,
                "totalAssetsInOutput": len(self.asset_details),
                "totalAssetsTracked": len(self.asset_index),
            },
        }


    # ------------------------------------------------------------------
    # ADDITIVE (AE) — do not touch build_output() semantics
    # ------------------------------------------------------------------

    def emit_intent(self, edge: IntentEdge) -> None:
        """Record an ARS (BI→warehouse) intent edge — an "ARS-dependent" asset.

        Always logs the exact ``arsIdentity`` (``components`` + canonical
        ``identity_hash``) the connector sent to the publish-app, so a missing
        end-to-end lineage can be traced to what was (or wasn't) requested. The
        full per-edge log is :attr:`intent_edges`.

        Classification: the bearing asset is **ARS-dependent** — its Process is
        minted by the publish-app at resolution time, not by the connector at
        transform. So it is NOT counted as ``withLineage`` (the connector created
        nothing) and NOT counted as ``missingLineage`` (the connector did its
        job). It is demoted out of the ``shouldHaveLineage`` denominator so the
        :meth:`build_output` coverage ratio reflects native (BI→BI) lineage only,
        and is reported as a separate bucket via :meth:`ars_summary`. If the
        asset already has native lineage, the intent is logged for debugging
        without changing its covered status (and :meth:`mark_output_lineage`
        likewise supersedes a prior ARS-dependent classification).
        """
        if not edge.entity_type or not edge.entity_id:
            LOGGER.warning("Skipping emit_intent for invalid edge: %s", edge)
            return
        self.register_asset(edge.entity_type, edge.entity_id, edge.qualified_name)
        key = self._key(edge.entity_type, edge.entity_id)
        self.intent_edges.append(
            {
                "entityType": edge.entity_type,
                "entityId": edge.entity_id,
                "qualifiedName": edge.qualified_name,
                "direction": edge.direction,
                "ordinal": edge.ordinal,
                "components": edge.components,
                "identityHash": edge.identity_hash,
                "matchTypeNames": edge.match_type_names,
                "noMatchAction": edge.no_match_action,
                "edgeIntent": edge.edge_intent,
            }
        )
        state = self.asset_index.get(key)
        if not state or state.get("hasLineage"):
            # Already created natively (or invalid) — identity logged above; do
            # not change the covered status.
            return
        # ARS-dependent: clear any prior miss and demote out of the BI→BI
        # coverage denominator, exactly once per asset.
        self._clear_missing_state(key, edge.entity_type)
        if key not in self.ars_dependent_keys:
            self.ars_dependent_keys.add(key)
            state["arsDependent"] = True
            self.counts_ars_dependent_by_type[edge.entity_type] = (
                self.counts_ars_dependent_by_type.get(edge.entity_type, 0) + 1
            )
            if state.get("shouldHaveLineage"):
                state["shouldHaveLineage"] = False
                self.totals["shouldHaveLineage"] = max(
                    0, self.totals["shouldHaveLineage"] - 1
                )
                if edge.entity_type in self.counts_by_type:
                    self.counts_by_type[edge.entity_type]["shouldHaveLineage"] = max(
                        0,
                        self.counts_by_type[edge.entity_type]["shouldHaveLineage"] - 1,
                    )

    def ars_summary(self) -> Dict[str, Any]:
        """ARS-dependent bucket summary — the separate bucket beside build_output().

        ``arsDependentAssets``  : distinct assets whose lineage is ARS (resolved
                                  at publish), excluded from the coverage ratio.
        ``arsDependentByType``  : per-asset-type breakdown.
        ``arsIntentEdges``      : number of arsIdentities logged in intent_edges.
        """
        return {
            "arsDependentAssets": len(self.ars_dependent_keys),
            "arsDependentByType": dict(self.counts_ars_dependent_by_type),
            "arsIntentEdges": len(self.intent_edges),
        }

    def success_keys(self) -> set:
        """Return the ``{assetType}:{assetId}`` keys that have lineage.

        This is the merge numerator: misses-only per-asset JSONL excludes
        successes, so the distributed reduce recovers the numerator from this
        ledger rather than from the asset-details output.
        """
        return {
            key for key, state in self.asset_index.items() if state.get("hasLineage")
        }
