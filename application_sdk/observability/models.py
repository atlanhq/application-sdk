"""Data models for observability metrics.

This module contains dataclass models and enums used across the observability system.
Separated from metrics_adaptor.py to avoid circular dependencies.
"""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from application_sdk.contracts.base import SerializableEnum


class MetricType(SerializableEnum):
    """Enum for metric types."""

    COUNTER = "counter"
    GAUGE = "gauge"
    HISTOGRAM = "histogram"


@dataclass
class MetricRecord:
    """A dataclass representing a metric record in the system.

    Attributes:
        timestamp (float): Unix timestamp when the metric was recorded
        name (str): Name of the metric
        value (float): Numeric value of the metric
        type (str): Type of metric (counter, gauge, or histogram)
        labels (Dict[str, Any]): Key-value pairs for metric dimensions
        description (Optional[str]): Optional description of the metric
        unit (Optional[str]): Optional unit of measurement
    """

    timestamp: float
    name: str
    value: float
    type: MetricType  # counter, gauge, histogram
    labels: Dict[str, Any]
    description: Optional[str] = None
    unit: Optional[str] = None


@dataclass
class TraceRecord:
    """A dataclass representing a trace record in the system.

    Attributes:
        timestamp (float): Unix timestamp when the trace was recorded
        trace_id (str): Unique identifier for the trace
        span_id (str): Unique identifier for this span
        name (str): Name of the trace/span
        kind (str): Type of span (SERVER, CLIENT, INTERNAL, etc.)
        status_code (str): Status of the trace (OK, ERROR, etc.)
        attributes (Dict[str, Any]): Key-value pairs for trace context
        duration_ms (float): Duration of the trace in milliseconds
        parent_span_id (Optional[str]): ID of the parent span, if any
        status_message (Optional[str]): Additional status information
        events (Optional[list[Dict[str, Any]]]): List of events in the trace
    """

    timestamp: float
    trace_id: str
    span_id: str
    name: str
    kind: str  # SERVER, CLIENT, INTERNAL, etc.
    status_code: str  # OK, ERROR, etc.
    attributes: Dict[str, Any]
    duration_ms: float
    parent_span_id: Optional[str] = None
    status_message: Optional[str] = None
    events: Optional[List[Dict[str, Any]]] = None
