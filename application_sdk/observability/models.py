"""Data models for observability metrics.

This module contains Pydantic models and enums used across the observability system.
Separated from metrics_adaptor.py to avoid circular dependencies.
"""

from enum import Enum
from typing import Dict, Optional

from pydantic import BaseModel


class MetricType(str, Enum):
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
