# ADR-0004: Build-Time Type Safety Over Runtime Validation

## Status
**Accepted**

## Context

Type safety in Python can be enforced at two points:
- **Build time**: Static analysis with pyright/mypy, type hints, and import-time validation hooks
- **Runtime**: Validation libraries like Pydantic that check types during execution

We needed to decide when and how to validate contracts (inputs/outputs for Apps and Tasks). The choice affects:
- Performance: Runtime validation adds overhead to every execution
- Developer experience: When and how developers discover type errors
- Tooling compatibility: IDE support, refactoring, static analysis

## Decision

We chose **build-time type safety**: strongly-typed contracts with pyright in CI and `__init_subclass__`/decorator hooks that validate contracts at class-definition time (import time). All contracts use a single type system:

- **All Temporal contracts** (`Input`, `Output`, `HeartbeatDetails`, `Record`, `FileReference`, `CredentialRef`, credential types): `pydantic.BaseModel`. Serialised through Temporal via the official `pydantic_data_converter`. `pydantic_core.to_json` is 8–9x faster than the prior msgspec converter for serialization, which dominates round-trip cost.
- No zone distinction: the two-zone model (dataclass for Temporal, Pydantic for external) has been unified. All contracts are Pydantic.

The build-time principle holds in both zones: pyright enforces type correctness statically, and import-time hooks catch structural violations before any code runs.

## Options Considered

### Option 1: Build-Time Validation with Strongly-Typed Contracts (Chosen)

Contracts are strongly-typed `pydantic.BaseModel` subclasses. Type checking happens through:
1. pyright strict mode in CI
2. `__init_subclass__` hooks that validate contracts at class definition time (import time)
3. `@task` decorator validation at decoration time

```python
# contracts/base.py — validation runs at import time
class Input(BaseModel):
    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        validate_payload_safety(cls)  # raises PayloadSafetyError at import if unsafe
```

```python
# app/task.py — validation runs when decorator is applied
@task
async def fetch_data(self, input: FetchInput) -> FetchOutput:
    # Contract validated at class definition, not at call time
    ...
```

**Pros:**
- **Fast failure**: Type errors discovered at import time or during static analysis
- **IDE support**: Full autocomplete, refactoring, and go-to-definition
- **Static analysis**: pyright can catch type errors across the entire codebase without running code
- **Pydantic validation**: Optional runtime coercion and validation in addition to static types
- **Temporal compatibility**: Pydantic models serialize cleanly through `pydantic_data_converter` (~3–4x faster round-trip than the prior msgspec converter)

**Cons:**
- **No runtime protection**: Invalid data passed at runtime won't be caught automatically
- **Requires discipline**: Developers must run pyright and pay attention to type hints

### Option 2: No Validation (Not Chosen)

Skip all validation, rely purely on duck typing.

**Cons:**
- Type errors discovered only when code executes with bad data
- Errors manifest far from their source

## Rationale

1. **Performance**: `pydantic_core.to_json` is 8–9x faster than the prior custom msgspec converter for serialization, which dominates Temporal round-trip cost. Net performance is 3–4x faster end-to-end.
2. **Testability**: pyright catches bugs without running tests.
3. **Unified type system**: All contracts use the same type system — no mental overhead of "which zone am I in".
4. **Temporal compatibility**: Pydantic models serialize naturally through the official `pydantic_data_converter` without custom adapters.

## Consequences

**Positive:**
- Type errors caught during development, not in production
- Zero runtime overhead for type checking
- Full IDE support (autocomplete, refactoring, navigation)
- Simple mental model

**Negative:**
- Must configure and run pyright in CI
- No automatic type coercion
- Developers must add type annotations everywhere

## Implementation

**Import-time validation** (`application_sdk/contracts/base.py`):
- `Input.__init_subclass__()` validates payload safety when any `Input` subclass is defined
- `Output.__init_subclass__()` does the same
- Validation happens once at import time, not on every instantiation

**Decorator-time validation** (`application_sdk/app/task.py`):
- `@task` calls signature validation when applied
- Validates single-contract: one `Input` param, one `Output` return type
- Errors raised immediately when the module is imported

**Development workflow:**
```bash
# Type checking (catches errors without running code)
uv run pyright application_sdk/

# Tests verify runtime behaviour
uv run pytest tests/
```
