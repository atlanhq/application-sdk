"""Data models for observability metrics.

This module contains Pydantic models and enums used across the observability system.
Separated from metrics_adaptor.py to avoid circular dependencies.
"""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel

from application_sdk.contracts.base import SerializableEnum


class MetricType(SerializableEnum):
    """Enum for metric types."""

    COUNTER = "counter"
    GAUGE = "gauge"
    HISTOGRAM = "histogram"


class MetricRecord(BaseModel):
    """A Pydantic model representing a metric record in the system.

    This model defines the structure for metric data with fields for timestamp,
    name, value, type, labels, and optional description and unit.

    Attributes:
        timestamp (float): Unix timestamp when the metric was recorded
        name (str): Name of the metric
        value (float): Numeric value of the metric
        type (str): Type of metric (counter, gauge, or histogram)
        labels (Dict[str, str]): Key-value pairs for metric dimensions
        description (Optional[str]): Optional description of the metric
        unit (Optional[str]): Optional unit of measurement
    """

    timestamp: float
    name: str
    value: float
    type: MetricType  # counter, gauge, histogram
    labels: Dict[str, str]
    description: Optional[str] = None
    unit: Optional[str] = None


class TraceRecord(BaseModel):
    """A Pydantic model representing a trace record in the system.

    This model defines the structure for distributed tracing data with fields for
    trace identification, timing, status, and additional context.

    Attributes:
        timestamp (float): Unix timestamp when the trace was recorded
        trace_id (str): Unique identifier for the trace
        span_id (str): Unique identifier for this span
        parent_span_id (Optional[str]): ID of the parent span, if any
        name (str): Name of the trace/span
        kind (str): Type of span (SERVER, CLIENT, INTERNAL, etc.)
        status_code (str): Status of the trace (OK, ERROR, etc.)
        status_message (Optional[str]): Additional status information
        attributes (Dict[str, Any]): Key-value pairs for trace context
        events (Optional[list[Dict[str, Any]]]): List of events in the trace
        duration_ms (float): Duration of the trace in milliseconds
    """

    timestamp: float
    trace_id: str
    span_id: str
    parent_span_id: Optional[str] = None
    name: str
    kind: str  # SERVER, CLIENT, INTERNAL, etc.
    status_code: str  # OK, ERROR, etc.
    status_message: Optional[str] = None
    attributes: Dict[str, Any]
    events: Optional[List[Dict[str, Any]]] = None
    duration_ms: float
