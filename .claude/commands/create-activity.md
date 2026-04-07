# Create Activity

Create a new Temporal activity with state management and heartbeating.

## Source of Truth

Read these before starting:
- `docs/decisions/0001-temporal-activity-patterns.md` — state management, heartbeating, auto-refresh
- `docs/agents/coding-standards.md` — formatting, naming, type hints

## Reference Implementations

Study these for patterns:
- `application_sdk/activities/__init__.py` — `ActivitiesInterface`, `_get_state()`, `_set_state()`
- `application_sdk/activities/metadata_extraction/` — concrete activity implementations
- `application_sdk/activities/common/utils.py` — `@auto_heartbeater`, `build_output_path()`

## Instructions

1. **Create state class** extending `ActivitiesState[YourHandlerType]` if custom state fields are needed
2. **Create activity class** extending `ActivitiesInterface[YourHandlerType]`
3. **Implement activity methods** with both decorators:
   ```python
   @activity.defn
   @auto_heartbeater
   async def your_activity(self) -> ActivityResult:
       state = await self._get_state()
       # ... implementation
   ```
4. **Access state** via `await self._get_state()` — never store state in instance variables
5. **Return `ActivityResult`** from each activity method
6. **Run pre-commit**: `uv run pre-commit run --files <your-activity-file>`
7. **Create tests** at `tests/unit/activities/test_<name>.py` — mock Temporal context, test state lifecycle

$ARGUMENTS
