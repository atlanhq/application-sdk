# Coding Standards

- Primary coding standards live in `docs/standards/coding.md` (formatting, naming, docstrings for all functions/classes/modules).
- Logging, exception, and performance guidance are in `docs/standards/logging.md`, `docs/standards/exceptions.md`, and `docs/standards/performance.md`.
- Tooling enforcement is defined in `.pre-commit-config.yaml` (ruff, isort, pyright).

## Key Rules Enforced by Pre-commit

1. **No bare `except:`** - Always use `except Exception:` or a specific exception type
2. **No useless f-strings** - Don't use `f"string"` if there are no `{placeholders}`
3. **No Unicode in print statements** - Windows CI fails on emojis (`✓`, `❌`, `⚠️`). Use ASCII: `PASS`, `FAIL`, `WARNING`
4. **Import sorting** - Imports must be sorted (isort with black profile)
5. **Type hints** - Pyright enforces type checking
6. **Conventional commits** - Commit messages must follow conventional format (e.g., `fix:`, `feat:`, `chore:`)

## Serialization & Type Systems

Use the right type system for each zone:

| Zone | Type System | When to Use | Example |
|------|-------------|-------------|---------|
| **Temporal contracts** | `pydantic.BaseModel` | Anything serialized through Temporal wire (workflow/activity I/O) | `Input`, `Output`, `FileReference`, `CredentialRef` |
| **High-volume / low-level** | `msgspec.Struct` or plain dicts | Performance-critical paths: pyatlan asset types, logging internals | pyatlan_v9 types, log record construction |

**Rules:**
- All contracts (`Input`, `Output`, `HeartbeatDetails`, `Record`, `FileReference`, `CredentialRef`, credential types) **MUST** extend the appropriate base class from `contracts/base.py` or `contracts/types.py`. They are `pydantic.BaseModel` subclasses serialized through Temporal via `pydantic_data_converter`.
- Define contracts as plain class bodies — no `@dataclass` decorator. Pydantic handles `__init__`, validation, and serialization automatically.
- For frozen (immutable) contracts (e.g., `FileReference`, `CredentialRef`): use `class Foo(BaseModel, frozen=True)` or `model_config = ConfigDict(frozen=True)`.
- Use `Field(default_factory=...)` for mutable defaults (lists, dicts, nested models). Do **not** use `__post_init__` — that is a dataclass pattern.
- Avoid Pydantic on high-volume paths (e.g., every log line). Use plain dicts instead — Pydantic validation overhead accumulates significantly.
- Always use Pydantic v2 `model_config = ConfigDict(...)` style. Do not use the v1 inner `class Config:` pattern.

## Temporal Determinism (CRITICAL)

Code in `run()` and `@entrypoint` methods MUST be deterministic. Temporal replays workflows from history on worker restart — non-deterministic code corrupts the replay.

| DO | DON'T |
|----|-------|
| `self.now()` | `datetime.now()`, `datetime.utcnow()` |
| `self.uuid()` | `uuid.uuid4()`, `uuid.uuid1()` |
| `await self.my_task(input)` | `await http_client.get(url)` |
| Framework-provided random | `random.random()`, `random.choice()` |

All I/O, network calls, and non-deterministic operations go in `@task` methods.

## Contract Evolution

- NEVER remove or rename fields on Input/Output classes
- NEVER change field types
- Add new fields with defaults only (`field: str = ""`)
- Use `Field(default_factory=list)` for mutable defaults, never `field: list = []`

Breaking a contract silently corrupts in-flight Temporal workflows.

## Blocking Operations

In `@task` methods, wrap blocking calls with `self.run_in_thread()`:

```python
# Wrong — blocks event loop, kills heartbeats
result = requests.get(url)

# Right
result = await self.run_in_thread(requests.get, url)
```

## Before Every Commit

```bash
uv run pre-commit run --files <changed-files>
```

Or install hooks to run automatically:
```bash
uv run pre-commit install
```
