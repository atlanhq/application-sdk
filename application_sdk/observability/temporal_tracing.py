"""Temporal activity tracing setup for OpenTelemetry.

This module configures OpenTelemetry tracing for Temporal activities. It sets up
a TracerProvider with an ErrorOnlySpanProcessor so only failed activity spans
are exported to the OTel collector.

The setup is designed to work with Temporal's TracingInterceptor, which creates
spans for each activity. This module ensures those spans are properly exported
when activities fail, making failure data queryable in ClickHouse.

Note:
    This module sets the global TracerProvider. Since there can only be ONE
    global TracerProvider per process, this may conflict with ENABLE_OTLP_TRACES
    if both are enabled. In practice, ENABLE_OTLP_TRACES defaults to false in
    most deployments, so this won't conflict.
"""

from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)

_temporal_tracing_configured: bool = False


def setup_temporal_tracing() -> None:
    """Set up OpenTelemetry tracing for Temporal activities.

    This function configures a TracerProvider with an ErrorOnlySpanProcessor
    that only exports spans with ERROR status. It is idempotent and will only
    configure tracing once per process.

    The TracerProvider is set globally so that Temporal's TracingInterceptor
    can use it to create spans for activity executions.

    Environment variables used:
        - OTEL_EXPORTER_OTLP_ENDPOINT: The OTel collector endpoint (required)
        - OTEL_SERVICE_NAME: Service name for the resource
        - OTEL_SERVICE_VERSION: Service version for the resource

    Raises:
        No exceptions are raised. All errors are logged and the function
        returns gracefully to avoid crashing the worker.
    """
    global _temporal_tracing_configured

    if _temporal_tracing_configured:
        return

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider

        from application_sdk.constants import (
            OTEL_EXPORTER_OTLP_ENDPOINT,
            OTEL_EXPORTER_TIMEOUT_SECONDS,
            SERVICE_NAME,
            SERVICE_VERSION,
        )
        from application_sdk.observability.error_only_processor import (
            ErrorOnlySpanProcessor,
        )

    except ImportError as e:
        logger.warning(
            f"OpenTelemetry dependencies not available for Temporal tracing: {e}. "
            "Install opentelemetry-sdk and opentelemetry-exporter-otlp-proto-grpc."
        )
        return

    try:
        if not OTEL_EXPORTER_OTLP_ENDPOINT:
            logger.warning(
                "Temporal OTel tracing enabled but OTEL_EXPORTER_OTLP_ENDPOINT not configured. "
                "Skipping tracing setup."
            )
            return

        existing_provider = trace.get_tracer_provider()
        if isinstance(existing_provider, TracerProvider):
            logger.warning(
                "A TracerProvider is already configured. Temporal tracing will use a new "
                "provider, which may result in duplicate span exports if ENABLE_OTLP_TRACES "
                "is also enabled."
            )

        resource = Resource.create(
            {
                "service.name": SERVICE_NAME,
                "service.version": SERVICE_VERSION,
            }
        )

        otlp_exporter = OTLPSpanExporter(
            endpoint=OTEL_EXPORTER_OTLP_ENDPOINT,
            timeout=OTEL_EXPORTER_TIMEOUT_SECONDS,
        )

        error_only_processor = ErrorOnlySpanProcessor(exporter=otlp_exporter)

        provider = TracerProvider(resource=resource)
        provider.add_span_processor(error_only_processor)

        trace.set_tracer_provider(provider)

        _temporal_tracing_configured = True
        logger.info(
            f"Temporal OTel tracing configured with endpoint {OTEL_EXPORTER_OTLP_ENDPOINT}. "
            "Only ERROR spans will be exported."
        )

    except Exception as e:
        logger.warning(
            f"Failed to configure Temporal OTel tracing: {e}. "
            "Activity failures will not be exported to OTel collector."
        )
