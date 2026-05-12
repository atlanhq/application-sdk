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

## Writer.close() contract (3.7.0+)

`ParquetFileWriter.close()` (and other `Writer` subclasses) returns a `WriterResult`
with two fields:

- `result.statistics` — `TaskStatistics` (record count, chunk count, partitions, typename)
- `result.files` — ephemeral `FileReference` scoped to the writer's output directory

Pass `result.files` through your task's typed Output and the activity interceptor
auto-uploads it (SHA-256 sidecars + parallel transfers via `_gather_with_semaphore`).
Do not call `persist_file_reference` yourself, and do not reconstruct paths with
`os.path.join` / `FileReference.from_local`.

```python
async with ParquetFileWriter(path=base, typename="users") as writer:
    await writer.write(df)
result = writer.last_result  # WriterResult
return MyOutput(statistics=result.statistics, data=result.files)
```

The writer creates its own sub-directory (`<typename>` if set, else `_parquet_<uuid>`)
so the resulting `FileReference` covers only the chunks this writer wrote — never
sibling content in the caller's `path`. Skipping `close()` means no `WriterResult`
and no Output, so the contract is structurally enforced (loud `NameError`, not silent
data loss). Use `async with` whenever possible.

## Before Every Commit

```bash
uv run pre-commit run --files <changed-files>
```

Or install hooks to run automatically:
```bash
uv run pre-commit install
```
