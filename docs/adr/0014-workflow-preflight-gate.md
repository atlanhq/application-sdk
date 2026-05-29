# ADR-0014: Workflow-Side Preflight Gate

## Status
**Proposed**

## Context

The SDK already defines `Handler.preflight_check()` (see `application_sdk/handler/base.py:145` and `PreflightInput`/`PreflightOutput` in `handler/contracts.py`). It is reachable via two paths today:

1. **Handler HTTP endpoint** (`application_sdk/handler/service.py:611`) — `POST /workflows/v1/check`, hit by the Atlan UI's "Test Auth" button before the user saves a connection.
2. **Hand-rolled invocation from inside the workflow** — each connector decides whether to call its own `preflight_check` as the first task of an entrypoint.

Path 1 is fleet-wide. Path 2 is per-connector and **most connectors do not implement it**. Audit at the time of writing (29 May 2026):

| State | Connectors |
|---|---|
| Gates workflow on preflight | `atlan-cassandra-dse-app`, `atlan-cosmosdb-app`, `atlan-fabric-app`, `atlan-saperp-app`, `atlan-trino-app`, `atlan-domo-app` |
| Defines preflight in handler only (workflow proceeds on stale creds) | BigQuery, Snowflake, Redshift, Postgres, MSSQL, Tableau, PowerBI, Glue, Athena, Oracle, MongoDB, DynamoDB, Hightouch, Hive, Synapse, SAP HANA, OpenAPI, Anomalo, CloudSQL-Postgres, CrateDB, Dremio, Teradata, … (~24 connectors) |

This is a regression from the Argo world. Every Argo connector template included a shared `preflight-checks` DAG step backed by the `atlan-preflight` template, with a `preflight-hard-fail` parameter that defaulted to true. Invalid credentials → workflow failed immediately at the preflight step with the exact failing check's message.

In v3, the same connection with expired credentials produces a silent cascade:

1. Scheduled workflow starts.
2. Extract activity silently returns partial/empty data (the source API returns 401 or 403; the connector swallows it as "transient").
3. Process / publish fail far downstream with a cryptic error (e.g. `ObjectStoreDownloadError: No files found locally and failed to download from object store`) that has no clear pointer to "the credential is dead."
4. Customer sees a workflow failure 30+ minutes into the run with no actionable message. Often misdiagnosed as a connector bug.

The most recent expression of this was [HYP-1469](https://linear.app/atlan-epd/issue/HYP-1469) on the Domo connector, where an expired Domo developer token on `prd-domo-thrive` failed three layers of defense in a row before surfacing. The connector now ships five fixes for that single failure mode; **every other connector in the "handler only" table is silently vulnerable to the same cascade**.

Related precedents in the same class — silent failure modes that did not exist in Argo because Argo's shared infrastructure caught them, but exist in v3 because every connector reinvents (or forgets) the gate:

- [KAA-751](https://linear.app/atlan-epd/issue/KAA-751) — BigQuery agent-json placeholder
- [WARE-1377](https://linear.app/atlan-epd/issue/WARE-1377) — Snowflake migrated `include-filter`
- [WARE-1379](https://linear.app/atlan-epd/issue/WARE-1379) — Snowflake legacy `agent-json` metadata
- [WARE-1673](https://linear.app/atlan-epd/issue/WARE-1673) — Breakthru Beverage Group 97% delete

## Decision

The SDK will ship a **workflow-side preflight gate** that auto-invokes `Handler.preflight_check()` as the first task of every `@entrypoint` method, raising `PreflightError` on any required-check failure. The behavior is **opt-in** in the first release (connectors adopt by setting a class attribute) and becomes the default in a follow-up release once the fleet has migrated.

### Shape

```python
class App(ABC):
    # Class attributes controlling the auto-injected gate.

    PREFLIGHT_GATE_ENABLED: ClassVar[bool] = False
    """When True, the SDK calls ``handler.preflight_check()`` as the first
    task of every ``@entrypoint`` method. Failures of required checks raise
    ``PreflightError`` and the workflow fails before any extract activity
    runs."""

    PREFLIGHT_HARD_FAIL: ClassVar[bool] = True
    """When True (default), any required-check failure raises and stops
    the workflow. When False, failures are logged as warnings and the
    workflow proceeds (legacy-equivalent of Argo's
    ``preflight-hard-fail: false`` parameter — kept for emergency overrides,
    not recommended as steady state)."""
```

Connectors enable the gate by:

```python
class MyConnectorApp(SqlConnectorApp):
    PREFLIGHT_GATE_ENABLED = True
```

The gate construction:

1. SDK introspects the connector's handler at app-startup, confirms it implements `preflight_check`.
2. For each `@entrypoint` method, the SDK wraps it: before the user's code runs, dispatch a `_sdk_preflight` activity that calls `handler.preflight_check(PreflightInput.from_workflow_args(args))`.
3. The activity inspects the typed `PreflightOutput`. Any check with `status="failed"` whose name is in the handler's `required_checks` list raises `PreflightError(failed_checks=[...])`.
4. The workflow fails with a structured `applicationFailureInfo.type="PreflightError"` carrying the per-check messages — surfaceable in Temporal UI, AE monitor, and customer-facing error reports.

Connectors that already implement a hand-rolled preflight task (the six listed above) are expected to **remove their custom invocation** when they migrate to the gate. Until they do, the gate is a no-op for them (because `PREFLIGHT_GATE_ENABLED` defaults to False on the App base class and they have not opted in). After they opt in, the SDK's invocation replaces theirs — they delete their hand-rolled call.

### Default for new connectors

New v3 connectors generated from the SDK templates inherit `PREFLIGHT_GATE_ENABLED = True` from the template's `App` subclass, so new connectors are protected by default and cannot accidentally regress.

### Migration target for the follow-up

In a subsequent SDK release (proposed: SDK 4.0), `PREFLIGHT_GATE_ENABLED` defaults to `True` on the base `App` class itself. Connectors that for any reason cannot use the gate set it to `False` explicitly. This mirrors Argo's `preflight-hard-fail: true` default and matches the Atlan platform invariant that "scheduled runs should not silently proceed on stale credentials."

## Options Considered

### Option 1: Base-class hook with class-attribute opt-in (Chosen)

Described above. SDK-owned, single point of fix when the contract evolves, opt-in adoption avoids fleet-wide simultaneous behavior change.

**Pros:**
- One implementation, one set of tests, all ~30 connectors inherit when they opt in.
- Backward-compatible — connectors that don't opt in see no behavior change.
- Hard-fail vs warn is configurable per connector (matching Argo's `preflight-hard-fail` parameter).
- New connectors generated from templates are protected by default.

**Cons:**
- Requires per-connector PRs to flip the opt-in. The SDK PR alone does not close the gap.
- Connectors that already hand-roll preflight need to migrate to avoid double-running it (one-line change per connector).

### Option 2: Manifest-driven

Declare preflight checks in `app.pkl` (already partially the case via `Config.SageV2`). The SDK's manifest generator emits a `preflight` task as the first dependency of every other task in the generated DAG.

**Pros:**
- Single source of truth — the manifest already names the checks; tying the gate to it removes another place to forget.
- Argo's path was effectively manifest-driven (the `atlan-preflight` template was wired in from the marketplace package template).

**Cons:**
- Requires changes to both the SDK and the manifest generator / Pkl schema.
- Less flexible for connectors that need to vary preflight behavior per entrypoint (rare, but exists for SDR / SAP-style connectors with multiple workflow types).
- Coupling: a change to the preflight contract requires changes in three places (Pkl schema, generator, SDK).

Option 1 is strictly a subset of what Option 2 can do. We can ship Option 1 now and layer Option 2 on top later by having the manifest generator simply set the class attribute via codegen.

### Option 3: Per-connector hand-rolled preflight (status quo)

What the six gating connectors do today. Rejected because:

- 24 connectors are silently broken right now.
- Every connector reinvents the gate; six versions, six bug surfaces (Domo's was a `metadata.checks=[]` bypass that ran zero checks, the very thing it was supposed to gate against).
- No shared error type, no shared metric, no shared trace span — observability is per-connector.

### Option 4: Do nothing, document the convention

Document "you should call preflight_check from your workflow" in a coding-standards doc, leave connectors to implement it.

Rejected because:
- 24 connectors are already shipped without it. Documentation does not retroactively fix shipped code.
- Even with documentation, future connectors will forget — this is a known pattern in v3 (the migration-class bugs cited above are all variants of "forgot a step the framework should own").

## Consequences

### Positive

- **Fleet-wide observability parity with Argo.** Invalid credentials produce loud, immediate failures at the preflight step, with the exact failing-check message visible in Temporal UI / AE monitor / customer-facing error reports.
- **Removes per-connector reinvention of the gate.** Six hand-rolled implementations collapse to one SDK-owned implementation with one set of tests.
- **Customer escalation reduction.** Failures from credential expiry / scope change are immediately actionable ("Domo developer token returned 401 — rotate the access token in connection settings") instead of cryptic ("No files found locally and failed to download from object store").
- **Migration safety net.** Argo→AE migrations on customer tenants frequently surface credential-shape issues. With the gate in place, those issues fail loudly at the first scheduled run instead of silently corrupting data for hours.

### Negative

- **Each connector that has a working hand-rolled preflight needs a small PR** to opt in and remove the hand-rolled call. Six connectors; estimated 30 minutes each.
- **Latency overhead.** Every workflow run pays the cost of one extra activity (the preflight call). In practice this is sub-second for healthy credentials and far less than the cost of a failed extract — but it is non-zero, and connectors with extremely tight SLOs may want to scope which checks run via the existing `required_checks` mechanism.
- **Increased blast radius of bugs in `Handler.preflight_check`.** A bug in a connector's preflight implementation now fails the workflow, not just the UI auth-test. This is desired (it surfaces real problems) but means preflight code paths need test coverage they did not have before.

### Risk mitigation

- Roll out as opt-in in the first SDK release. Default flip happens only after 5+ connectors have adopted successfully and the implementation has been exercised in production for at least one ring cycle.
- Provide a `PREFLIGHT_HARD_FAIL = False` escape hatch for connectors that need the gate's diagnostics without the immediate-fail behavior during their initial adoption.
- Ship a smoke test in the SDK that exercises both pass and fail paths (PreflightOutput with all-passed, with one-failed-required, with one-failed-optional).

## References

- [BLDX-1345](https://linear.app/atlan-epd/issue/BLDX-1345) — tracking issue for this ADR
- [HYP-1469](https://linear.app/atlan-epd/issue/HYP-1469) — the rollout that exposed the gap on Domo
- ADR-0001 — Per-App Handlers (where `preflight_check` is currently surfaced for UI)
- ADR-0002 — Per-App Workers (the runtime where the new gate will execute)
- ADR-0006 — Schema-Driven Contracts (the `Config.SageV2` declaration this could layer onto for Option 2)
- ADR-0013 — Error Hierarchy and Failure Taxonomy (where `PreflightError` slots in)
