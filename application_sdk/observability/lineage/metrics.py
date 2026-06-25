"""Metrics protocol for the lineage-observability framework.

Connectors (or the SDK telemetry layer) implement this protocol to forward
missing-lineage reasons to a metrics backend (Prometheus via the SDK metrics
adaptor, Segment/Mixpanel, etc.). The tracker calls ``missing_lineage_event()``
once per asset when ``record_missing_reason()`` is invoked.

Kept verbatim from the argo framework so the proven call contract is unchanged.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class MissingLineageMetrics(Protocol):
    """Protocol for receiving missing-lineage events."""

    def missing_lineage_event(self, reason: str) -> None:
        """Called when a missing-lineage reason is recorded.

        Args:
            reason: The reason code string (e.g., ``"CONNECTION_CACHE_MISS"``).
        """
        ...
