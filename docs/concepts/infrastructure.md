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
| `storage` | `ObjectStore` | obstore S3/GCS/Azure | local filesystem |
| `event_binding` | `Binding` | `DaprBinding` | `MockBinding` |

All fields are optional (`None` by default). Accessing a `None` field raises a configuration error at runtime.

`PubSub` and `CapacityPool` are separate infrastructure capabilities managed by their own module-level singletons — see [State, Secrets, Pub/Sub & Bindings](state-secrets-pubsub.md) for usage.

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
    async def save(self, key: str, value: dict[str, Any]) -> None: ...
    async def load(self, key: str) -> dict[str, Any] | None: ...
    async def delete(self, key: str) -> bool: ...
    async def list_keys(self, prefix: str = "") -> list[str]: ...
```

### SecretStore

```python
class SecretStore(Protocol):
    async def get(self, name: str) -> str: ...
    async def get_optional(self, name: str) -> str | None: ...
    async def get_bulk(self, names: list[str]) -> dict[str, str]: ...
    async def list_names(self) -> list[str]: ...
```

Returns raw string values (often JSON-encoded). Used by `CredentialResolver` to fetch raw credential payloads. See [Credentials](credentials.md).

### Binding

```python
class Binding(Protocol):
    @property
    def name(self) -> str: ...

    async def invoke(
        self,
        operation: str,
        data: bytes | None = None,
        metadata: dict[str, str] | None = None,
    ) -> BindingResponse: ...
```

Wraps Dapr output bindings (queues, storage, SMTP, etc.). Accessed via `get_infrastructure().event_binding`.

### CapacityPool

Distributes concurrency slots across workers. In production the framework auto-configures a Redis-backed pool (controlled by the `REDIS_*` env vars); `LocalCapacityPool` is the public implementation for tests and single-process dev. See [State, Secrets, Pub/Sub & Bindings](state-secrets-pubsub.md#capacitypool) for the full Protocol, configuration, and usage examples.

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
