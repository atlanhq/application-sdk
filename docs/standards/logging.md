# Logging Guidelines

## Exceptions

- **Observability Module**: The `application_sdk/observability/` module uses **loguru** directly to implement the `AtlanLoggerAdapter` / `get_logger` wrapper. Stdlib `logging` is used internally for fallback error reporting (e.g. `InterceptHandler`, `basicConfig`) and to silence noisy third-party loggers — this is an internal implementation detail. **All application code must use `get_logger`** and must not call stdlib `logging.*` directly, as it bypasses the OTel pipeline.

- **Logger Configuration**
    - Use `AtlanLoggerAdapter` for all logging
    - Configure loggers using `get_logger(__name__)`
    - Use structured logging with context
    - Include request IDs in logs
    - Use appropriate log levels:
        - DEBUG: Detailed information for debugging
        - INFO: General operational information
        - WARNING: A failure the code handled — it caught the exception and carried on (a fallback, a retry, a best-effort cleanup, a skipped item). Log it with `exc_info=True`.
        - ERROR: The operation failed — the handler raises, exits, or returns a failure result to its caller. The customer-facing log view filters at ERROR, so every ERROR record reads as a failed run; a handled failure logged at ERROR is a false alarm. Conformance L022 reports `logger.error()` in an `except` block that neither raises, exits, nor returns a failure result.
        - CRITICAL: Use ERROR instead; process termination is communicated via exit codes, not log level

- **Log Format**

    Colorized (terminal):
    ```
    <green>{time:YYYY-MM-DD HH:mm:ss}</green> <blue>[{level}]</blue><magenta> trace_id=...</magenta><yellow> correlation_id=...</yellow> <cyan>{extra[logger_name]}</cyan> - <level>{message}</level>
    ```
    Plain (JSON sink / OTEL):
    ```
    {time:YYYY-MM-DD HH:mm:ss} [{level}] trace_id=... correlation_id=... {extra[logger_name]} - {message}
    ```
    `trace_id` and `correlation_id` are injected by `LogInterceptor` / `AtlanLoggerAdapter` — see `application_sdk/observability/logger_adaptor.py`.

    **Stream routing**: WARNING and below are written to **stdout**; ERROR and CRITICAL are written to **stderr**. This maps to GCP Cloud Logging severity correctly, but operators tailing `stderr` to collect warnings will not see them — use a log aggregator that captures both streams.

- **Logging Best Practices**
    - Include relevant context in log messages
    - Use structured logging for machine processing
    - Log exceptions with stack traces
    - Include request IDs in distributed systems
    - Use appropriate log levels
    - Avoid logging sensitive information
    - Include workflow and activity context in Temporal workflows

- **Example Usage**
    ```python
    from application_sdk.observability import get_logger

    logger = get_logger(__name__)

    # Basic logging
    logger.info("Operation completed successfully")

    # Logging with context — embed values in the message body using %-style
    logger.info("Processing request request_id=%s user=%s", request_id, user)

    # A handled failure: the code recovers, so the record is a WARNING
    try:
        client.close()
    except Exception:
        logger.warning("Close failed; continuing; client=%s", client_name, exc_info=True)

    # A failed operation: the failure reaches the caller as a result, so ERROR
    try:
        await client.authenticate()
    except Exception as e:
        logger.error("Auth test failed; error_type=%s", type(e).__name__, exc_info=True)
        return AuthOutput(status=AuthStatus.FAILED)
    ```

    An exception you re-raise is logged once, where it is finally handled, not
    at every layer it passes through (conformance L009).

- **Observability self-failures**: a failure inside the logging, metrics or
  tracing pipeline itself (an exporter, sink or buffer) is handled — the
  application carries on — so it is a WARNING. Log it through
  `self_failure_logger` from `application_sdk.observability.utils`: a stdlib
  logger, so a broken sink cannot recurse into itself, rate-limited to one
  record per message per minute, so a sink that fails on every record does
  not flood the log.
