"""Data models for observability metrics.

This module contains Pydantic models and enums used across the observability system.
Separated from metrics_adaptor.py to avoid circular dependencies.
"""

from enum import Enum
from time import time as get_current_time
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

    class Config:
        """Configuration for the MetricRecord Pydantic model.

        Provides custom parsing logic to ensure consistent data types and structure
        for metric records, including validation and type conversion for all fields.
        """

        @classmethod
        def parse_obj(cls, obj):
            if isinstance(obj, dict):
                # Ensure labels is a dictionary with consistent structure
                if "labels" in obj:
                    # Create a new labels dict with only the expected fields
                    new_labels = {}
                    expected_fields = [
                        "database",
                        "status",
                        "type",
                        "mode",
                        "workflow_id",
                        "workflow_type",
                    ]

                    # Copy only the expected fields if they exist
                    for field in expected_fields:
                        if field in obj["labels"]:
                            new_labels[field] = str(obj["labels"][field])

                    obj["labels"] = new_labels

                # Ensure value is float
                if "value" in obj:
                    try:
                        obj["value"] = float(obj["value"])
                    except (ValueError, TypeError):
                        obj["value"] = 0.0

                # Ensure timestamp is float
                if "timestamp" in obj:
                    try:
                        obj["timestamp"] = float(obj["timestamp"])
                    except (ValueError, TypeError):
                        obj["timestamp"] = get_current_time()

                # Ensure type is MetricType
                if "type" in obj:
                    try:
                        obj["type"] = MetricType(obj["type"])
                    except ValueError:
                        obj["type"] = MetricType.COUNTER

                # Ensure name is string
                if "name" in obj:
                    obj["name"] = str(obj["name"])

                # Ensure description is string or None
                if "description" in obj and obj["description"] is not None:
                    obj["description"] = str(obj["description"])

                # Ensure unit is string or None
                if "unit" in obj and obj["unit"] is not None:
                    obj["unit"] = str(obj["unit"])

            return super().parse_obj(obj)
