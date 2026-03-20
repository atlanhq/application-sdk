# ADR-0006: Schema-Driven Contracts with Additive Evolution

## Status
**Accepted**

## Context

Apps and Tasks need clear contracts defining their inputs and outputs. These contracts must:
- Be type-safe and statically analyzable
- Support evolution without breaking running workflows or existing callers
- Serialize correctly through Temporal's JSON data converter
- Be visible and understandable in the Temporal UI

The challenge is that Temporal may have workflows running for hours, days, or weeks. If a contract changes, in-flight workflows must continue working. We needed rules for how contracts can evolve safely.

## Decision

We chose **single-dataclass contracts with base classes**: every `run()` method and every `@task` method accepts exactly one dataclass extending `Input` and returns exactly one dataclass extending `Output`. Evolution follows strict rules: ADD new fields with defaults, NEVER remove or change types.

This approach is **advocated by Temporal as a best practice**. Temporal's documentation recommends using single-object parameters for workflow and activity signatures to enable backwards-compatible evolution.

## Options Considered

### Option 1: Single-Dataclass Pattern with Base Classes (Chosen)

```python
from dataclasses import dataclass
from application_sdk.contracts import Input, Output

@dataclass
class ExtractInput(Input):
    source_url: str
    max_records: int = 1000          # Optional with default

@dataclass
class ExtractOutput(Output):
    records_processed: int
    checkpoint_path: str

class Extractor(App):
    async def run(self, input: ExtractInput) -> ExtractOutput:
        ...
```

**Safe evolution example:**
```python
@dataclass
class ExtractInput(Input):
    source_url: str
    max_records: int = 1000
    retry_on_failure: bool = True    # NEW — safe: has default
    timeout_seconds: int = 300       # NEW — safe: has default
```

**Pros:**
- **Temporal best practice**: Aligns with Temporal's recommended patterns for versioning
- **Backwards compatible by design**: New fields with defaults don't break existing callers
- **Named fields**: Self-documenting contracts, visible in Temporal UI
- **Extensible**: Can always add context without signature changes
- **Type inference**: pyright can track types through the entire call chain
- **Serialization**: Plain dataclasses serialize cleanly through Temporal's JSON converter
- **Validation hooks**: Base class `__init_subclass__` enables import-time payload safety validation

**Cons:**
- **Verbosity**: Simple operations need input/output wrapper classes
- **Learning curve**: Developers must understand the single-dataclass pattern

### Option 2: Multiple Parameters (Not Chosen)

```python
@task
async def extract(self, url: str, max_records: int = 1000) -> list[dict]:
    ...
```

**Cons:**
- **Cannot evolve**: Adding parameters may change call site requirements
- **Against Temporal guidance**: Temporal recommends single-object params
- **Serialization issues**: Multiple params need manual handling

### Option 3: Tuple/Union Returns (Not Chosen)

**Cons:**
- **Positional ambiguity**: `tuple[int, str, list]` — which int? which string?
- **Cannot extend**: Adding to a tuple return breaks all callers
- **No field names**: Debugging shows `[42, "ok"]`, not `{count: 42, status: "ok"}`

## Rationale

1. **Temporal best practice**: Temporal's own documentation recommends wrapping workflow and activity parameters in a single object. This pattern is proven across the Temporal ecosystem.
2. **Version coexistence**: When deploying a new app version, old workflows continue running with old workers. If the new version adds a field with a default, old inputs (without that field) still deserialize correctly.
3. **Observability**: Temporal UI displays workflow/activity inputs and outputs. Named dataclass fields like `{source_url: "...", max_records: 1000}` are far more readable than positional tuples or raw dicts.
4. **Contract documentation**: The `Input`/`Output` base class inheritance clearly marks "this is a contract boundary."

## Consequences

**Positive:**
- Contracts can evolve without breaking callers
- Clear visual pattern: `Input` → `run()`/`@task` → `Output`
- Full type safety from caller through execution
- Self-documenting contracts in code and Temporal UI
- Framework validates contracts at import time

**Negative:**
- More classes to define (even for simple operations)
- Developers must remember "add defaults for new fields"
- Cannot use Python idioms like `*args` or `**kwargs`

## Evolution Rules

| Action | Allowed |
|--------|---------|
| Add new fields with default values | ✅ Yes |
| Remove fields | ❌ No |
| Change field types | ❌ No |
| Change method signatures | ❌ No |

Backward compatibility can be checked programmatically via `is_backwards_compatible(old_cls, new_cls)` in `application_sdk.contracts.base`.
