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

    checkpoint = await state.load(f"checkpoint:{input.workflow_id}")
    start_page = checkpoint["page"] if checkpoint else 1

    for page in range(start_page, total_pages + 1):
        # ... fetch page ...
        await state.save(f"checkpoint:{input.workflow_id}", {"page": page})

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

Provides access to secrets at runtime. The SDK uses this internally to resolve credential names / refs (legacy: credential GUIDs) — your app typically does not call `SecretStore` directly; use `CredentialResolver` instead (see [Credentials](credentials.md)).

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

Topic-based messaging. Use `PubSub` to publish events — for example, notifying downstream apps when extraction completes. To receive them, declare a `SubscriptionConfig` (below).

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
```

!!! warning "`subscribe()` is deprecated — removal in v4.0.0"

    `PubSub.subscribe()`, `Subscription` and `MessageHandler` are deprecated. `DaprPubSub.subscribe()` never delivered a message: it stored the handler and nothing called it. `MockPubSub.subscribe()` did deliver, so a test could pass while production received nothing. Both now emit a `DeprecationWarning`.

    To receive messages, declare a `SubscriptionConfig` and pass it to the handler service. The SDK lists it at `/dapr/subscribe` and mounts `handler` as a FastAPI `POST` route at `/subscriptions/v1/{route}`, where Dapr delivers each message:

    ```python
    from application_sdk.handler.contracts import SubscriptionConfig

    async def on_extraction_complete(event: dict) -> None:
        ...

    subscriptions = [
        SubscriptionConfig(
            component_name="pubsub",
            topic="extraction-complete",
            route="extraction-complete",
            handler=on_extraction_complete,
        )
    ]
    # create_app_handler_service(..., subscriptions=subscriptions)
    ```

    In a unit test, call `on_extraction_complete` directly.

**Publishing a message:**

`PubSub` is not part of `InfrastructureContext`. Use `DaprPubSub` from `application_sdk.infrastructure` — it implements the `PubSub` Protocol and handles serialisation and error wrapping:

```python
from application_sdk.infrastructure import AsyncDaprClient, DaprPubSub

@task(timeout_seconds=60)
async def notify_complete(self, input: NotifyInput) -> NotifyOutput:
    async with AsyncDaprClient() as dapr:
        # Targets the Dapr pub/sub component named "pubsub" (components/pubsub.yaml).
        pubsub = DaprPubSub(dapr)
        await pubsub.publish(
            topic="extraction-complete",
            data={"workflow_id": input.workflow_id, "record_count": input.record_count},
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

`DaprPubSub` publishes to the component named `pubsub` unless you pass `pubsub_name`. Do not pass `EVENT_STORE_NAME`: `eventstore` is a **binding** component (`bindings.localstorage` locally), which the SDK uses for its own lifecycle events through `get_infrastructure().event_binding`. It is not a pub/sub component.

**Testing without Dapr:**

```python
from application_sdk.testing import MockPubSub

mock_pubsub = MockPubSub()
# Records publish calls; use mock_pubsub.get_published(topic) to assert.
# Its subscribe() is deprecated (see above).
```

---

## Binding

Generic I/O bindings connect your app to external resources — object stores, queues, HTTP endpoints, email gateways — using Dapr's binding abstraction. For object storage (the most common use case), prefer the `storage` module's `upload_file`/`download_file` helpers (see [Storage](storage.md)); bindings are for everything else.

**Protocol:**

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

**Response type:**

```python
@dataclass(frozen=True)
class BindingResponse:
    data: bytes | None = None
    metadata: dict[str, str] = field(default_factory=dict)
```

!!! warning "`InputBinding`, `OutputBinding` and `BindingRequest` are deprecated — removal in v4.0.0"

    The SDK never shipped an implementation of `InputBinding` or `OutputBinding`, and nothing accepts a `BindingRequest`. Use `Binding.invoke(operation, data, metadata)`, which `DaprBinding` implements.

---

## CapacityPool (deprecated)

!!! warning "Deprecated — removal in v4.0.0"

    `CapacityPool`, `LocalCapacityPool`, `RedisCapacityPool`, `configure_capacity_pool()` and `get_capacity_pool()` are deprecated and emit a `DeprecationWarning`. The framework never configured a pool at startup — setting the `REDIS_*` env vars did not create one — and nothing in the SDK reads it, so `get_capacity_pool()` returned `None` unless the app called `configure_capacity_pool()` itself.

    To cap parallel work inside a task, bound it in the app:

    ```python
    sem = asyncio.Semaphore(8)

    async def fetch_one(item_id: str) -> dict:
        async with sem:
            return await self._fetch_item(item_id)
    ```

---

## Infrastructure Summary

| Protocol | Dapr Component | Mock | Access | Purpose |
|---|---|---|---|---|
| `StateStore` | `statestore` (`STATE_STORE_NAME`) | `MockStateStore` | `get_infrastructure().state_store` | Checkpoint / resume |
| `SecretStore` | Dapr secret store | `MockSecretStore` | `get_infrastructure().secret_store` | Credential resolution |
| `Binding` | Dapr binding | `MockBinding` | `get_infrastructure().event_binding` | External I/O |
| `PubSub` | `pubsub` | `MockPubSub` | `DaprPubSub(AsyncDaprClient())` | Event publishing (`subscribe()` deprecated) |
| `CapacityPool` (deprecated) | — | `LocalCapacityPool` | `get_capacity_pool()` | Deprecated; removal in v4.0.0 |

`StateStore`, `SecretStore`, and `Binding` are held in `InfrastructureContext` and accessed via `get_infrastructure()`. `PubSub` is built from a Dapr client with `DaprPubSub`. `CapacityPool` is deprecated (see [above](#capacitypool-deprecated)). See [Infrastructure](infrastructure.md) for the test fixture pattern and `set_infrastructure()` / `clear_infrastructure()` usage.

## See Also

- [Infrastructure](infrastructure.md) — `InfrastructureContext`, `set_infrastructure()`, test patterns
- [Credentials](credentials.md) — `SecretStore` in the credential resolution flow
- [Secret Stores](../guides/secretstores.md) — Dapr component configuration (K8s, AWS, Vault)
- [Storage](storage.md) — `upload_file`/`download_file` for object storage
- [Configuration](../configuration.md) — Redis lock, Dapr component names
