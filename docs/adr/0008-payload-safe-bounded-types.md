# ADR-0008: Payload-Safe Bounded Types in Contracts

## Status
**Accepted**

## Context

Temporal has a **2MB payload limit** for workflow and activity inputs/outputs. If a payload exceeds this limit, the workflow fails at runtime with a cryptic error. This is particularly problematic because:

1. Payloads can grow slowly (e.g., accumulating records) and only fail in production under real data volumes
2. Runtime failures are harder to debug than import-time errors
3. Developers may not be aware of the limit until they hit it

We needed a strategy to prevent payload size issues before they reach production.

## Decision

We chose **import-time payload safety validation**: contracts are validated when their class is defined (at import time), rejecting types that could grow unbounded. Bounded alternatives (`MaxItems`, `FileReference`) are provided for legitimate use cases.

## Options Considered

### Option 1: Import-Time Validation with Bounded Types (Chosen)

Forbid unbounded types at class definition time and provide safe alternatives:

**Forbidden types** (raise `PayloadSafetyError` at import):
- `Any` — cannot validate size constraints
- `bytes` / `bytearray` — binary data can be arbitrarily large
- Bare `list[T]` without `MaxItems` annotation
- Bare `dict[K, V]` without `MaxItems` annotation

**Safe alternatives:**
```python
from typing import Annotated
from application_sdk.contracts import Input, Output
from application_sdk.contracts.types import MaxItems, FileReference

class ProcessInput(Input):
    # SAFE: bounded list
    records: Annotated[list[dict[str, str]], MaxItems(1000)]

    # SAFE: bounded dict
    metadata: Annotated[dict[str, str], MaxItems(100)]

    # SAFE: reference to external storage (large data stays out of payload)
    data_ref: FileReference
```

**Escape hatch** for exceptional cases:
```python
class LegacyInput(Input, allow_unbounded_fields=True):
    """Use with caution — large payloads will fail at runtime."""
    data: dict[str, Any]
```

**Pros:**
- **Early failure**: Errors raised at import time, not runtime in production
- **Clear guidance**: Error messages explain what's wrong and how to fix it
- **Zero runtime cost**: Validation happens once at import, not on every instantiation
- **Escape hatch**: Power users can bypass with explicit opt-in

**Cons:**
- **More verbose**: Must annotate collections with `MaxItems`
- **False positives**: Small collections still require bounds annotation

### Option 2: No Restrictions (Not Chosen)

Allow any types in contracts, let Temporal enforce limits at runtime.

**Cons:**
- **Late failure**: Payload errors only surface in production with real data
- **Hard to debug**: Temporal's payload error doesn't indicate which field is too large
- **Inconsistent failures**: Works in dev (small data), fails in prod (large data)

### Option 3: Runtime-Only Validation (Not Chosen)

Validate payload sizes at runtime when objects are created.

**Cons:**
- Still fails in production, not development
- Runtime overhead on every object creation

### Option 4: Automatic Chunking (Not Chosen)

Automatically split large payloads into chunks, reassemble transparently.

**Cons:**
- **Hidden complexity**: Magic behaviour is hard to debug
- **Consistency issues**: Partial failures in multi-chunk transfers

## Rationale

1. **Fail fast**: Catching payload issues at import time means developers see the error the moment they run their code — not after deploying to production.
2. **Clear error messages**: `PayloadSafetyError` explains exactly what's wrong and provides concrete fixes.
3. **`FileReference` pattern**: Makes the trade-off explicit — large data goes to external storage (`application_sdk.storage`), only a reference travels through Temporal.
4. **Escape hatch**: `allow_unbounded_fields=True` respects that framework users may have legitimate cases. They can opt out with a clear acknowledgment of risk.

## Consequences

**Positive:**
- Payload issues caught at import time, not production runtime
- Clear error messages with actionable fixes
- `FileReference` pattern encourages proper handling of large data
- Zero runtime validation overhead

**Negative:**
- More verbose type annotations for collections
- Developers must learn `MaxItems` and `FileReference` patterns
- Escape hatch can be abused (but explicit opt-in makes abuse visible)

## Implementation

**`PayloadSafetyError`** (`application_sdk/contracts/base.py`): raised at class definition time when a forbidden type is found. Message includes the class name, field name, type, and how to fix it.

**`validate_payload_safety()`**: called by `Input.__init_subclass__()` and `Output.__init_subclass__()`. Checks each field; respects `allow_unbounded_fields=True`.

**`MaxItems`** (`application_sdk/contracts/types.py`): `@dataclass(frozen=True)` constraint marker used with `Annotated`.

**`FileReference`** (`application_sdk/contracts/types.py`): dataclass with `path: str`, optional `size_bytes`, `checksum`, and `content_type`. Store large data via `application_sdk.storage.upload_file()` and pass only the reference through Temporal.
