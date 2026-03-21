# 0001: Temporal Activity Patterns

**Status**: Accepted

## Context

The SDK needs a consistent pattern for Temporal activities that:
- Supports multiple concurrent workflow executions without state leakage
- Handles long-running I/O operations with heartbeating
- Provides type-safe state management across activity methods
- Auto-refreshes stale credentials and configuration

## Decision

### Class-based activities with Generic state

Activities extend `ActivitiesInterface[StateType]`, a generic base that parameterizes the handler type. This gives each activity class a typed `_state` dictionary keyed by `workflow_id`, ensuring isolation between concurrent executions.

```python
class MyActivities(ActivitiesInterface[MyHandler]):
    @activity.defn
    @auto_heartbeater
    async def fetch_data(self) -> ActivityResult:
        state = await self._get_state()
        handler = state.handler
        # ... use handler to fetch data
```

### `_state` dict keyed by workflow_id

Each workflow execution gets its own `ActivitiesState` entry in `self._state[workflow_id]`. State is initialized via `_set_state()` during `get_workflow_args` and cleaned up via `_clean_state()` after workflow completion.

### `@auto_heartbeater` on all I/O activities

The `@auto_heartbeater` decorator wraps activity methods to send periodic heartbeats at `heartbeat_timeout / 3` intervals. This prevents Temporal from considering long-running activities as dead. Every `@activity.defn` method that performs I/O **must** use this decorator.

### State auto-refresh after 15 minutes

`_get_state()` checks `last_updated_timestamp` on each access. If the state is older than 15 minutes, it re-fetches workflow args from the state store, re-initializes the handler, and updates the timestamp. This ensures long-running workflows pick up credential rotations and config changes.

## Consequences

- **Pro**: Type-safe, isolated state per workflow execution
- **Pro**: Automatic heartbeating reduces boilerplate and prevents timeout failures
- **Pro**: Stale credentials are auto-refreshed without workflow restarts
- **Con**: `_state` dict grows with concurrent executions (mitigated by `_clean_state()`)
- **Con**: `@auto_heartbeater` must be manually applied (caught by code review)

## References

- `application_sdk/activities/__init__.py` — `ActivitiesInterface`, `_get_state()`, `_set_state()`
- `application_sdk/activities/common/utils.py` — `@auto_heartbeater`, `send_periodic_heartbeat()`
- `application_sdk/constants.py` — `HEARTBEAT_TIMEOUT`, `START_TO_CLOSE_TIMEOUT`
