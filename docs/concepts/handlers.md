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
    message: str = ""        # optional detail; overwritten from error.message on a failed result
    identities: list[str] = []  # verified identities (usernames, roles)
    scopes: list[str] = []     # authorized scopes or permissions
    expires_at: str = ""       # ISO-8601 expiry timestamp
    error: FailureDetails | None = None  # typed failure; omitted on success
```

Each `HandlerCredential` has a `key: str` and `value: str`.

`AuthOutput.error` is additive (`None` by default). Success paths that omit it are unchanged. On a failed result, `error.message` overwrites `message`, so HTTP and SDR callers read the same text. For a failed `test_auth`, return `error=err.to_failure_details()`: the field is typed `FailureDetails | None`, and no separate `message=` is needed because `error.message` fills it. A bare `AppError` is also coerced at runtime, but type-checkers reject it. Avoid a fixed string, which throws away the reason.

### PreflightInput / PreflightOutput

```python
class PreflightInput(BaseModel):
    credentials: list[HandlerCredential] = []  # single-credential apps: the resolved credential
    credentials_by_name: dict[str, list[HandlerCredential]] = {}  # multi-credential apps: per named ref
    connection_config: dict[str, Any] = {}     # host, port, database, etc.
    checks_to_run: list[str] = []              # specific checks (empty = all)
    tiers: frozenset[CheckTier] = ALL_CHECK_TIERS  # run only these tiers; skip checks outside them (default = every tier)
    timeout_seconds: int = 60                  # on the gate path the SDK stamps the real per-attempt budget (~25s); advisory on HTTP/SDR

class PreflightOutput(BaseModel):
    status: PreflightStatus           # READY or NOT_READY; PARTIAL is deprecated (removal anchored at v3.40.0); PENDING is set by the SDK, never by a handler
    checks: list[PreflightCheck] = [] # individual check results
    message: str = ""                 # human-readable summary (used when error is unset)
    error: FailureDetails | None = None  # typed aggregate failure; wins over message
    total_duration_ms: float = 0.0    # total time for all checks
    warmup: WarmupObservation | None = None  # set by the SDK on a PENDING /check verdict; a handler leaves it unset
```

`PreflightOutput.error` is additive (`None` by default). Handlers that only set `message` keep working. When `error` is set, `resolved_message` prefers it over `message` — the same precedence `PreflightCheck.error` already uses. Pass a `FailureDetails` (or a bare `AppError`, which is coerced).

**Return the verdict; do not raise it.** On the gate path, anything that escapes `preflight_check` is treated as a statement about the source (typed) or an app fault (untyped), and a hard-mode app blocks on it. On the HTTP `/workflows/v1/check` path a raise keeps its HTTP status and `detail` string, and the body additionally carries the same verdict shape a returned `NOT_READY` would: `preflight.status` is `not_ready` and one `preflightVerdict` check carries the raise as typed `FailureDetails`, the leaf's own for a typed raise or `InternalError` with `classification_pending` for a crash, so the caller always has per-check data and a classification. A transient the extraction can cope with — a 429, a database still resuming — is a failed check on a `PARTIAL` output, with `error=RateLimitedError(...).to_failure_details()` (retryable): the run proceeds in both modes, the outcome row names the check's code, and the other checks survive. Bound every probe to `input.timeout_seconds` so the handler returns its own typed verdict before the gate's cancel; a probe the gate has to end leaves no check evidence.

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

#### Check tiers and warmup

Some checks need source compute to answer: a query against a suspended warehouse, a probe that waits for a job-queue slot. Running them on every **Test** click makes every click cost compute. An app marks those checks `tier=CheckTier.WARMUP` and overrides one optional `Handler` method, `warmup`:

```python
class MyHandler(Handler):
    async def warmup(self, input: WarmupInput) -> WarmupObservation:
        # SELECT 1 also resumes a suspended warehouse, so the probe pushes the
        # warmup forward and reports it in one call.
        if await self.client.probe(input, timeout=input.probe_timeout_seconds):
            return WarmupObservation(state=WarmupState.READY)
        warehouse = await self.client.warehouse_status(input)
        return WarmupObservation(
            state=WarmupState.QUEUED if warehouse.queued else WarmupState.WARMING,
            source_state=warehouse.state,  # e.g. "RESUMING"
            queued_queries=warehouse.queued,
        )
```

The default `warmup` answers `READY`, so an app that does not override it behaves exactly as before. A check's tier defaults to `CheckTier.PREFLIGHT`, and a `PREFLIGHT` check is serialised without a `tier` key, so an app that never sets a tier emits byte-identical `/check` output.

| Route | Body | Answer |
| --- | --- | --- |
| `POST /workflows/v1/warmup` | same as `/check` | one `warmup` probe; its `WarmupObservation` plus the app's `ceiling_seconds` under `data`; `success` is `false` only for `unavailable` |
| `POST /workflows/v1/check` with `tiers` | `/check` body plus `"tiers": ["preflight"]`, `["warmup"]` or both | only the checks in those tiers; `pending` while the `warmup` tier could not run, with the observation under `preflight.warmup` |

A request with no `tiers` runs every check and never calls `warmup`. That covers every caller written before tiers. A request whose `tiers` includes `warmup` probes once before any check runs: on `ready` the handler runs every requested tier, otherwise it runs the rest and the verdict is `pending`, or `not_ready` when the probe reported `unavailable`. A request for `["preflight"]` alone never probes, so it never starts compute. See [`POST /workflows/v1/check`](../reference/http-api.md#post-workflowsv1check) for the full rules.

The SDK checks every returned row's tier against the tiers the handler was asked for. A row outside them means the handler ran a check it was told not to, so it gets no verdict rather than a silent drop: `/check` answers `500` with an unverifiable `INTERNAL` verdict, and the gate records `no_verdict` / `gate_broken`. Read `input.tiers` and skip the checks outside it.

The injected gate probes `warmup` at gate start for every app, in its own `{app}:preflight_warmup` activity ahead of the check activity, so the probe never spends the check budget. `READY` is followed by one check dispatch with every tier, as before. Otherwise the gate runs the `PREFLIGHT` tier, polls `warmup` on durable timers, and runs the `WARMUP` tier once it reports `ready`. A typed AUTH, PERMISSION or NOT_FOUND raise from `warmup` ends the gate's wait at once; any other raise, or a probe that overruns its timeout, reads as `warming` and is polled again. See [Waiting for a warmup](apps.md#waiting-for-a-warmup-opt-in).

##### The tier model

| Tier | Runs | When the gate runs it | Typical checks |
| --- | --- | --- | --- |
| `PREFLIGHT` (the default) | on every `/check`, with no preparation | at gate start | DNS / TCP reachability, authentication, a grant read from a system catalog, server version |
| `WARMUP` | only once `warmup` reports `ready` | at gate start when the first probe answers `ready`; otherwise after the warmup wait, up to `preflight_warmup_ceiling_seconds` | a query that needs a running warehouse, a scan of a catalog that must be indexed first, a job that must leave a queue |

A check is `PREFLIGHT` unless you mark it. Leave it that way unless you have a reason to change it. The cost of a wrong `WARMUP` mark is real: the check stops running on the user's first **Test**, and the gate only reaches it after a wait.

##### Does this check need source compute?

Ask one question per check: **can the source answer it without starting, resuming, or queueing for compute it bills for?**

- **Yes → `PREFLIGHT`.** The source answers from its control plane or metadata service: login, a token introspection, `SHOW GRANTS`, an `information_schema` read the service handles without a running warehouse, a REST listing, the reachability of an endpoint. These answer in seconds whether or not the source is warm.
- **No → `WARMUP`.** The answer only exists once compute is running: anything that executes on a suspended warehouse or cluster (`SELECT` against a table, even `SELECT 1` on engines that resume for it), a probe that has to wait for a slot in a job queue, a scan that needs an index built first.
- **Not sure? Measure it.** Run the check against a source you have just suspended. If it either fails or takes far longer than it does against a warm source, it needs compute. A check that is only slow because it fans out over many schemas is a sizing problem, not a warmup. Bound it (see [Sizing the check budget](apps.md#sizing-the-check-budget)) rather than moving it to `WARMUP`.

Two rules follow from this:

1. **Never make the auth check `WARMUP`.** Bad credentials must fail on the first click and at gate start, not after a ten-minute resume. If the source can only authenticate by running a query, split the check: authenticate against the control plane in `PREFLIGHT` and keep the query in `WARMUP`.
2. **Do not resume the source from a `PREFLIGHT` check.** A `PREFLIGHT` check that quietly wakes the warehouse makes every **Test** click cost compute, and that cost is what the tiers exist to avoid. The resume belongs in `warmup`.

##### The warmup probe

`warmup` is stateless and idempotent. Each call both pushes the warmup forward and reports where it is, and the SDK holds no warmup state between calls: it calls `warmup` once per `/warmup` request, once per `/check` that asks for the `warmup` tier, and once per gate probe activity (the first at gate start, then each poll). A query-probe app submits its probe query, waits up to `input.probe_timeout_seconds` (`App.preflight_warmup_probe_timeout_seconds`, default 10s), and reports `ready` if it answered, `warming` or `queued` if not. The SDK cancels a probe that overruns the timeout and reads it as `warming`. Whether to cancel a probe query still pending at the timeout is the app's call; each poll submits again, so the default guidance is to cancel.

| `WarmupState` | Meaning | What the caller does |
| --- | --- | --- |
| `cold` | compute is suspended or not started | waits and probes again |
| `warming` | compute is starting | waits and probes again |
| `queued` | compute is up, but statements wait for a slot | waits and probes again |
| `ready` | the `WARMUP` checks can run | runs them |
| `unavailable` | the source will not get ready on its own | stops waiting; the failure is attributed to the source |

Raise a typed `AuthError`, `AppPermissionDeniedError` or `NotFoundError` for a failure no amount of waiting fixes. Return `unavailable` for a source that will not come back, such as a dropped or disabled warehouse. Never use privileges beyond what the app's checks already verify.

The other fields are for the user and the poll cadence; nothing branches on them. `source_state` is the source's own label (`RESUMING`): the gate puts it on the run's health line and in the exhausted-ceiling error, so it is what the user reads while they wait. `queued_queries` is how many statements are waiting for a slot, a count rather than a duration. `next_poll_seconds` is the source's own suggestion for when to ask again; the gate honours it with a 5s floor and never polls past the ceiling, and without it backs off from 5s, doubling to 30s.

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
from application_sdk.errors.leaves import AuthError
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
        except Exception as exc:
            err = AuthError(message="Could not connect to the database.", cause=exc)
            return AuthOutput(
                status=AuthStatus.FAILED, error=err.to_failure_details()
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

By default the Temporal namespace ceiling applies to workflows started by the handler service. To cap execution time at the SDK level, set `ATLAN_WORKFLOW_MAX_TIMEOUT_HOURS` in your deployment environment:

| Env var | Default | Effect |
|---|---|---|
| `ATLAN_WORKFLOW_MAX_TIMEOUT_HOURS` | unset (no SDK cap) | Maximum wall-clock hours a workflow may run before Temporal terminates it. Applies to every workflow started via `/workflows/v1/start` and `/events/v1/event/{event_id}`. |

Non-positive values (`0`, negative numbers) are treated as unset and emit a boot-time warning. Set in `atlan.yaml`:

```yaml
# atlan.yaml
env:
  - name: ATLAN_WORKFLOW_MAX_TIMEOUT_HOURS
    value: "4"   # workflows are capped at 4 hours
```

## Per-Entry-Point Handlers

A single app-level `Handler` serves `/workflows/v1/{auth,check,metadata}` for most apps. A **multi-entry-point** app can additionally provide *per-entry-point* implementations by dropping a `handler.py` in the entry point's package:

```python
# app/asset_export_advanced/handler.py
from application_sdk.handler.contracts import AuthInput, AuthOutput
from application_sdk.handler.context import HandlerContext

async def test_auth(input: AuthInput, ctx: HandlerContext) -> AuthOutput: ...
async def preflight_check(input: PreflightInput, ctx: HandlerContext) -> PreflightOutput: ...
async def fetch_metadata(input: MetadataInput, ctx: HandlerContext) -> MetadataOutput: ...
async def warmup(input: WarmupInput, ctx: HandlerContext) -> WarmupObservation: ...  # optional
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

## The Handler Never Imports Worker Code

Imports run **worker → handler only**. The worker may import and call the handler; the handler never imports, reuses or calls into worker code (`application_sdk.execution*`, `temporalio.worker*`, `temporalio.activity`). The handler is moving to a shared pod that serves every app, with no worker beside it, and has to stay movable into its own codebase.

`tests/unit/handler/test_import_boundary.py` enforces this. It imports `application_sdk.handler`, `.contracts`, `.base`, `.service` and `application_sdk._runtime.offload` in a fresh interpreter and lists any worker module that loaded. When it fails, move the shared piece down into a neutral module (`handler/`, `contracts`, `errors`, `common`, `_runtime`) and have the worker import it from there. Don't import worker code lazily from the handler: that hides the edge from the test without removing it.

- The `/check` route's outcome-row helpers (`PreflightSurface`, `emit_preflight_check_outcome`, `emit_preflight_crash_outcome`, `rows_outside_tiers`) live in `application_sdk/handler/_preflight_outcome.py`. The gate and SDR import them from there.
- `application_sdk.handler` serves `create_app_handler_service` and `run_app_handler_service` lazily, so importing a contract never loads the HTTP server.
- **Temporary allowance:** `temporalio.client` and everything it imports (which includes `temporalio.activity`) are allowed, because the `/workflows/v1/start` route starts workflows with it. That route is expected to go away in the shared-server migration. Delete the allowance in the test together with the route.

The gate reaches the handler through one seam, `PreflightTransport` (`application_sdk/execution/_temporal/preflight_transport.py`). It has two methods: `preflight_check`, taking a `PreflightInput` and returning a `PreflightOutput`, and `warmup`, taking a `WarmupInput` and returning a `WarmupObservation`. The worker uses `InProcessPreflightTransport(handler)`. Those contracts already serialise as JSON on the `/check` and `/warmup` routes, so an HTTP transport can implement the same protocol later without changing the gate's budgets, cancellation or failure attribution.

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

### Testing a warmup without a warehouse

`WarmingSource` plays a source that warms up from a script, so warmup tests need no real warehouse and no wait. The script is the sequence of answers the source gives, one per probe. Each step is a `WarmupState`, a full `WarmupObservation` (to script a `source_state`, a queue depth or a poll hint), or an exception to raise. Each `probe()` answers the current step and moves one step on, and the last step repeats when the script runs out, so a script that ends in `WARMING` warms forever and reaches the gate's ceiling. `probes` counts the calls and `reported` lists the state each one returned.

Back your source-client fake with it, so the handler's real `warmup` runs against the script. `probe()` is synchronous and takes no arguments, so wrap it for an async client:

```python
from unittest.mock import AsyncMock

from application_sdk.handler.contracts import WarmupInput, WarmupState
from application_sdk.testing import WarmingSource

async def test_warmup_reports_queued_then_ready(fake_client):
    source = WarmingSource([WarmupState.COLD, WarmupState.QUEUED, WarmupState.READY])
    fake_client.warehouse_probe = AsyncMock(side_effect=lambda *_, **__: source.probe())
    handler = MyHandler(client=fake_client)

    assert (await handler.warmup(WarmupInput())).state is WarmupState.COLD
    assert (await handler.warmup(WarmupInput())).state is WarmupState.QUEUED
    assert (await handler.warmup(WarmupInput())).state is WarmupState.READY
    assert source.probes == 3
```

`probe()` returns a `WarmupObservation`; a bare `WarmupState` step comes back with the state's name as its `source_state`. If your client returns the source's own state instead, such as a warehouse status string, have the client fake translate it.

To test the gate itself, pass `WarmingSourceHandler(source)` as the worker's handler. Its `warmup` probes the source, and its `preflight_check` answers one `PREFLIGHT` row (`reachable`) and one `WARMUP` row (`catalogScan`), each only when its tier is requested; `ignores_tiers=True` returns both whatever was asked, to exercise the post-call tier check. It records every call the gate makes in `calls` (`"probe"`, `"check:preflight+warmup"`). `tests/integration/test_preflight_warmup.py` runs the whole gate wait this way, through a real worker on a time-skipping server.
