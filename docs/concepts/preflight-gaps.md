# Preflight Check Gap Analysis

**Ticket:** HYP-829
**Component:** `application_sdk/handler/` × `heracles/handler/` × `heracles/pkg/app/`
**Status:** Analysis complete — fixes tracked in HYP-829

> **Production incident (2026-05-13, datavant):** A publish-app workflow ran through the full
> extraction and query-parsing pipeline, then failed at the publish phase with `"user disabled"`.
> Mustafa T confirmed the triggering user's account was disabled in Keycloak. The entire extract
> cost was wasted because no check validated IdP account status before the workflow started.
> This is documented as **G6** below.

---

## Scope of This Document

This document covers only gaps that:

1. **Preflight can directly detect** — the information needed to catch the failure is available
   at preflight time, not just at runtime.
2. **Are not the connector/app's responsibility** — token refresh, connection pool tuning,
   SQL template correctness, and heartbeat configuration are the connector author's concern and
   are intentionally excluded here.
3. **Really cause failures** — each gap has either caused a production incident or represents a
   common, high-probability failure path.

The gaps below fall into two groups:
- **Platform gaps** — the SDK or Heracles layer should enforce these regardless of which
  connector is running (G1, G2, G7, G10).
- **Permission gaps** — the user or service account does not have access to something the
  workflow needs: the source system, the Atlan connection, or the Atlan platform itself (G3,
  G15).

---

## Background: How Preflight Fits the Execution Flow

### What preflight is supposed to do

Before a user clicks **Run Workflow**, the UI calls `POST /workflows/v1/check` on the SDK app.
The app runs its `Handler.preflight_check()` implementation, which should validate everything
the workflow will need at runtime: source connectivity, credentials, permissions, and
Atlan-side publish access. The SDK returns a `PreflightOutput` with an overall `status`
(READY / PARTIAL / NOT_READY) and a list of named `PreflightCheck` results.

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
        └─▶ handler/workflow.go: processAutomationEngineWorkflow()
              └─▶ appClient.GetManifest()       → fetch DAG template
              └─▶ substituteTemplateVars()       → inject credentials + filters
              └─▶ AE: create + submit workflow   → Temporal picks up activities
                    └─▶ sdk app activities start
                          └─▶ extract_databases / extract_schemas /
                              extract_tables / extract_columns
                          └─▶ query_parsing
                          └─▶ publish to Atlan   ← user account needed here
```

---

## Gap Summary

| ID | Group | One-line description | Severity |
|----|-------|----------------------|----------|
| [G1](#g1--defaulthandler-returns-ready-with-no-checks) | Platform | `DefaultHandler.preflight_check()` returns READY silently — zero checks performed | High |
| [G2](#g2--partial-status-collapses-to-successfalse) | Platform | `PARTIAL` status maps to `success=false`, blocking workflows that have partial access | High |
| [G3](#g3--source-system-permission-validation-missing) | Permission | Connectivity check passes but actual read grants on source tables are never verified | High |
| [G4](#g4--timeout_seconds-accepted-but-never-enforced) | Platform | `timeout_seconds` in `PreflightInput` is silently ignored — preflight can hang forever | Medium |
| [G5](#g5--automation-engine-path-has-no-preflight-call) | Platform | AE workflow submission path never calls preflight — API-driven runs bypass it entirely | High |
| [G6](#g6--user-account-status-and-publish-permission-never-validated) | Permission | Disabled IdP user or missing Atlan publish permission discovered only after full extraction runs | **Critical** |

---

## Detailed Findings

---

### G1 — `DefaultHandler` returns READY with no checks

**Group:** Platform
**Severity:** High
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

`DefaultHandler` is the base every connector inherits from. If a connector author does not
override `preflight_check`, the SDK immediately responds `{"success": true, "status": "READY"}`
with no validation performed at all.

#### Why this matters

The message `"All preflight checks passed"` looks identical in logs and in the UI to a response
that actually ran checks. An operator sees a green screen and starts the workflow. It then fails
in a Temporal activity with a connection error, an auth failure, or a missing permission — with
no prior warning.

#### The fix

Make the no-op visible so it shows up in logs and in the UI response:

```python
# application_sdk/handler/base.py

async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
    logger.warning(
        "DefaultHandler.preflight_check() called with no override — "
        "no checks were performed. Override preflight_check() in your "
        "Handler subclass to validate connectivity and permissions."
    )
    return PreflightOutput(
        status=PreflightStatus.READY,
        message="No preflight checks implemented — override Handler.preflight_check()",
    )
```

---

### G2 — PARTIAL status collapses to `success=false`

**Group:** Platform
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

Both `PARTIAL` and `NOT_READY` produce `success=False`. Heracles and the UI treat them
identically and block the workflow from starting.

#### Why this matters

A connector that correctly returns `PARTIAL` — "auth works, schema ANALYTICS is inaccessible,
schema STAGING is fine" — gets treated the same as a hard failure. The operator sees a red
screen with no indication that a partial run is possible. The per-check details in `data` are
present but most UI implementations stop reading once they see `success=False`.

#### The fix

```python
# application_sdk/handler/service.py:670
success=result.status != PreflightStatus.NOT_READY,
```

`READY` → `True` (unchanged). `PARTIAL` → `True` (partial pass is now surfaced, not blocked).
`NOT_READY` → `False` (only hard failure blocks the workflow).

---

### G3 — Source system permission validation missing

**Group:** Permission
**Severity:** High
**SDK files:** `application_sdk/handler/base.py:144–157` (abstract contract),
`application_sdk/templates/sql_app.py:225–292` (runtime queries)

#### What runtime actually requires

Every `SqlApp` extract task queries `information_schema` to enumerate objects:

```sql
SELECT ... FROM information_schema.databases WHERE ...
SELECT ... FROM information_schema.schemata WHERE schema_name LIKE {include_regex}
SELECT ... FROM information_schema.tables WHERE table_schema IN (...)
SELECT ... FROM information_schema.columns WHERE table_name IN (...)
```

These require explicit grants — `SHOW DATABASES`, `SELECT ON information_schema.*`, or the
source-specific equivalent — that are commonly absent from least-privilege service accounts.

#### The preflight gap

The `Handler` ABC defines `preflight_check()` as abstract with no guidance on what permissions
to verify. A connector that tests connectivity with `SELECT 1` gets a green preflight. Every
extract activity then fails on the first `information_schema` query.

#### The fix

Add a built-in `check_information_schema_access` helper that SQL connectors call from
their `preflight_check()`. This runs the same query pattern as the extract tasks, at minimal
scope:

```python
# application_sdk/handler/checks.py  (new file)

from application_sdk.handler.contracts import PreflightCheck

async def check_information_schema_access(client) -> PreflightCheck:
    """
    Verify the credential can read information_schema.
    Required by every SqlApp extract task — catches missing grants before
    any Temporal activity starts.
    """
    try:
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
                f"Cannot read information_schema: {exc}. "
                "Grant SELECT ON information_schema.* to the service account."
            ),
        )
```

`SqlApp.preflight_check()` should call this by default alongside the basic connectivity check.

---

### G4 — `timeout_seconds` accepted but never enforced

**Group:** Platform
**Severity:** Medium
**SDK file:** `application_sdk/handler/service.py:626–701`

#### What the code does

`PreflightInput` carries a `timeout_seconds` field. The `/check` endpoint calls:

```python
result = await handler.preflight_check(preflight_input)
```

There is no `asyncio.wait_for` wrapper. If a preflight check hangs on a TCP half-open
connection or a slow LDAP replica, the HTTP request hangs indefinitely with no timeout at
the platform layer.

#### The fix

```python
# application_sdk/handler/service.py — inside the preflight_check endpoint

timeout = preflight_input.timeout_seconds   # default defined in contracts
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

`AppTimeoutError` maps to HTTP 504 via the `FailureCategory.http_status` property (added in
the semantic HTTP status PR), so the caller receives a structured, retryable error instead of
a silent hang.

---

### G5 — Automation Engine path has no preflight call

**Group:** Platform
**Severity:** High
**Heracles file:** `handler/workflow.go:1876–2188` (`processAutomationEngineWorkflow`)

#### The gap

The AE workflow submission path goes directly from credential resolution to DAG submission:

```
processAutomationEngineWorkflow()
  └─▶ appClient.GetManifest()         ← no preflight before this
  └─▶ substituteTemplateVars()
  └─▶ AE: create + publish + submit
```

No preflight call exists anywhere in this path. The assumption is that the UI already called
`/check` before the user clicked Run — but this assumption breaks in two common situations:

**API-driven workflow submission** — teams submitting workflows from CI/CD pipelines or
automation scripts never go through the UI and never call the preflight endpoint. The workflow
is dispatched to Temporal with no prior validation.

**Stale preflight result** — the UI ran preflight at T+0 (green). The user changed the
credential or connection config at T+5, then clicked Run at T+10. The workflow runs with
the new, unvalidated config.

#### The fix

Add a preflight call at the start of `processAutomationEngineWorkflow` before any Temporal
work is dispatched:

```go
// handler/workflow.go — beginning of processAutomationEngineWorkflow

preflightResult, err := appClient.RunPreflightCheck(connector, credential, formData)
if err != nil {
    return ctx.JSON(http.StatusBadRequest, map[string]interface{}{
        "success": false,
        "message": fmt.Sprintf("Preflight check failed: %v", err),
    })
}
if status, _ := preflightResult["status"].(string); status == "NOT_READY" {
    return ctx.JSON(http.StatusUnprocessableEntity, map[string]interface{}{
        "success": false,
        "message": "Preflight checks did not pass — resolve the issues above before running",
        "data":    preflightResult,
    })
}
// READY or PARTIAL — proceed with workflow submission
```

---

### G6 — User account status and publish permission never validated

**Group:** Permission
**Severity:** Critical
**Confirmed by:** Production incident — datavant.atlan.com (2026-05-13)
**Heracles file:** `handler/workflow.go` (pre-submission gate, missing)

#### What happened (production incident)

```
Temporal workflow: 9ef985fd-8216-4be…/019e2012-1574-7a63-9b17-4a5e1800bddb
Failure phase:     publish-app activity
Error:             "user disabled"
Root cause:        triggering user's Keycloak account was disabled
                   (confirmed by Mustafa T)
Cost wasted:       full extraction + query-parsing pipeline ran to completion
                   before the publish phase tried to act on behalf of the user
```

The workflow ran through:
1. **Extract** — connected to source, pulled schemas/tables/columns *(compute + time spent)*
2. **Query parsing** — parsed and normalized query history *(compute + time spent)*
3. **Publish** — attempted to write assets to Atlan as the triggering user → `"user disabled"` → failed

Steps 1 and 2 were entirely wasted. The failure was predictable before the workflow started
— Heracles already holds the user's authenticated session and can query Keycloak.

#### The broader permission gap

The same pattern applies beyond a disabled account. The publish phase requires:

- **Active IdP account** — user must be enabled in Keycloak
- **Atlan role with write access** — user must have permission to write assets to the
  target connection
- **Valid Atlan session** — the session token must be usable at publish time

None of these are validated at preflight. Only source-system connectivity is checked.
Atlan-side publish permissions are discovered only when the publish activity fails —
after all extraction work is already done.

#### Fix — Layer 1: Heracles user-status gate (ship first)

Add a Keycloak user-status check in `handler/workflow.go` before dispatching to AE.
Heracles already has a Keycloak client (`gocloak`) used in auth flows:

```go
// handler/workflow.go — processAutomationEngineWorkflow, before GetManifest

user, err := h.keycloakClient.GetUserByID(ctx, adminToken, realm, userID)
if err != nil {
    return ctx.JSON(http.StatusUnauthorized, map[string]interface{}{
        "success": false,
        "message": "Unable to verify user account status — cannot start workflow",
    })
}
if user.Enabled != nil && !*user.Enabled {
    return ctx.JSON(http.StatusForbidden, map[string]interface{}{
        "success": false,
        "message": fmt.Sprintf(
            "User account '%s' is disabled. Contact your administrator to "+
            "re-enable the account before running workflows.", userID,
        ),
    })
}
```

This fires before any Temporal work is dispatched — zero extraction cost for a disabled user.
This single check would have prevented the datavant production incident.

#### Fix — Layer 2: SDK publish-permission preflight check (follow-on)

After the Heracles gate is in place, add a `check_atlan_publish_permission` helper that
connectors call from `preflight_check()` to validate the user can write to the target
Atlan connection:

```python
# application_sdk/handler/checks.py

async def check_atlan_publish_permission(
    atlan_client, connection_qualified_name: str
) -> PreflightCheck:
    """
    Verify the triggering user has write access to the target Atlan connection.
    Catches disabled accounts, revoked roles, and missing connection permissions
    before the extraction pipeline runs.
    """
    try:
        conn = await atlan_client.asset.get_by_qualified_name(
            qualified_name=connection_qualified_name,
            asset_type=Connection,
        )
        if conn is None:
            return PreflightCheck(
                name="AtlanPublishPermission",
                passed=False,
                message=(
                    f"Connection '{connection_qualified_name}' not found in Atlan. "
                    "Verify the connection exists and the user has access to it."
                ),
            )
        return PreflightCheck(name="AtlanPublishPermission", passed=True)
    except AtlanError as exc:
        # 403 → no write permission; 401 → account disabled or token invalid
        return PreflightCheck(
            name="AtlanPublishPermission",
            passed=False,
            message=(
                f"Cannot verify publish permission for '{connection_qualified_name}': {exc}. "
                "Ensure the user account is active and has the required Atlan role."
            ),
        )
```

---

## Confirmed Real-World Tickets from Linear

Cross-referencing the gap analysis against open Linear tickets surfaces the following
unsolved issues where a preflight check either failed to catch something it should have,
or is missing entirely. All tickets below are currently **open (not Done/Cancelled)**.

---

### SS-17 — Workflow failures due to disabled / invalid workflow creator user

**Status:** Todo (not started)
**Team:** Support Signals
**Maps to:** G6 — User account status and publish permission never validated

The ticket documents a recurring pattern across multiple tenants:

- Workflows fail because the creator user's account is disabled in Keycloak.
- Workflows fail because the user's Atlan role was downgraded (e.g. admin → member)
  after the workflow was created.
- **Scheduled runs fail while manual runs succeed** — because the "run-as" user stored
  in the cron configuration is stale or invalid, but the manual runner uses the current
  live session which is still valid.

The third variant is particularly insidious: a user sets up a scheduled workflow, their
account is later offboarded, and every scheduled run fails while anyone manually re-running
it sees success. Preflight run manually passes (current session is valid); the cron fires
and uses the stored, now-disabled user.

This ticket has been open since January 2026 with no fix.

**What G6's Heracles gate catches:** the disabled-account case at workflow submission time.
**What it still doesn't catch:** the stale "run-as" user in a cron schedule — that user
identity is resolved at cron trigger time, not at preflight time.

---

### SS-27 — Snowflake preflight permission validation for asset ingestion

**Status:** Backlog (not started)
**Team:** Support Signals
**Maps to:** G3 — Source system permission validation missing

The ticket documents a large and recurring class of false positives specific to Snowflake:
workflows **complete successfully** (no error, exit code 0) but return **partial or empty
results** because the Atlan service account lacks object-level grants for the asset types
being extracted.

A representative case (Zendesk 118017): Snowflake streams and cross-database streams were
silently skipped. The workflow extracted databases, schemas, and tables correctly, but every
stream was absent. Root cause: the Atlan role had `SELECT` on streams but was missing:

- `USAGE` on the stream's home database and base-table database
- `USAGE` on both schemas
- `REFERENCES` on the base tables

The current preflight checks connectivity and broad account-level grants. It does not
enumerate the configured target scope (databases, asset types) and validate that the role
holds **per-asset-type privileges** for each object in that scope.

**G3 as documented in the gap analysis covers the general principle.** SS-27 goes deeper —
it proposes a Snowflake-specific privilege matrix (database/schema/table/view/stream/stage/tag)
and per-asset-type validation. That level of specificity lives in the Snowflake connector,
not in the SDK, but the SDK can provide the `check_information_schema_access` foundation and
the `PreflightCheck` contract for connectors to build on.

---

### WARE-723 — Miner preflight checks stay credible after retry

**Status:** Backlog
**Team:** App Warelines Studio
**Maps to:** G3 — Source system permission validation missing

Preflight on the Snowflake miner passes even when the workflow subsequently fails on
credential authentication. The ticket links a concrete SLA breach on `tripactions-vc` where
0% of miner runs succeeded despite a green preflight.

The underlying mechanism matches the pattern in G3: the credential validated at preflight
time is not the same credential (or credential state) used at runtime. After a retry,
the credential may have been re-resolved from a different path, or the preflight ran
against a cached connection while the live system rejected the actual auth.

**The gap:** preflight validates a credential snapshot at request time. Runtime activities
re-resolve credentials independently. If those two paths diverge after a retry, preflight
provides no protection.

---

### WARE-38 — DBT Core preflight checks accept invalid storage credentials

**Status:** Backlog
**Team:** App Warelines Studio
**Maps to:** G3 — Source system permission validation missing (storage variant)

DBT Core has three auth methods (cloud, objectstore, core). Cloud and objectstore have
robust preflight checks built in 2024. Core does not.

A concrete case: on `tel.atlan.com` a customer connected to an Azure blob storage bucket
using DBT Core and the preflight passed with **completely invalid / gibberish credentials**.
No actual storage access check was performed. The workflow failed later when trying to
reach the bucket.

This is a pure false positive: green preflight, workflow fails on first storage access.
The storage permission check that G3's `check_information_schema_access` addresses for SQL
connectors has an exact analogue for object-store connectors: attempt a lightweight read
against the configured storage path.

---

### DBBI-687 — Tableau: preflight regex validation + PAT isolation check

**Status:** Backlog (High priority)
**Team:** DB and BI
**Maps to:** G3 (two new dimensions not yet in the gap doc)

Two distinct false positives, both in production:

**1. User-supplied regex crashes at runtime**

`exclude_projects_regex = ^(?:{user})$` uses Python 3.11 inline flags in a position that
causes a crash. The regex is accepted at config time and preflight passes. At runtime
`re.search()` raises `re.error` at multiple extract locations
(`metadata_verification/main.py:98, 146, 231`). The failure ran for **11+ consecutive days**
on a Platinum tenant (`globaovp02`) before being caught.

Fix: run `re.compile()` against all user-supplied regex fields at preflight and return
a `PreflightCheck(passed=False)` with the exact `re.error` message if compilation fails.

**2. PAT (Personal Access Token) collision — no isolation check**

When the same Tableau PAT is used across multiple Atlan connections simultaneously,
Tableau invalidates the first active session when a second one opens. No preflight check
detects this. Preflight tests connectivity with the PAT, passes, and then the first
connection's workflow fails mid-run when the second connection opens.

Fix: add a PAT-isolation check at preflight that warns when the same credential is shared
across more than one active Atlan connection.

Both failures are **undetected by preflight today and are actively causing production
incidents**.

---

### BLDX-1204 — Preflight checks do not load on rerun

**Status:** Todo (Medium)
**Team:** Builder Experience
**Maps to:** Platform-level — preflight reliability

When a user reruns an existing workflow, the preflight check UI gets stuck in a loading
state indefinitely. Reproduced on Snowflake and BigQuery connectors across at least two
tenants.

This is not a false positive in the traditional sense — it is a reliability failure of the
preflight system itself. If preflight does not load, the user has no signal before they
run. They either skip preflight (run blind) or cannot rerun at all.

The root cause is not yet diagnosed. It may be related to G2 (PARTIAL status handling) or
the response shape differing on a rerun vs first run.

---

### ARUN-11 — Heracles azure tenant object-store preflight check

**Status:** Backlog
**Team:** App Runtime
**Maps to:** G3 (storage permission variant) + G5 (missing preflight gate in a path)

All Azure tenants must use Azure Blob Storage as their object store before a planned Heracles
rollout. A curl-based preflight check in Heracles is needed to validate that the tenant's
object store is correctly configured as Azure storage before the change is applied.

This is a deployment-gate preflight, not a connector preflight, but it illustrates the same
gap: a required precondition (object-store type must be Azure) is not validated before the
workflow proceeds, so misconfigured tenants would fail mid-rollout.

---

### Summary: open tickets mapped to gaps

| Ticket | Status | Maps to gap | Core problem |
|--------|--------|-------------|--------------|
| [SS-17](https://linear.app/atlan-epd/issue/SS-17) | Todo | G6 | Disabled user / downgraded role / stale cron user — workflow runs under invalid identity |
| [SS-27](https://linear.app/atlan-epd/issue/SS-27) | Backlog | G3 | Snowflake asset-type–specific grant gaps cause silent partial extraction |
| [WARE-723](https://linear.app/atlan-epd/issue/WARE-723) | Backlog | G3 | Miner preflight passes; auth fails at runtime after credential re-resolution |
| [WARE-38](https://linear.app/atlan-epd/issue/WARE-38) | Backlog | G3 | DBT Core azure bucket — preflight accepts gibberish credentials |
| [DBBI-687](https://linear.app/atlan-epd/issue/DBBI-687) | Backlog | G3 | Tableau regex crash at runtime + PAT collision not detected at preflight |
| [BLDX-1204](https://linear.app/atlan-epd/issue/BLDX-1204) | Todo | Platform | Preflight UI stuck loading on rerun — users run blind |
| [ARUN-11](https://linear.app/atlan-epd/issue/ARUN-11) | Backlog | G3 / G5 | Azure object-store precondition check missing before tenant rollout |

### Already fixed (same pattern, for reference)

| Ticket | Maps to | What was fixed |
|--------|---------|----------------|
| [DQ-727](https://linear.app/atlan-epd/issue/DQ-727) | G3 | DQ preflight validated JDBC but not catalog storage — ADLS firewall blocked runs; now fixed in gandalf |
| [WARE-1250](https://linear.app/atlan-epd/issue/WARE-1250) | G1 | Snowflake crawler preflight handler silently dropped checks declared in the form contract; now fixed |

---

## Fix Roadmap

| Priority | Gap | Owner | Effort | Impact |
|----------|-----|-------|--------|--------|
| **P0** | **G6 Layer 1 — Keycloak user-status gate** | Heracles | ~15 lines Go | Prevents all wasted extraction for disabled users — production incident |
| P0 | G2 — PARTIAL flattened to failure | SDK | 1 line | Unblocks workflows with partial source access |
| P1 | G6 Layer 2 — Atlan publish-permission check | SDK | ~25 lines | Catches revoked roles before extraction runs |
| P1 | G3 — Source `information_schema` permission check | SDK | ~25 lines | Catches missing read grants on source |
| P1 | G4 — `timeout_seconds` enforcement | SDK | ~8 lines | Prevents silent preflight hangs |
| P2 | G5 — AE path preflight gate | Heracles | ~20 lines Go | Fixes API-driven and stale-config bypass |
| P3 | G1 — `DefaultHandler` warning | SDK | 2 lines | Surfaces silent no-op checks in logs |

---

## Related Documents

- `docs/concepts/handlers.md` — Handler ABC, contracts, usage examples
- `docs/adr/0013-error-hierarchy-and-failure-taxonomy.md` — `AppError` and `FailureCategory`
- `docs/adr/0001-per-app-handlers.md` — Handler design rationale
- HYP-829 — Linear ticket tracking all implementation work
