# Handlers

Handlers implement the API contract for your application's HTTP endpoints: authentication testing, preflight checks, and metadata browsing. In v3, handlers use the `Handler` ABC with typed contracts and automatic context injection, replacing v2's `HandlerInterface` with its untyped `*args/**kwargs` signatures and manual `load()` method.

## Defining a Handler

```python
from application_sdk.handler import (
    Handler,
    AuthInput, AuthOutput, AuthStatus,
    PreflightInput, PreflightOutput, PreflightStatus, PreflightCheck,
    MetadataInput, SqlMetadataOutput, SqlMetadataObject,
)

class MyHandler(Handler):
    async def test_auth(self, input: AuthInput) -> AuthOutput:
        api_key = self.context.get_credential("api_key")
        ok = await verify_key(api_key)
        return AuthOutput(
            status=AuthStatus.SUCCESS if ok else AuthStatus.FAILED,
        )

    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        return PreflightOutput(status=PreflightStatus.READY)

    async def fetch_metadata(self, input: MetadataInput) -> SqlMetadataOutput:
        return SqlMetadataOutput(objects=[
            SqlMetadataObject(TABLE_CATALOG="DEFAULT", TABLE_SCHEMA="ANALYTICS"),
        ])
```

## Typed Contracts

Every handler method takes a single typed `Input` and returns a single typed `Output`. The contracts are defined in `application_sdk.handler.contracts`:

All contract classes are Pydantic `BaseModel` subclasses. Import them from `application_sdk.handler.contracts`.

### AuthInput / AuthOutput

```python
from pydantic import BaseModel

class AuthInput(BaseModel):
    credentials: list[HandlerCredential] = []  # credential key/value pairs
    connection_id: str = ""                     # optional connection ID
    timeout_seconds: int = 30                   # max wait time

class AuthOutput(BaseModel):
    status: AuthStatus       # SUCCESS, FAILED, EXPIRED, or INVALID_CREDENTIALS
    message: str = ""        # optional detail message
    identities: list[str] = []  # verified identities (usernames, roles)
    scopes: list[str] = []     # authorized scopes or permissions
    expires_at: str = ""       # ISO-8601 expiry timestamp
```

Each `HandlerCredential` has a `key: str` and `value: str`.

### PreflightInput / PreflightOutput

```python
class PreflightInput(BaseModel):
    credentials: list[HandlerCredential] = []  # single-credential apps: the resolved credential
    credentials_by_name: dict[str, list[HandlerCredential]] = {}  # multi-credential apps: per named ref
    connection_config: dict[str, Any] = {}     # host, port, database, etc.
    checks_to_run: list[str] = []              # specific checks (empty = all)
    timeout_seconds: int = 60                  # on the gate path the SDK stamps the real per-attempt budget (~25s); advisory on HTTP/SDR

class PreflightOutput(BaseModel):
    status: PreflightStatus           # READY, NOT_READY, or PARTIAL
    checks: list[PreflightCheck] = [] # individual check results
    message: str = ""                 # human-readable summary (used when error is unset)
    error: FailureDetails | None = None  # typed aggregate failure; wins over message
    total_duration_ms: float = 0.0    # total time for all checks
```

`PreflightOutput.error` is additive (`None` by default). Handlers that only set `message` keep working. When `error` is set, `resolved_message` prefers it over `message` — the same precedence `PreflightCheck.error` already uses. Pass a `FailureDetails` (or a bare `AppError`, which is coerced).

#### Multi-credential preflight

Most apps use one credential and read `input.credentials`. Apps that need
several credentials of different auth types (for example an API key plus an
object-store credential) declare a **class-level** `preflight_credential_refs`
map on their extraction-input contract — ref name to the top-level guid field
that carries it:

```python
class MyExtractInput(ExtractionInput):
    preflight_credential_refs: ClassVar[dict[str, str]] = {
        "api": "api_credential_guid",
        "object_store": "object_store_credential_guid",
    }
```

The injected gate resolves each guid inside the activity frame under one
fail-open taxonomy — a confirmed dependency outage propagates (the workflow
fails open, never blocks a healthy run), a genuinely absent credential becomes
an empty group — and hands the handler `input.credentials_by_name`:

```python
async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
    api = input.credentials_by_name["api"]
    obj = input.credentials_by_name.get("object_store", [])
    ...
```

It **must** be a `ClassVar`, not a pydantic field: declared as a field the gate
reads `{}` and silently falls back to the single-credential path. Apps that
declare nothing keep the unchanged single-credential path via `input.credentials`.

An agent credential spec routes to agent resolution only when it is
*populated*: `agent-name` plus a fetch anchor — `secret-path` (bundle fetch) or
`key-type: single-key` (per-key fetch). A name-only spec (for example the
Automation Engine placeholder `{"agent-name": "agent-name", ...}` stamped on
non-agent runs) is not populated and falls through to `credential_guid`
routing.

#### Single-key secret resolution

When a credential arrives as a flat dict of fields rather than a named ref, the
SDK probes each string value to see whether it is a key in the secret store
(`application_sdk/credentials/agent.py`). These probes are independent point
lookups, so they run **concurrently**, bounded by a small fan-out cap
(`_MAX_CONCURRENT_SINGLE_KEY_PROBES = 8`) so a wide credential does not pay one
full store retry ladder per field. Results are merged in candidate order, so
resolution is byte-identical to the previous serial behavior.

What the logs tell you, and what they cannot:

- **Some fields resolved** logs INFO with the counts ("resolved N of M probed
  fields"). Ref-key names are never logged — they encode secret-store topology —
  so probes are identified by a `sha256:` prefix.
- **Nothing resolved** also logs INFO ("resolved 0 of N probed fields"). This is
  *not* treated as an error: a credential that carries literal usernames and
  passwords inline rather than ref-keys legitimately resolves nothing, and those
  workflows work. It is deliberately not a WARNING, because for such a
  credential it is the expected steady state on every run.
- **A probe hit a store-level error** logs a WARNING for that probe. Note that a
  scope-restricted store answers a non-allowlisted key with `403`
  (`ERR_PERMISSION_DENIED`) rather than an "absent" `500`, so an inline-literal
  credential against such a store produces one of these per field while still
  being a working configuration.

The limitation worth knowing: Dapr's secrets API returns `500`/`ERR_SECRET_GET`
for *any* backend error, and models "not found" nowhere — so a genuinely missing
key, a throttled vault, and an expired vault credential are indistinguishable to
the SDK. That is why nothing here can be raised on: "resolved nothing" cannot be
told apart from "nothing to resolve". Tracked in
[#2995](https://github.com/atlanhq/application-sdk/issues/2995).

### MetadataInput / MetadataOutput

```python
class MetadataInput(BaseModel):
    credentials: list[HandlerCredential] = []  # credentials for discovery
    connection_config: dict[str, Any] = {}     # connection configuration
    object_filter: str = ""                    # filter pattern (e.g. 'public.*')
    include_fields: bool = True                # include field/column details
    max_objects: int = 1000                    # max objects to return
    timeout_seconds: int = 120                 # max wait time

class MetadataOutput(BaseModel):
    objects: list[Any] = []  # base class — use SqlMetadataOutput or ApiMetadataOutput

class SqlMetadataOutput(MetadataOutput):
    objects: list[SqlMetadataObject] = []  # for sqltree widget

class ApiMetadataOutput(MetadataOutput):
    objects: list[ApiMetadataObject] = []  # for apitree widget
```

### Event and Subscription Contracts

For event-driven handlers, additional contracts are available:

```python
from application_sdk.handler.contracts import (
    EventTriggerConfig,   # configure a Dapr subscription trigger
    SubscriptionConfig,   # full Dapr pub/sub subscription spec
    CloudEventEnvelope,   # typed wrapper for incoming Dapr cloud events
    FileUploadResponse,   # response for file upload endpoints
)
```

### DefaultHandler

`DefaultHandler` is a pre-built `Handler` subclass that implements all three methods with sensible no-op responses. Useful for apps that only need workflow orchestration and don't expose auth/preflight/metadata UI.

Handler selection is convention-based: the SDK looks for `{AppClassName}Handler` in the same module as your `App`, then scans for any `Handler` subclass, and finally falls back to `DefaultHandler` automatically. There is no `handler_class` attribute on `App` — to rely on `DefaultHandler`, simply don't define a `Handler` subclass.

To specify a handler explicitly, use the `--handler` CLI flag or `ATLAN_HANDLER_MODULE` env var (see [CLI reference](../reference/cli.md)):

```bash
application-sdk --mode handler --handler myapp.handlers:MyHandler
```

To define a custom handler using the convention-based approach:

```python
from application_sdk.handler import Handler, AuthInput, AuthOutput

class MyAppHandler(Handler):   # name must be {AppClassName}Handler
    async def test_auth(self, input: AuthInput) -> AuthOutput:
        ...
```

## Context Injection

There is no `load()` method in v3. The service layer injects `self.context` before each handler method call and clears it after. This makes handlers stateless and safe for concurrent requests.

Access infrastructure through `self.context`:

```python
class MyHandler(Handler):
    async def test_auth(self, input: AuthInput) -> AuthOutput:
        # Get a credential value by key from the request credentials
        api_key = self.context.get_credential("api_key")

        # Get a secret from the secret store
        secret = await self.context.get_secret("my-secret-name")

        # Access all credentials as a list
        all_creds = self.context.credentials

        # Check if a credential exists
        if self.context.has_credential("api_key"):
            ...
```

## Error Handling with HandlerError

Raise `HandlerError` to return a structured HTTP error response:

```python
from application_sdk.handler import Handler, HandlerError

class MyHandler(Handler):
    async def test_auth(self, input: AuthInput) -> AuthOutput:
        api_key = await self.context.get_secret("my-api-key")
        if not api_key:
            raise HandlerError(
                message="API key not configured",
                http_status=400,
            )
        ...
```

`HandlerError` is translated by the server into an HTTP response with the specified status code and a JSON body containing the error message.

## SQL Handler Pattern

For SQL-based connectors, your handler typically delegates to a SQL client:

```python
from application_sdk.handler.contracts import (
    AuthInput, AuthOutput, AuthStatus,
    MetadataInput, SqlMetadataOutput, SqlMetadataObject,
)

class MySQLHandler(Handler):
    async def test_auth(self, input: AuthInput) -> AuthOutput:
        host = self.context.get_credential("host")
        username = self.context.get_credential("username")
        password = self.context.get_credential("password")
        try:
            async with create_connection(host, username, password) as conn:
                await conn.execute("SELECT 1")
            return AuthOutput(status=AuthStatus.SUCCESS)
        except Exception:
            return AuthOutput(
                status=AuthStatus.FAILED, message="Connection failed"
            )

    async def fetch_metadata(self, input: MetadataInput) -> SqlMetadataOutput:
        host = self.context.get_credential("host")
        username = self.context.get_credential("username")
        password = self.context.get_credential("password")
        async with create_connection(host, username, password) as conn:
            rows = await conn.execute(
                "SELECT TABLE_CATALOG, TABLE_SCHEMA "
                "FROM information_schema.schemata"
            )
            return SqlMetadataOutput(objects=[
                SqlMetadataObject(
                    TABLE_CATALOG=r["TABLE_CATALOG"],
                    TABLE_SCHEMA=r["TABLE_SCHEMA"],
                )
                for r in rows
            ])
```

## Workflow Execution Timeout

Workflows started by the handler service are capped at **72 hours** by default, so a hung run always reaches a terminal state instead of sitting in `RUNNING` until the Temporal namespace ceiling (if any) fires. Override with `ATLAN_WORKFLOW_MAX_TIMEOUT_HOURS` in your deployment environment:

| Env var | Default | Effect |
|---|---|---|
| `ATLAN_WORKFLOW_MAX_TIMEOUT_HOURS` | `72` | Maximum wall-clock hours a workflow may run before Temporal terminates it. Applies to every workflow started via `/workflows/v1/start` and `/events/v1/event/{event_id}`. |

Set `0` to opt out of the SDK cap entirely and fall back to the Temporal namespace default. Negative values are also treated as no-cap but emit a boot-time warning. Set in `atlan.yaml`:

```yaml
# atlan.yaml
env:
  - name: ATLAN_WORKFLOW_MAX_TIMEOUT_HOURS
    value: "4"   # workflows are capped at 4 hours
```

The effective ceiling is still bounded by the Temporal namespace's own
`workflow_execution_timeout` maximum — if the namespace caps runs below 72
hours, that lower value wins.

## Per-Entry-Point Handlers

A single app-level `Handler` serves `/workflows/v1/{auth,check,metadata}` for most apps. A **multi-entry-point** app can additionally provide *per-entry-point* implementations by dropping a `handler.py` in the entry point's package:

```python
# app/asset_export_advanced/handler.py
from application_sdk.handler.contracts import AuthInput, AuthOutput
from application_sdk.handler.context import HandlerContext

async def test_auth(input: AuthInput, ctx: HandlerContext) -> AuthOutput: ...
async def preflight_check(input: PreflightInput, ctx: HandlerContext) -> PreflightOutput: ...
async def fetch_metadata(input: MetadataInput, ctx: HandlerContext) -> MetadataOutput: ...
```

These are **module-level `async` functions** taking `(input, ctx)` — not methods on the `Handler` class.

**Dispatch & precedence.** Each request carries an `entrypoint` field — the bare entry-point name (e.g. `asset-export-advanced`) the orchestrator resolves from the Global Marketplace catalog and sends explicitly. When it's set and a conforming `app.<segment>.handler.<fn>` exists, the SDK routes to it **by exact name** and it **pre-empts** the app-level `Handler.<fn>`. When `entrypoint` is empty (single-entry-point apps) or the module/function is absent, dispatch falls through to the app-level `Handler` — 1:1 with today's behaviour. A non-empty but **malformed** name is rejected with `400` (consistent across the auth/check/metadata, manifest, and input-contract routes) rather than silently falling back to the default entrypoint. A non-`async` function is ignored (falls through) rather than failing at request time.

> ⚠️ **Things to know — the per-entrypoint module silently wins.** Dispatch is intentionally best-effort and resolved per request, so a few situations are easy to get wrong:
>
> - **Shadowing is silent.** If you define *both* `MyAppHandler.test_auth` (class) and `app/<segment>/handler.py:test_auth` (module) for the same entry point, the **module wins** and the class method never runs — with no error or log. If a per-entry-point module exists, treat it as the source of truth for that entry point.
> - **Per-op, not all-or-nothing.** A module that defines only `fetch_metadata` leaves `test_auth`/`preflight_check` for that entry point falling back to the app-level `Handler`. One entry point's lifecycle can therefore be split across two files — don't assume `handler.py` owns everything.
> - **Wrong name / wrong shape falls through quietly.** A misspelled function name, or a non-`async def`, won't match discovery and silently falls back to the app-level `Handler` (which may be `DefaultHandler`, returning a generic success). If your per-entry-point hook "isn't running," check the exact name and that it's `async`.
> - **Which code runs depends on the request.** The same endpoint routes to `app.<segment>.handler` vs the app-level `Handler` purely based on the request's `entrypoint` field — you can't tell from the code alone.

> The `entrypoint`/`entrypoint_ref` fields on the input contracts: `entrypoint` is the authoritative bare name used for routing; `entrypoint_ref` carries the legacy `connector` wire value (accepted via a validation alias, serialized back as `connector`) and is **informational only** — it is not parsed for dispatch. See [Entry Points — Per-entry-point handler & core modules](entry-points.md#per-entry-point-handler--core-modules) for the kebab→snake module-name rule.

## Testing Handlers

Test handlers by injecting mock infrastructure:

```python
import pytest
from application_sdk.testing import MockSecretStore
from application_sdk.infrastructure import InfrastructureContext, set_infrastructure
from application_sdk.handler.contracts import AuthInput, AuthStatus

@pytest.fixture
def infra():
    ctx = InfrastructureContext(
        secret_store=MockSecretStore({"my-api-key": "test-secret"}),
    )
    set_infrastructure(ctx)
    return ctx

async def test_auth_success(infra):
    handler = MyHandler()
    result = await handler.test_auth(AuthInput(credentials=[]))
    assert result.status == AuthStatus.SUCCESS
```
