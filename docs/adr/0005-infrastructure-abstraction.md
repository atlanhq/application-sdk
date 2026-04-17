# ADR-0005: Abstraction of Underlying Infrastructure

## Status
**Accepted**

## Context

The SDK is built on two powerful but complex technologies:
- **Temporal**: Durable execution engine with workflows, activities, and determinism constraints
- **Dapr**: Distributed application runtime for state, secrets, and bindings

Both have significant learning curves. Temporal requires understanding workflows vs activities, determinism rules, replay safety, and sandbox restrictions. Dapr requires understanding sidecars, component configurations, and various building blocks.

We needed to decide how much of this complexity to expose to app developers.

## Decision

We chose **complete abstraction**: developers work with `App`, `@task`, and Protocol-based interfaces. They never import from or interact with Temporal or Dapr directly.

## Options Considered

### Option 1: Complete Abstraction (Chosen)

Developers use framework abstractions exclusively:

```python
from application_sdk.app import App, Input, Output, task

class MyPipeline(App):

    @task
    async def fetch_data(self, input: FetchInput) -> FetchOutput:
        # Can do any I/O here - no sandbox restrictions
        return FetchOutput(data=await response.json())

    async def run(self, input: MyInput) -> MyOutput:
        # Use self.now() instead of datetime.now()
        started_at = self.now()
        # Use self.uuid() instead of uuid.uuid4()
        request_id = self.uuid()
        data = await self.fetch_data(FetchInput(url=input.url))
        return MyOutput(data=data.data, started_at=started_at)
```

Behind the scenes:
- `App.__init_subclass__` applies Temporal's `@workflow.defn` and `@workflow.run` automatically
- `@task` applies `@activity.defn` and registers the activity with `TaskRegistry`
- `create_worker()` auto-discovers all registered apps and tasks

**Architectural patterns:**

1. **Decorator abstraction**: `App` hides `@workflow.defn` + `@workflow.run`; `@task` hides `@activity.defn`

2. **Internal module convention**: Implementation details live in `_`-prefixed directories:
   ```
   application_sdk/
   ├── execution/
   │   └── _temporal/          # Internal — never import directly
   └── infrastructure/
       └── _dapr/              # Internal — never import directly
   ```

3. **Protocol-based interfaces**: Infrastructure capabilities exposed through Protocols:
   ```python
   class StateStore(Protocol):
       async def save(self, key: str, value: dict) -> None: ...
       async def load(self, key: str) -> dict | None: ...
       async def delete(self, key: str) -> bool: ...
   ```
   Production uses `DaprStateStore`; tests use `MockStateStore` (from `application_sdk.testing.mocks`). App code sees only the Protocol.

4. **Safe determinism helpers**: Framework provides replay-safe alternatives:
   ```python
   self.now()   # wraps workflow.now() — deterministic for Temporal replay
   self.uuid()  # wraps workflow.uuid4() — deterministic for Temporal replay
   ```

5. **`InfrastructureContext`**: All infrastructure held in a frozen dataclass stored in a `ContextVar`. Set once at startup; accessed anywhere via `get_infrastructure()`.

**Pros:**
- **Low learning curve**: Developers learn `App` and `@task`, not workflows and activities
- **Portable**: Apps don't depend on specific infrastructure — could swap Temporal for another engine
- **Consistent experience**: Same patterns work everywhere
- **Execution mode parity**: Same code runs in tests and production; no conditional paths
- **Testability transformation**: Mock implementations (`MockStateStore`, `MockSecretStore`, etc. from `application_sdk.testing.mocks`) mean unit tests need no Dapr/Temporal sidecar

**Cons:**
- **Power user friction**: Experts who know Temporal may find abstractions limiting
- **Debugging indirection**: Stack traces include framework wrapper layers
- **Feature lag**: New Temporal/Dapr features require framework updates to expose

### Option 2: Expose Raw SDKs (Not Chosen)

Let developers use Temporal and Dapr SDKs directly.

**Cons:**
- **Steep learning curve**: Must understand Temporal concepts (workflows, activities, determinism, replay)
- **Vendor lock-in**: Code tightly coupled to specific technologies
- **Inconsistent patterns**: Each developer may use SDKs differently
- **Easy mistakes**: Determinism violations, improper activity configuration

## Rationale

1. **Accessibility**: Most developers building data pipelines don't need to become Temporal experts. They need to define inputs, outputs, and processing logic.
2. **Guardrails**: The framework enforces best practices (single-dataclass contracts, proper timeout defaults) that would be optional with raw SDK usage.
3. **Execution mode parity**: The same code must run identically in tests and production. Abstraction makes this natural.
4. **Progressive disclosure**: Start simple with `App` and `@task`. Power users can configure advanced options (timeouts, retries, `passthrough_modules`) without dropping to raw SDK.

## Consequences

**Positive:**
- Developers productive without learning Temporal/Dapr internals
- Consistent patterns across all apps
- Framework can evolve underlying infrastructure without breaking app code
- Testing is straightforward — mock the Protocols, not the SDKs

**Negative:**
- Framework team must keep abstractions current with SDK features
- Stack traces include framework layers
- Some advanced Temporal features (signals, queries, continue-as-new) require explicit framework support
