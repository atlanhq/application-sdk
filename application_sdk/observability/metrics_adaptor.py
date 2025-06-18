import asyncio
import logging
import threading
from enum import Enum
from time import time
from typing import Any, Dict, Optional

from opentelemetry import metrics
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from pydantic import BaseModel

from application_sdk.constants import (
    ENABLE_OTLP_METRICS,
    METRICS_BATCH_SIZE,
    METRICS_CLEANUP_ENABLED,
    METRICS_FILE_NAME,
    METRICS_FLUSH_INTERVAL_SECONDS,
    METRICS_RETENTION_DAYS,
    OBSERVABILITY_DIR,
    OTEL_BATCH_DELAY_MS,
    OTEL_EXPORTER_OTLP_ENDPOINT,
    OTEL_EXPORTER_TIMEOUT_SECONDS,
    OTEL_RESOURCE_ATTRIBUTES,
    OTEL_WF_NODE_NAME,
    SERVICE_NAME,
    SERVICE_VERSION,
)
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.observability.observability import AtlanObservability
from application_sdk.observability.utils import get_workflow_context


class MetricType(Enum):
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
                        obj["timestamp"] = time()

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
                if "description" in obj:
                    obj["description"] = (
                        str(obj["description"])
                        if obj["description"] is not None
                        else None
                    )

                # Ensure unit is string or None
                if "unit" in obj:
                    obj["unit"] = str(obj["unit"]) if obj["unit"] is not None else None

            return super().parse_obj(obj)


class AtlanMetricsAdapter(AtlanObservability[MetricRecord]):
    """A metrics adapter for Atlan that extends AtlanObservability.

    This adapter provides functionality for recording, processing, and exporting
    metrics to various backends including OpenTelemetry and parquet files.

    Features:
    - Metric recording with labels and units
    - OpenTelemetry integration
    - Periodic metric flushing
    - Console logging
    - Parquet file storage
    """

    _flush_task_started = False

    def __init__(self):
        """Initialize the metrics adapter with configuration and setup.

        This initialization:
        - Sets up base observability configuration
        - Configures date-based file settings
        - Initializes OpenTelemetry metrics if enabled
        - Starts periodic flush task for metric buffering
        """
        super().__init__(
            batch_size=METRICS_BATCH_SIZE,
            flush_interval=METRICS_FLUSH_INTERVAL_SECONDS,
            retention_days=METRICS_RETENTION_DAYS,
            cleanup_enabled=METRICS_CLEANUP_ENABLED,
            data_dir=OBSERVABILITY_DIR,
            file_name=METRICS_FILE_NAME,
        )

        # Initialize OpenTelemetry metrics if enabled
        if ENABLE_OTLP_METRICS:
            self._setup_otel_metrics()

        # Start periodic flush task if not already started
        if not AtlanMetricsAdapter._flush_task_started:
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    loop.create_task(self._periodic_flush())
                else:
                    threading.Thread(
                        target=self._start_asyncio_flush, daemon=True
                    ).start()
                AtlanMetricsAdapter._flush_task_started = True
            except Exception as e:
                logging.error(f"Failed to start metrics flush task: {e}")

    def _setup_otel_metrics(self):
        """Set up OpenTelemetry metrics exporter and configuration.

        This method:
        - Configures resource attributes
        - Creates OTLP exporter
        - Sets up metric reader
        - Initializes meter provider
        - Creates meter for the service

        Raises:
            Exception: If setup fails, logs error and continues without OTLP
        """
        try:
            # Get workflow node name for Argo environment
            workflow_node_name = OTEL_WF_NODE_NAME

            # Parse resource attributes
            resource_attributes = self._parse_otel_resource_attributes(
                OTEL_RESOURCE_ATTRIBUTES
            )

            # Add default service attributes if not present
            if "service.name" not in resource_attributes:
                resource_attributes["service.name"] = SERVICE_NAME
            if "service.version" not in resource_attributes:
                resource_attributes["service.version"] = SERVICE_VERSION

            # Add workflow node name if running in Argo
            if workflow_node_name:
                resource_attributes["k8s.workflow.node.name"] = workflow_node_name

            # Create resource
            resource = Resource.create(resource_attributes)

            # Create OTLP exporter
            exporter = OTLPMetricExporter(
                endpoint=OTEL_EXPORTER_OTLP_ENDPOINT,
                timeout=OTEL_EXPORTER_TIMEOUT_SECONDS,
            )

            # Create metric reader
            reader = PeriodicExportingMetricReader(
                exporter,
                export_interval_millis=OTEL_BATCH_DELAY_MS,
            )

            # Create meter provider
            self.meter_provider = MeterProvider(
                resource=resource,
                metric_readers=[reader],
            )

            # Set global meter provider
            metrics.set_meter_provider(self.meter_provider)

            # Create meter
            self.meter = self.meter_provider.get_meter(SERVICE_NAME)

        except Exception as e:
            logging.error(f"Failed to setup OTLP metrics: {e}")

    def _parse_otel_resource_attributes(self, env_var: str) -> dict[str, str]:
        """Parse OpenTelemetry resource attributes from environment variable.

        Args:
            env_var (str): Comma-separated string of key-value pairs

        Returns:
            dict[str, str]: Dictionary of parsed resource attributes

        Example:
            Input: "service.name=myapp,service.version=1.0"
            Output: {"service.name": "myapp", "service.version": "1.0"}
        """
        try:
            if env_var:
                attributes = env_var.split(",")
                return {
                    item.split("=")[0].strip(): item.split("=")[1].strip()
                    for item in attributes
                    if "=" in item
                }
        except Exception as e:
            logging.error(f"Failed to parse OTLP resource attributes: {e}")
        return {}

    def _start_asyncio_flush(self):
        """Start an asyncio event loop for periodic metric flushing.

        Creates a new event loop and runs the periodic flush task in the background.
        This is used when no existing event loop is available.
        """
        asyncio.run(self._periodic_flush())

    def process_record(self, record: Any) -> Dict[str, Any]:
        """Process a metric record into a standardized dictionary format.

        Args:
            record (Any): Input metric record, can be MetricRecord or dict

        Returns:
            Dict[str, Any]: Standardized dictionary representation of the metric

        This method ensures metrics are properly formatted for storage in METRICS_FILE_NAME.
        It converts the MetricRecord into a dictionary with all necessary fields.
        """
        if isinstance(record, MetricRecord):
            # Convert the record to a dictionary with all fields
            metric_dict = {
                "timestamp": record.timestamp,
                "name": record.name,
                "value": record.value,
                "type": record.type.value,
                "labels": record.labels,
                "description": record.description,
                "unit": record.unit,
            }
            return metric_dict
        return record

    def export_record(self, record: Any) -> None:
        """Export a metric record to external systems.

        Args:
            record (Any): Metric record to export

        This method:
        - Validates the record is a MetricRecord
        - Sends to OpenTelemetry if enabled
        - Logs to console
        """
        if not isinstance(record, MetricRecord):
            return

        # Send to OpenTelemetry if enabled
        if ENABLE_OTLP_METRICS:
            self._send_to_otel(record)

        # Log to console
        self._log_to_console(record)

    def _send_to_otel(self, metric_record: MetricRecord):
        """Send metric to OpenTelemetry.

        Args:
            metric_record (MetricRecord): Metric record to send

        This method:
        - Creates appropriate metric type (counter, gauge, or histogram)
        - Adds/records the metric value with labels
        - Handles errors gracefully

        Raises:
            Exception: If sending fails, logs error and continues
        """
        try:
            if metric_record.type == MetricType.COUNTER:
                counter = self.meter.create_counter(
                    name=metric_record.name,
                    description=metric_record.description,
                    unit=metric_record.unit,
                )
                counter.add(metric_record.value, metric_record.labels)
            elif metric_record.type == MetricType.GAUGE:
                gauge = self.meter.create_observable_gauge(
                    name=metric_record.name,
                    description=metric_record.description,
                    unit=metric_record.unit,
                )
                gauge.add(metric_record.value, metric_record.labels)
            elif metric_record.type == MetricType.HISTOGRAM:
                histogram = self.meter.create_histogram(
                    name=metric_record.name,
                    description=metric_record.description,
                    unit=metric_record.unit,
                )
                histogram.record(metric_record.value, metric_record.labels)
        except Exception as e:
            logging.error(f"Error sending metric to OpenTelemetry: {e}")

    def _log_to_console(self, metric_record: MetricRecord):
        """Log metric to console using the logger.

        Args:
            metric_record (MetricRecord): Metric record to log

        This method:
        - Formats metric information into a readable string
        - Includes name, value, type, labels, description, and unit
        - Uses the metric-specific logger level

        Raises:
            Exception: If logging fails, logs error and continues
        """
        try:
            log_message = (
                f"{metric_record.name} = {metric_record.value} "
                f"({metric_record.type.value})"
            )
            if metric_record.labels:
                log_message += f" Labels: {metric_record.labels}"
            if metric_record.description:
                log_message += f" Description: {metric_record.description}"
            if metric_record.unit:
                log_message += f" Unit: {metric_record.unit}"
            logger = get_logger()
            logger.metric(log_message)
        except Exception as e:
            logging.error(f"Error logging metric to console: {e}")

    def record_metric(
        self,
        name: str,
        value: float,
        metric_type: MetricType,
        labels: Dict[str, str],
        description: Optional[str] = None,
        unit: Optional[str] = None,
    ):
        """Record a metric with the given parameters.

        Args:
            name (str): Name of the metric
            value (float): Numeric value of the metric
            metric_type (str): Type of metric (counter, gauge, or histogram)
            labels (Dict[str, str]): Key-value pairs for metric dimensions
            description (Optional[str]): Optional description of the metric
            unit (Optional[str]): Optional unit of measurement

        This method:
        - Creates a MetricRecord with current timestamp
        - Adds the record to the buffer for processing
        - Handles errors gracefully

        Raises:
            Exception: If recording fails, logs error and continues
        """
        labels.update(get_workflow_context().model_dump())

        try:
            # Create metric record
            metric_record = MetricRecord(
                timestamp=time(),
                name=name,
                value=value,
                type=metric_type,
                labels=labels,
                description=description,
                unit=unit,
            )

            # Add record using base class method
            self.add_record(metric_record)

        except Exception as e:
            logging.error(f"Error recording metric: {e}")


# Create a singleton instance of the metrics adapter
_metrics_instance: Optional[AtlanMetricsAdapter] = None


def get_metrics() -> AtlanMetricsAdapter:
    """Get or create a singleton instance of AtlanMetricsAdapter.

    Returns:
        AtlanMetricsAdapter: Singleton instance of the metrics adapter

    This function ensures only one instance of the metrics adapter exists
    throughout the application lifecycle.
    """
    global _metrics_instance
    if _metrics_instance is None:
        _metrics_instance = AtlanMetricsAdapter()
    return _metrics_instance
