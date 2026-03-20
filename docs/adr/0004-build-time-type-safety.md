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

We chose **build-time type safety**: strongly-typed contracts with pyright in CI and `__init_subclass__`/decorator hooks that validate contracts at class-definition time (import time). The specific typing mechanism (plain dataclasses, Pydantic v2, msgspec, etc.) is left to the implementer, provided it is statically analysable by pyright and serialises correctly through Temporal's JSON data converter.

## Options Considered

### Option 1: Build-Time Validation with Strongly-Typed Contracts (Chosen)

Contracts are strongly-typed classes (the current SDK uses plain dataclasses; Pydantic v2 and msgspec are also acceptable). Type checking happens through:
1. pyright strict mode in CI
2. `__init_subclass__` hooks that validate contracts at class definition time (import time)
3. `@task` decorator validation at decoration time

```python
# contracts/base.py — validation runs at import time
@dataclass
class Input:
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
- **Zero runtime overhead**: No validation cost during workflow execution
- **Fast failure**: Type errors discovered at import time or during static analysis
- **IDE support**: Full autocomplete, refactoring, and go-to-definition
- **Static analysis**: pyright can catch type errors across the entire codebase without running code
- **Simplicity**: Standard Python dataclasses, no hidden validation layers
- **Temporal compatibility**: Plain dataclasses serialize cleanly through Temporal's JSON data converter

**Cons:**
- **No runtime protection**: Invalid data passed at runtime won't be caught automatically
- **Requires discipline**: Developers must run pyright and pay attention to type hints

### Option 2: No Validation (Not Chosen)

Skip all validation, rely purely on duck typing.

**Cons:**
- Type errors discovered only when code executes with bad data
- Errors manifest far from their source

## Rationale

1. **Performance**: Temporal activities may execute millions of times. Runtime validation on every call adds measurable overhead. With build-time validation, there is zero cost at execution time.
2. **Testability**: pyright catches bugs without running tests.
3. **Predictability**: Dataclasses do exactly what they look like — no hidden validation, coercion, or transformation. Debugging is straightforward.
4. **Temporal compatibility**: Plain dataclasses serialize naturally through Temporal's JSON data converter without special adapters.

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
- Validates single-dataclass contract: one `Input` param, one `Output` return type
- Errors raised immediately when the module is imported

**Development workflow:**
```bash
# Type checking (catches errors without running code)
uv run pyright application_sdk/

# Tests verify runtime behaviour
uv run pytest tests/
```
