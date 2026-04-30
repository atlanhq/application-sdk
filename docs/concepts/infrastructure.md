# Infrastructure

The Application SDK accesses all external infrastructure — state, secrets, pub/sub, bindings, and capacity — through Protocol-based interfaces. Production code uses Dapr-backed implementations; tests inject in-memory mocks. App code never imports from `application_sdk.infrastructure._dapr` directly.

---

## InfrastructureContext

`InfrastructureContext` is a frozen dataclass that holds all infrastructure handles. It is stored in a process-level singleton and read by `get_infrastructure()` wherever infrastructure access is needed.

```python
from application_sdk.infrastructure import (
    InfrastructureContext,
    set_infrastructure,
    get_infrastructure,
    clear_infrastructure,
)
```

### Fields

| Field | Protocol | Default impl | Test impl |
|-------|----------|--------------|-----------|
| `secret_store` | `SecretStore` | `DaprSecretStore` | `MockSecretStore` |
| `state_store` | `StateStore` | `DaprStateStore` | `MockStateStore` |
| `pubsub` | `PubSub` | `DaprPubSub` | `MockPubSub` |
| `binding` | `Binding` | `DaprBinding` | `MockBinding` |
| `capacity_pool` | `CapacityPool` | Redis-backed | `LocalCapacityPool` |

All fields are optional. When a field is `None`, the corresponding feature raises a configuration error at runtime.

---

## Setting Up Infrastructure

### Production

In production, `application_sdk.main` constructs `InfrastructureContext` from environment variables and calls `set_infrastructure()` once at startup. App code never constructs `InfrastructureContext` directly.

### Tests

Inject mock infrastructure in a pytest fixture:

```python
import pytest
from application_sdk.infrastructure import (
    InfrastructureContext,
    set_infrastructure,
    clear_infrastructure,
)
from application_sdk.testing import MockSecretStore, MockStateStore

@pytest.fixture
def infra():
    ctx = InfrastructureContext(
        secret_store=MockSecretStore({
            "my-db": '{"type": "basic", "username": "admin", "password": "secret"}',
        }),
        state_store=MockStateStore(),
    )
    set_infrastructure(ctx)
    yield ctx
    clear_infrastructure()
```

Call `clear_infrastructure()` in teardown so other tests start with a clean slate.

---

## Protocols

### StateStore

```python
class StateStore(Protocol):
    async def save(self, key: str, value: Any, store_name: str = ...) -> None: ...
    async def load(self, key: str, store_name: str = ...) -> Any: ...
    async def delete(self, key: str, store_name: str = ...) -> None: ...
    async def list_keys(self, prefix: str, store_name: str = ...) -> list[str]: ...
```

Used by `App.persistent_state` to store durable workflow data across runs.

### SecretStore

```python
class SecretStore(Protocol):
    async def get(self, key: str, store_name: str = ...) -> dict: ...
    async def get_optional(self, key: str, store_name: str = ...) -> dict | None: ...
    async def get_bulk(self, keys: list[str], store_name: str = ...) -> dict[str, dict]: ...
```

Used by `CredentialResolver` to fetch raw credential payloads. See [Credentials](credentials.md).

### PubSub

```python
class PubSub(Protocol):
    async def publish(self, topic: str, data: Any, pubsub_name: str = ...) -> None: ...
    async def subscribe(self, topic: str, pubsub_name: str = ...) -> AsyncIterator[Any]: ...
```

Used by event-driven apps to publish and consume messages.

### Binding

```python
class Binding(Protocol):
    async def invoke(self, name: str, operation: str, data: Any = None) -> Any: ...
```

Wraps Dapr output bindings (queues, storage, SMTP, etc.).

### CapacityPool

```python
class CapacityPool(Protocol):
    async def acquire(self, key: str, slots: int = 1) -> str: ...  # returns lock token
    async def release(self, key: str, token: str) -> None: ...
    async def renew(self, key: str, token: str) -> None: ...
```

Distributes concurrency slots across workers. `RedisCapacityPool` (production) uses Redis for cross-pod coordination; `LocalCapacityPool` uses an in-process semaphore.

---

## Accessing Infrastructure in App Code

Inside `@task` methods, access the context via `self.context`:

```python
class MyConnector(App):
    @task
    async def read_checkpoint(self, input: ReadInput) -> ReadOutput:
        infra = get_infrastructure()
        value = await infra.state_store.load(f"checkpoint:{input.connection_id}")
        ...
```

Or use the higher-level `App.persistent_state` accessor which wraps the state store with structured paths.

---

## Module Location

Public imports live in `application_sdk.infrastructure` — never import from the `_dapr` or `_redis` sub-packages directly:

```python
# Good
from application_sdk.infrastructure import InfrastructureContext, set_infrastructure
from application_sdk.infrastructure import StateStore, SecretStore  # Protocols

# Bad — private implementation detail
from application_sdk.infrastructure._dapr.client import DaprStateStore
```
