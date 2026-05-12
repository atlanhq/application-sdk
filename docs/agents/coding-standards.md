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
Migrate now — the replacement is fully supported and fully optimised
(SHA-256 dedup + sidecars + parallel transfers via `_gather_with_semaphore`).

### Recommended: `RollingFileWriter`

`application_sdk/storage/rolling.py` provides a small format-agnostic helper
that encapsulates the common pattern:

- Owns a scoped output sub-directory (so the resulting `FileReference`
  covers only your chunks — never sibling content in `base_path`).
- Buffers appended batches and rolls over to a new chunk file every
  `chunk_interval_seconds` of wall clock (default 60s).
- Optional `on_chunk_complete(chunk_index, chunk_path)` callback wires
  cleanly to `activity.heartbeat(...)` for within-heartbeat checkpointing.
- `writer.file_reference` returns an ephemeral `FileReference` for your
  typed Output; the activity interceptor uploads on task return.

**Why time-based rollover (not record-count):** the SDK runs in two extreme
regimes — slow JDBC streams (~200 rows/min) and fast msgspec transforms
(~10 000 records/ms). A record-count threshold like `chunk_size=10_000`
either never trips (slow stream → one huge final file, blocks heartbeat)
or trips every millisecond (fast stream → thousands of tiny files).
A wall-clock interval gives both regimes the same predictable checkpoint
cadence regardless of throughput.

#### Parquet (pandas)

```python
import pandas as pd

from application_sdk.contracts.types import FileReference
from application_sdk.storage.rolling import RollingFileWriter


def _flush_parquet(batches: list[pd.DataFrame], path: str) -> None:
    pd.concat(batches, ignore_index=True).to_parquet(path)


async def extract_users(self, inp: ExtractInput) -> ExtractOutput:
    async with RollingFileWriter[pd.DataFrame](
        base_path=inp.output_path,
        extension=".parquet",
        flush_fn=_flush_parquet,
        chunk_interval_seconds=60.0,
        on_chunk_complete=self._heartbeat_chunk,   # optional — see below
    ) as writer:
        async for df in self._stream_users():
            await writer.append(df)
    return ExtractOutput(data=writer.file_reference)


async def _heartbeat_chunk(self, chunk_index: int, chunk_path: str) -> None:
    from temporalio import activity  # noqa: PLC0415
    activity.heartbeat(f"wrote {chunk_path}")
```

#### JSON (line-delimited, `orjson`)

```python
import orjson

from application_sdk.contracts.types import FileReference
from application_sdk.storage.rolling import RollingFileWriter


def _flush_jsonl(batches: list[list[dict]], path: str) -> None:
    with open(path, "wb") as f:
        for batch in batches:
            for record in batch:
                f.write(orjson.dumps(record))
                f.write(b"\n")


async def extract_events(self, inp: ExtractInput) -> ExtractOutput:
    async with RollingFileWriter[list[dict]](
        base_path=inp.output_path,
        extension=".json",
        flush_fn=_flush_jsonl,
        chunk_interval_seconds=60.0,
    ) as writer:
        async for records in self._stream_events():
            await writer.append(records)
    return ExtractOutput(data=writer.file_reference)
```

To support a new format, write a 3-line `flush_fn` and plug it in. CSV via
`pandas.to_csv`, Arrow IPC via `pyarrow.ipc`, msgpack via `msgpack.pack` —
all the same shape.

### Alternative: bare copy-paste (no helper class)

If you want zero new dependencies, the loop the helper wraps is small enough
to inline directly.

**Parquet:**

```python
import os
import uuid
import pandas as pd
from application_sdk.contracts.types import FileReference


async def extract_users(self, inp: ExtractInput) -> ExtractOutput:
    output_dir = os.path.join(inp.output_path, f"users_{uuid.uuid4().hex[:8]}")
    os.makedirs(output_dir, exist_ok=True)
    chunk_index = 0
    async for df in self._stream_users():
        df.to_parquet(os.path.join(output_dir, f"chunk-{chunk_index}.parquet"))
        chunk_index += 1
    return ExtractOutput(data=FileReference.from_local(output_dir))
```

**JSON (line-delimited):**

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
        with open(os.path.join(output_dir, f"chunk-{chunk_index}.json"), "wb") as f:
            for record in records:
                f.write(orjson.dumps(record))
                f.write(b"\n")
        chunk_index += 1
    return ExtractOutput(data=FileReference.from_local(output_dir))
```

The bare pattern has no rollover or heartbeat awareness — each iteration of
your stream loop produces one chunk file. Prefer `RollingFileWriter` when
you want predictable checkpoint cadence on long-running activities.

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
