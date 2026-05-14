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
  connector is running (G1, G2, G4, G5).
- **Permission gaps** — the user or service account does not have access to something the
  workflow needs: the source system, the Atlan connection, or the Atlan platform itself (G3,
  G6).

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

## Confirmed Real-World Issues from Linear

The gaps described above are not theoretical. Searching Linear found **7 open tickets**
where exactly these patterns caused real customer failures. None of them are fixed yet.

A quick way to read each entry: **What the user saw → What actually went wrong → Why
preflight didn't catch it → Current status.**

---

### SS-17 — Workflow fails because the person who set it up no longer has a valid account

**Status:** Open since January 2026 — no fix yet
**Team:** Support Signals
**Relates to:** G6

**What the user sees:** A workflow that ran fine last week suddenly fails every time it
runs on a schedule. But if someone clicks "Run now" manually, it works perfectly.

**What actually went wrong:** Every scheduled workflow stores the identity of the person
who created it. If that person leaves the company, gets suspended, or has their role
downgraded (e.g., from admin to a basic member), the scheduled run still tries to act as
that person — and gets rejected. The manual run works because it uses the current user's
live session instead.

**Why preflight didn't catch it:** When the workflow was set up, the creator's account was
valid. Preflight ran at that time and passed. No check runs again at schedule time to
confirm the stored identity is still active.

**Three variants documented in this ticket:**
1. User account disabled in Keycloak (the identity provider) — workflow fails at publish
2. User's Atlan role downgraded — workflow runs but can't write results
3. Scheduled run stores a stale or mistyped user ID — manual runs pass, cron runs always fail

The datavant production incident (G6 in this doc) is variant 1. This ticket confirms it is
a recurring pattern seen across multiple customers.

---

### SS-27 — Snowflake workflow finishes successfully but half the data is missing

**Status:** Backlog — no fix yet
**Team:** Support Signals
**Relates to:** G3

**What the user sees:** The workflow shows "Completed" with no errors. But when they open
Atlan, a large chunk of their Snowflake objects are missing — no streams, no certain tables,
no tags. They file a support ticket because they think something is broken in Atlan.

**What actually went wrong:** The Snowflake service account used by Atlan has permission to
connect and read some things, but not everything. Snowflake silently skips any object it
can't access rather than throwing an error. The workflow completes and reports success on
the objects it could see.

**A real example (Zendesk 118017):** A customer's Atlan role could read databases, schemas,
and tables — but streams were invisible because the role was missing `USAGE` on the
databases those streams belonged to. The workflow ran for its full duration, extracted
everything it could, and finished with exit code 0. No error. Just missing data.

**Why preflight didn't catch it:** Preflight checks whether Atlan can connect to Snowflake.
It does not check whether Atlan can read each specific type of object the customer has
configured for extraction. The gap between "can connect" and "can read everything we need"
is where the false positive lives.

**This is the most commonly reported class of false positive for Snowflake.** The fix
requires checking permissions per object type (table, stream, stage, tag, etc.) before
the workflow runs — not just a connection ping.

---

### WARE-723 — Snowflake miner: preflight passes, every run fails

**Status:** Backlog — no fix yet
**Team:** App Warelines Studio
**Relates to:** G3

**What the user sees:** The preflight check screen shows green for the Snowflake miner.
When the workflow runs, it fails on authentication. 0% success rate. The customer cannot
use query history ingestion at all despite seeing a passing preflight.

**A real case:** `tripactions-vc` had an SLA breach because every miner run failed after
preflight said it was ready.

**What actually went wrong:** The credential that preflight tested was not the same
credential state the workflow used at runtime. After a retry or a re-resolution from the
credential store, the miner got a different (invalid) version of the credential. Preflight
had already confirmed the old version.

**In plain terms:** Preflight is like checking your passport at the airline check-in desk.
The miner's bug is equivalent to the boarding gate scanning a different document — one
that's expired. The check-in passed, but the gate rejected you.

---

### WARE-38 — DBT Core accepts completely wrong storage credentials at preflight

**Status:** Backlog — no fix yet
**Team:** App Warelines Studio
**Relates to:** G3

**What the user sees:** They configure a dbt Core connection to an Azure storage bucket,
enter credentials (even random test values), and preflight passes. They run the workflow
and it immediately fails trying to reach the bucket.

**What actually went wrong:** The dbt Core connector has three ways to authenticate (cloud,
object store, and "core" mode). The cloud and object-store modes have real validation built
in. The "core" mode does not — it accepts any input without testing it against the actual
storage.

**Confirmed on `tel.atlan.com`:** A customer entered gibberish credentials. Preflight
showed green. The workflow failed immediately on the first real storage access.

**The fix is straightforward:** Try to read a small test file from the configured storage
path at preflight time. If it fails, surface the error before any workflow starts.

---

### DBBI-687 — Tableau: two separate preflight gaps causing active production failures

**Status:** Backlog (High priority) — no fix yet
**Team:** DB and BI
**Relates to:** G3

Two unrelated issues, both in production, both uncaught by preflight.

**Issue 1: A filter setting crashes the workflow after 11+ days of failures**

Tableau lets users configure a filter called `exclude_projects_regex` to skip certain
projects. Some users enter patterns using advanced syntax that Tableau's underlying Python
code cannot handle. Preflight accepts the setting without checking whether it's valid. The
crash only happens during the actual extraction — and it has been happening on every single
run on one Platinum-tier tenant for 11 days straight.

The fix: test the filter pattern at preflight time and tell the user immediately if it's
invalid, before any workflow runs.

**Issue 2: Sharing a credential token across multiple connections breaks both**

Tableau uses a "Personal Access Token" (PAT) — a single key that identifies a user
session. If the same PAT is used to set up two separate Atlan connections, opening one
connection's session automatically invalidates the other. Preflight tests each connection
in isolation, passes both, and then during the actual run the first connection's workflow
fails because the second one stole its session.

The fix: during preflight, check whether the same PAT is already in use by another active
Atlan connection, and warn the user before they create a conflict.

---

### BLDX-1204 — Preflight screen gets stuck loading when a user tries to rerun a workflow

**Status:** Open — no fix yet
**Team:** Builder Experience
**Relates to:** G1 / platform reliability

**What the user sees:** They open a previous workflow to rerun it. The preflight check
panel spins indefinitely and never loads. They either wait and give up, or click "Run
anyway" without any validation at all.

**Reproduced on:** Snowflake and BigQuery, across at least two different tenants.

**Why this matters beyond the UX:** If preflight doesn't load, users lose the one signal
that could prevent a failed run. They either run blind or waste time waiting. This issue
makes all the gap fixes above irrelevant for reruns — the screen never renders the results.

The root cause hasn't been fully diagnosed yet. It may be connected to how the preflight
response is shaped differently on a rerun compared to a first-time run.

---

### ARUN-11 — Azure storage precondition not checked before a Heracles platform rollout

**Status:** Backlog — no fix yet
**Team:** App Runtime
**Relates to:** G3 / G5

**Context:** Heracles needs all Azure-hosted tenants to be using Azure Blob Storage as
their object store before a planned infrastructure change can be rolled out. If a tenant
is misconfigured (e.g., still pointing at S3 instead of Azure Blob), the rollout will
break them.

**The gap:** There is no check that verifies this precondition before the rollout proceeds
tenant by tenant. A misconfigured tenant would only discover the problem after the change
is applied and their workflows start failing.

**This is the same pattern as the connector false positives** — a required condition is
not validated before work begins, so failures are discovered at runtime instead of at
validation time.

---

### Open tickets at a glance

| Ticket | What users experience | Open since | Gap |
|--------|-----------------------|------------|-----|
| [SS-17](https://linear.app/atlan-epd/issue/SS-17) | Scheduled runs fail; manual runs pass — creator account was disabled or role downgraded | Jan 2026 | G6 |
| [SS-27](https://linear.app/atlan-epd/issue/SS-27) | Snowflake workflow completes with no error, but data is partially missing | Feb 2026 | G3 |
| [WARE-723](https://linear.app/atlan-epd/issue/WARE-723) | Snowflake miner preflight green, every run fails with auth error | Mar 2026 | G3 |
| [WARE-38](https://linear.app/atlan-epd/issue/WARE-38) | DBT Core accepts any credentials at preflight; workflow fails immediately | Jan 2026 | G3 |
| [DBBI-687](https://linear.app/atlan-epd/issue/DBBI-687) | Tableau: filter crash after 11+ days; shared token breaks both connections | May 2026 | G3 |
| [BLDX-1204](https://linear.app/atlan-epd/issue/BLDX-1204) | Preflight panel stuck loading on rerun — users run without any checks | Apr 2026 | Platform |
| [ARUN-11](https://linear.app/atlan-epd/issue/ARUN-11) | Azure storage misconfiguration not caught before platform rollout | Jan 2026 | G3 / G5 |

### Recently fixed — same patterns

| Ticket | What was wrong | Fixed |
|--------|---------------|-------|
| [DQ-727](https://linear.app/atlan-epd/issue/DQ-727) | DQ preflight said "all checks passed" but Azure storage firewall blocked the actual run (P&G) | May 2026 |
| [WARE-1250](https://linear.app/atlan-epd/issue/WARE-1250) | Snowflake crawler declared 4 preflight checks in the UI contract but only ran 2 — 2 checks were silently never emitted | May 2026 |

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
