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
        - WARNING: Warning messages for potential issues
        - ERROR: Error messages for recoverable errors
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
    `trace_id` and `correlation_id` are injected by `CorrelationContextInterceptor` / `AtlanLoggerAdapter` — see `application_sdk/observability/logger_adaptor.py`.

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

    # Error logging
    try:
        # Some operation
        pass
    except Exception as e:
        logger.error("Operation failed; operation=%s error=%s", operation_name, e, exc_info=True)
    ```
