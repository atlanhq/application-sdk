import logging
import threading
import time
from typing import Any, Dict, Optional

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SpanExporter,
)
from opentelemetry.trace import SpanKind

from application_sdk.constants import (
    ENABLE_OTLP_TRACES,
    ENABLE_TRACES_CONSOLE,
    OTEL_BATCH_DELAY_MS,
    OTEL_EXPORTER_OTLP_ENDPOINT,
    OTEL_EXPORTER_TIMEOUT_SECONDS,
    SERVICE_NAME,
)
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.observability.models import TraceRecord
from application_sdk.observability.observability import (
    TRACES_FILE_NAME,
    AtlanObservability,
)
from application_sdk.observability.utils import (
    build_otel_resource,
    get_observability_dir,
)

# Passed to the AtlanObservability base, which this adapter still extends.
# Unused: the adapter neither buffers nor flushes (see AtlanTracesAdapter).
_TRACES_BATCH_SIZE = 100
_TRACES_FLUSH_INTERVAL_SECONDS = 5

__all__ = ["TraceRecord", "AtlanTracesAdapter", "get_traces", "setup_tracer_provider"]

# SDK logger for module-level diagnostics (init failures, etc.). Uses the SDK
# logger rather than the root `logging` module so messages are routed through
# the configured handlers.
_module_logger = get_logger(__name__)


# The process-wide provider: the one this module installed as the OTel
# global, or the one it found already installed. Guarded by the lock so
# concurrent callers (run_main, TraceInterceptor, the adapter) agree on one.
_tracer_provider: Optional[trace.TracerProvider] = None
_tracer_provider_lock = threading.Lock()


def setup_tracer_provider() -> Optional[trace.TracerProvider]:
    """Install the process-wide OpenTelemetry tracer provider, once.

    Temporal's ``TracingInterceptor`` (behind ``TraceInterceptor``) emits spans
    through the *global* tracer provider; until one is installed those spans
    go to OTel's no-op provider and nothing is exported. ``run_main`` calls
    this at process start, beside ``get_metrics()``.

    Does nothing and returns ``None`` unless ``ATLAN_ENABLE_OTLP_TRACES`` is
    on. Spans export via OTLP to ``OTEL_EXPORTER_OTLP_ENDPOINT``; they are
    also printed to stdout only when ``ATLAN_ENABLE_TRACES_CONSOLE`` is on,
    a development-only switch.

    The global provider is owned by whoever installs it first. If the host
    process (or OTel auto-instrumentation) already installed one, it is
    returned as is and the SDK's exporters are not attached: export is that
    provider's job. Thread-safe and idempotent: every call returns the
    provider that is actually global. If setup fails the error is logged,
    nothing is installed and ``None`` is returned — there is no fallback that
    would write spans to stdout instead.

    Returns:
        The global provider, or ``None`` when tracing is off or setup failed.
    """
    global _tracer_provider
    if not ENABLE_OTLP_TRACES:
        return None
    with _tracer_provider_lock:
        if _tracer_provider is not None:
            return _tracer_provider

        existing = trace.get_tracer_provider()
        if not isinstance(existing, trace.ProxyTracerProvider):
            _module_logger.info(
                "An OpenTelemetry tracer provider is already installed; spans "
                "go through it and the SDK's span exporters are not attached"
            )
            _tracer_provider = existing
            return existing

        try:
            provider = _build_tracer_provider()
            trace.set_tracer_provider(provider)
        except Exception:
            _module_logger.error(
                "Failed to set up the OpenTelemetry tracer provider; tracing disabled",
                exc_info=True,
            )
            return None

        installed = trace.get_tracer_provider()
        if installed is not provider:
            # Code outside this module installed a provider between the
            # check above and ours; OTel kept theirs. Use it, and stop ours
            # so its batch processors don't linger.
            provider.shutdown()
            _tracer_provider = installed
            return installed

        _tracer_provider = provider
        return provider


def _build_tracer_provider() -> TracerProvider:
    """A ``TracerProvider`` with the configured span exporters attached."""
    provider = TracerProvider(resource=build_otel_resource())
    exporters: list[SpanExporter] = []
    if OTEL_EXPORTER_OTLP_ENDPOINT:
        try:
            exporters.append(
                OTLPSpanExporter(
                    endpoint=OTEL_EXPORTER_OTLP_ENDPOINT,
                    timeout=OTEL_EXPORTER_TIMEOUT_SECONDS,
                )
            )
        except Exception:
            _module_logger.warning(
                "Failed to set up the OTLP span exporter; spans will not be exported over OTLP",
                exc_info=True,
            )
    if ENABLE_TRACES_CONSOLE:
        exporters.append(ConsoleSpanExporter())
    if not exporters:
        _module_logger.warning(
            "ATLAN_ENABLE_OTLP_TRACES=true but no span exporter is configured "
            "(OTEL_EXPORTER_OTLP_ENDPOINT is empty or its exporter failed); "
            "spans will be dropped"
        )
    for exporter in exporters:
        provider.add_span_processor(
            BatchSpanProcessor(exporter, schedule_delay_millis=OTEL_BATCH_DELAY_MS)
        )
    return provider


class AtlanTracesAdapter(AtlanObservability[TraceRecord]):
    """Emit the spans ``@observability`` and app code record, through OpenTelemetry.

    A thin span emitter. :meth:`record_trace` turns one finished operation into
    one OTel span on the process-wide tracer provider (see
    :func:`setup_tracer_provider`), timed from its real start to its end and
    parented to whatever span is current at the call site (for example the
    Temporal activity span from ``TraceInterceptor``). Each record is also
    written as a ``tracing``-level log line.

    Nothing is buffered, written to disk or uploaded. Earlier versions also
    batched records into gzip NDJSON files under the object store's
    ``observability/.../traces/`` prefix on a periodic flush task. Nothing read
    those files, and the flush task started on whatever event loop built the
    adapter, including a workflow's. The class still extends
    :class:`AtlanObservability` so existing ``isinstance`` checks and inherited
    methods keep working, but :meth:`add_record` no longer buffers and the
    store sink is off for this signal.

    With ``ATLAN_ENABLE_OTLP_TRACES`` off (the default) no span is created.
    """

    def __init__(self):
        """Bind to the process-wide tracer provider when OTLP traces are enabled."""
        super().__init__(
            batch_size=_TRACES_BATCH_SIZE,
            flush_interval=_TRACES_FLUSH_INTERVAL_SECONDS,
            data_dir=get_observability_dir(),
            file_name=TRACES_FILE_NAME,
        )
        self.tracer_provider: Optional[trace.TracerProvider] = None
        self.tracer: Optional[trace.Tracer] = None
        if ENABLE_OTLP_TRACES:
            self._setup_otel_traces()

    def _setup_otel_traces(self):
        """Bind the adapter to the process-wide tracer provider.

        The provider is installed once per process by
        :func:`setup_tracer_provider` (normally already done by ``run_main``);
        the adapter reuses it rather than installing a second one. When setup
        failed, ``tracer_provider`` and ``tracer`` stay ``None`` and
        ``_send_to_otel`` becomes a no-op.
        """
        self.tracer_provider = setup_tracer_provider()
        self.tracer = (
            self.tracer_provider.get_tracer(SERVICE_NAME)
            if self.tracer_provider is not None
            else None
        )

    def _store_sink_enabled(self) -> bool:
        """Traces never go to the object store; spans leave through OTLP only."""
        return False

    def add_record(self, record: TraceRecord) -> None:
        """Export *record* at once. Nothing is buffered for a later flush."""
        try:
            self.export_record(record)
        except Exception:
            logging.error("Error adding record", exc_info=True)

    def process_record(self, record: Any) -> Dict[str, Any]:
        """Convert a trace record into a plain dictionary.

        Args:
            record (Any): Input trace record, can be TraceRecord or dict

        Returns:
            Dict[str, Any]: The record's fields, or *record* unchanged when it
            is not a ``TraceRecord``.
        """
        if isinstance(record, TraceRecord):
            return {
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
        return record

    def export_record(self, record: Any) -> None:
        """Send a trace record to OpenTelemetry (when enabled) and the log.

        Args:
            record (Any): Trace record to export; anything that is not a
                ``TraceRecord`` is ignored.
        """
        if not isinstance(record, TraceRecord):
            return

        if ENABLE_OTLP_TRACES:
            self._send_to_otel(record)

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
        """Emit *trace_record* as one finished OpenTelemetry span.

        The record describes an operation that has already finished: it ended
        at ``timestamp`` and took ``duration_ms``. The span is given exactly
        that start and end, and its parent is the span current at the call
        site. Errors are logged and swallowed, so tracing never breaks the
        traced code.

        Args:
            trace_record (TraceRecord): Trace record to send
        """
        if self.tracer is None:
            # Setup failed and left the adapter without a tracer; skip silently
            # rather than AttributeError on every emit.
            return
        try:
            end_ns = self._timestamp_to_nanos(trace_record.timestamp)
            start_ns = end_ns - int(trace_record.duration_ms * 1_000_000)
            span = self.tracer.start_span(
                name=trace_record.name,
                kind=self._str_to_span_kind(trace_record.kind),
                attributes=trace_record.attributes,
                start_time=start_ns,
            )
            try:
                if trace_record.status_code == "ERROR":
                    span.set_status(
                        trace.Status(
                            trace.StatusCode.ERROR, trace_record.status_message
                        )
                    )
                else:
                    span.set_status(trace.Status(trace.StatusCode.OK))

                for event in trace_record.events or []:
                    span.add_event(
                        name=event.get("name", ""),
                        attributes=event.get("attributes", {}),
                        timestamp=self._timestamp_to_nanos(
                            event.get("timestamp", trace_record.timestamp)
                        ),
                    )
            finally:
                span.end(end_time=end_ns)

        except Exception:
            logging.error("Error sending trace to OpenTelemetry", exc_info=True)

    def _log_to_console(self, trace_record: TraceRecord):
        """Write *trace_record* as one ``tracing``-level log line.

        Includes the trace and span IDs, status, attributes and duration.
        Errors are logged and swallowed.

        Args:
            trace_record (TraceRecord): Trace record to log
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
        except Exception:
            logging.error("Error logging trace to console", exc_info=True)

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
        """Record one operation that has just finished.

        Call it as the operation ends: the span ends now and starts
        ``duration_ms`` earlier. ``trace_id``, ``span_id`` and
        ``parent_span_id`` are caller labels carried on the record and the log
        line; the OTel span gets its IDs and parent from the active context.

        Args:
            name (str): Name of the trace
            trace_id (str): Caller-chosen trace label
            span_id (str): Caller-chosen span label
            kind (str): Type of span
            status_code (str): ``"OK"`` or ``"ERROR"``
            attributes (Dict[str, Any]): Trace attributes
            parent_span_id (Optional[str]): Caller-chosen parent label
            status_message (Optional[str]): Status message
            events (Optional[list[Dict[str, Any]]]): Trace events
            duration_ms (Optional[float]): How long the operation took
        """
        try:
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
            self.add_record(trace_record)

        except Exception:
            # conformance: ignore[L009] the ERROR record here is the operator-visible failure signal (add_record failures are otherwise silent) — an existing test pins it, not merely a re-raise duplicate
            logging.error("Error recording trace", exc_info=True)
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
