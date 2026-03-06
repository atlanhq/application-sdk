"""Error-only span processor for Temporal activity tracing.

This module provides a custom SpanProcessor that only exports spans with ERROR status.
It is used for Temporal activity tracing to avoid spamming ClickHouse with successful
activity spans — success spans are created in-memory for correct timing but dropped
before export.

Only failed activity spans (connection timeouts, API errors, auth failures, etc.)
are forwarded to the OTel collector, making failure data queryable via SQL,
dashboardable in Grafana, and alertable.
"""

from typing import Optional

from opentelemetry.context import Context
from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter
from opentelemetry.trace import StatusCode


class ErrorOnlySpanProcessor(SpanProcessor):
    """A SpanProcessor that only exports spans with ERROR status.

    This processor wraps a BatchSpanProcessor and filters spans on export.
    Spans with StatusCode.ERROR are forwarded to the delegate processor,
    while successful spans (OK or UNSET) are silently dropped.

    The on_start method always forwards to the delegate to ensure correct
    span timing and parent-child relationships.

    Args:
        exporter: The SpanExporter to use for exporting error spans.
        max_queue_size: Maximum queue size for the batch processor.
        schedule_delay_millis: Delay between batch exports in milliseconds.
        max_export_batch_size: Maximum batch size for exports.
        export_timeout_millis: Timeout for export operations in milliseconds.
    """

    def __init__(
        self,
        exporter: SpanExporter,
        max_queue_size: int = 2048,
        schedule_delay_millis: int = 5000,
        max_export_batch_size: int = 512,
        export_timeout_millis: int = 30000,
    ):
        """Initialize the ErrorOnlySpanProcessor with a BatchSpanProcessor delegate."""
        self._delegate = BatchSpanProcessor(
            exporter,
            max_queue_size=max_queue_size,
            schedule_delay_millis=schedule_delay_millis,
            max_export_batch_size=max_export_batch_size,
            export_timeout_millis=export_timeout_millis,
        )

    def on_start(
        self, span: ReadableSpan, parent_context: Optional[Context] = None
    ) -> None:
        """Forward span start to the delegate for correct timing.

        All spans are forwarded on start to maintain accurate start timestamps
        and parent-child relationships.

        Args:
            span: The span that is starting.
            parent_context: Optional parent context for the span.
        """
        self._delegate.on_start(span, parent_context)

    def on_end(self, span: ReadableSpan) -> None:
        """Forward only ERROR spans to the delegate; drop successful spans.

        Only spans with StatusCode.ERROR are forwarded to the delegate for
        export. Spans with OK, UNSET, or no status are silently dropped.

        Args:
            span: The span that is ending.
        """
        if span.status is None:
            return

        if span.status.status_code == StatusCode.ERROR:
            self._delegate.on_end(span)

    def shutdown(self) -> None:
        """Shutdown the delegate processor.

        Forwards the shutdown call to the delegate BatchSpanProcessor,
        ensuring all pending spans are flushed and resources are released.
        """
        self._delegate.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        """Force flush the delegate processor.

        Forwards the force_flush call to the delegate BatchSpanProcessor.

        Args:
            timeout_millis: Maximum time to wait for flush in milliseconds.

        Returns:
            bool: True if flush succeeded, False otherwise.
        """
        return self._delegate.force_flush(timeout_millis)
