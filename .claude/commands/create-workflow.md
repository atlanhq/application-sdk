# Create Workflow

Compose a Temporal workflow with retry policies and timeouts.

## Source of Truth

Read these before starting:
- `docs/decisions/0001-temporal-activity-patterns.md` — workflow/activity relationship
- `application_sdk/constants.py` — `HEARTBEAT_TIMEOUT`, `START_TO_CLOSE_TIMEOUT`

## Reference Implementations

Study these for patterns:
- `application_sdk/workflows/__init__.py` — `WorkflowInterface`, default `run()` implementation

## Instructions

1. **Create `@workflow.defn` class** extending `WorkflowInterface[YourActivitiesType]`
2. **Set `activities_cls`** as a static attribute pointing to your activities class
3. **Implement `get_activities()`** returning `Sequence[Callable]` of activity methods to register
4. **Override `run()`** with `@workflow.run` decorator:
   ```python
   @workflow.run
   async def run(self, workflow_config: dict[str, Any]) -> Optional[dict[str, Any]]:
       # Call get_workflow_args first
       # Then preflight_check
       # Then your activity sequence
   ```
5. **Set retry and timeout policies** on `workflow.execute_activity()` calls:
   - `heartbeat_timeout=timedelta(seconds=HEARTBEAT_TIMEOUT)`
   - `start_to_close_timeout=timedelta(seconds=START_TO_CLOSE_TIMEOUT)`
   - `retry_policy=RetryPolicy(maximum_attempts=N)`
6. **Ensure determinism** — no `random`, no `datetime.now()`, no I/O, no `asyncio.sleep()` (use `workflow.sleep()`)

$ARGUMENTS
