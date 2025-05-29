import asyncio
import logging
import threading
import time
from typing import Any, Dict, Optional

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
from opentelemetry.trace import SpanKind
from pydantic import BaseModel

from application_sdk.constants import (
    ENABLE_OTLP_TRACES,
    OBSERVABILITY_DIR,
    OTEL_BATCH_DELAY_MS,
    OTEL_EXPORTER_OTLP_ENDPOINT,
    OTEL_EXPORTER_TIMEOUT_SECONDS,
    OTEL_RESOURCE_ATTRIBUTES,
    OTEL_WF_NODE_NAME,
    SERVICE_NAME,
    SERVICE_VERSION,
    TRACES_BATCH_SIZE,
    TRACES_CLEANUP_ENABLED,
    TRACES_FILE_NAME,
    TRACES_FLUSH_INTERVAL_SECONDS,
    TRACES_RETENTION_DAYS,
)
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.observability.observability import AtlanObservability


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
    events: Optional[list[Dict[str, Any]]] = None
    duration_ms: float


class AtlanTracesAdapter(AtlanObservability[TraceRecord]):
    """A traces adapter for Atlan that extends AtlanObservability.

    This adapter provides functionality for recording, processing, and exporting
    distributed traces to various backends including OpenTelemetry and parquet files.

    Features:
    - Distributed tracing with spans and events
    - OpenTelemetry integration
    - Periodic trace flushing
    - Console logging
    - Parquet file storage
    """

    _flush_task_started = False

    def __init__(self):
        """Initialize the traces adapter with configuration and setup.

        This initialization:
        - Sets up base observability configuration
        - Initializes OpenTelemetry traces if enabled
        - Starts periodic flush task for trace buffering
        """
        super().__init__(
            batch_size=TRACES_BATCH_SIZE,
            flush_interval=TRACES_FLUSH_INTERVAL_SECONDS,
            retention_days=TRACES_RETENTION_DAYS,
            cleanup_enabled=TRACES_CLEANUP_ENABLED,
            data_dir=OBSERVABILITY_DIR,
            file_name=TRACES_FILE_NAME,
        )

        # Initialize OpenTelemetry traces if enabled
        if ENABLE_OTLP_TRACES:
            self._setup_otel_traces()

        # Start periodic flush task if not already started
        if not AtlanTracesAdapter._flush_task_started:
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    loop.create_task(self._periodic_flush())
                else:
                    threading.Thread(
                        target=self._start_asyncio_flush, daemon=True
                    ).start()
                AtlanTracesAdapter._flush_task_started = True
            except Exception as e:
                logging.error(f"Failed to start traces flush task: {e}")

    def _setup_otel_traces(self):
        """Set up OpenTelemetry traces exporter and configuration.

        This method:
        - Configures resource attributes
        - Creates console and OTLP exporters
        - Sets up span processors
        - Initializes tracer provider
        - Creates tracer for the service

        Falls back to console-only tracing if setup fails.
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

            # Create exporters
            exporters = []

            # Add console exporter for local development
            console_exporter = ConsoleSpanExporter()
            exporters.append(console_exporter)

            # Add OTLP exporter if endpoint is configured
            if OTEL_EXPORTER_OTLP_ENDPOINT:
                try:
                    otlp_exporter = OTLPSpanExporter(
                        endpoint=OTEL_EXPORTER_OTLP_ENDPOINT,
                        timeout=OTEL_EXPORTER_TIMEOUT_SECONDS,
                    )
                    exporters.append(otlp_exporter)
                except Exception as e:
                    logging.warning(
                        f"Failed to setup OTLP exporter: {e}. Falling back to console only."
                    )

            # Create span processors for each exporter
            span_processors = [
                BatchSpanProcessor(
                    exporter,
                    schedule_delay_millis=OTEL_BATCH_DELAY_MS,
                )
                for exporter in exporters
            ]

            # Create tracer provider
            self.tracer_provider = TracerProvider(
                resource=resource,
            )

            # Add all span processors
            for processor in span_processors:
                self.tracer_provider.add_span_processor(processor)

            # Set global tracer provider
            trace.set_tracer_provider(self.tracer_provider)

            # Create tracer
            self.tracer = self.tracer_provider.get_tracer(SERVICE_NAME)

        except Exception as e:
            logging.error(f"Failed to setup OpenTelemetry traces: {e}")
            # Fall back to console-only tracing
            self._setup_console_only_traces()

    def _setup_console_only_traces(self):
        """Set up console-only tracing as fallback.

        This method:
        - Creates basic resource attributes
        - Sets up console exporter
        - Configures span processor
        - Initializes tracer provider
        - Creates tracer for the service
        """
        try:
            # Create resource with basic attributes
            resource = Resource.create(
                {
                    "service.name": SERVICE_NAME,
                    "service.version": SERVICE_VERSION,
                }
            )

            # Create console exporter
            console_exporter = ConsoleSpanExporter()

            # Create span processor
            span_processor = BatchSpanProcessor(
                console_exporter,
                schedule_delay_millis=OTEL_BATCH_DELAY_MS,
            )

            # Create tracer provider
            self.tracer_provider = TracerProvider(
                resource=resource,
            )
            self.tracer_provider.add_span_processor(span_processor)

            # Set global tracer provider
            trace.set_tracer_provider(self.tracer_provider)

            # Create tracer
            self.tracer = self.tracer_provider.get_tracer(SERVICE_NAME)

        except Exception as e:
            logging.error(f"Failed to setup console-only tracing: {e}")

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
        """Start an asyncio event loop for periodic trace flushing.

        Creates a new event loop and runs the periodic flush task in the background.
        This is used when no existing event loop is available.
        """
        asyncio.run(self._periodic_flush())

    def process_record(self, record: Any) -> Dict[str, Any]:
        """Process a trace record into a standardized dictionary format.

        Args:
            record (Any): Input trace record, can be TraceRecord or dict

        Returns:
            Dict[str, Any]: Standardized dictionary representation of the trace

        This method ensures traces are properly formatted for storage in traces.parquet.
        It converts the TraceRecord into a dictionary with all necessary fields.
        """
        if isinstance(record, TraceRecord):
            # Convert the record to a dictionary with all fields
            trace_dict = {
                "timestamp": record.timestamp,
                "trace_id": record.trace_id,
                "span_id": record.span_id,
                "parent_span_id": record.parent_span_id,
                "name": record.name,
                "kind": record.kind,
                "status_code": record.status_code,
                "status_message": record.status_message,
                "attributes": record.attributes,
                "events": record.events,
                "duration_ms": record.duration_ms,
            }
            return trace_dict
        return record

    def export_record(self, record: Any) -> None:
        """Export a trace record to external systems.

        Args:
            record (Any): Trace record to export

        This method:
        - Validates the record is a TraceRecord
        - Sends to OpenTelemetry if enabled
        - Logs to console
        """
        if not isinstance(record, TraceRecord):
            return

        # Send to OpenTelemetry if enabled
        if ENABLE_OTLP_TRACES:
            self._send_to_otel(record)

        # Log to console
        self._log_to_console(record)

    def _str_to_span_kind(self, kind: str) -> SpanKind:
        """Convert string kind to OpenTelemetry SpanKind enum.

        Args:
            kind (str): String representation of span kind

        Returns:
            SpanKind: OpenTelemetry SpanKind enum value

        Defaults to INTERNAL if kind is not recognized.
        """
        kind_map = {
            "INTERNAL": SpanKind.INTERNAL,
            "SERVER": SpanKind.SERVER,
            "CLIENT": SpanKind.CLIENT,
            "PRODUCER": SpanKind.PRODUCER,
            "CONSUMER": SpanKind.CONSUMER,
        }
        return kind_map.get(kind, SpanKind.INTERNAL)

    def _timestamp_to_nanos(self, timestamp: float) -> int:
        """Convert Unix timestamp to nanoseconds.

        Args:
            timestamp (float): Unix timestamp in seconds

        Returns:
            int: Timestamp in nanoseconds
        """
        return int(timestamp * 1_000_000_000)  # Convert seconds to nanoseconds

    def _send_to_otel(self, trace_record: TraceRecord):
        """Send trace to OpenTelemetry.

        Args:
            trace_record (TraceRecord): Trace record to send

        This method:
        - Creates a span with trace data
        - Sets span status and attributes
        - Adds events if present
        - Handles errors gracefully

        Raises:
            Exception: If sending fails, logs error and continues
        """
        try:
            # Convert string kind to SpanKind enum
            span_kind = self._str_to_span_kind(trace_record.kind)

            # Create a span with the trace record data
            with self.tracer.start_as_current_span(
                name=trace_record.name,
                kind=span_kind,  # Use converted SpanKind enum
                attributes=trace_record.attributes,
            ) as span:
                # Set span status
                if trace_record.status_code == "ERROR":
                    span.set_status(
                        trace.Status(
                            trace.StatusCode.ERROR, trace_record.status_message
                        )
                    )
                else:
                    span.set_status(trace.Status(trace.StatusCode.OK))

                # Add events if any
                if trace_record.events:
                    for event in trace_record.events:
                        # Convert timestamp to nanoseconds
                        timestamp = event.get("timestamp", time.time())
                        timestamp_nanos = self._timestamp_to_nanos(timestamp)

                        span.add_event(
                            name=event.get("name", ""),
                            attributes=event.get("attributes", {}),
                            timestamp=timestamp_nanos,
                        )

        except Exception as e:
            logging.error(f"Error sending trace to OpenTelemetry: {e}")

    def _log_to_console(self, trace_record: TraceRecord):
        """Log trace to console using the logger.

        Args:
            trace_record (TraceRecord): Trace record to log

        This method:
        - Formats trace information into a readable string
        - Includes trace ID, span ID, status, and duration
        - Uses the tracing-specific logger level

        Raises:
            Exception: If logging fails, logs error and continues
        """
        try:
            log_message = (
                f"Trace: {trace_record.name} "
                f"(ID: {trace_record.trace_id}, Span: {trace_record.span_id}) "
                f"Status: {trace_record.status_code}"
            )
            if trace_record.status_message:
                log_message += f" Message: {trace_record.status_message}"
            if trace_record.attributes:
                log_message += f" Attributes: {trace_record.attributes}"
            if trace_record.duration_ms:
                log_message += f" Duration: {trace_record.duration_ms}ms"

            logger = get_logger()
            logger.tracing(log_message)
        except Exception as e:
            logging.error(f"Error logging trace to console: {e}")

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
        events: Optional[list[Dict[str, Any]]] = None,
        duration_ms: Optional[float] = None,
    ) -> None:
        """Record a trace directly without context manager.

        Args:
            name (str): Name of the trace
            trace_id (str): Unique identifier for the trace
            span_id (str): Unique identifier for this span
            kind (str): Type of span
            status_code (str): Status code
            attributes (Dict[str, Any]): Trace attributes
            parent_span_id (Optional[str]): Parent span ID
            status_message (Optional[str]): Status message
            events (Optional[list[Dict[str, Any]]]): Trace events
            duration_ms (Optional[float]): Duration in milliseconds

        This method directly records a trace without using a context manager.
        It's a simpler alternative to the context manager pattern.
        """
        try:
            # Create trace record
            trace_record = TraceRecord(
                timestamp=time.time(),
                trace_id=trace_id,
                span_id=span_id,
                parent_span_id=parent_span_id,
                name=name,
                kind=kind,
                status_code=status_code,
                status_message=status_message,
                attributes=attributes,
                events=events,
                duration_ms=duration_ms or 0.0,
            )

            # Add record using base class method
            self.add_record(trace_record)

        except Exception as e:
            logging.error(f"Error recording trace: {e}")
            raise


# Create a singleton instance of the traces adapter
_traces_instance: Optional[AtlanTracesAdapter] = None


def get_traces() -> AtlanTracesAdapter:
    """Get or create a singleton instance of AtlanTracesAdapter.

    Returns:
        AtlanTracesAdapter: Singleton instance of the traces adapter

    This function ensures only one instance of the traces adapter exists
    throughout the application lifecycle.
    """
    global _traces_instance
    if _traces_instance is None:
        _traces_instance = AtlanTracesAdapter()
    return _traces_instance
