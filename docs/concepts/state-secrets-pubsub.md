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

The Dapr component name is `statestore` by default (read at module import time from `STATE_STORE_NAME`).

**Testing without Dapr:**

```python
from application_sdk.testing import MockStateStore

mock_state = MockStateStore()
# Supports save/load/delete/list_keys and records all calls for assertions
```

---

## SecretStore

Provides access to secrets at runtime. The SDK uses this internally to resolve credential GUIDs — your app typically does not call `SecretStore` directly; use `CredentialResolver` instead (see [Credentials](credentials.md)).

**Protocol:**

```python
class SecretStore(Protocol):
    async def get(self, name: str) -> str: ...
    async def get_optional(self, name: str) -> str | None: ...
    async def get_bulk(self, names: list[str]) -> dict[str, str]: ...
    async def list_names(self) -> list[str]: ...
```

**Two built-in implementations:**

| Class | Source | Use case |
|---|---|---|
| `EnvironmentSecretStore` | `os.environ` | Local dev, CI |
| `DaprSecretStore` | Dapr secret store sidecar | Production |

**Injecting for tests:**

```python
import json
from application_sdk.testing import MockSecretStore

secret_store = MockSecretStore({
    "my-credential-name": json.dumps({
        "type": "basic",
        "username": "admin",
        "password": "secret",
        "host": "localhost",
    })
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
    ) -> Subscription: ...
```

**Publishing a message:**

`PubSub` is not part of `InfrastructureContext`. Use `DaprPubSub` from `application_sdk.infrastructure` — it implements the `PubSub` Protocol and handles serialisation and error wrapping:

```python
import os
from application_sdk.infrastructure import AsyncDaprClient, DaprPubSub

@task(timeout_seconds=60)
async def notify_complete(self, input: NotifyInput) -> NotifyOutput:
    async with AsyncDaprClient() as dapr:
        # DaprPubSub constructor default is "pubsub"; pass EVENT_STORE_NAME to match the Dapr component name ("eventstore" by default)
        pubsub = DaprPubSub(dapr, pubsub_name=os.getenv("EVENT_STORE_NAME", "eventstore"))
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

The component name is read from `EVENT_STORE_NAME` at module import time (default: `eventstore`).

**Testing without Dapr:**

```python
from application_sdk.testing import MockPubSub

mock_pubsub = MockPubSub()
# Supports publish/subscribe and records calls; use mock_pubsub.get_published(topic) to assert
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
        metadata: dict[str, str] | None = None,
    ) -> BindingResponse: ...


class InputBinding(Protocol):
    """Receives data from an external source (e.g. a queue)."""
    @property
    def name(self) -> str: ...

    async def read(self) -> tuple[bytes, dict[str, str]]: ...


class OutputBinding(Protocol):
    """Sends data to an external target (e.g. an HTTP endpoint or email)."""
    @property
    def name(self) -> str: ...

    async def write(
        self,
        data: bytes,
        metadata: dict[str, str] | None = None,
    ) -> None: ...
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
    async def acquire(
        self,
        pool_name: str,
        requested: int,
        *,
        min_useful: int = 1,
        holder_id: str = "",
        ttl_seconds: int = 120,
    ) -> int: ...   # returns slots granted

    async def release(self, pool_name: str, holder_id: str) -> None: ...

    async def renew(
        self, pool_name: str, holder_id: str, ttl_seconds: int = 120
    ) -> bool: ...
```

`CapacityPool` is accessed as a module-level singleton (not part of `InfrastructureContext`). Use `get_capacity_pool()`:

**Using a capacity pool:**

```python
import uuid
from application_sdk.infrastructure import get_capacity_pool

@task(timeout_seconds=3600)
async def parallel_fetch(self, input: FetchInput) -> FetchOutput:
    pool = get_capacity_pool()

    async def fetch_one(item_id: str) -> dict:
        holder = str(uuid.uuid4())
        if pool:
            await pool.acquire("fetch-pool", requested=1, holder_id=holder)
        try:
            return await self._fetch_item(item_id)
        finally:
            if pool:
                await pool.release("fetch-pool", holder_id=holder)

    results = await asyncio.gather(*(fetch_one(i) for i in input.item_ids))
    return FetchOutput(results=results)
```

**Built-in implementations:**

| Class | Backend | Use case |
|---|---|---|
| `LocalCapacityPool` | None (no-op) | Local dev / no-Redis fallback; always grants the full requested amount |
| Redis-backed pool (internal) | Redis | Cross-pod, distributed concurrency |

**Enabling the Redis-backed pool:**

Set `REDIS_HOST`, `REDIS_PORT`, and `REDIS_PASSWORD` env vars and the framework configures a Redis-backed capacity pool automatically at startup — no code changes needed. See [Configuration](../configuration.md) for all Redis env vars.

---

## Infrastructure Summary

| Protocol | Dapr Component | Mock | Access | Purpose |
|---|---|---|---|---|
| `StateStore` | `statestore` (`STATE_STORE_NAME`) | `MockStateStore` | `get_infrastructure().state_store` | Checkpoint / resume |
| `SecretStore` | Dapr secret store | `MockSecretStore` | `get_infrastructure().secret_store` | Credential resolution |
| `Binding` | Dapr binding | `MockBinding` | `get_infrastructure().event_binding` | External I/O |
| `PubSub` | `eventstore` (`EVENT_STORE_NAME`) | `MockPubSub` | Dapr client directly | Event emission/subscription |
| `CapacityPool` | Redis (optional) | `LocalCapacityPool` | `get_capacity_pool()` | Concurrency limits |

`StateStore`, `SecretStore`, and `Binding` are held in `InfrastructureContext` and accessed via `get_infrastructure()`. `PubSub` is accessed via the Dapr client directly. `CapacityPool` is a separate module-level singleton accessed via `get_capacity_pool()`. See [Infrastructure](infrastructure.md) for the test fixture pattern and `set_infrastructure()` / `clear_infrastructure()` usage.

## See Also

- [Infrastructure](infrastructure.md) — `InfrastructureContext`, `set_infrastructure()`, test patterns
- [Credentials](credentials.md) — `SecretStore` in the credential resolution flow
- [Secret Stores](../guides/secretstores.md) — Dapr component configuration (K8s, AWS, Vault)
- [Storage](storage.md) — `upload_file`/`download_file` for object storage
- [Configuration](../configuration.md) — Redis lock, Dapr component names, capacity pool env vars
