"""Light traces for APPLICATION_MODE=SERVER to avoid loading pandas/dapr/observability."""

from typing import Any, Dict, List, Optional


class _NoOpTraces:
    """No-op traces adapter. record_trace is a no-op."""

    def record_trace(
        self,
        name: str,
        trace_id: str,
        span_id: str,
        kind: str,
        status_code: str,
        attributes: Dict[str, Any],
        parent_span_id: Optional[str] = None,
        status_message: Optional[str] = None,
        events: Optional[List[Dict[str, Any]]] = None,
        duration_ms: Optional[float] = None,
    ) -> None:
        pass


_instance: Optional[_NoOpTraces] = None


def get_traces() -> _NoOpTraces:
    global _instance
    if _instance is None:
        _instance = _NoOpTraces()
    return _instance
