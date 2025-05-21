import asyncio
import logging
import threading
from time import time
from typing import Any, Dict, Optional

from opentelemetry import metrics
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from pydantic import BaseModel

from application_sdk.common.logger_adaptors import get_logger
from application_sdk.common.observability import AtlanObservability
from application_sdk.constants import (
    ENABLE_OTLP_METRICS,
    METRICS_BATCH_SIZE,
    METRICS_CLEANUP_ENABLED,
    METRICS_DATE_FORMAT,
    METRICS_FILE_NAME,
    METRICS_FLUSH_INTERVAL_SECONDS,
    METRICS_RETENTION_DAYS,
    METRICS_USE_DATE_BASED_FILES,
    OBSERVABILITY_DIR,
    OTEL_BATCH_DELAY_MS,
    OTEL_EXPORTER_OTLP_ENDPOINT,
    OTEL_EXPORTER_TIMEOUT_SECONDS,
    OTEL_RESOURCE_ATTRIBUTES,
    OTEL_WF_NODE_NAME,
    SERVICE_NAME,
    SERVICE_VERSION,
)


class MetricRecord(BaseModel):
    """Pydantic model for metric records."""

    timestamp: float
    name: str
    value: float
    type: str  # counter, gauge, histogram
    labels: Dict[str, str]
    description: Optional[str] = None
    unit: Optional[str] = None

    class Config:
        """Pydantic model configuration."""

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

                # Ensure type is string
                if "type" in obj:
                    obj["type"] = str(obj["type"])

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
    """Metrics adapter for Atlan."""

    _flush_task_started = False

    def __init__(self):
        """Initialize the metrics adapter."""
        super().__init__(
            batch_size=METRICS_BATCH_SIZE,
            flush_interval=METRICS_FLUSH_INTERVAL_SECONDS,
            retention_days=METRICS_RETENTION_DAYS,
            cleanup_enabled=METRICS_CLEANUP_ENABLED,
            data_dir=OBSERVABILITY_DIR,
            file_name=METRICS_FILE_NAME,
        )

        # Override the date-based file settings
        self._use_date_based_files = METRICS_USE_DATE_BASED_FILES
        self._date_format = METRICS_DATE_FORMAT

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
        """Setup OpenTelemetry metrics exporter."""
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
        """Parse OpenTelemetry resource attributes from environment variable."""
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
        """Start the asyncio flush task."""
        asyncio.run(self._periodic_flush())

    async def _periodic_flush(self):
        """Periodically flush metrics buffer."""
        while True:
            await asyncio.sleep(self._flush_interval)
            await self._flush_buffer(force=True)

    def process_record(self, record: Any) -> Dict[str, Any]:
        """Process a metric record into a dictionary format.

        This method ensures metrics are properly formatted for storage in metrics.parquet.
        It converts the MetricRecord into a dictionary with all necessary fields.
        """
        if isinstance(record, MetricRecord):
            # Convert the record to a dictionary with all fields
            metric_dict = {
                "timestamp": record.timestamp,
                "name": record.name,
                "value": record.value,
                "type": record.type,
                "labels": record.labels,
                "description": record.description,
                "unit": record.unit,
            }
            return metric_dict
        return record

    def export_record(self, record: Any) -> None:
        """Export a metric record to external systems."""
        if not isinstance(record, MetricRecord):
            return

        # Send to OpenTelemetry if enabled
        if ENABLE_OTLP_METRICS:
            self._send_to_otel(record)

        # Log to console
        self._log_to_console(record)

    def _send_to_otel(self, metric_record: MetricRecord):
        """Send metric to OpenTelemetry."""
        try:
            if metric_record.type == "counter":
                counter = self.meter.create_counter(
                    name=metric_record.name,
                    description=metric_record.description,
                    unit=metric_record.unit,
                )
                counter.add(metric_record.value, metric_record.labels)
            elif metric_record.type == "gauge":
                gauge = self.meter.create_observable_gauge(
                    name=metric_record.name,
                    description=metric_record.description,
                    unit=metric_record.unit,
                )
                gauge.add(metric_record.value, metric_record.labels)
            elif metric_record.type == "histogram":
                histogram = self.meter.create_histogram(
                    name=metric_record.name,
                    description=metric_record.description,
                    unit=metric_record.unit,
                )
                histogram.record(metric_record.value, metric_record.labels)
        except Exception as e:
            logging.error(f"Error sending metric to OpenTelemetry: {e}")

    def _log_to_console(self, metric_record: MetricRecord):
        """Log metric to console."""
        try:
            log_message = (
                f"{metric_record.name} = {metric_record.value} "
                f"({metric_record.type})"
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
        metric_type: str,
        labels: Dict[str, str],
        description: Optional[str] = None,
        unit: Optional[str] = None,
    ):
        """Record a metric with the given parameters."""
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
    """Get or create an instance of AtlanMetricsAdapter.
    Returns:
        AtlanMetricsAdapter: Metrics adapter instance
    """
    global _metrics_instance
    if _metrics_instance is None:
        _metrics_instance = AtlanMetricsAdapter()
    return _metrics_instance
