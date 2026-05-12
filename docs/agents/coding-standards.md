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

## Format-specific writers/readers are deprecated (removal in v4.0)

`ParquetFileWriter`, `JsonFileWriter`, `ParquetFileReader`, and `JsonFileReader`
emit `DeprecationWarning` on construction and are planned for removal in v4.0.
The canonical v4.0 pattern is to write the file with the library of your choice
(`df.to_parquet`, `orjson.dumps + open`, daft's `write_parquet`, etc.) and let
`FileReference` carry the result through your task's typed Output. The activity
interceptor handles upload with SHA-256 sidecars and parallel transfers via
`_gather_with_semaphore` — no caller-side `persist_file_reference` needed.

### Opt-in deferred uploads in 3.x (`ParquetFileWriter`)

`ParquetFileWriter` accepts `defer_uploads=True` to opt into the v4.0-style
contract today, without breaking existing apps. Default (`False`) preserves
the pre-3.7 inline-upload behaviour — apps that don't pass the flag see no
change.

```python
# Opt-in: writer skips all inline uploads. Caller surfaces result.files
# on a typed Output; the activity interceptor uploads it on task return.
async with ParquetFileWriter(
    path=base, typename="users", defer_uploads=True,
) as writer:
    await writer.write(df)
result = writer.last_result            # WriterResult (subclass of TaskStatistics)
return MyOutput(
    statistics=result,                 # `WriterResult` IS a `TaskStatistics`
    data=result.files,                 # ephemeral FileReference scoped to the
                                       # writer's output directory
)
```

`WriterResult` subclasses `TaskStatistics`, so `result.total_record_count`,
`result.chunk_count`, `result.partitions`, and `result.typename` continue to
work — `defer_uploads=True` only adds the `result.files` field. Default mode
returns `result.files = None` so the interceptor never double-uploads files
that are already in the store.

When `defer_uploads=True` is set without `typename`, the writer creates a
scoped sub-directory (`_parquet_<8-char-uuid>`) under `path` so the resulting
`FileReference` covers only the chunks this writer wrote — never sibling
content in the caller's `path`. Default mode (`defer_uploads=False`) preserves
main's path layout unchanged.

Skipping `close()` means no `WriterResult` and no Output payload, so the
contract is structurally enforced (loud `NameError`, not silent data loss).
Use `async with` whenever possible.

## Before Every Commit

```bash
uv run pre-commit run --files <changed-files>
```

Or install hooks to run automatically:
```bash
uv run pre-commit install
```
