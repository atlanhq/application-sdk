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
Migrate now — the replacement pattern is fully supported, fully optimised
(SHA-256 dedup + sidecars + parallel transfers via `_gather_with_semaphore`),
and copy-paste ready.

The replacement is the same shape for every format: write your chunks locally
with whichever library suits the format, then surface an ephemeral
`FileReference` on your task's typed Output. The Temporal activity interceptor
calls `persist_file_refs` automatically on task return — you write **no**
upload code.

### Copy-paste — Parquet writer

```python
import pandas as pd

from application_sdk.contracts.types import FileReference
from application_sdk.storage.chunked import TimeChunkedWriter


def _flush_parquet(batches: list[pd.DataFrame], path: str) -> None:
    """Combine buffered batches into one parquet chunk file on disk."""
    pd.concat(batches, ignore_index=True).to_parquet(path)


async def extract_users(self, inp: ExtractInput) -> ExtractOutput:
    async with TimeChunkedWriter[pd.DataFrame](
        base_path=inp.output_path,
        extension=".parquet",
        flush_fn=_flush_parquet,
        chunk_interval_seconds=60.0,                 # roll once per minute
        on_chunk_complete=self._heartbeat_chunk,     # optional; see below
    ) as writer:
        async for df in self._stream_users():
            await writer.append(df)
    return ExtractOutput(data=writer.file_reference)


async def _heartbeat_chunk(self, chunk_index: int, chunk_path: str) -> None:
    from temporalio import activity                  # noqa: PLC0415
    activity.heartbeat(f"wrote {chunk_path}")
```

### Copy-paste — JSON writer (line-delimited)

```python
import orjson

from application_sdk.contracts.types import FileReference
from application_sdk.storage.chunked import TimeChunkedWriter


def _flush_jsonl(batches: list[list[dict]], path: str) -> None:
    """Write buffered record batches as one line-delimited JSON file."""
    with open(path, "wb") as f:
        for batch in batches:
            for record in batch:
                f.write(orjson.dumps(record))
                f.write(b"\n")


async def extract_events(self, inp: ExtractInput) -> ExtractOutput:
    async with TimeChunkedWriter[list[dict]](
        base_path=inp.output_path,
        extension=".json",
        flush_fn=_flush_jsonl,
        chunk_interval_seconds=60.0,
    ) as writer:
        async for records in self._stream_events():
            await writer.append(records)
    return ExtractOutput(data=writer.file_reference)
```

### Why `TimeChunkedWriter` (and why time-based rollover)

The two copy-paste blocks above are nearly identical because the only
format-specific piece is `flush_fn`. Everything else — scoped output
sub-directory, chunk rollover, optional heartbeat, `FileReference`
construction — is common, so `TimeChunkedWriter` encapsulates it. To add a
new format (CSV, msgpack, Arrow IPC, anything), write a 3-line `flush_fn` and
plug it in.

The rollover trigger is **wall-clock time**, not record count. This matters
because the SDK runs in two extreme regimes:

- **Slow JDBC streams** (legacy connectors): 200 rows/min. A "10 000 records
  per chunk" threshold would never trigger inside a single activity, producing
  one huge final file and blocking the Temporal heartbeat during the flush.
- **Fast msgspec transforms**: 10 000 records/ms. The same record-count
  threshold would fire every 1 ms, drowning the activity in tiny files and
  starving real work of CPU.

`chunk_interval_seconds=60.0` gives both regimes the same checkpoint cadence:
roughly once per minute. Pair it with the `on_chunk_complete` callback
(`activity.heartbeat(...)`) and your activity heartbeats at exactly the rate
you want, regardless of upstream throughput. The writer-owned sub-directory
guarantees the resulting `FileReference` covers only the chunks this writer
wrote, even when `base_path` is a shared directory like `/tmp`.

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

For JSON inputs, swap `pd.read_parquet` for `orjson.loads` over the file
lines. No `ParquetFileReader` / `JsonFileReader` construction required —
they exist only to bridge the legacy inline-upload contract and will be
deleted in v4.0.

### Transitional opt-in on the legacy `ParquetFileWriter`

If you cannot migrate to `TimeChunkedWriter` immediately but want the
SHA-256 / parallel-transfer benefits today, `ParquetFileWriter` accepts a
`defer_uploads=True` flag. Default (`False`) preserves the pre-3.7 inline-
upload behaviour so existing call sites are unaffected; `True` switches to
the FileReference boundary. `close()` always returns a `WriterResult`
which subclasses `TaskStatistics` (so `result.total_record_count` etc.
continue to work via inheritance) and gains a `result.files`
`FileReference | None` field — `None` in default mode (no double-upload
risk), ephemeral in opt-in mode.

```python
async with ParquetFileWriter(
    path=base, typename="users", defer_uploads=True,
) as writer:
    await writer.write(df)
result = writer.last_result
return MyOutput(statistics=result, data=result.files)
```

The opt-in flag is a bridge for in-flight migrations only. New code should
go straight to `TimeChunkedWriter`.

## Before Every Commit

```bash
uv run pre-commit run --files <changed-files>
```

Or install hooks to run automatically:
```bash
uv run pre-commit install
```
