import asyncio
import logging
import threading
import uuid
from time import time
from typing import Any, Dict, Optional

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
from opentelemetry.trace import SpanKind
from pydantic import BaseModel

from application_sdk.common.logger_adaptors import get_logger
from application_sdk.common.observability import AtlanObservability
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


class TraceRecord(BaseModel):
    """Pydantic model for trace records."""

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


class TraceContext:
    """Context manager for trace recording."""

    def __init__(
        self,
        adapter: "AtlanTracesAdapter",
        name: str,
        trace_id: str,
        span_id: str,
        kind: str,
        status_code: str,
        attributes: Dict[str, Any],
        parent_span_id: Optional[str] = None,
        status_message: Optional[str] = None,
        events: Optional[list[Dict[str, Any]]] = None,
    ):
        self.adapter = adapter
        self.name = name
        self.trace_id = trace_id
        self.span_id = span_id
        self.kind = kind
        self.status_code = status_code
        self.attributes = attributes
        self.parent_span_id = parent_span_id
        self.status_message = status_message
        self.events = events
        self.start_time = time()

    def __enter__(self) -> "TraceContext":
        """Enter the context."""
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Exit the context and record the trace."""
        duration_ms = (time() - self.start_time) * 1000
        status_code = "ERROR" if exc_type is not None else self.status_code
        status_message = str(exc_val) if exc_type is not None else self.status_message

        self.adapter.record_trace(
            name=self.name,
            trace_id=self.trace_id,
            span_id=self.span_id,
            kind=self.kind,
            status_code=status_code,
            status_message=status_message,
            attributes=self.attributes,
            parent_span_id=self.parent_span_id,
            events=self.events,
            duration_ms=duration_ms,
        )


class AtlanTracesAdapter(AtlanObservability[TraceRecord]):
    """Traces adapter for Atlan."""

    _flush_task_started = False

    def __init__(self):
        """Initialize the traces adapter."""
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
        """Setup OpenTelemetry traces exporter."""
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
        """Setup console-only tracing as fallback."""
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
        """Periodically flush traces buffer."""
        while True:
            await asyncio.sleep(self._flush_interval)
            await self._flush_buffer(force=True)

    def process_record(self, record: Any) -> Dict[str, Any]:
        """Process a trace record into a dictionary format.

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
        """Export a trace record to external systems."""
        if not isinstance(record, TraceRecord):
            return

        # Send to OpenTelemetry if enabled
        if ENABLE_OTLP_TRACES:
            self._send_to_otel(record)

        # Log to console
        self._log_to_console(record)

    def _str_to_span_kind(self, kind: str) -> SpanKind:
        """Convert string kind to SpanKind enum."""
        kind_map = {
            "INTERNAL": SpanKind.INTERNAL,
            "SERVER": SpanKind.SERVER,
            "CLIENT": SpanKind.CLIENT,
            "PRODUCER": SpanKind.PRODUCER,
            "CONSUMER": SpanKind.CONSUMER,
        }
        return kind_map.get(kind, SpanKind.INTERNAL)

    def _timestamp_to_nanos(self, timestamp: float) -> int:
        """Convert Unix timestamp to nanoseconds."""
        return int(timestamp * 1_000_000_000)  # Convert seconds to nanoseconds

    def _send_to_otel(self, trace_record: TraceRecord):
        """Send trace to OpenTelemetry."""
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
                        timestamp = event.get("timestamp", time())
                        timestamp_nanos = self._timestamp_to_nanos(timestamp)

                        span.add_event(
                            name=event.get("name", ""),
                            attributes=event.get("attributes", {}),
                            timestamp=timestamp_nanos,
                        )

        except Exception as e:
            logging.error(f"Error sending trace to OpenTelemetry: {e}")

    def _log_to_console(self, trace_record: TraceRecord):
        """Log trace to console."""
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
    ) -> TraceContext:
        """Create a trace context for recording traces.

        This method returns a context manager that will automatically record the trace
        when the context is exited, including duration and any exceptions that occurred.
        """
        return TraceContext(
            adapter=self,
            name=name,
            trace_id=trace_id,
            span_id=span_id,
            kind=kind,
            status_code=status_code,
            attributes=attributes,
            parent_span_id=parent_span_id,
            status_message=status_message,
            events=events,
        )

    def _record_trace(
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
        """Internal method to record a trace."""
        try:
            # Create trace record
            trace_record = TraceRecord(
                timestamp=time(),
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

    def __enter__(self):
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        if exc_type is not None:
            # If there was an exception, record it
            self._record_trace(
                name="error",
                trace_id=str(uuid.uuid4()),
                span_id=str(uuid.uuid4()),
                kind="INTERNAL",
                status_code="ERROR",
                status_message=str(exc_val),
                attributes={"error_type": str(exc_type.__name__)},
            )


# Create a singleton instance of the traces adapter
_traces_instance: Optional[AtlanTracesAdapter] = None


def get_traces() -> AtlanTracesAdapter:
    """Get or create an instance of AtlanTracesAdapter.
    Returns:
        AtlanTracesAdapter: Traces adapter instance
    """
    global _traces_instance
    if _traces_instance is None:
        _traces_instance = AtlanTracesAdapter()
    return _traces_instance
