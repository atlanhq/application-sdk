# 0004: Interceptor Chain Design

**Status**: Accepted

## Context

Temporal workflows and activities need cross-cutting concerns — failure logging, resource cleanup, correlation context propagation, event publishing, and distributed locking. Embedding these directly in activity/workflow code would create tight coupling and make it impossible to enable/disable behaviors independently.

## Decision

### Separate interceptors for each concern

Each cross-cutting concern is implemented as its own interceptor class, inheriting from Temporal's `ActivityInboundInterceptor` or `WorkflowInboundInterceptor`:

| Interceptor | File | Purpose |
|---|---|---|
| Activity Failure Logging | `activity_failure_logging.py` | Logs activity failures with structured context (opt-in via `ENABLE_TEMPORAL_ACTIVITY_FAILURE_LOGGING`) |
| Cleanup | `cleanup.py` | Resource cleanup after activity/workflow completion |
| Correlation Context | `correlation_context.py` | Propagates trace_id, atlan-* headers, and temporal.* context across activity boundaries |
| Events | `events.py` | Publishes application lifecycle events (start, complete, fail) to the event store |
| Lock | `lock.py` | Acquires/releases distributed locks for workflow exclusivity |

### Adding new behavior

To add a new cross-cutting concern:

1. Create a new file in `application_sdk/interceptors/`
2. Implement the appropriate Temporal interceptor interface
3. Register the interceptor in the worker's interceptor chain

No existing interceptor or activity code needs modification.

### Event metadata model

All interceptors share a common `EventMetadata` model (defined in `models.py`) that includes application name, workflow context, activity context, attempt number, and topic name. This ensures consistent event structure across all cross-cutting concerns.

## Consequences

- **Pro**: Each concern is isolated — can be enabled/disabled independently
- **Pro**: New behaviors require zero changes to existing code
- **Pro**: Consistent event metadata across all interceptors
- **Con**: Interceptor ordering matters and is implicit (set at worker registration time)
- **Con**: Debugging cross-cutting behavior requires tracing through multiple interceptor files

## References

- `application_sdk/interceptors/` — all interceptor implementations
- `application_sdk/interceptors/models.py` — `EventMetadata`, `EventTypes`, `ApplicationEventNames`
