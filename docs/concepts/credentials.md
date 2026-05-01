# Credentials

The Application SDK provides a typed credential system that eliminates `dict["password"]`-style access patterns. Credentials are stored in a `SecretStore` and resolved at runtime into strongly-typed objects via `resolve_credential()`.

---

## CredentialRef

A `CredentialRef` is a pointer to a credential stored externally. It carries a name and optional routing metadata (store name, credential GUID, type hint). App code holds refs, not raw secrets.

```python
from application_sdk.credentials import basic_ref, api_key_ref, bearer_token_ref

# Point to a basic credential named "my-db"
ref = basic_ref("my-db")

# Point to an API key credential
ref = api_key_ref("my-service")
```

### Factory functions

| Function | Credential type | Fields resolved |
|----------|----------------|-----------------|
| `basic_ref(name)` | `BasicCredential` | `username`, `password` |
| `api_key_ref(name)` | `ApiKeyCredential` | `api_key`, `header_name`, `prefix` |
| `bearer_token_ref(name)` | `BearerTokenCredential` | `token`, `expires_at` |
| `oauth_client_ref(name)` | `OAuthClientCredential` | `client_id`, `client_secret`, `token_url`, `scopes`, `access_token`, `refresh_token`, `expires_at` |
| `certificate_ref(name)` | `CertificateCredential` | `cert_data`, `key_data`, `ca_data`, `passphrase` |
| `git_ssh_ref(name)` | `GitSshCredential` | `key_data`, `passphrase` |
| `git_token_ref(name)` | `GitTokenCredential` | `token`, `expires_at` |
| `atlan_api_token_ref(name)` | `AtlanApiToken` | `token`, `base_url`, `expires_at` |
| `atlan_oauth_client_ref(name)` | `AtlanOAuthClient` | `client_id`, `client_secret`, `token_url`, `scopes`, `access_token`, `refresh_token`, `expires_at`, `base_url` |
| `legacy_credential_ref(guid, credential_type="unknown")` | `RawCredential` | `data` (raw dict, legacy fallback) — **deprecated**, emits `DeprecationWarning` |

All factory functions accept an optional `store_name` keyword argument (default: `"default"`) to route to a specific `SecretStore`.

---

## Credential Types

All credential types are frozen Pydantic models.

```python
from application_sdk.credentials import (
    BasicCredential,
    ApiKeyCredential,
    BearerTokenCredential,
    OAuthClientCredential,
    CertificateCredential,
    RawCredential,
    AtlanApiToken,
    AtlanOAuthClient,
)
```

### BasicCredential

```python
cred: BasicCredential = await ctx.resolve_credential(basic_ref("my-db"))
print(cred.username)   # str
print(cred.password)   # str
```

### ApiKeyCredential

```python
cred: ApiKeyCredential = await ctx.resolve_credential(api_key_ref("my-svc"))
header = f"{cred.header_name}: {cred.prefix}{cred.api_key}"
```

### OAuthClientCredential

```python
cred: OAuthClientCredential = await ctx.resolve_credential(oauth_client_ref("my-oauth"))
# cred.needs_refresh() → bool (True if access_token is absent or expired)
```

### RawCredential (legacy fallback)

```python
cred: RawCredential = await ctx.resolve_credential(legacy_credential_ref(guid))
value = cred.data.get("some_field")  # dict access — use only when migrating v2 code
# or equivalently: cred.get("some_field")
```

---

## Resolving Credentials

In `@task` methods, resolve via `self.context.resolve_credential()`:

```python
from application_sdk.credentials import basic_ref, BasicCredential

class MyConnector(App):
    @task
    async def connect(self, input: ConnectInput) -> ConnectOutput:
        ref = basic_ref("my-db")
        cred: BasicCredential = await self.context.resolve_credential(ref)
        conn = await open_connection(cred.username, cred.password)
        ...
```

`AppContext.resolve_credential()` calls the injected `SecretStore` (production: Dapr; tests: `MockSecretStore`) and deserialises the payload into the correct typed model.

---

## Custom Credential Types

Register custom credential types via `register_credential_type`:

```python
from pydantic import BaseModel
from application_sdk.credentials import register_credential_type, CredentialRef

class SlackCredential(BaseModel, frozen=True):
    bot_token: str
    signing_secret: str

def _parse_slack(data: dict) -> SlackCredential:
    return SlackCredential(**data)

register_credential_type("slack", SlackCredential, _parse_slack)
```

Retrieve the registered class with `get_registry().get_class("slack")` (import `get_registry` from `application_sdk.credentials`).

---

## AtlanClientMixin

Mix in `AtlanClientMixin` when your App needs to call the Atlan API. The mixin provides `get_or_create_async_atlan_client()`, which:

1. Checks the per-execution cache.
2. Re-uses any `AsyncAtlanClient` already created during `validate()`.
3. Creates and caches a new client if neither is available.

```python
from application_sdk.credentials import AtlanClientMixin
from application_sdk.app import App, task

class MyConnector(AtlanClientMixin, App):
    @task
    async def push_lineage(self, input: LineageInput) -> LineageOutput:
        client = await self.get_or_create_async_atlan_client(input.credential)
        await client.asset.upsert(...)
        return LineageOutput(pushed=True)
```

---

## Secret Stores

`SecretStore` is a Protocol — the same interface is implemented by Dapr (production) and in-memory mocks (tests).

```python
from application_sdk.infrastructure import SecretStore

class SecretStore(Protocol):
    async def get(self, name: str) -> str: ...
    async def get_optional(self, name: str) -> str | None: ...
    async def get_bulk(self, names: list[str]) -> dict[str, str]: ...
    async def list_names(self) -> list[str]: ...
```

Methods return raw string values (often JSON-encoded). `CredentialResolver` parses the string into the requested typed model.

### Production

`DaprSecretStore` (the default in production) routes requests through the Dapr sidecar to whatever secret backend the Helm chart configures (Kubernetes Secrets, Vault, AWS Secrets Manager, etc.).

### `EnvironmentSecretStore`

Reads secrets from environment variables — useful for simple local setups or CI environments:

```python
from application_sdk.infrastructure import EnvironmentSecretStore

# Optional prefix — e.g. prefix="MYAPP_" maps secret "DB_PASSWORD" → env var "MYAPP_DB_PASSWORD"
store = EnvironmentSecretStore(prefix="")
```

### `MockSecretStore` (tests)

```python
from application_sdk.testing import MockSecretStore

store = MockSecretStore({
    "my-db": '{"type": "basic", "username": "admin", "password": "secret"}',
})
```

See [Testing Apps](apps.md#testing-apps) and [Integration Testing](../guides/integration-testing.md) for how to inject mock stores.

---

## Utility: parse_credentials_extra

For connectors that receive credentials as a flat dict (e.g. from Heracles), use `parse_credentials_extra` to extract nested fields:

```python
from application_sdk.credentials.utils import parse_credentials_extra

raw = {"host": "db.example.com", "extra": '{"schema": "public"}'}
extra = parse_credentials_extra(raw)
# extra == {"schema": "public"}   ← returns the parsed extra dict only, not the full credentials
schema = extra.get("schema", "public")
```
