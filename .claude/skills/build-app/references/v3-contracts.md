# V3 Contracts — Quick Reference

## Base Classes

All contracts live in `application_sdk.contracts.base`:

- `Input` — for `run()` and `@task` method parameters
- `Output` — for `run()` and `@task` method return types
- `HeartbeatDetails` — for typed heartbeat progress
- `Record` — for domain data records (has mandatory `id: str`)
- `SerializableEnum` — for enums that cross Temporal boundaries

## Built-in Fields on Input

Every `Input` subclass automatically has:
- `workflow_id: str = ""` — set by framework at dispatch time
- `correlation_id: str = ""` — caller-supplied tracing ID

## Payload Safety (Validated at Import Time)

### Forbidden Types

| Type | Why | Fix |
|------|-----|-----|
| `Any` | Cannot validate size | Use concrete types |
| `bytes` / `bytearray` | Binary data can be huge | Use `FileReference` |
| `list[T]` (unbounded) | Can grow past 2MB limit | Use `Annotated[list[T], MaxItems(N)]` |
| `dict[K, V]` (unbounded) | Can grow past 2MB limit | Use `Annotated[dict[K, V], MaxItems(N)]` or `allow_unbounded_fields=True` on top-level Input only |

### Safe Types

```python
from typing import Annotated
from application_sdk.contracts.types import FileReference, MaxItems

class SafeInput(Input):
    name: str                                          # Simple scalar
    count: int = 0                                     # With default
    tags: Annotated[list[str], MaxItems(100)]          # Bounded list
    config: Annotated[dict[str, str], MaxItems(50)]    # Bounded dict
    data_ref: FileReference = FileReference()           # Large data
    status: MyStatus = MyStatus.PENDING                # Enum
```

### Escape Hatch: allow_unbounded_fields

ONLY for top-level `run()` Input that receives arbitrary dicts from the Atlan platform:

```python
class MyRunInput(Input, allow_unbounded_fields=True):
    credential_guid: str = ""
    connection: dict[str, Any] = {}   # From Atlan platform
    metadata: dict[str, Any] = {}     # From Atlan platform
```

Never use this on task-level contracts.

## FileReference

For data larger than ~1MB:

```python
from application_sdk.contracts.types import FileReference

class FetchOutput(Output):
    results: FileReference = FileReference()

# In task:
@task(timeout_seconds=600)
async def fetch(self, input: FetchInput) -> FetchOutput:
    # Write data to local file
    write_data("/tmp/output/results.parquet", data)
    # Create reference
    ref = FileReference.from_local("/tmp/output/results.parquet")
    return FetchOutput(results=ref)

# In run(): upload to object store
up = await self.upload(UploadInput(local_path="/tmp/output/"))
```

## SerializableEnum

For enums in contracts:

```python
from application_sdk.contracts.base import SerializableEnum

class ExtractionStatus(SerializableEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
```

Must inherit from `SerializableEnum` (which is `StrEnum`), not plain `Enum`.

## Evolution Rules

Contracts may be used by running workflows. Changes must be backwards-compatible:

| Safe | Unsafe |
|------|--------|
| Add field with default value | Remove field |
| Add new enum value | Rename field |
| | Change field type |
| | Remove default from field |

## HeartbeatDetails

For resumable long-running tasks:

```python
from application_sdk.contracts.base import HeartbeatDetails

class FetchProgress(HeartbeatDetails):
    last_cursor: str = ""
    records_done: int = 0

@task(heartbeat_timeout_seconds=60)
async def fetch_all(self, input: FetchInput) -> FetchOutput:
    prev = self.get_heartbeat_details(FetchProgress)
    cursor = prev.last_cursor if prev else ""

    while True:
        batch, next_cursor = await fetch_batch(cursor)
        process(batch)
        self.heartbeat(FetchProgress(last_cursor=next_cursor, records_done=count))
        if not next_cursor:
            break
```
