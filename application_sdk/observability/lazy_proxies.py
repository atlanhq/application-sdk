from typing import Any

from application_sdk.observability.metrics_adaptor import get_metrics
from application_sdk.observability.traces_adaptor import get_traces


class LazyMetricsProxy:
    """Lazy proxy for metrics adapter initialization."""

    def __getattr__(self, name: str) -> Any:
        return getattr(get_metrics(), name)


class LazyTracesProxy:
    """Lazy proxy for traces adapter initialization."""

    def __getattr__(self, name: str) -> Any:
        return getattr(get_traces(), name)
