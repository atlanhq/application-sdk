---
name: observability
description: Add structured logging, metrics, and tracing to SDK components
user-invocable: true
---

# Add Observability

## Process

1. **Logging** — Use `AtlanLoggerAdapter` via `get_logger(__name__)`:
   - Import: `from application_sdk.observability.logger_adaptor import get_logger`
   - Initialize at module level: `logger = get_logger(__name__)`
   - Use structured fields: `logger.info("Fetching data", source=source_name, count=n)`
   - Never log sensitive data (credentials, tokens, connection strings)
   - Log levels: DEBUG (dev detail), INFO (operations), WARNING (recoverable), ERROR (failures), CRITICAL (fatal)
   - Custom levels: ACTIVITY (activity lifecycle), METRIC (measurements), TRACING (span data)
2. **Context propagation** — Correlation context (trace_id, atlan-* headers) is auto-propagated via interceptors. No manual setup needed.
3. **OTLP export** — Enabled via `ENABLE_OTLP_LOGS` or `ENABLE_OTLP_WORKFLOW_LOGS` env vars
4. **Parquet sink** — Logs written to Parquet files when `ENABLE_OBSERVABILITY_DAPR_SINK` is set
5. **Periodic flush** — Logger auto-flushes every `LOG_FLUSH_INTERVAL_SECONDS`; force flush on error/critical

## Key References

- `application_sdk/observability/logger_adaptor.py` — `AtlanLoggerAdapter`, `get_logger()`
- `.cursor/rules/logging.mdc` — logging standards and patterns
- `application_sdk/interceptors/correlation_context.py` — context propagation

## Handling `$ARGUMENTS`

If arguments specify a component to instrument, focus on adding appropriate log statements at key execution points (entry, exit, error, key decisions).
