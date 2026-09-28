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
cred: BasicCredential = await self.context.resolve_credential(basic_ref("my-db"))
print(cred.username)   # str
print(cred.password)   # str
```

### ApiKeyCredential

```python
cred: ApiKeyCredential = await self.context.resolve_credential(api_key_ref("my-svc"))
header = f"{cred.header_name}: {cred.prefix}{cred.api_key}"
```

### OAuthClientCredential

```python
cred: OAuthClientCredential = await self.context.resolve_credential(oauth_client_ref("my-oauth"))
# cred.needs_refresh() → bool (True if access_token is absent or expired)
```

### RawCredential (legacy fallback)

```python
cred: RawCredential = await self.context.resolve_credential(legacy_credential_ref(guid))
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

## Routing a Workflow Input's Credentials

A connector input can name its credential four ways: a pre-built `CredentialRef` (the generic `credential_ref` field, or the `<app>_credential` field the contract toolkit generates), a `credential_guid`, an `agent_json` spec, or inline `credentials`. Do not write your own routing function for these. Call `route_credentials` in the entry point, pass the `(ref, inline)` pair to each `@task` input, and resolve it in the task with `resolve_credential_raw_or_inline`:

```python
from pydantic import Field

from application_sdk.contracts.base import Input
from application_sdk.credentials import (
    CredentialMap,
    CredentialRef,
    InlineCredentials,
    route_credentials,
)


class MyInput(AppInputContract):  # generated: credential_guid, agent_json, my_credential, ...
    credentials: InlineCredentials = Field(default_factory=list)


class MyTaskInput(Input):
    credential_ref: CredentialRef | None = None
    inline_credentials: CredentialMap = Field(default_factory=dict)


class MyApp(App):
    @entrypoint
    async def run(self, input: MyInput) -> MyOutput:
        ref, inline = route_credentials(input)
        await self.extract(MyTaskInput(credential_ref=ref, inline_credentials=inline))
        ...

    @task
    async def extract(self, input: MyTaskInput) -> ExtractOutput:
        raw = await self.context.resolve_credential_raw_or_inline(
            input.credential_ref, input.inline_credentials
        )
        client = MyClient.from_credentials(raw)  # same nested shape on both paths
        ...
```

`route_credentials` tries each source in this order:

1. **A pre-built ref wins.** `credential_ref` is checked first, then the one other populated `CredentialRef` field. If the input has several, name the one the run uses with a class attribute: `run_credential_field: ClassVar[str] = "my_credential"`. The preflight gate reads the same attribute, so the gate and the tasks always check the same credential.
2. **Strict routing.** A `credential_guid`, agent mode, or a populated `agent_json` goes through `CredentialRef.resolve`, the same call the preflight gate uses. `extraction_method="agent"` routes to the agent spec. `direct`, and a miner's `query_history` or `s3`, route by GUID. Any other value raises `CredentialRoutingError`, so a misspelled mode is refused rather than guessed. A misrouted agent run (agent mode with an empty spec) raises straight away too. It never falls back to the GUID or to inline credentials.
3. **Inline, for local dev and tests only.** If the input names no credential, the `credentials` field is used. Production never gets here: `/workflows/v1/start` strips `credentials` from every request, and the platform always sends a GUID or an agent spec. Inline credentials only reach a workflow started in-process, such as an `AppExecutor` integration test or a unit test.

Inline credentials always come out in one shape: a flat dict with dotted keys, like `{"host": "h", "extra.client_id": "c"}`. The same keys result whether the input sent `[{key, value}]` pairs, a nested dict, or `extra` as a JSON string. That shape fits `CredentialMap`, a bounded contract type that holds only scalar values (`CredentialValue = str | int | float | bool | None`), so it can pass through a `@task` input. `CredentialMap` also flattens a nested dict when it is assigned. `flatten_dotted_keys` and `expand_dotted_keys` convert losslessly between the flat and nested shapes. `resolve_credential_raw_or_inline` uses `expand_dotted_keys` to turn inline credentials back into the nested shape that `resolve_raw` returns.

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

Mix in `AtlanClientMixin` when your App needs to call the Atlan API. The mixin provides `get_or_create_async_atlan_client()`, which returns the cached `AsyncAtlanClient` for the current execution if one exists, or creates and caches a new one.

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

## agent_json ingress

`agent_json` names an agent-shape credential *reference* used by SDR
(customer-infra) runs. It reaches the SDK in an arbitrary combination of three
alias spellings (`agent_json`, `agentJson`, `agent-json`), four container
positions (top level, `metadata`, `connection_config`, `credentials` — the last
in both the v2 dict and the v3 `list[{key, value}]` shape) and three types (JSON
string, dict, `AgentCredentialSpec`). It may also carry a meaningless
placeholder: eleven marketplace packages default the Argo `agent-json` param to
a blob whose values are the key names (`{"port": "port", ...}`), that blob is
persisted on the connection record, and it is replayed verbatim into v3 typed
requests.

`application_sdk.credentials.ingress` is the only place that tolerates any of
that. **Every reader takes the typed field.** Do not add a guard of your own.

```python
from application_sdk.credentials import lift_agent_json, normalize_agent_json

# One value -> a typed spec, or None. None means "no agent reference here":
# absent, empty, unparseable, or a placeholder that fails typed validation.
spec = normalize_agent_json(raw_value)

# A whole request body -> the same body with every agent-json key stripped from
# every container and the typed spec promoted to `body["agent_json"]`.
body = lift_agent_json(await request.json())
```

Two things stay outside the normaliser:

- **`is_populated()` is the consumers' call.** A spec that validates but carries
  no fetch anchor (a name with no `secret-path`) is a real reference as far as
  ingress is concerned; whoever resolves it decides whether that is usable.
- **Connector subclasses.** Pass `spec_type=` (or use
  `declared_agent_spec_type(MyInput)`) when the reader's field narrows
  `agent_json` to an `AgentCredentialSpec` subclass, so validation uses that
  subclass's rules and the reader gets the type it declared.

A malformed body reaching a handler endpoint is answered with **422** naming the
offending field, not a plain-text 500.

---

## Utility: parse_credentials_extra

For connectors that receive credentials as a flat dict (e.g. from Heracles), use `parse_credentials_extra` to extract nested fields:

```python
from application_sdk.credentials import parse_credentials_extra

raw = {"host": "db.example.com", "extra": '{"schema": "public"}'}
extra = parse_credentials_extra(raw)
# extra == {"schema": "public"}   ← returns the parsed extra dict only, not the full credentials
schema = extra.get("schema", "public")
```
