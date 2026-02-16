"""Light metrics for APPLICATION_MODE=SERVER to avoid loading pandas/dapr/observability."""

from typing import Any, Dict, Optional

from application_sdk.observability.models import MetricType


class _NoOpMetrics:
    """No-op metrics adapter. record_metric is a no-op."""

    def record_metric(
        self,
        name: str,
        value: float,
        metric_type: Any,
        labels: Dict[str, str],
        description: Optional[str] = None,
        unit: Optional[str] = None,
    ) -> None:
        pass


_instance: Optional[_NoOpMetrics] = None


def get_metrics() -> _NoOpMetrics:
    global _instance
    if _instance is None:
        _instance = _NoOpMetrics()
    return _instance
