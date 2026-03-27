# Create Handler

Implement a handler bridging clients and activities.

## Source of Truth

Read these before starting:
- `docs/agents/coding-standards.md` — formatting, naming, type hints

## Reference Implementations

Study these for patterns:
- `application_sdk/handlers/__init__.py` — `HandlerInterface`, configmap loading

## Instructions

1. **Create class** inheriting from `HandlerInterface`
2. **Implement required methods**:
   - `load(*args, **kwargs)` — Initialize the handler with credentials/config, create and load the client
   - `test_auth(*args, **kwargs)` — Validate authentication against the data source
   - `preflight_check(*args, **kwargs)` — Pre-workflow validation (permissions, connectivity)
   - `fetch_metadata(*args, **kwargs)` — Retrieve metadata from the data source
3. **Wire up client initialization** in `load()`:
   ```python
   async def load(self, *args, **kwargs):
       self.client = MyClient(credentials=self.credentials)
       await self.client.load()
   ```
4. **Override `get_configmap()`** if the handler needs custom configuration beyond the contract-generated JSON
5. **Add type hints and docstrings** to all public methods
6. **Run pre-commit**: `uv run pre-commit run --files <your-handler-file>`
7. **Create tests** at `tests/unit/handlers/test_<name>.py`

$ARGUMENTS
