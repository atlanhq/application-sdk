# G6 Fix — Catching Disabled Users Before the Workflow Starts

**Ticket:** HYP-829
**Branch:** `rupesh/hyp-829-g6-user-status-gate` (Heracles + application-sdk)
**Related gap:** G6 in `docs/concepts/preflight-gaps.md`

---

## The Problem — What Was Happening Before

Imagine you click **Run Workflow** to pull metadata from Snowflake into Atlan.
Behind the scenes, the system does roughly three things in order:

1. **Extract** — connect to Snowflake and pull all the schemas, tables, and columns
2. **Parse queries** — analyse query history to figure out which tables are popular
3. **Publish** — write everything into Atlan so users can search and browse it

Steps 1 and 2 can take anywhere from a few minutes to a few hours depending on
how big the data source is.

Now imagine the person who set up that workflow has since had their Atlan account
**disabled** — maybe they left the company, or an admin turned it off.
The system would still happily run steps 1 and 2, burning all that time and compute,
and only discover the problem at step 3 — the publish phase — when it tries to write
data on behalf of that user and gets back an error: `"user disabled"`.

**That is exactly what happened on a production tenant (2026-05-13).** The full
extraction pipeline ran to completion, then failed at publish because the triggering
user's account had been disabled in Keycloak (the identity system that manages
user accounts). Mustafa T confirmed this after investigation.

All of that work was wasted. The failure was 100% predictable before the workflow
even started, because the system already had the user's identity — it just never
checked whether that identity was still active.

---

## What We Fixed — Two Layers

We fixed this in two places. Think of them as an outer gate and an inner safety net.

---

### Layer 1 — The Outer Gate (Heracles)

**Repo:** `heracles` | **File:** `handler/workflow.go`

**What Heracles is:** Heracles is the orchestrator — it's the service that receives a
"run this workflow" request from the UI and decides how to dispatch it. Every workflow
submission passes through here.

**What we added:**

Before Heracles hands the job off to the Temporal workflow engine (which actually
does the extraction), it now runs one quick check:

> "Is the user who triggered this workflow still active?"

It does this by asking Keycloak — the identity provider — to look up the user's
account status. Keycloak already knows whether a user is enabled or disabled.
Heracles already had the code to talk to Keycloak for other purposes (like
impersonation and user management), so this was a matter of calling that same
system one extra time, earlier in the process.

**What happens now:**

- If the user **is active** → workflow proceeds as normal, nothing changes.
- If the user **is disabled** → Heracles immediately returns an error:
  > *"User account 'jane.doe' is disabled. Contact your administrator to re-enable
  > the account before running workflows."*
  
  No extraction runs. No compute is wasted. The error appears right away, before
  anything expensive happens.

- If Keycloak itself is temporarily unreachable → Heracles logs a warning and lets
  the workflow through. We don't want a Keycloak hiccup to block every single
  workflow. The real-time extraction will surface the error if there's a genuine
  problem.

**In plain terms:** It's like a security guard checking your badge before letting you
into the building, instead of letting you walk all the way to your desk and then
telling you your access was revoked.

---

### Layer 2 — The Inner Safety Net (application-sdk)

**Repo:** `application-sdk` | **File:** `application_sdk/handler/checks.py`

**What the SDK is:** The application-sdk is the Python framework that each connector
(Snowflake miner, dbt miner, Tableau miner, etc.) is built on. Every connector has
a `preflight_check()` method — a quick health check that runs before the workflow
starts and is supposed to catch problems early.

**What we added:**

A new helper function called `check_atlan_publish_permission`. Connectors can call
this from their `preflight_check()` to verify:

> "Can the user who triggered this workflow actually write data into the Atlan
> connection they're trying to publish to?"

It does this by making a quick API call to Atlan using the triggering user's own
credentials. If that call comes back with an error, we know the publish phase will
fail — and we can say so immediately.

**The three cases it catches:**

| What Atlan returns | What it means | Message the user sees |
|---|---|---|
| **401** | Account is disabled or the login token is no longer valid | "Your account appears to be disabled. Contact your administrator." |
| **403** | Account is active, but the user doesn't have permission to write to this connection | "You don't have the required Atlan role to publish to this connection." |
| Connection not found | The connection the workflow is pointing to doesn't exist in Atlan | "Connection not found — check that it exists and you have access to it." |

**Why this is the "inner" safety net:** Layer 1 (Heracles) catches the disabled-account
case before the workflow even reaches the connector. Layer 2 catches the more nuanced
permission cases — a user can be active but still lack the specific Atlan role needed
to write to a particular connection. Layer 2 gives connectors an easy, one-line way
to check that, without each connector team having to figure out how to do it
themselves.

**How a connector uses it:**

```python
from application_sdk import handler

class MyConnectorHandler(Handler):
    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        checks = []

        # ... other connectivity checks ...

        # Add this one line to catch disabled accounts and missing permissions
        checks.append(
            await handler.check_atlan_publish_permission(
                atlan_client=self.atlan_client,
                connection_qualified_name=input.connection_config.get("connection"),
            )
        )

        all_passed = all(c.passed for c in checks)
        return PreflightOutput(
            status=PreflightStatus.READY if all_passed else PreflightStatus.NOT_READY,
            checks=checks,
        )
```

---

## Before vs. After

| Situation | Before this fix | After this fix |
|---|---|---|
| User account disabled | Full extraction runs, fails at publish | Rejected immediately at workflow submission with a clear error |
| User lacks Atlan write permission | Full extraction runs, fails at publish | Caught at preflight, before extraction starts |
| Keycloak temporarily unreachable | N/A (no check existed) | Warning logged, workflow allowed through |
| User account active and has permission | Workflow runs normally | Workflow runs normally (no change) |

---

## What Was Not Changed

- **Existing workflows** that are already running are unaffected.
- **Connector code** does not need to change to benefit from Layer 1 — every AE
  workflow submission now goes through the Heracles gate automatically.
- Layer 2 (`check_atlan_publish_permission`) is **opt-in** — connectors add the
  one-line call when they are ready. It does not run automatically.

---

## Files Changed

| Repo | File | What changed |
|---|---|---|
| `heracles` | `handler/workflow.go` | Added 24-line user-status gate at the top of `processAutomationEngineWorkflow` |
| `application-sdk` | `application_sdk/handler/checks.py` | New file — `check_atlan_publish_permission` helper (~108 lines) |
| `application-sdk` | `application_sdk/handler/__init__.py` | Exported `check_atlan_publish_permission` so connectors can import it |
