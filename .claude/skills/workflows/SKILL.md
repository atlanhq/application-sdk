---
name: workflows
description: Compose Temporal workflows with retry policies and timeouts
user-invocable: true
---

# Build a Workflow

## Process

1. Read `docs/decisions/0001-temporal-activity-patterns.md` for workflow patterns
2. Create a `@workflow.defn` class extending `WorkflowInterface[YourActivitiesType]`
3. Set `activities_cls` static attribute to your activities class
4. Implement `get_activities()` returning the list of activity methods to register with the worker
5. Override `run()` with `@workflow.run`:
   - Call `get_workflow_args` to load config from state store
   - Call `preflight_check` to validate before execution
   - Execute your activity sequence with proper timeouts and retry policies
6. Use constants for timeouts:
   - `HEARTBEAT_TIMEOUT` (300s) for heartbeat intervals
   - `START_TO_CLOSE_TIMEOUT` (2h) for activity execution limits
7. Ensure determinism: no `random`, no `datetime.now()`, no I/O, no `asyncio.sleep()` (use `workflow.sleep()`)
8. Run pre-commit and create tests

## Key References

- `application_sdk/workflows/__init__.py` — `WorkflowInterface`, default `run()` pattern
- `application_sdk/constants.py` — timeout constants, retry defaults

## Handling `$ARGUMENTS`

If arguments describe the workflow's purpose or the activities it orchestrates, use them to structure the activity execution sequence in `run()`.
