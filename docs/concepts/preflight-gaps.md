# Preflight Check Gap Analysis

**Ticket:** HYP-829
**Component:** `application_sdk/handler/` × `heracles/handler/` × `heracles/pkg/app/`
**Status:** Analysis complete — fixes tracked in HYP-829

This document maps every known gap between what the SDK's preflight system checks and what the
runtime workflow execution actually requires. Each gap explains the execution flow involved, the
failure pattern it produces, and a concrete fix.

---

## Background: How Preflight Fits the Execution Flow

### What preflight is supposed to do

Before a user clicks **Run Workflow**, the UI calls `POST /workflows/v1/check` on the SDK app.
The app runs its `Handler.preflight_check()` implementation, which should validate everything the
workflow will need at runtime: source connectivity, credentials, permissions, filter syntax, and
resource availability. The SDK returns a `PreflightOutput` with an overall `status` (READY /
PARTIAL / NOT_READY) and a list of named `PreflightCheck` results.

### The actual call chain

```
UI / API caller
  └─▶ Heracles  POST /credentials/{connector}/preflight
        └─▶ handler/credential.go: shouldRoutePreflightToApp()
              └─▶ pkg/app/client.go: RunPreflightCheck()
                    └─▶ SDK app  POST /workflows/v1/check
                          └─▶ handler/service.py: preflight_check()
                                └─▶ Handler.preflight_check(PreflightInput)
                                      └─▶ connector implementation
```

### What happens after preflight passes

```
UI / API caller
  └─▶ Heracles  POST /workflows/v1/create
        └─▶ handler/workflow.go: processAutomationEngineWorkflow()   (AE path)
        │     └─▶ appClient.GetManifest()          → fetch DAG template
        │     └─▶ substituteTemplateVars()          → inject credentials + filters
        │     └─▶ AE: create + submit workflow      → Temporal picks up activities
        │
        └─▶ handler/workflow.go: processNativeWorkflow()             (direct path)
              └─▶ executeAllPayloads()              → resolve + store credential
              └─▶ appClient.StartWorkflowWithPayload()
                    └─▶ SDK app: Temporal activities start
                          └─▶ sql_app.py: asyncio.gather(extract_databases,
                                                          extract_schemas,
                                                          extract_tables,
                                                          extract_columns)
                                └─▶ clients/sql.py: run_query()  ← actual SQL
```

The key observation: **preflight runs once on the HTTP thread; the real work runs later, in
Temporal activities, under different context, with credentials re-resolved from a vault, across
4 concurrent connections, on queries the preflight never touched.**

---

## Gap Summary Table

| ID | Category | One-line description | Severity |
|----|----------|----------------------|----------|
| [G1](#g1--defaulthandler-returns-ready-with-no-checks) | Missing Validation | `DefaultHandler.preflight_check()` returns READY silently with zero checks | Critical |
| [G2](#g2--partial-status-collapses-to-successfalse) | Insufficient Coverage | `PARTIAL` status maps to `success=false` — hides partial connectivity | High |
| [G3](#g3--no-schematable-level-permission-validation) | Permission Gap | No validation of `information_schema` or table-level read grants | High |
| [G4](#g4--checks_to_run-accepted-but-never-dispatched) | Missing Validation | `checks_to_run` field accepted but always ignored | Medium |
| [G5](#g5--sql-template-syntax-never-validated-at-preflight) | Missing Validation | SQL templates with include/exclude filters never rendered at preflight | High |
| [G6](#g6--single-connection-preflight-vs-4-way-parallel-runtime) | Scope Mismatch | Preflight opens 1 connection; runtime opens 4 concurrently | High |
| [G7](#g7--timeout_seconds-accepted-but-never-enforced) | Missing Validation | `timeout_seconds` in `PreflightInput` is ignored — requests can hang forever | Medium |
| [G8](#g8--oauth-token-staleness-between-preflight-and-runtime) | Temporal Gap | OAuth/service-account tokens expire between preflight and activity start | High |
| [G9](#g9--token-refresh-not-called-in-heracles-workflow-execution-path) | Temporal Gap | Heracles refreshes tokens in interactive paths but not before workflow start | High |
| [G10](#g10--automation-engine-path-has-no-preflight-call) | Missing Validation | AE workflow submission path never calls preflight — API-driven runs bypass it entirely | High |
| [G11](#g11--no-disk-space-pre-check-for-extract-output) | Missing Validation | Extract writes multiple concurrent JSONL files; disk space never pre-checked | Medium |
| [G12](#g12--heartbeat-timeout-not-exercised-at-preflight) | Insufficient Coverage | Preflight timeout is 55 s; runtime heartbeat window is 120 s — slow queries invisible | Medium |
| [G13](#g13--worker-eviction-path-not-exercised-at-preflight) | Insufficient Coverage | `asyncio.CancelledError` (worker eviction) handled in activities but not in preflight | Low |
| [G14](#g14--sdr-agent-liveness-not-re-validated-before-temporal-dispatch) | Temporal Gap | SDR agent may go offline between preflight and workflow submission | Medium |

---

## Detailed Findings

---

### G1 — `DefaultHandler` returns READY with no checks

**Category:** Missing Validation  
**Severity:** Critical  
**SDK file:** `application_sdk/handler/base.py:190–195`

#### What the code does

```python
class DefaultHandler(Handler):
    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        """Always returns READY with no checks."""
        return PreflightOutput(
            status=PreflightStatus.READY,
            message="All preflight checks passed",
        )
```

`DefaultHandler` is the base class every connector inherits from. When a connector author does
not override `preflight_check` — or explicitly calls `super().preflight_check(input)` — the SDK
responds with `{"success": true, "status": "READY"}` immediately, without performing any
validation whatsoever.

#### How this causes false positives

The message `"All preflight checks passed"` is indistinguishable in logs and in the UI from a
response that actually ran checks. An operator sees a green preflight screen and starts the
workflow, which then fails at the Temporal activity level with a connection error, an auth
failure, or a missing grant.

#### The fix

Make the no-op behavior visible in logs and in the response message, so it surfaces in both
connector development and production monitoring:

```python
# application_sdk/handler/base.py

async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
    logger.warning(
        "DefaultHandler.preflight_check() was called without an override. "
        "No checks were performed. Subclass Handler and implement "
        "preflight_check() to validate connectivity and permissions."
    )
    return PreflightOutput(
        status=PreflightStatus.READY,
        message="No preflight checks implemented — override Handler.preflight_check()",
    )
```

---

### G2 — PARTIAL status collapses to `success=false`

**Category:** Insufficient Coverage  
**Severity:** High  
**SDK file:** `application_sdk/handler/service.py:670`

#### What the code does

```python
return JSONResponse(
    content=_wrap_response(
        v2_data,
        message=result.message or f"Preflight check {result.status.value}",
        success=result.status == PreflightStatus.READY,   # PARTIAL → False
    )
)
```

The v2 response envelope's top-level `success` field is `True` only for `READY`. Both `PARTIAL`
and `NOT_READY` produce `success=False`.

#### How this causes false positives (in reverse)

A connector that correctly distinguishes partial access — "auth works but schema `ANALYTICS` is
not accessible; schema `STAGING` is fine" — returns `PreflightStatus.PARTIAL` with per-check
details. Heracles and the UI see `success=False` and block the workflow. The operator receives
no signal that a partial run is possible.

The per-check results in `data` are present but are buried below the top-level failure signal,
which most UI implementations stop reading once they see `success=False`.

#### The fix

```python
# application_sdk/handler/service.py:670
success=result.status != PreflightStatus.NOT_READY,
```

`READY` → `True` (unchanged), `PARTIAL` → `True` (partial pass surfaced), `NOT_READY` → `False`
(only hard failure blocks the workflow).

> **Note:** This change is also tracked as a standalone item in HYP-829 and is the highest
> user-visible fix in this document.

---

### G3 — No schema/table-level permission validation

**Category:** Permission Gap  
**Severity:** High  
**SDK files:** `application_sdk/handler/base.py:144–157` (abstract contract),
`application_sdk/templates/sql_app.py:225–292` (runtime queries)

#### What the runtime actually does

Every `SqlApp` extract task executes queries against `information_schema`:

```sql
-- extract_databases
SELECT ... FROM information_schema.databases WHERE ...

-- extract_schemas
SELECT ... FROM information_schema.schemata WHERE schema_name LIKE {include_regex}

-- extract_tables
SELECT ... FROM information_schema.tables WHERE table_schema IN (...)

-- extract_columns
SELECT ... FROM information_schema.columns WHERE table_name IN (...)
```

These queries require explicit grants (`SHOW DATABASES`, `SELECT ON information_schema.*`, or
equivalent) that are often absent from least-privilege service accounts.

#### The preflight gap

The `Handler` ABC declares `preflight_check()` as:

```python
@abstractmethod
async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
    """Run preflight checks (connectivity, permissions, etc.)."""
    ...
```

There is no enforcement of what "permissions etc." means. A connector author who tests auth with
`SELECT 1` will get a green preflight, and the workflow will fail on every `information_schema`
query in the first extract activity.

#### The fix

Add a standard `InformationSchemaCheck` to the SDK's built-in check library that `SqlApp` and
SQL connectors can call directly from their `preflight_check()`:

```python
# application_sdk/handler/checks.py  (new file)

from application_sdk.handler.contracts import PreflightCheck

async def check_information_schema_access(client) -> PreflightCheck:
    """
    Verify the credential can query information_schema.
    Required by every SqlApp extract task.
    """
    try:
        # Use the same pattern as run_query() but with minimal scope
        async for _ in client.run_query(
            "SELECT table_name FROM information_schema.tables LIMIT 1",
            batch_size=1,
        ):
            break
        return PreflightCheck(name="InformationSchemaAccess", passed=True)
    except Exception as exc:
        return PreflightCheck(
            name="InformationSchemaAccess",
            passed=False,
            message=(
                f"Cannot query information_schema: {exc}. "
                "Grant SELECT ON information_schema.* to the service account."
            ),
        )
```

`SqlApp.preflight_check()` should call this alongside the basic connectivity check so the
permission gap is caught before any Temporal activity starts.

---

### G4 — `checks_to_run` accepted but never dispatched

**Category:** Missing Validation  
**Severity:** Medium  
**SDK file:** `application_sdk/handler/contracts.py` (`PreflightInput`),
`application_sdk/handler/base.py`

#### What the contract exposes

`PreflightInput` has a `checks_to_run: list[str]` field that lets callers request a named subset
of checks — for example, Heracles might request only `["AuthCheck"]` before a fast-path
operation, or the UI might request all checks on initial load.

Neither `DefaultHandler` nor any SDK template reads this field. Every call runs all checks (or
no checks, in `DefaultHandler`'s case), regardless of the request.

#### The fix

Document the dispatch contract on the abstract method and provide a helper:

```python
# application_sdk/handler/base.py

@abstractmethod
async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
    """
    Run preflight checks for the given input.

    Implementations MUST respect ``input.checks_to_run``:
    - If ``checks_to_run`` is empty, run all checks.
    - If ``checks_to_run`` is non-empty, run only the named checks.
    Use ``should_run_check()`` from ``application_sdk.handler.checks`` for the
    dispatch decision.
    """
    ...
```

```python
# application_sdk/handler/checks.py

def should_run_check(check_name: str, checks_to_run: list[str]) -> bool:
    """Return True if this check should run given the requested subset."""
    return not checks_to_run or check_name in checks_to_run
```

Usage in a connector:

```python
async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
    checks = []
    if should_run_check("ConnectivityCheck", input.checks_to_run):
        checks.append(await self._run_connectivity_check())
    if should_run_check("InformationSchemaAccess", input.checks_to_run):
        checks.append(await check_information_schema_access(self._client))
    return PreflightOutput(
        status=derive_status(checks),
        checks=checks,
    )
```

---

### G5 — SQL template syntax never validated at preflight

**Category:** Missing Validation  
**Severity:** High  
**SDK files:** `application_sdk/templates/sql_app.py:573–603` (`_prepare_sql()`),
`application_sdk/templates/sql_app.py:661–662` (`run_query()`)

#### What `_prepare_sql()` does

Every extract task calls `_prepare_sql()` to render the SQL template string with
`include_regex`, `exclude_regex`, schema filters, and other `metadata_config` values before
passing the SQL string to `run_query()`.

```python
# sql_app.py (simplified)
def _prepare_sql_tables(self, task_input: ExtractionTaskInput) -> str:
    return self.TABLE_SQL_TEMPLATE.format(
        include_regex=task_input.metadata_config.include_tables_regex,
        exclude_regex=task_input.metadata_config.exclude_tables_regex,
        schema_filter=...,
    )
```

If `metadata_config` contains a malformed regex (`[invalid`), an unsupported filter key, or a
value that breaks the SQL string formatting, `_prepare_sql()` raises at activity start — after
Temporal has accepted the workflow.

#### The preflight gap

Preflight never calls `_prepare_sql()`. The `PreflightInput` carries `connection_config` and
`credentials` but the `metadata_config` with the include/exclude filters that determine the SQL
templates is only present at runtime.

#### The fix

Add a dry-run template rendering check to `SqlApp.preflight_check()`:

```python
async def _check_sql_template_rendering(
    self, task_input: ExtractionTaskInput
) -> PreflightCheck:
    """
    Render all SQL templates with the actual metadata_config filters.
    Discards the result — catches format errors and regex issues before
    any Temporal activity starts.
    """
    try:
        self._prepare_sql_databases(task_input)
        self._prepare_sql_schemas(task_input)
        self._prepare_sql_tables(task_input)
        self._prepare_sql_columns(task_input)
        return PreflightCheck(name="SqlTemplateRendering", passed=True)
    except Exception as exc:
        return PreflightCheck(
            name="SqlTemplateRendering",
            passed=False,
            message=f"SQL template rendering failed with current filters: {exc}",
        )
```

---

### G6 — Single-connection preflight vs. 4-way parallel runtime

**Category:** Scope Mismatch  
**Severity:** High  
**SDK files:**
- Preflight path: `application_sdk/clients/sql.py:113–133`
- Runtime path: `application_sdk/templates/sql_app.py:464–470`

#### The divergence

**Preflight** (inside `SqlClient.load()`):

```python
# clients/sql.py:119-128
with _engine.connect() as _:
    pass        # single connection, open and immediately close
self.connection = None   # dispose, don't keep it
```

**Runtime** (`SqlApp.run()`):

```python
# sql_app.py:464-470
db_result, schema_result, table_result, column_result = await asyncio.gather(
    self.extract_databases(task_input),   # connection 1 — streams up to N×10k rows
    self.extract_schemas(task_input),     # connection 2
    self.extract_tables(task_input),      # connection 3
    self.extract_columns(task_input),     # connection 4
)
```

Each extract task creates a fresh `SqlClient`, calls `load()`, then streams rows through a
server-side cursor in batches of 10,000 rows. All four run concurrently.

#### How this causes false positives

Any source system with:
- A `max_connections` or `max_sessions` limit < 4
- A firewall rule allowing only N simultaneous sessions per service account
- A per-user connection quota

...will pass the single-connection preflight test and fail when the 4-way gather opens the
second, third, or fourth connection simultaneously.

#### The fix

Add a parallel-connection stress check that mirrors the `asyncio.gather()` fan-out:

```python
# application_sdk/handler/checks.py

import asyncio

async def check_concurrent_connections(
    client_factory, n: int = 4
) -> PreflightCheck:
    """
    Open n connections simultaneously.
    Mirrors the asyncio.gather() fan-out in SqlApp.run().
    """
    async def one_connect():
        client = client_factory()
        await client.load(credentials)   # creates engine + one connection test
        await client.close()

    try:
        await asyncio.gather(*[one_connect() for _ in range(n)])
        return PreflightCheck(name="ConcurrentConnections", passed=True)
    except Exception as exc:
        return PreflightCheck(
            name="ConcurrentConnections",
            passed=False,
            message=(
                f"Cannot open {n} simultaneous connections: {exc}. "
                "Check max_connections limit, firewall rules, or per-user session quotas."
            ),
        )
```

---

### G7 — `timeout_seconds` accepted but never enforced

**Category:** Missing Validation  
**Severity:** Medium  
**SDK file:** `application_sdk/handler/service.py:626–701`,
`application_sdk/handler/contracts.py` (`PreflightInput.timeout_seconds`)

#### What the code does

`PreflightInput` carries a `timeout_seconds` field. The `/check` endpoint calls:

```python
result = await handler.preflight_check(preflight_input)
```

There is no `asyncio.wait_for` wrapper. If a preflight check hangs (TCP half-open to a
database that does not reset, LDAP query waiting on a replica) the HTTP request hangs
indefinitely. In the SDR path, the Temporal `start_to_close=55s` deadline eventually kills it;
in the direct HTTP path there is no deadline at all.

#### The fix

```python
# application_sdk/handler/service.py — inside the preflight_check endpoint

timeout = preflight_input.timeout_seconds   # default in contracts: 60
try:
    result = await asyncio.wait_for(
        handler.preflight_check(preflight_input),
        timeout=timeout,
    )
except asyncio.TimeoutError:
    raise AppTimeoutError(
        message=f"Preflight check timed out after {timeout}s",
        retryable=True,
    )
```

This also ensures that `AppTimeoutError` → HTTP 504 is returned (via the `except AppError`
clause added in the semantic HTTP status PR), not a raw 500.

---

### G8 — OAuth token staleness between preflight and runtime

**Category:** Temporal Gap  
**Severity:** High  
**SDK file:** `application_sdk/clients/sql.py:80–134`  
**Heracles file:** `handler/credential.go:232–406`, `handler/workflow.go:2138–2150`

#### The timeline

```
T+0   User completes credential setup → Heracles refreshes OAuth token
T+1   UI calls /check → preflight runs with freshly-refreshed token in request body → PASS
T+2   User reviews results, clicks "Run Workflow"
T+3   Heracles stores credential GUID in StateStore (NOT the refreshed token)
T+10  Workflow is queued in Temporal; task queue latency = several minutes
T+14  Temporal dispatches extract activity to worker
      └─▶ SqlClient.load() calls CredentialResolver.resolve_raw(credential_guid)
          └─▶ Fetches credential from vault/StateStore
              └─▶ Token was minted at T+0, now 14 min old
              └─▶ If TTL < 14 min → auth failure; workflow fails on first activity
```

#### Three distinct failure modes

1. **Short-TTL OAuth tokens** — GCP access tokens (1 h), AWS STS session tokens (15 min–1 h),
   Azure AD tokens (1 h). If the activity queue is backed up, the token is expired by the time
   the activity starts.

2. **Credential rotation between preflight and start** — An admin rotates the service account
   key between T+1 and T+14. Preflight used the old key; runtime gets the new (different)
   credentials from the vault.

3. **Resolution mode mismatch** — Preflight is called with inline credentials
   (`credentialSource: "direct"`). For SDR workflows, the runtime resolves via
   `DaprCredentialVault` in `AGENT` mode, which follows a different lookup path. A credential
   that resolves in one mode may be unavailable in the other.

#### The fix

**SDK side** — add a `CredentialFreshnessCheck` that re-resolves via the same vault path the
runtime will use, not the inline request body:

```python
# application_sdk/handler/checks.py

async def check_credential_freshness(
    secret_store, credential_ref: str
) -> PreflightCheck:
    """
    Re-resolve the credential through the runtime vault path.
    Catches rotation and resolution-mode mismatches before any activity starts.
    """
    try:
        creds = await secret_store.get_credentials(credential_ref)
        if not creds:
            return PreflightCheck(
                name="CredentialFreshness",
                passed=False,
                message=f"Credential '{credential_ref}' not found in vault",
            )
        return PreflightCheck(name="CredentialFreshness", passed=True)
    except Exception as exc:
        return PreflightCheck(
            name="CredentialFreshness",
            passed=False,
            message=f"Runtime credential resolution failed: {exc}",
        )
```

**Heracles side** — add a TTL proximity warning before returning preflight success
(`handler/credential.go`):

```go
// After RunPreflightCheck succeeds — check token expiry proximity
if tokenExpiry, ok := credential["token_expiry"].(string); ok {
    expiry, err := time.Parse(time.RFC3339, tokenExpiry)
    if err == nil && time.Until(expiry) < 10*time.Minute {
        // Inject a warning field into the response so the UI can surface it
        res["_tokenExpiryWarning"] = fmt.Sprintf(
            "Token expires in %s — workflow may fail if queue is delayed",
            time.Until(expiry).Round(time.Second),
        )
    }
}
```

---

### G9 — Token refresh not called in Heracles workflow execution path

**Category:** Temporal Gap  
**Severity:** High  
**Heracles files:**
- `handler/credential.go:232–406` — `refreshAccessToken()` definition
- `handler/credential.go:684–714` — called in `UseCredential()`
- `handler/workflow.go:1341` — `executeAllPayloads()` — no refresh call
- `handler/workflow.go:2024–2050` — AE path credential GUID extraction — no refresh call

#### What `refreshAccessToken` does

`refreshAccessToken()` handles OAuth code exchange, AWS STS `AssumeRole`, Azure managed
identity token fetch, and JDBC-specific token refresh. It is called in interactive credential
operations (`UseCredential`, `TestCredentialByGuid`) but is **absent from both
`processNativeWorkflow` and `processAutomationEngineWorkflow`**.

#### The failure pattern

1. User sets up an OAuth credential, uses it interactively (token is refreshed in `UseCredential`)
2. User runs a workflow 45 minutes later
3. `processNativeWorkflow` calls `executeAllPayloads()` → stores credential GUID
4. SDK activity resolves credential GUID from vault → gets 45-minute-old OAuth token
5. Activity fails with `401 Unauthorized` on first SQL query

#### The fix

Add a best-effort token refresh before building the workflow payload in both execution paths:

```go
// handler/workflow.go — inside processNativeWorkflow and
// processAutomationEngineWorkflow, before buildNativeAppPayload / AE submit

if err := h.refreshCredentialTokenIfNeeded(ctx, &resolvedCredential); err != nil {
    // Non-fatal: log and continue. Workflow may succeed if token is still valid.
    log.Warnf(
        "Token refresh failed before workflow start (connector=%s): %v — "+
        "workflow will proceed but may fail if token is expired",
        connector, err,
    )
}
```

---

### G10 — Automation Engine path has no preflight call

**Category:** Missing Validation  
**Severity:** High  
**Heracles file:** `handler/workflow.go:1876–2188` (`processAutomationEngineWorkflow`)

#### The gap

The AE workflow submission path (`processAutomationEngineWorkflow`) does:

1. `appClient.GetManifest()` — fetch DAG template
2. `substituteTemplateVars()` — inject credentials + filters into DAG
3. AE: create workflow version, publish, submit to Temporal

There is no preflight call anywhere in this path. The assumption is that the UI called `/check`
before the user clicked Run — but that assumption breaks in two common scenarios:

**Scenario A — API-driven workflow submission (CI/CD pipelines)**

Many teams submit workflows programmatically via the Heracles API. These callers skip the UI
entirely and never call the preflight endpoint. The workflow is accepted by AE and dispatched to
Temporal with no prior validation.

**Scenario B — Stale preflight result**

The UI ran preflight at T+0 and showed green. The user edited the credential or changed the
connection config at T+5, then clicked Run at T+10. The workflow runs with the new, unvalidated
config.

#### The fix

Add a best-effort preflight call at the start of `processAutomationEngineWorkflow`:

```go
// handler/workflow.go — beginning of processAutomationEngineWorkflow

preflightResult, err := appClient.RunPreflightCheck(connector, credential, formData)
if err != nil {
    log.Warnf("Preflight check failed before AE submission (connector=%s): %v", connector, err)
    // Policy decision: warn-only vs hard-block
    // For now: warn and continue; allow flag to make it blocking per connector
} else if !isPreflightReady(preflightResult) {
    log.Warnf(
        "Preflight returned non-READY status before AE submission "+
        "(connector=%s status=%v) — workflow may fail at runtime",
        connector, preflightResult["status"],
    )
}
```

---

### G11 — No disk space pre-check for extract output

**Category:** Missing Validation  
**Severity:** Medium  
**SDK file:** `application_sdk/templates/sql_app.py:225–292`

#### What the extract phase writes

Each of the 4 parallel extract tasks streams rows to disk in JSONL batches:

```
extract_databases  → {output_path}/databases.jsonl
extract_schemas    → {output_path}/schemas.jsonl
extract_tables     → {output_path}/tables.jsonl
extract_columns    → {output_path}/columns.jsonl
```

For a large source — 500 schemas, 50,000 tables, 1,000,000 columns — the column extract alone
can write several gigabytes. All four write concurrently.

#### The failure pattern

If the worker pod runs out of disk mid-extract, the activity fails with:

```
OSError: [Errno 28] No space left on device: '/tmp/atlan/extract/columns.jsonl'
```

There is no actionable message for the operator ("disk full on worker pod" is invisible from the
Atlan UI). Temporal retries the activity, which immediately fails again.

#### The fix

```python
# application_sdk/handler/checks.py

import shutil

async def check_disk_space(
    output_path: str, min_gb: float = 5.0
) -> PreflightCheck:
    """
    Verify available disk space at the extract output path.
    Catches low-disk conditions before any Temporal activity writes to disk.
    """
    try:
        stat = shutil.disk_usage(output_path)
        free_gb = stat.free / (1024 ** 3)
        if free_gb < min_gb:
            return PreflightCheck(
                name="DiskSpace",
                passed=False,
                message=(
                    f"Only {free_gb:.1f} GB free at '{output_path}'; "
                    f"need at least {min_gb} GB. "
                    "Free disk space on the worker node before running."
                ),
            )
        return PreflightCheck(name="DiskSpace", passed=True)
    except Exception as exc:
        return PreflightCheck(
            name="DiskSpace",
            passed=False,
            message=f"Cannot check disk space at '{output_path}': {exc}",
        )
```

---

### G12 — Heartbeat timeout not exercised at preflight

**Category:** Insufficient Coverage  
**Severity:** Medium  
**SDK files:**
- `application_sdk/execution/_temporal/sdr.py:59–100` — SDR preflight timeout: `start_to_close=55s`
- `application_sdk/execution/_temporal/activities.py:149–195` — runtime auto-heartbeat loop: 120 s window

#### The timeout mismatch

| Context | Timeout |
|---------|---------|
| Preflight (SDR Temporal path) | 55 s `start_to_close` |
| Preflight (HTTP path) | None (G7) |
| Extract/transform activities | 1800 s `schedule_to_close`, 120 s `heartbeat_timeout` |

The auto-heartbeat loop in `activities.py` sends a Temporal heartbeat every
`auto_heartbeat_seconds` (default: 30 s). If an activity function stalls — a slow
`SELECT COUNT(*)` on a 100M-row table, a network partition mid-query — and the loop cannot fire
for > 120 s, Temporal kills the activity.

Preflight never exercises this path. A connector whose connectivity check completes in 10 s is
considered healthy. If its actual SQL queries take 130 s without the heartbeat loop running
(e.g., a blocking cursor call on the main async thread), the activity is killed silently.

#### The fix

If `SqlApp.preflight_check()` runs a representative sample query, measure the response time and
warn when it approaches the heartbeat window:

```python
import time

async def check_query_responsiveness(
    client, warn_threshold_s: float = 20.0
) -> PreflightCheck:
    """
    Measure time for a minimal query.
    Warns when response time approaches the Temporal heartbeat window (120 s).
    """
    start = time.monotonic()
    async for _ in client.run_query("SELECT 1", batch_size=1):
        break
    elapsed = time.monotonic() - start

    if elapsed > warn_threshold_s:
        return PreflightCheck(
            name="QueryResponsiveness",
            passed=True,   # warn, not fail — it did succeed
            message=(
                f"Sample query took {elapsed:.1f}s. "
                "If real extract queries are slower, the Temporal heartbeat window "
                "(120s) may be exceeded. Consider increasing heartbeat_timeout_seconds "
                "on extract tasks or optimizing the source query."
            ),
        )
    return PreflightCheck(name="QueryResponsiveness", passed=True)
```

---

### G13 — Worker eviction path not exercised at preflight

**Category:** Insufficient Coverage  
**Severity:** Low  
**SDK file:** `application_sdk/execution/_temporal/activities.py:212–247`

#### The divergence

Runtime activities handle `asyncio.CancelledError` explicitly (worker eviction / pod
termination):

```python
# activities.py:212-247
except asyncio.CancelledError:
    logger.warning("Activity cancelled (worker eviction) ...")
    raise ApplicationError(
        "Activity cancelled — worker was evicted",
        non_retryable=False,   # Temporal will schedule a free retry
    )
```

Preflight runs on the HTTP handler thread. If the pod receives SIGTERM during a slow preflight
check, `asyncio.CancelledError` propagates out of `handler.preflight_check()`, bypasses all
`except` clauses in `service.py`, and produces a raw 500 with no body — not a structured error.

#### The fix

Catch `asyncio.CancelledError` in the `/check` endpoint and map it to a structured
`DependencyUnavailableError` so the UI can show "worker was restarting — retry" instead of a
generic failure:

```python
# application_sdk/handler/service.py — inside preflight_check endpoint

except asyncio.CancelledError:
    raise DependencyUnavailableError(
        message="Preflight check was interrupted (worker eviction) — retry",
        retryable=True,
    )
```

---

### G14 — SDR agent liveness not re-validated before Temporal dispatch

**Category:** Temporal Gap  
**Severity:** Medium  
**Heracles file:** `handler/workflow.go:2088–2111` (SDR task queue stamping)

#### What happens

For SDR (self-deployed runtime) workflows, Heracles stamps the Temporal task queue with
`atlan-{deploymentName}` to route activities to the customer's SDR worker. The SDR preflight
check runs as a Temporal workflow (`SdrPreflightCheckWorkflow`) routed to the same task queue.

If the SDR agent goes offline **after** preflight completes (pod restart, network partition,
scaling event) and **before** the workflow is submitted to AE, the situation is:

- Preflight result: READY (agent was alive)
- Workflow submission: accepted by AE, dispatched to Temporal
- Temporal: no worker polling the `atlan-{deploymentName}` queue
- Outcome: workflow stalls at `ScheduleToStart` until `schedule_to_close` timeout (can be hours)

The operator sees a workflow stuck in "Running" state with no activity.

#### The fix

After preflight succeeds for SDR, verify the task queue has at least one active poller before
submitting to AE:

```go
// handler/workflow.go — before AE workflow creation for SDR path

pollers, err := h.temporalClient.DescribeTaskQueue(
    ctx,
    fmt.Sprintf("atlan-%s", deploymentName),
    enums.TASK_QUEUE_TYPE_ACTIVITY,
)
if err != nil || len(pollers.GetPollers()) == 0 {
    return ctx.JSON(http.StatusServiceUnavailable, map[string]interface{}{
        "success": false,
        "message": fmt.Sprintf(
            "SDR agent '%s' is not polling — ensure the agent is running before starting a workflow",
            deploymentName,
        ),
    })
}
```

---

## Prioritized Fix Roadmap

| Priority | Gap | Effort | Impact |
|----------|-----|--------|--------|
| P0 | G2 — PARTIAL status flattened (in HYP-829) | 1 line | Unblocks partial-access workflows immediately |
| P1 | G7 — `timeout_seconds` enforcement (in HYP-829) | ~5 lines SDK | Prevents infinite hangs in HTTP path |
| P1 | G1 — `DefaultHandler` warning | 2 lines | Surfaces silent no-op checks in every log stream |
| P1 | G3 — `information_schema` permission check | ~25 lines (new fn) | Catches most common SQL connector false positive |
| P2 | G5 — SQL template dry-run | ~30 lines | Catches filter/regex errors before Temporal accepts |
| P2 | G6 — Parallel connection stress check | ~25 lines | Catches connection-pool limits before gather() fails |
| P2 | G8/G9 — Token freshness (SDK + Heracles) | ~25 lines each | High impact for all OAuth/STS connectors |
| P3 | G10 — AE path preflight gate (Heracles) | ~20 lines Go | Fixes API-driven workflow bypass |
| P3 | G4 — `checks_to_run` dispatch contract | ~20 lines SDK | Required before selective preflight can be used |
| P3 | G11 — Disk space check | ~20 lines | Prevents opaque `ENOSPC` mid-extract |
| P4 | G12 — Query responsiveness timing | ~15 lines | Informational warning for slow sources |
| P4 | G14 — SDR agent liveness (Heracles) | ~15 lines Go | Prevents silent stalls in SDR deployments |
| P5 | G13 — `CancelledError` in preflight | ~5 lines | Improves error message consistency only |

---

## Related Documents

- `docs/concepts/handlers.md` — Handler ABC, contracts, usage examples
- `docs/adr/0013-error-hierarchy-and-failure-taxonomy.md` — `AppError` and `FailureCategory`
- `docs/adr/0001-per-app-handlers.md` — Handler design rationale
- HYP-829 — Linear ticket tracking all implementation work
