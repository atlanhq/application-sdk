import asyncio
import atexit
import logging
import threading
from time import time
from typing import Any, ClassVar

from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider

from application_sdk.constants import (
    METRICS_BATCH_SIZE,
    METRICS_CLEANUP_ENABLED,
    METRICS_FILE_NAME,
    METRICS_FLUSH_INTERVAL_SECONDS,
    METRICS_RETENTION_DAYS,
    SEGMENT_API_URL,
    SEGMENT_BATCH_SIZE,
    SEGMENT_BATCH_TIMEOUT_SECONDS,
    SEGMENT_DEFAULT_USER_ID,
    SEGMENT_WRITE_KEY,
    SERVICE_NAME,
)
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.observability.models import MetricRecord, MetricType
from application_sdk.observability.observability import AtlanObservability
from application_sdk.observability.segment_client import SegmentClient
from application_sdk.observability.utils import (
    build_otel_resource,
    get_metric_labels,
    get_observability_dir,
)

# MetricRecord and MetricType are imported from models.py to avoid circular dependencies
logger = get_logger(__name__)


class AtlanMetricsAdapter(AtlanObservability[MetricRecord]):
    """A metrics adapter for Atlan that extends AtlanObservability.

    This adapter provides functionality for recording, processing, and exporting
    metrics to various backends including OpenTelemetry, Segment API, and parquet files.

    Features:
    - Metric recording with labels and units
    - OpenTelemetry integration
    - Segment API integration
    - Periodic metric flushing
    - Console logging
    - Parquet file storage
    """

    _flush_task_started: ClassVar[bool] = False

    @classmethod
    def _reset_for_testing(cls) -> None:
        """Reset initialization state for test isolation."""
        cls._flush_task_started = False

    def __init__(self):
        """Initialize the metrics adapter with configuration and setup.

        This initialization:
        - Sets up base observability configuration
        - Configures date-based file settings
        - Initializes OpenTelemetry metrics if enabled
        - Initializes Segment API client if enabled
        - Starts periodic flush task for metric buffering
        """
        super().__init__(
            batch_size=METRICS_BATCH_SIZE,
            flush_interval=METRICS_FLUSH_INTERVAL_SECONDS,
            retention_days=METRICS_RETENTION_DAYS,
            cleanup_enabled=METRICS_CLEANUP_ENABLED,
            data_dir=get_observability_dir(),
            file_name=METRICS_FILE_NAME,
        )

        # Prometheus is the canonical metrics surface and is always on.
        self._setup_otel_metrics()

        # Initialize Segment client (enabled automatically if write key is present)
        self.segment_client = SegmentClient(
            write_key=SEGMENT_WRITE_KEY,
            api_url=SEGMENT_API_URL,
            default_user_id=SEGMENT_DEFAULT_USER_ID,
            batch_size=SEGMENT_BATCH_SIZE,
            batch_timeout_seconds=SEGMENT_BATCH_TIMEOUT_SECONDS,
        )
        # Register cleanup handler to close SegmentClient on shutdown
        atexit.register(self.segment_client.close)

        # Start periodic flush task if not already started
        if not AtlanMetricsAdapter._flush_task_started:
            try:
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(self._periodic_flush())
                except RuntimeError:
                    threading.Thread(
                        target=self._start_asyncio_flush, daemon=True
                    ).start()
                AtlanMetricsAdapter._flush_task_started = True
            except Exception:
                logging.error("Failed to start metrics flush task", exc_info=True)

    def _setup_otel_metrics(self):
        """Set up the OpenTelemetry MeterProvider with the Prometheus reader.

        Production exposes metrics via Prometheus only — both server scrape
        (FastAPI ``/metrics``) and worker push (Pushgateway) read from
        ``prometheus_client.REGISTRY``, which the ``PrometheusMetricReader``
        writes to.
        """
        try:
            from opentelemetry.exporter.prometheus import (  # noqa: PLC0415 — cold path: prometheus exporter only loaded during MeterProvider setup
                PrometheusMetricReader,
            )

            self._prometheus_reader = PrometheusMetricReader()
            self.meter_provider = MeterProvider(
                resource=build_otel_resource(),
                metric_readers=[self._prometheus_reader],
            )
            metrics.set_meter_provider(self.meter_provider)
            self.meter = self.meter_provider.get_meter(SERVICE_NAME)
            logging.info("Prometheus metrics reader enabled")
        except Exception:
            logging.error("Failed to setup OTel meter provider", exc_info=True)

    def _start_asyncio_flush(self):
        """Start an asyncio event loop for periodic metric flushing.

        Creates a new event loop and runs the periodic flush task in the background.
        This is used when no existing event loop is available.
        """
        asyncio.run(self._periodic_flush())

    def process_record(self, record: Any) -> dict[str, Any]:
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
        - Sends to Segment API if enabled
        - Logs to console
        """
        if not isinstance(record, MetricRecord):
            return

        # Always emit through the OTel meter — Prometheus reader picks it up.
        self._send_to_otel(record)

        # Send to Segment (client handles enable/disable internally)
        self.segment_client.send_metric(record)

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
            otel_attrs = {
                k: v
                for k, v in metric_record.labels.items()
                if isinstance(v, (str, int, float, bool))
            }
            if metric_record.type == MetricType.COUNTER:
                counter = self.meter.create_counter(
                    name=metric_record.name,
                    description=metric_record.otel_description,
                    unit=metric_record.otel_unit,
                )
                counter.add(metric_record.value, otel_attrs)
            elif metric_record.type == MetricType.GAUGE:
                gauge = self.meter.create_gauge(
                    name=metric_record.name,
                    description=metric_record.otel_description,
                    unit=metric_record.otel_unit,
                )
                gauge.set(metric_record.value, otel_attrs)
            elif metric_record.type == MetricType.HISTOGRAM:
                histogram = self.meter.create_histogram(
                    name=metric_record.name,
                    description=metric_record.otel_description,
                    unit=metric_record.otel_unit,
                )
                histogram.record(metric_record.value, otel_attrs)
        except Exception:
            logging.error("Error sending metric to OpenTelemetry", exc_info=True)

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
        except Exception:
            logging.error("Error logging metric to console", exc_info=True)

    def record_metric(
        self,
        name: str,
        value: float,
        metric_type: MetricType,
        labels: dict[str, Any],
        description: str | None = None,
        unit: str | None = None,
    ):
        """Record a metric with the given parameters.

        Args:
            name (str): Name of the metric
            value (float): Numeric value of the metric
            metric_type (str): Type of metric (counter, gauge, or histogram)
            labels (Dict[str, Any]): Key-value pairs for metric dimensions
            description (Optional[str]): Optional description of the metric
            unit (Optional[str]): Optional unit of measurement

        This method:
        - Creates a MetricRecord with current timestamp
        - Adds the record to the buffer for processing
        - Handles errors gracefully

        Raises:
            Exception: If recording fails, logs error and continues
        """
        labels.update(get_metric_labels())

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

        except Exception:
            logging.error("Error recording metric", exc_info=True)


# Create a singleton instance of the metrics adapter
_metrics_instance: AtlanMetricsAdapter | None = None


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
