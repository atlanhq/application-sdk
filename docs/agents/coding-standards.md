# Coding Standards

- Primary coding standards live in `docs/standards/coding.md` (formatting, naming, docstrings for all functions/classes/modules).
- Logging, exception, and performance guidance are in `docs/standards/logging.md`, `docs/standards/exceptions.md`, and `docs/standards/performance.md`.
- Metric / Prometheus label cardinality rules are in `docs/standards/metrics.md` — read before adding a `record_metric()` call or a new OTel instrument.
- Tooling enforcement is defined in `.pre-commit-config.yaml` (ruff, isort, pyright).

## Key Rules Enforced by Pre-commit

1. **No bare `except:`** - Always use `except Exception:` or a specific exception type
2. **No useless f-strings** - Don't use `f"string"` if there are no `{placeholders}`
3. **No Unicode in print statements** - Windows CI fails on emojis (`✓`, `❌`, `⚠️`). Use ASCII: `PASS`, `FAIL`, `WARNING`
4. **Import sorting** - Imports must be sorted (isort with black profile)
5. **Type hints** - Pyright enforces type checking
6. **Conventional commits** - Commit messages must follow conventional format (e.g., `fix:`, `feat:`, `chore:`)
7. **No customer names** - Never reference customer names, tenant names, run IDs, or any customer-identifiable information in code, comments, docstrings, commit messages, or PR descriptions. Use generic language: "a production incident", "a prior RCA".

## Serialization & Type Systems

Use the right type system for each zone:

| Zone | Type System | When to Use | Example |
|------|-------------|-------------|---------|
| **Temporal contracts** | `pydantic.BaseModel` | Anything serialized through Temporal wire (workflow/activity I/O) | `Input`, `Output`, `FileReference`, `CredentialRef` |
| **High-volume / low-level** | `msgspec.Struct` or plain dicts | Performance-critical paths: pyatlan_v9 asset types, logging internals | pyatlan_v9 types, log record construction |

**Rules:**
- All contracts (`Input`, `Output`, `HeartbeatDetails`, `Record`, `FileReference`, `CredentialRef`, credential types) **MUST** be frozen `pydantic.BaseModel` subclasses (typically via helpers in `contracts/base.py` or `contracts/types.py`; `CredentialRef` and credential types live in `application_sdk.credentials`). They are serialised through Temporal via `pydantic_data_converter`.
- Define contracts as plain class bodies — no `@dataclass` decorator. Pydantic handles `__init__`, validation, and serialization automatically.
- For frozen (immutable) contracts (e.g., `FileReference`, `CredentialRef`): use `class Foo(BaseModel, frozen=True)` or `model_config = ConfigDict(frozen=True)`.
- Use `Field(default_factory=...)` for mutable defaults (lists, dicts, nested models). Do **not** use `__post_init__` — that is a dataclass pattern.
- Avoid Pydantic on high-volume paths (e.g., every log line). Use plain dicts instead — Pydantic validation overhead accumulates significantly.
- Always use Pydantic v2 `model_config = ConfigDict(...)` style. Do not use the v1 inner `class Config:` pattern.

## Large Payloads and FileReference

Use `FileReference` for any data that cannot fit in Temporal's 2 MB payload limit.
See `docs/concepts/file-reference.md` for the full guide: decision matrix, lifecycle,
the `Lazy()` marker for selective materialization, dedup behaviour, and observability events.

## Replacing `ParquetFileWriter` / `JsonFileWriter` (v4.0 removal path)

`ParquetFileWriter`, `JsonFileWriter`, `ParquetFileReader`, and `JsonFileReader`
emit `DeprecationWarning` on construction and **will be removed in v4.0**.
Migrate now — the replacement pattern is fully supported and fully optimised
(SHA-256 dedup + sidecars + parallel transfers via `_gather_with_semaphore`),
copy-paste ready.

The replacement is the same shape for every format:

1. Pick an output directory the writer alone owns (so the resulting
   `FileReference` covers only your chunks, never sibling content).
2. Write each chunk to a file on disk using whichever library suits the
   format (`pandas.to_parquet`, `orjson.dumps`, etc.).
3. Surface a `FileReference.from_local(output_dir)` on your task's typed
   Output. The Temporal activity interceptor calls `persist_file_refs`
   automatically on task return — you write **no** upload code.

### Copy-paste — Parquet writer

```python
import os
import uuid
from pathlib import Path

import pandas as pd

from application_sdk.contracts.types import FileReference


async def extract_users(self, inp: ExtractInput) -> ExtractOutput:
    # Writer-owned subdir: nothing else writes here, so the FileReference
    # below covers only what this task wrote.
    output_dir = os.path.join(inp.output_path, f"users_{uuid.uuid4().hex[:8]}")
    os.makedirs(output_dir, exist_ok=True)

    chunk_index = 0
    async for df in self._stream_users():
        chunk_path = os.path.join(output_dir, f"chunk-{chunk_index}.parquet")
        df.to_parquet(chunk_path)
        chunk_index += 1
        # Optional: heartbeat between chunks for long-running activities.
        # from temporalio import activity
        # activity.heartbeat(f"wrote {chunk_path}")

    return ExtractOutput(data=FileReference.from_local(output_dir))
```

### Copy-paste — JSON writer (line-delimited)

```python
import os
import uuid

import orjson

from application_sdk.contracts.types import FileReference


async def extract_events(self, inp: ExtractInput) -> ExtractOutput:
    output_dir = os.path.join(inp.output_path, f"events_{uuid.uuid4().hex[:8]}")
    os.makedirs(output_dir, exist_ok=True)

    chunk_index = 0
    async for records in self._stream_events():
        chunk_path = os.path.join(output_dir, f"chunk-{chunk_index}.json")
        with open(chunk_path, "wb") as f:
            for record in records:
                f.write(orjson.dumps(record))
                f.write(b"\n")
        chunk_index += 1
        # Optional: heartbeat between chunks.
        # activity.heartbeat(f"wrote {chunk_path}")

    return ExtractOutput(data=FileReference.from_local(output_dir))
```

### Reading a `FileReference` (replaces `ParquetFileReader` / `JsonFileReader`)

Declare the upstream artifact as a `FileReference` field on the consuming
task's typed Input. The activity interceptor auto-materialises it to a local
path before the task runs (with sidecar verification + parallel transfers).
Read it directly with the library of your choice:

```python
class TransformInput(Input):
    data: FileReference                              # auto-materialised

async def transform_users(self, inp: TransformInput) -> TransformOutput:
    df = pd.read_parquet(inp.data.local_path)        # or daft.read_parquet
    ...
```

For JSON inputs, swap `pd.read_parquet` for an `orjson.loads` loop over the
file lines. No `ParquetFileReader` / `JsonFileReader` construction required —
they exist only to bridge the legacy inline-upload contract and will be
deleted in v4.0.

### Transitional opt-in on the legacy `ParquetFileWriter`

If you cannot migrate to the direct pattern immediately but want the SHA-256
+ parallel-transfer benefits today, `ParquetFileWriter` accepts a
`defer_uploads=True` flag. Default (`False`) preserves the pre-3.8 inline-
upload behaviour so existing call sites are unaffected; `True` switches to
the `FileReference` boundary. `close()` always returns a `WriterResult` that
subclasses `TaskStatistics` (so `result.total_record_count` etc. continue to
work via inheritance) and gains a `result.files: FileReference | None`
field — `None` in default mode (no double-upload risk), ephemeral in opt-in
mode.

```python
async with ParquetFileWriter(
    path=base, typename="users", defer_uploads=True,
) as writer:
    await writer.write(df)
result = writer.last_result
return MyOutput(statistics=result, data=result.files)
```

The opt-in flag is a bridge for in-flight migrations only. New code should
go straight to the direct copy-paste pattern above.

## Before Every Commit

```bash
uv run pre-commit run --files <changed-files>
```

Or install hooks to run automatically:
```bash
uv run pre-commit install
```
