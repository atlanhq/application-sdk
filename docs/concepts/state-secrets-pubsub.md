# State, Secrets, Pub/Sub, and Bindings

The Application SDK abstracts four infrastructure capabilities behind Protocols: `StateStore`, `SecretStore`, `PubSub`, and `Binding`. In production, Dapr implements all four. In tests, lightweight mock implementations let you inject any behavior you need without Dapr running.

Access these through `InfrastructureContext`. See [Infrastructure](infrastructure.md) for how to set and retrieve the context.

---

## StateStore

Key-value storage scoped to a workflow run. Use it to persist intermediate results between tasks — for example, storing a checkpoint after each page of an API call so a retry can resume from where it left off.

**Protocol:**

```python
class StateStore(Protocol):
    async def save(self, key: str, value: dict[str, Any]) -> None: ...
    async def load(self, key: str) -> dict[str, Any] | None: ...
    async def delete(self, key: str) -> bool: ...
    async def list_keys(self, prefix: str = "") -> list[str]: ...
```

**Accessing in a task:**

```python
from application_sdk.infrastructure import get_infrastructure

@task(timeout_seconds=3600)
async def fetch_pages(self, input: FetchInput) -> FetchOutput:
    state = get_infrastructure().state_store
    if state is None:
        raise RuntimeError("StateStore not configured")

    checkpoint = await state.load(f"checkpoint:{input.run_id}")
    start_page = checkpoint["page"] if checkpoint else 1

    for page in range(start_page, total_pages + 1):
        # ... fetch page ...
        await state.save(f"checkpoint:{input.run_id}", {"page": page})

    return FetchOutput(...)
```

**Dapr backend** — in `atlan.yaml`, enable the state store:

```yaml
dapr:
  statestore:
    enabled: true
```

The Dapr component name is `atlan-state-store` by default (read at module import time from `DAPR_STATE_STORE_COMPONENT_NAME`).

**Testing without Dapr:**

```python
from unittest.mock import AsyncMock, MagicMock

mock_state = MagicMock()
mock_state.load = AsyncMock(return_value=None)
mock_state.save = AsyncMock()
mock_state.delete = AsyncMock(return_value=True)
mock_state.list_keys = AsyncMock(return_value=[])
```

---

## SecretStore

Provides access to secrets at runtime. The SDK uses this internally to resolve credential GUIDs — your app typically does not call `SecretStore` directly; use `resolve_credentials()` instead (see [Credentials](credentials.md)).

**Protocol:**

```python
class SecretStore(Protocol):
    async def get(self, name: str) -> str: ...
    async def get_optional(self, name: str) -> str | None: ...
    async def get_bulk(self, names: list[str]) -> dict[str, str]: ...
```

**Two built-in implementations:**

| Class | Source | Use case |
|---|---|---|
| `EnvironmentSecretStore` | `os.environ` | Local dev, CI |
| `DaprSecretStore` | Dapr secret store sidecar | Production |

**Injecting for tests:**

```python
from application_sdk.testing import MockSecretStore

secret_store = MockSecretStore({
    "my-credential-guid": {
        "username": "admin",
        "password": "secret",
        "host": "localhost",
    }
})
```

See [Credentials](credentials.md) for the full credential resolution flow, and [Secret Stores](../guides/secretstores.md) for Dapr component configuration (AWS Secrets Manager, HashiCorp Vault, Kubernetes Secrets).

---

## PubSub

Topic-based publish/subscribe messaging. Use it when your app needs to emit events or react to external triggers asynchronously — for example, notifying downstream apps when extraction completes, or subscribing to incremental change feeds.

**Protocol:**

```python
class PubSub(Protocol):
    async def publish(
        self,
        topic: str,
        data: dict[str, Any],
        *,
        metadata: dict[str, str] | None = None,
    ) -> None: ...

    async def subscribe(
        self,
        topic: str,
        handler: MessageHandler,
        *,
        metadata: dict[str, str] | None = None,
    ) -> Subscription: ...
```

**Publishing a message:**

```python
from application_sdk.infrastructure import get_infrastructure

@task(timeout_seconds=60)
async def notify_complete(self, input: NotifyInput) -> NotifyOutput:
    pubsub = get_infrastructure().pubsub
    if pubsub is None:
        raise RuntimeError("PubSub not configured")

    await pubsub.publish(
        topic="extraction-complete",
        data={"run_id": input.run_id, "record_count": input.record_count},
    )
    return NotifyOutput()
```

**`Message` dataclass:**

```python
@dataclass(frozen=True)
class Message:
    data: dict[str, Any]
    metadata: dict[str, str]   # default: {}
    id: str | None             # default: None
    topic: str | None          # default: None
```

**Dapr backend** — enable in `atlan.yaml`:

```yaml
dapr:
  pubsub:
    enabled: true
```

The component name is read from `DAPR_PUBSUB_COMPONENT_NAME` at module import time.

**Testing without Dapr:**

```python
from unittest.mock import AsyncMock, MagicMock

mock_pubsub = MagicMock()
mock_pubsub.publish = AsyncMock()
mock_pubsub.subscribe = AsyncMock(return_value=MagicMock(is_active=True))
```

---

## Binding

Generic I/O bindings connect your app to external resources — object stores, queues, HTTP endpoints, email gateways — using Dapr's binding abstraction. For object storage (the most common use case), prefer the `storage` module's `upload_file`/`download_file` helpers (see [Storage](storage.md)); bindings are for everything else.

**Protocols:**

```python
class Binding(Protocol):
    @property
    def name(self) -> str: ...

    async def invoke(
        self,
        operation: str,
        data: bytes | None = None,
        *,
        metadata: dict[str, str] | None = None,
    ) -> BindingResponse: ...


class InputBinding(Protocol):
    """Receives data from an external source (e.g. a queue)."""
    @property
    def topic(self) -> str: ...

    async def receive(self) -> Message: ...


class OutputBinding(Protocol):
    """Sends data to an external target (e.g. an HTTP endpoint or email)."""
    async def send(
        self,
        data: bytes,
        *,
        metadata: dict[str, str] | None = None,
    ) -> BindingResponse: ...
```

**Request/response types:**

```python
@dataclass(frozen=True)
class BindingRequest:
    operation: str
    data: bytes | None = None
    metadata: dict[str, str] = field(default_factory=dict)

@dataclass(frozen=True)
class BindingResponse:
    data: bytes | None = None
    metadata: dict[str, str] = field(default_factory=dict)
```

---

## CapacityPool

The `CapacityPool` Protocol governs concurrency limits — for example, capping the number of parallel database connections or API requests within a task.

**Protocol:**

```python
class CapacityPool(Protocol):
    async def acquire(self, count: int = 1) -> None: ...
    async def release(self, count: int = 1) -> None: ...

    @property
    def available(self) -> int: ...

    @property
    def capacity(self) -> int: ...
```

**Using a capacity pool:**

```python
from application_sdk.infrastructure import get_infrastructure

@task(timeout_seconds=3600)
async def parallel_fetch(self, input: FetchInput) -> FetchOutput:
    pool = get_infrastructure().capacity_pool

    async def fetch_one(item_id: str) -> dict:
        if pool:
            await pool.acquire()
        try:
            return await self._fetch_item(item_id)
        finally:
            if pool:
                await pool.release()

    results = await asyncio.gather(*(fetch_one(i) for i in input.item_ids))
    return FetchOutput(results=results)
```

**Built-in implementations:**

| Class | Backend | Use case |
|---|---|---|
| `LocalCapacityPool` | `asyncio.Semaphore` | Single-process concurrency |
| `RedisCapacityPool` | Redis | Cross-pod, distributed concurrency |

**Configuring `RedisCapacityPool`:**

```python
from application_sdk.infrastructure.capacity import configure_capacity_pool
from application_sdk.infrastructure._redis.capacity import RedisCapacityPool

configure_capacity_pool(
    RedisCapacityPool(
        redis_url=os.environ["REDIS_URL"],
        pool_name="my-connector-pool",
        capacity=10,
    )
)
```

Or set via env vars and the pool is configured automatically (see [Configuration](../configuration.md)).

---

## Infrastructure Summary

| Protocol | Dapr Component | Mock | Purpose |
|---|---|---|---|
| `StateStore` | `atlan-state-store` | `MagicMock` | Checkpoint / resume |
| `SecretStore` | Dapr secret store | `MockSecretStore` | Credential resolution |
| `PubSub` | `atlan-pubsub` | `MagicMock` | Event emission/subscription |
| `Binding` | Dapr binding | `MagicMock` | External I/O |
| `CapacityPool` | Redis (optional) | `LocalCapacityPool` | Concurrency limits |

All five are held in `InfrastructureContext` and accessed via `get_infrastructure()`. See [Infrastructure](infrastructure.md) for the test fixture pattern and `set_infrastructure()` / `clear_infrastructure()` usage.

## See Also

- [Infrastructure](infrastructure.md) — `InfrastructureContext`, `set_infrastructure()`, test patterns
- [Credentials](credentials.md) — `SecretStore` in the credential resolution flow
- [Secret Stores](../guides/secretstores.md) — Dapr component configuration (K8s, AWS, Vault)
- [Storage](storage.md) — `upload_file`/`download_file` for object storage
- [Configuration](../configuration.md) — Redis lock, Dapr component names, capacity pool env vars
