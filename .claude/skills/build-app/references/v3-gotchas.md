# V3 Known Gotchas — Learned from Real Builds

These are issues discovered during real app development and migrations. Read before building.

## 1. Handler Discovery

The framework discovers the `Handler` subclass by scanning the same module as the `App` class. If your handler is in a separate file (`app/handler.py`) and the App is in `app/my_app.py`, the SDK will silently use `DefaultHandler`.

**Fix:** Import the handler in your App module:
```python
# app/my_app.py
from app.handler import MyHandler  # noqa: F401 — registers handler
```

Or set the environment variable:
```bash
ATLAN_HANDLER_MODULE=app.handler:MyHandler
```

## 2. Credentials Format in Handlers

Handlers receive credentials as `list[HandlerCredential]` (key-value pairs). The Atlan platform and existing frontends may send the v2 nested dict format (`{host, username, password, extra: {workspace}}`). The SDK normalizes this automatically in `service.py`, but you must handle the flattened format in your handler:

```python
def _credentials_to_dict(credentials: list[HandlerCredential]) -> dict[str, str]:
    return {cred.key: cred.value for cred in credentials}
```

Extra fields arrive with `extra.` prefix: `extra.workspace`, `extra.database`, etc.

## 3. PayloadSafetyError at Import Time

If you see this error when importing your app:
```
PayloadSafetyError: Field 'items' in MyInput uses unsafe type list. Unbounded list may exceed payload limits.
```

Your contract uses a forbidden type. Fix by bounding the list:
```python
# Wrong
items: list[str]

# Right
items: Annotated[list[str], MaxItems(1000)]
```

For top-level run() Input that receives arbitrary config from the platform:
```python
class MyRunInput(Input, allow_unbounded_fields=True):
    connection: dict[str, Any] = {}
```

## 4. Dockerfile CMD Must Be Empty

Do NOT hardcode `CMD ["--mode", "combined"]`. In production, Helm sets `APPLICATION_MODE` env var. The base image's `entrypoint.sh` reads this. Hardcoding the mode overrides Helm's setting and causes worker pods to try starting uvicorn on port -1.

```dockerfile
# WRONG
CMD ["--mode", "combined"]

# CORRECT
CMD []
```

## 5. Accessing self.context Outside @task

`self.context` is only available inside `@task` methods and during `run()` (for logging only). If you try to access it in `__init__` or in a regular method, you get:
```
AppContextError: App context is only available during run() execution.
```

**Fix:** Move infrastructure access into a `@task` method.

## 6. run() Must Be Deterministic

`run()` is replayed by Temporal on worker restart. Non-deterministic code breaks replay:

```python
# WRONG — breaks on replay
async def run(self, input: MyInput) -> MyOutput:
    now = datetime.now()              # Different on each replay
    id = str(uuid.uuid4())            # Different on each replay
    data = await httpx.get(url)       # Side effect in run()

# RIGHT
async def run(self, input: MyInput) -> MyOutput:
    now = self.now()                  # Deterministic
    id = str(self.uuid())             # Deterministic
    data = await self.fetch(input)    # Side effect in @task
```

## 7. Pre-commit Must Exclude Generated Files

If your app uses contract generation (`app/generated/`), exclude it from linting:

```yaml
# .pre-commit-config.yaml
repos:
  - repo: https://github.com/astral-sh/ruff-pre-commit
    hooks:
      - id: ruff
        exclude: app/generated/
      - id: ruff-format
        exclude: app/generated/
```

## 8. Local Dev — Dapr Detection

The SDK checks `DAPR_HTTP_PORT` (not `ATLAN_DAPR_HTTP_PORT`) to detect Dapr. Without this env var, it falls back to InMemory + LocalStore silently. This means Dapr misconfigurations won't be caught until production.

When running manually (not via `atlan app run`):
```bash
export DAPR_HTTP_PORT=3500
uv run python main.py
```

## 9. Task Timeouts

Default `timeout_seconds` is 600 (10 minutes). If your task does large data fetches or slow API calls, increase it. Temporal will kill the activity after the timeout.

```python
@task(
    timeout_seconds=3600,    # 1 hour for large fetches
    heartbeat_timeout_seconds=120,  # Kill if no heartbeat in 2 min
    auto_heartbeat_seconds=30,      # Framework heartbeats every 30s
)
async def large_fetch(self, input: FetchInput) -> FetchOutput:
    ...
```

## 10. Enum Serialization

Regular Python `Enum` won't serialize through Temporal. Use `SerializableEnum`:

```python
# WRONG — fails at serialization
class Status(Enum):
    PENDING = "pending"

# RIGHT
from application_sdk.contracts.base import SerializableEnum

class Status(SerializableEnum):
    PENDING = "pending"
```

## 11. run() Output Must Match Platform Expectations

If your app integrates with the Atlan platform's Automation Engine (AE), the output contract from `run()` must include fields AE reads via JSONPath:

```python
class MyExtractionOutput(Output):
    transformed_data_prefix: str = ""    # AE reads this
    connection_qualified_name: str = ""  # AE reads this
```

Empty or misnamed fields → AE can't find output → publish step fails silently.

## 12. Blocking Code in @task

If your data source client is synchronous (uses `requests`, JDBC, etc.), wrap it:

```python
@task(timeout_seconds=600)
async def fetch(self, input: FetchInput) -> FetchOutput:
    # WRONG — blocks the event loop
    result = sync_client.query("SELECT ...")

    # RIGHT — runs in thread pool
    result = await self.run_in_thread(sync_client.query, "SELECT ...")
    return FetchOutput(count=len(result))
```

## When to Flag SDK Issues

If you encounter behavior that seems like a bug rather than a usage error:

1. **PayloadSafetyError on a type that should be safe** — e.g., `Annotated[list[str], MaxItems(100)]` still raises
2. **self.context is None inside a @task method** — framework should always inject this
3. **Handler gets unexpected credential format** — normalization should handle v2 format
4. **App registration fails silently** — should raise a clear error
5. **run_dev_combined crashes on startup** — not related to your app code

**Action:** Raise a PR against `atlanhq/application-sdk` branch `refactor-v3`, or share the full traceback with the SDK team for triage.
