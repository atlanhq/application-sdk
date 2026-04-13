"""Helper to read current OpenTelemetry trace context.

When Temporal's built-in TracingInterceptor (or any upstream tracer) is
active, this returns the current trace_id and span_id as hex strings so
they can be attached to our App Vitals events and metrics.

When no tracer is active, returns empty strings. This is safe — it just
means our events won't be linkable to traces until a tracer is configured.

Attaching these IDs is free at emission time. When the OTel traces pipeline
comes online, correlation between our logs and traces becomes automatic.
"""

from __future__ import annotations

from opentelemetry import trace


def get_trace_context() -> tuple[str, str]:
    """Return (trace_id, span_id) from the current OTel span context.

    Returns:
        Tuple of (trace_id_hex, span_id_hex). Both empty strings if no
        valid span context is active.
    """
    try:
        span = trace.get_current_span()
        ctx = span.get_span_context()
        if not ctx.is_valid:
            return "", ""
        return format(ctx.trace_id, "032x"), format(ctx.span_id, "016x")
    except Exception:
        return "", ""
