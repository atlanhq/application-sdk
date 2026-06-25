"""Zero-cost no-op tracker.

Used when observability is disabled (``ObservabilityConfig.enabled=False``).
Every method is a no-op so the connector's instrumentation call sites cost
essentially an attribute lookup in transform hot loops. Ported verbatim from the
argo framework, plus the two additive AE methods.
"""

from __future__ import annotations

from typing import Any, Dict


class NoOpLineageObservabilityTracker:
    """Zero-cost no-op implementation of the tracker interface."""

    def register_asset(self, *args: Any, **kwargs: Any) -> None:
        pass

    def mark_output_lineage(self, *args: Any, **kwargs: Any) -> None:
        pass

    def mark_input_lineage(self, *args: Any, **kwargs: Any) -> None:
        pass

    def record_failed_path_attempt(self, *args: Any, **kwargs: Any) -> None:
        pass

    def record_missing_reason(self, *args: Any, **kwargs: Any) -> None:
        pass

    def apply_relationship_lineage(self, *args: Any, **kwargs: Any) -> None:
        pass

    def emit_intent(self, *args: Any, **kwargs: Any) -> None:
        pass

    def ars_summary(self) -> Dict[str, Any]:
        return {}

    def success_keys(self) -> set:
        return set()

    def build_output(self) -> Dict[str, Any]:
        return {}

    def build_asset_details(self) -> list:
        return []
