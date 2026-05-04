# Contracts

Every boundary in the SDK — `run()`, `@task`, and `Handler` methods — uses exactly one `Input` model and one `Output` model. The framework validates this contract at class-definition time, before any code runs.

---

## Input and Output

`Input` and `Output` are Pydantic `BaseModel` subclasses with extra restrictions:

```python
from application_sdk.contracts import Input, Output

class ExtractInput(Input):
    connection_id: str
    max_records: int = 1000   # default = backwards compatible

class ExtractOutput(Output):
    record_count: int
    checkpoint: str
```

**Rules:**

- Add new fields with defaults — backwards compatible across workflow versions.
- Never remove fields or change their types — Temporal serialises task inputs/outputs; a type mismatch on replay crashes the worker.
- `Any`, `bytes`, and unbounded `list`/`dict` are rejected at class-definition time (see [Payload Safety](#payload-safety) below).

### Importing

```python
# Preferred: top-level re-export
from application_sdk.contracts import Input, Output, HeartbeatDetails

# Also works
from application_sdk.contracts.base import Input, Output, HeartbeatDetails
```

---

## HeartbeatDetails

`HeartbeatDetails` models the checkpoint state sent by `self.task_context.heartbeat()` and read back by `self.task_context.get_heartbeat_details()`. Define one per long-running task to enable resume-after-failure.

```python
from application_sdk.contracts import HeartbeatDetails

class PageProgress(HeartbeatDetails):
    last_page: int = 0
    records_done: int = 0

class MyConnector(App):
    @task(timeout_seconds=3600, auto_heartbeat_seconds=10)
    async def paginate(self, input: PaginateInput) -> PaginateOutput:
        prev = self.task_context.get_heartbeat_details(PageProgress)
        start_page = prev.last_page if prev else 0
        records_done = prev.records_done if prev else 0

        async for page in fetch_pages(start=start_page):
            records_done += page.count
            self.task_context.heartbeat(
                PageProgress(last_page=page.number, records_done=records_done)
            )

        return PaginateOutput(total=records_done)
```

Note: `heartbeat()` and `get_heartbeat_details()` are **synchronous** — do not `await` them.

---

## Payload Safety

The framework rejects unsafe field types at class-definition time:

| Type | Status | Safe alternative |
|------|--------|-----------------|
| `Any` | Rejected | Use a concrete type |
| `bytes` | Rejected | Use `FileReference` |
| `bytearray` | Rejected | Use `FileReference` |
| `list[T]` (unbounded) | Rejected | Use `Annotated[list[T], MaxItems(N)]` |
| `dict[str, Any]` (unbounded) | Rejected | Use a typed Pydantic model |

This prevents Temporal's 2 MB payload limit from being hit silently in production.

### MaxItems

```python
from typing import Annotated
from application_sdk.contracts import Input, MaxItems

class BatchInput(Input):
    ids: Annotated[list[str], MaxItems(500)]
```

The `MaxItems` annotation enforces a maximum list length both at class-definition time and at runtime validation.

---

## FileReference

Use `FileReference` to pass large data between tasks through object storage rather than the Temporal payload:

```python
from application_sdk.contracts import Input, Output, FileReference, UploadInput

class FetchOutput(Output):
    data_file: FileReference

class TransformInput(Input):
    data_file: FileReference

class TransformOutput(Output):
    output_path: str
    record_count: int

class ExtractionOutput(Output):
    output_file: FileReference
    record_count: int

class MyConnector(App):
    @task
    async def fetch_data(self, input: FetchInput) -> FetchOutput:
        path = "/tmp/data.parquet"
        write_parquet(path, records)
        return FetchOutput(data_file=FileReference.from_local(path))

    @task
    async def transform(self, input: TransformInput) -> TransformOutput:
        df = read_parquet(input.data_file.local_path)
        ...
        return TransformOutput(output_path="/tmp/output.parquet", record_count=len(df))

    async def run(self, input: ExtractionInput) -> ExtractionOutput:
        fetch = await self.fetch_data(FetchInput(...))
        # FileReference passes through the contract automatically — no manual upload between tasks.
        transformed = await self.transform(TransformInput(data_file=fetch.data_file))
        # Upload the final result at the end of run(), after all processing is complete.
        up = await self.upload(UploadInput(local_path=transformed.output_path))
        return ExtractionOutput(output_file=up.ref, record_count=transformed.record_count)
```

See [Storage](storage.md) for full `FileReference` and `StorageTier` documentation.

---

## GitReference

`GitReference` passes a git repository reference in a task contract:

```python
from application_sdk.contracts import GitReference

class DeployInput(Input):
    repo: GitReference
```

`GitReference` fields: `repo_url` (required), `branch` (default `"main"`), `path` (default `""`), `tag` (default `""`), `commit` (default `""`), optional `credential` (`CredentialRef | None`). Checkout precedence: commit > tag > branch.

---

## SerializableEnum

Use `SerializableEnum` for enum fields in contracts. It serialises as a string in Temporal payloads (unlike standard `Enum`, which can fail to round-trip):

```python
from application_sdk.contracts import SerializableEnum

class ExtractionMode(SerializableEnum):
    FULL = "full"
    INCREMENTAL = "incremental"

class ExtractInput(Input):
    mode: ExtractionMode = ExtractionMode.FULL
```

---

## Evolution Rules

| Change | Safe? | Notes |
|--------|-------|-------|
| Add a field with a default | Yes | Old workers ignore it; new workers use the default |
| Add a required field | No | Breaks existing in-flight workflows on replay |
| Remove a field | No | Old payloads still have the field; deserialization fails |
| Change a field type | No | Payload deserialization may fail silently |
| Rename a field | No | Equivalent to remove + add; breaks in-flight workflows |

For breaking changes, version your contract models (`ExtractInputV2`) and migrate using a new task queue deployment.

---

## Handler Contracts

Handler endpoint contracts (`AuthInput`, `PreflightInput`, `MetadataInput`, etc.) follow the same rules. See [Handlers](handlers.md) for the full list.
