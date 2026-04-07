---
name: activities
description: Create Temporal activities with state management and heartbeating
user-invocable: true
---

# Build an Activity

## Process

1. Read `docs/decisions/0001-temporal-activity-patterns.md` for state management and heartbeating patterns
2. Create or extend a state class if custom fields are needed (beyond `handler` and `workflow_args`)
3. Create the activity class extending `ActivitiesInterface[YourHandlerType]`
4. Implement each activity method with both decorators:
   - `@activity.defn` — registers with Temporal
   - `@auto_heartbeater` — sends periodic heartbeats for I/O operations
5. Access state exclusively via `await self._get_state()` — never store workflow-specific data in instance variables
6. Return `ActivityResult` from each method
7. State is auto-refreshed after 15 minutes — no manual refresh needed
8. Clean up state via `_clean_state()` in the final activity of a workflow
9. Run pre-commit and create tests

## Key References

- `application_sdk/activities/__init__.py` — `ActivitiesInterface`, `ActivitiesState`, `_get_state()`, `_set_state()`
- `application_sdk/activities/common/utils.py` — `@auto_heartbeater`, `build_output_path()`, `get_workflow_id()`
- `application_sdk/constants.py` — `HEARTBEAT_TIMEOUT` (300s), `START_TO_CLOSE_TIMEOUT` (2h)

## Handling `$ARGUMENTS`

If arguments specify the handler type or activity purpose, use them to name the class and determine which operations the activities perform.
