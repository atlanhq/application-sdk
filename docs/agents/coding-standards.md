# Coding Standards

- Primary coding standards live in `.cursor/rules/standards.mdc` (formatting, naming, docstrings for all functions/classes/modules).
- Logging, exception, and performance guidance are in `.cursor/rules/logging.mdc`, `.cursor/rules/exception-handling.mdc`, and `.cursor/rules/performance.mdc`.
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
| **Temporal contracts** | Plain `@dataclass` | Anything serialized through Temporal wire (workflow/activity I/O) | `Input`, `Output`, `FileReference`, `CredentialRef` |
| **External boundaries** | `pydantic.BaseModel` | Any type whose shape is owned by external consumers: HTTP payloads, pub/sub event payloads, external config parsing | Handler HTTP DTOs, `contracts/events.py`, Azure cred parsing |
| **High-volume / low-level** | `msgspec.Struct` or plain dicts | Performance-critical paths: pyatlan asset types, logging internals | pyatlan_v9 types, log record construction |

**Rules:**
- `Input`/`Output`/`HeartbeatDetails`/`Record` in `contracts/base.py` **MUST** remain plain `@dataclass`. Pydantic would require a custom `pydantic_data_converter` for Temporal — unwanted overhead and brittleness.
- Pydantic is appropriate at external boundaries where the contract is owned by external consumers: HTTP clients (handlers), pub/sub subscribers (events), or external config sources. Use it where aliases (`eventId`, `incremental-extraction`), `extra="allow"`, `RootModel`, or FastAPI integration are genuine requirements.
- **Events** (`contracts/events.py`) use Pydantic even though they may transit Temporal. The pub/sub payload shape is owned by external subscribers, not the Temporal engine. Use `event.model_dump()` as the adapter when passing event data to Temporal — the resulting dict serialises cleanly through Temporal's JSON data converter.
- Avoid Pydantic on high-volume paths (e.g., every log line). Use plain dicts or dataclasses instead — Pydantic validation overhead accumulates significantly.
- Always use Pydantic v2 `model_config = {...}` style. Do not use the v1 inner `class Config:` pattern.

## Before Every Commit

```bash
uv run pre-commit run --files <changed-files>
```

Or install hooks to run automatically:
```bash
uv run pre-commit install
```
