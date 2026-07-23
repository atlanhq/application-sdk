---
name: adopt-preflight-gate
description: >
  Bump a v3 app to the latest application-sdk and adopt the SDK-native
  preflight gate safely. The gate runs the app's preflight_check handler as the
  mandatory first activity of every extraction workflow and always reports the
  verdict; by default it is soft (a NOT_READY verdict is reported but the run
  proceeds), and blocking real runs on NOT_READY is a per-app opt-in
  (preflight_gate_mode = "hard"). Classifies the app's rollout bucket,
  fixes name collisions, audits the handler's status logic against the new
  semantics, runs an interactive check-design session with the developer
  (visualized as a decision tree: which checks block, which are advisory, what
  is missing), hunts for hidden preflight logic by behavior (not name) and
  consolidates it into the handler so both surfaces run one implementation,
  adopts typed check errors, and updates tests. Interactive: every
  blocking/advisory/consolidation decision belongs to the developer; the
  skill proposes, never imposes.
mandatory_triggers:
  - "/adopt-preflight-gate"
  - "adopt the preflight gate"
  - "make this app preflight-safe"
  - "bump SDK preflight safe"
optional_triggers:
  - "update preflight checks for the gate"
  - "preflight gate migration"
  - "will this app break on SDK bump"
owner: connector-platform-team
last_updated: "2026-07-23"
staleness_days: 90
inputs:
  - app_root: "auto-detected — the directory containing app/ and pyproject.toml"
outputs:
  - uv.lock + pyproject.toml (SDK floor raised to >=3.24.0 and lockfile synced)
  - app/handler.py (status logic per the agreed check tree; typed errors on failed checks)
  - deleted app-owned preflight @task + call sites (collision class, plus any duplicate readiness activities the developer agrees to consolidate — always with a coverage diff)
  - contract/app.pkl + regenerated app/generated/ (only if form-only metadata keys must move onto the input contract)
  - updated unit tests covering every verdict path
---

# Adopt the SDK-native preflight gate

## Scope boundary — check first

This skill is for **v3 apps only** (subclasses `App`, `@entrypoint` methods,
`Handler` in `app/handler.py`). If the app is v2 (Argo-era layout,
`application_sdk.workflows`/`handlers` imports), STOP and run `/upgrade-v3`
first; this skill picks up after.

Reference implementation for everything below: **atlan-mysql-app** (PR #340) —
short-circuiting auth check with typed error, advisory tables check, PARTIAL
status. Read its `app/handler.py` before proposing changes.

## What changed (context you state to the developer up front)

- The SDK injects `{app}:preflight` as the first activity of every extraction
  workflow. It calls the app's one `Handler.preflight_check`.
- `PreflightOutput.status` is the gate verdict: `NOT_READY` is always reported
  (as `outcome="would_block"`), and **aborts the run** only when the app has
  opted into hard mode (`preflight_gate_mode = "hard"`, typed `PreflightFailed`,
  red activity); `READY` and `PARTIAL` proceed. `PARTIAL` is display-only — use
  it for "advisory check failed, run anyway".
- There is no per-check `blocking` flag. Importance is expressed by **control
  flow**: required checks short-circuit (return `NOT_READY` early), advisory
  checks run and only influence `PARTIAL`.
- **Raising does not block.** A raise from the handler is a gate plumbing
  failure → the SDK fails open (logs loudly, run proceeds). Blocks happen only
  via the returned status.
- A failed check should carry `error=<SDK leaf>(...).to_failure_details()` —
  category/code/audience/suggested_action flow to the Automation Engine and
  dashboards. Untyped failures fall back to the `PREFLIGHT_CHECK_FAILED`
  sentinel.
- Behavior to say plainly: by default the gate is **soft** — a handler that
  returns `NOT_READY` does **not** block the run; the verdict is always
  reported (as `outcome="would_block"`) but the run proceeds. Blocking real
  runs is a per-app opt-in (`preflight_gate_mode = "hard"`, next section) taken
  once the app's checks are trusted.
- The two surfaces receive **credentials** differently — a nuance the "one
  implementation" principle hides. The UI's Test-Connection sends credentials
  inline in the request body; the gate has no body and **resolves** credentials
  from the extraction input's top-level triple (`credential_guid` /
  `credential_ref` / `agent_json`). It does not read
  `connection.attributes.defaultCredentialGuid`. So a green UI check is not
  proof the gate will resolve the same credential — if the guid only reaches the
  input via the connection, the gate sees none (phase-0 bucket 9).

## Hard mode — the gate-level opt-in (CNCT-81)

Enforcement is a **gate** property, not a handler property. The handler always
returns the honest verdict; the gate decides what to do with `NOT_READY`:

- **soft** (default): never raise — the run proceeds and the dodged block is
  emitted as `outcome="would_block"` (with `gate_mode="soft"` and the per-check
  `check_matrix`) on the gate outcome event. The verdict is always reported, so
  connector-pulse can rank apps by how often they *would* have blocked real
  runs; that list is the "your checks are ready to enforce" queue.
- **hard**: raise `PreflightFailed`, run aborts on `NOT_READY`. The opt-in for
  every app whose checks are trusted to gate real runs.

Opting in is deliberately explicit and deliberately small:

```python
class MyApp(App):
    preflight_gate_mode = "hard"   # git-blamed: checks are trusted to block runs
```

or, ops-side without an app release: `ATLAN_PREFLIGHT_GATE_MODE=hard` on the
worker deployment (env wins over the attribute; any value other than the
literal `hard` resolves to soft — malformed config never blocks a run by
accident). The worker logs an INFO line per hard app at boot.

Rules the skill enforces during adoption:

- Never soften the handler to dodge the gate. Returning `PARTIAL` for a
  failure that should block hides the truth from every surface; posture
  belongs on the App class, verdicts belong in the handler.
- Soft is the default landing state with an exit: when pulse shows the app's
  `would_block` rows track real workflow failures (checks are right), add
  `preflight_gate_mode = "hard"` — that is the whole hard-fail flip.
- An app with no `preflight_check` handler needs no posture: the DefaultHandler
  never returns `NOT_READY`, so the gate never has anything to enforce.

## The design principle every decision flows from

**One implementation, two surfaces.** `Handler.preflight_check` is the single
authority on "is this source ready" — the UI's Test-Connection button and the
gate at the head of every run both execute the same function. Two copies of
readiness logic (a handler AND an activity, or checks buried in extraction
code) inevitably drift: the UI says ready while the run fails, or worse the
inverse. Drift is the anti-pattern this whole feature exists to kill.

Corollaries the skill acts on:

1. **Consolidation always flows activity → handler**, never the other way.
   The handler is reachable by both surfaces; an activity is reachable by one.
2. **Anything that behaves like preflight IS preflight.** If code verifies
   source readiness before extraction — regardless of what it is named or
   where it lives — its natural home is the handler, where the UI benefits
   from it too. Phase 0 hunts for these by behavior, not by name.
3. **Deletion requires a coverage diff.** Consolidating or deleting duplicate
   readiness logic must never lose a check: enumerate what the old code
   verified, prove each item exists in the handler, fold gaps in first.

Frame it to the developer as what they gain: write a check once, and the
Test-Connection button, every scheduled run, the Temporal failure pane, and
the failure dashboards all get it — with a typed, actionable error — for free.

## Phase 0 — Classify the app

Run these detections and report the bucket(s) before changing anything:

1. **Collision class** — a `@task` whose activity name resolves to
   `{app_name}:preflight` (a task method literally named `preflight`, or
   explicit `name="preflight"`). The worker will REFUSE TO BOOT on the bumped
   SDK (`WorkerActivityNameCollisionError`). Known fleet members: atlan-presto-app,
   clickhouse, power-bi-app, qlik-sense-cloud-app, redshift-app, teradata-app,
   trino.
2. **Coexistence class (named)** — other `*preflight*`-named `@task`s (e.g.
   `miner_preflight_check`). Non-breaking: they keep running alongside the
   gate. Each is a consolidation candidate for phase 2.
3. **Hidden preflight (semantic hunt)** — readiness logic that is preflight in
   behavior but not in name. Scan `@task` bodies and entrypoint code that runs
   BEFORE the extraction fan-out for:
   - connect-and-probe patterns (`SELECT 1`, ping, token validation, list-one
     API call) whose result only gates whether to continue;
   - tasks named `test_*`, `validate_*`, `check_*`, `verify_*`, `*_probe`;
   - early-exit guards in `run()` that abort before any data is extracted.
   Classify each hit with the developer in mind:
   - **True preflight** — read-only source-readiness verification, no
     side effects the extraction depends on → consolidation candidate:
     it belongs in the handler so the UI check runs it too.
   - **Execution guard owned by the SDK** (e.g. the sql template's
     `prime_sql_auth` warm-up) → leave alone; it is infrastructure, not app
     preflight, and the SDK maintains it.
   - **Business logic wearing a check's clothes** (produces state extraction
     consumes, seeds caches the run needs) → leave in the workflow; moving it
     to the handler would make the UI path perform work.
   When unsure which of the three, ask the developer — that is a phase-2
   question, not a guess.
4. **Gate eligibility** — the entrypoint input contract must carry the
   credential-routing triple (`extraction_method`, `credential_guid`,
   `agent_json`), normally by extending the toolkit `ExtractionInput`
   (check `app/generated/*_input.py` or `contract/`). Missing triple on a
   source-ful app = the gate silently skips; fix the contract.
5. **Handler presence** — no `Handler.preflight_check` at all → the gate runs
   the SDK DefaultHandler no-op (never blocks). Valid state; offer to write a
   handler in phase 2 but do not require it.
6. **Silent-drift audit** — list every `input.metadata` / `input.connection_config`
   key read inside `preflight_check`, and cross-check each against the input
   contract's fields. On the gate path, metadata is rebuilt from the extraction
   input's `model_dump`; a UI-form-only key is **absent** — a hard `[...]` read
   crashes (fail-open), a defensive `.get(..., default)` silently runs the
   check with wrong config. Every unmatched key needs a decision in phase 2.
7. **Multi-credential class** — an app that needs more than one credential to
   verify a source (e.g. an API token AND an object-store credential), whose
   per-auth-type guids live in separate input fields, not on the single
   top-level `credential_guid` triple. The gate resolves only that one triple,
   so the symptom on the gate path is the handler receiving `credentials=[]`,
   defaulting to one auth type, and raising missing-credential on every gated
   run (100% fail-open). Detect by: multiple `*_credential_guid` fields on the
   input contract, or a handler that resolves guids itself (`CredentialResolver`
   / `get_credentials` called inside `preflight_check`). If found, the app
   adopts the SDK `preflight_credential_refs` primitive in phase 2 (see 2g) — it
   must NOT hand-roll credential resolution or the fail-open taxonomy.
8. **Multi-entrypoint class (crawler + miner, etc.)** — more than one
   `@entrypoint` method on the App class (grep `@entrypoint`). The gate is
   injected per workflow type and `PreflightInput.entrypoint` is baked from the
   entry-point's registered name — so a miner run reliably arrives with
   `entrypoint="miner"`, a crawler run with `entrypoint="crawler"`. The single
   `preflight_check` must branch on `input.entrypoint` and run only that
   entrypoint's checks; each entrypoint gets its own tree in phase 2 (see 2h).
   Two traps: (a) a `getattr(input, "entrypoint", "crawler")`-style default is
   dead code — the field always exists, defaulting to `""`, which is falsy
   against `== "miner"`, so a mis-set default silently routes miner runs down
   the crawler branch; (b) entrypoints usually differ in how credentials arrive
   (next bucket).
9. **Late-credential-derivation class** — the input carries the credential
   triple, but `credential_guid` / `credential_ref` is populated *inside the
   entrypoint body* rather than at input construction. Classic case: a miner
   that reuses the crawler's connection, so the guid arrives only on
   `connection.attributes.defaultCredentialGuid` and the body derives it
   (`input.model_copy(update={"credential_guid": ...})`). The gate runs before
   any body code, so it resolves an **empty triple** → `credentials=[]` → the
   handler's connect step fails → soft `would_block`, while extraction (which
   derives the guid late) still succeeds. Tell-tale triad: UI Test-Connection
   green, miner gate soft-fails, extraction runs anyway. Detect by grepping
   entrypoint bodies for credential assignment or `defaultCredentialGuid` reads
   before the first extraction activity. Fix in phase 2 by lifting the
   derivation to input construction (see 2h). SDK-side fix tracked in CNCT-92.
10. **Non-routable `extraction_method` class** — the credential router
    (`CredentialRef.resolve`) reads `extraction_method` as the routing
    *selector* and accepts only `direct` (+ `credential_guid`) or `agent`
    (+ populated `agent_json`); anything else raises `CredentialRoutingError`.
    Query-extraction / miner-family manifests commonly set `extraction-method`
    to an extraction *kind* instead — e.g. `query_history` — which names what is
    extracted, not how creds are routed. Left as-is, the gate's `resolve()`
    raises on every run and survives **only** via the deprecated
    `legacy_credential_ref` fallback (removed in SDK v4.0) — so it looks like it
    works (a caught `CredentialRoutingError` stacktrace on every run) until v4.0
    removes the fallback or the app flips to hard mode, at which point the gate
    resolves `credentials=[]` and soft-skips or hard-aborts. Detect by grepping
    the manifest (`app/generated/**/*.json`) and the input model for
    `extraction-method` / `extraction_method` values that are not `direct` /
    `agent`. Fix in phase 2 by normalizing it at input construction (see 2h).
    Same SDK ticket: CNCT-92.

## Phase 1 — Bump and boot

1. Bump `atlan-application-sdk` to the current gate-capable floor —
   **`>=3.24.0`** — in `pyproject.toml`, then
   `uv lock --upgrade-package atlan-application-sdk` and
   `uv sync --all-extras --all-groups`. 3.24.0 is the baseline that carries the
   gate, the multi-credential primitive (`preflight_credential_refs` /
   `credentials_by_name`), and the per-entrypoint credential handling this skill
   relies on. Raise the declared floor if it sits below `>=3.24.0`. (Earlier
   revisions of this skill said "don't raise the floor" — that is retired; pin
   `>=3.24.0` from now.) After the bump, confirm the surfaces landed:
   `PreflightInput.credentials_by_name` / `preflight_credential_refs` must be
   importable, or the upgrade didn't take.
2. **Boot the worker.** Boot is the collision detector.
3. Collision fix (if bucket 1): delete the app's `@task preflight`, its call
   site in the workflow, and any now-orphaned private preflight input/output
   contracts. THEN — mandatory — **diff the deleted activity's checks against
   `Handler.preflight_check`**: every check the activity performed must exist
   in the handler (the gate runs the handler, so coverage moves — it must not
   vanish). Fold gaps into the handler before proceeding.
4. Run the app's existing test suite; fix fallout from the bump before
   touching preflight logic, so phase-2 diffs stay clean.

## Phase 2 — Interactive check-design session

This is the heart of the skill. Do not silently rewrite the handler — the
blocking/advisory structure is the developer's judgment; you propose and they
decide.

### 2a. Render the current state as a tree

Parse the existing `preflight_check` and draw what it does today, in
execution order, as a decision tree the developer can react to:

```
current structure (as-implemented):

[auth probe] ──fail──> return NOT_READY            (blocks: yes)
     │ pass
     ▼
[tables check] ──fail──> status = NOT_READY (!)    (blocks: yes — is this intended?)
     │ pass
     ▼
all pass ──> READY
```

Annotate every place where the *implemented* behavior may not match intent —
the classic bug is `status = READY if all(c.passed) else NOT_READY`, which
promotes every advisory check to run-blocking.

Include the phase-0 consolidation candidates in the same picture, marked as
living outside the handler, so the developer sees the whole readiness surface
at once:

```
outside the handler today (phase-0 findings):

[crawler_preflight @task]     duplicates auth+tables    → consolidate?
[validate_filters (in run())] read-only filter probe    → consolidate?
[warm_cache @task]            seeds extraction state    → stays (business logic)
```

### 2a-bis. The consolidation question, per candidate

For every named-coexistence activity and every "true preflight" semantic hit,
ask: *"This verifies <X> before extraction, but only runs in the workflow —
the UI Test-Connection never sees it. Move it into `preflight_check` so both
surfaces run it, and delete the activity?"* On yes: fold the logic into the
handler as a check in the tree below, then delete the activity **with the
coverage diff** (north-star corollary 3). On no: record why in the phase
report — it stays as accepted, documented duplication (phase-4 cleanup list).

### 2b. Ask the classification questions, per check

For each check, ask (AskUserQuestion where available, plain questions
otherwise), using these litmus tests:

- **"If this check fails, can extraction produce anything useful?"**
  No → required (short-circuit `NOT_READY`). Yes → advisory (`PARTIAL`).
- **"Does every later check depend on this one?"** Yes → it must run first and
  short-circuit (no point checking permissions when auth failed).
- **"Who fixes a failure of this check?"** Customer (credentials, network,
  grants) → the error's default USER audience is right. The app team or Atlan
  (internal state, unexpected config) → the error must use an APP_OWNER /
  PLATFORM-audience leaf, otherwise the blocked run is attributed to the
  customer's configuration in the SLA split.

### 2c. Propose the target tree and the canonical ordering

Present the proposed structure the same visual way, and let the developer
edit it before you write code. Canonical ordering (from the design review):
**reachability → authentication → authorization → advisory probes** — each
tier short-circuits the tiers after it.

```
proposed:

[connectivity]  ──fail──> NOT_READY  (error=SourceUnavailableError, stop)
     │ pass
[auth]          ──fail──> NOT_READY  (error=AuthError, stop)
     │ pass
[read access]   ──fail──> NOT_READY  (error=AppPermissionDeniedError, stop)
     │ pass
     ├─[server version]   ──fail──> mark failed, continue   (advisory)
     └─[optional feature] ──fail──> mark failed, continue   (advisory)

status: NOT_READY only via a short-circuit above;
        PARTIAL if any advisory check failed; READY otherwise.
```

### 2d. Suggest missing checks

Compare against the family baseline and propose (never force) additions:

- **SQL sources**: reachability/DNS of the host; authentication (connect +
  trivial query); read authorization (information_schema / catalog probe
  scoped by the configured filters); server version (advisory); optional
  extensions the miner needs (advisory — e.g. pg_stat_statements).
- **API/BI sources**: token validity; required scopes/permissions; access to
  the configured workspace/project/site; API version or tenant feature flags
  (advisory).
- **Any source**: if the check uses filter/config metadata, it must come from
  contract fields (phase-0 silent-drift audit) — suggest adding missing fields to
  `contract/app.pkl` and regenerating, rather than reading form-only keys.

### 2e. Implement

Write the handler to the agreed tree. Requirements:

- Checks append to a list as they run; short-circuits return the list built so
  far (`PreflightOutput(status=NOT_READY, checks=checks)`).
- Status logic is explicit — never `all(c.passed)` when any check is advisory.
- Time each check and set `duration_ms`; the UI shows it.
- `error=` only on failed checks, statically-clean form:
  `error=AuthError(message="...", suggested_action="...", cause=exc).to_failure_details()`
- `message` = one clean sentence, no hosts/credentials/stack traces (the
  `cause=` chain carries a redacted, capped repr for diagnostics).
- `suggested_action` only when there is a concrete user-fixable step.

Leaf selection quick table (`application_sdk.errors`):

| Failure | Leaf | Audience default |
|---|---|---|
| bad credentials / login rejected | `AuthError` | USER |
| host unreachable / refused / DNS | `SourceUnavailableError` | USER |
| missing grants / scopes | `AppPermissionDeniedError` | USER |
| required state/extension/version absent | `PreconditionError` | USER |
| app-internal inconsistency | `InternalError` (or subclass) | APP_OWNER |

### 2f. App-specific error classes (when the leaves aren't enough)

Three tiers, lightest first — pick the lightest that fits:

1. **Bare leaf** — `AuthError(message="...")`. Semantics (category/code/
   audience/retryable) come from the leaf. Fine for standard failures.
2. **Per-instance overrides** — `AuthError(message="...", suggested_action="...",
   retryable=True, cause=exc)`. `message`, `suggested_action`, `retryable`,
   `cause` are instance fields on every leaf. Right for one-off checks; no
   class needed.
3. **App subclass** — when the same message/action is reused across checks,
   when the app wants its own `code` for aggregation (dashboards and the
   Automation Engine group by code), or when the failure carries app-specific
   evidence. What you CANNOT override per-instance: `category`, `code`,
   `audience` — those are ClassVars (fixed aggregation keys), and changing
   them is exactly what a subclass is for.

Subclasses live in **`app/failures.py`** (same home the `/typed-failures`
skill establishes — reuse the file if it exists). Pattern:

```python
from dataclasses import dataclass
from typing import ClassVar
from application_sdk.errors import AuthError


@dataclass(kw_only=True)
class MySQLAccountLockedError(AuthError):
    """MySQL rejected login due to FAILED_LOGIN_ATTEMPTS lockout."""

    # reusable defaults — override AppError's instance fields
    message: str = "MySQL account is locked out after repeated failed logins."
    suggested_action: str | None = (
        "Wait for auto-unlock or have a DBA run ALTER USER ... ACCOUNT UNLOCK, "
        "then re-run preflight."
    )
    # distinct code for aggregation; category/audience inherit from AuthError
    code: ClassVar[str] = "MYSQL_ACCOUNT_LOCKED"

    # any extra dataclass field lands in FailureDetails.evidence automatically
    lockout_seconds_remaining: int | None = None
```

Usage in a check is identical to a leaf:

```python
error=MySQLAccountLockedError(cause=exc, lockout_seconds_remaining=300).to_failure_details()
```

What the owner gets for free: category `AUTH` and audience `USER` inherited
(no re-declaring), the distinct code shows up in the gate outcome event's
`reason` and in AE/top-error-code dashboards, `lockout_seconds_remaining`
rides in `evidence`, and the `cause` chain is redacted and capped by the SDK.
Two guardrails: evidence field names must not look secret-bearing
(`*_secret` / `*_password` / `*_token` are rejected at the wire boundary), and
if the failure is app-internal rather than customer-fixable, base the subclass
on an APP_OWNER-audience leaf (`InternalError`) — not on `AuthError` — or the
SLA split misattributes it (see the audience litmus question in 2b).

During the interactive session: when the developer picks the same leaf with
the same custom message/action for two or more checks, proactively offer to
extract a subclass into `app/failures.py`.

### 2g. Multi-credential apps — declare named refs, don't hand-roll

Only if phase 0 flagged the multi-credential class. The gate resolves exactly
one credential off the top-level triple, so an app with per-auth-type guids
must tell the gate which guids to resolve — **declaratively**. Do not
re-implement resolution or the outage-vs-not-found taxonomy in the handler:
that boilerplate is dangerous (a store outage misread as a bad credential
blocks healthy runs), and the SDK now owns the one correct implementation.

Declare a `ClassVar` map of ref-name → the input field holding that guid, on
the extraction input:

```python
class MyAppExtractionInput(ExtractionInput):
    preflight_credential_refs: ClassVar[dict[str, str]] = {
        "api": "api_credential_guid",
        "object_store": "object_store_credential_guid",
    }
```

The gate resolves each guid inside the activity under one fail-open taxonomy
(confirmed outage → the workflow fails open, never blocks; genuine not-found →
an empty group so the handler decides `NOT_READY`) and hands the handler the
results grouped by name:

```python
async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
    api = input.credentials_by_name["api"]
    obj = input.credentials_by_name.get("object_store", [])
    ...
```

Guardrails:

- It MUST be a `ClassVar`, not a pydantic field. Declared as a field, the gate
  reads `{}` and silently falls back to the single-triple path (the SDK logs a
  warning if you get this wrong, but it fires per gated run while building the
  gate input — not once at boot — so declare it correctly up front rather than
  relying on spotting the warning).
- The named guids must be top-level fields on the input contract (the gate reads
  them from the extraction-input snapshot). If they arrive nested under AE
  `metadata`, lift them onto the contract — the same fix as the phase-0
  silent-drift audit.
- Delete the app's hand-rolled credential resolution + taxonomy once the
  primitive is in; that consolidation is the point (one correct implementation
  in the SDK). Coverage diff still applies (north-star corollary 3).
- Single-credential apps declare nothing and keep the top-level triple path —
  zero change.

This primitive ships in the SDK release the phase-1 lock upgrade pulls; if
`PreflightInput.credentials_by_name` / `preflight_credential_refs` aren't present
on the installed SDK, the upgrade didn't land — re-run phase 1 before adopting.

### 2h. Multi-entrypoint apps — one handler, per-entrypoint trees + per-entrypoint creds

Only if phase 0 flagged the multi-entrypoint class (bucket 8). One
`preflight_check`, branching on `input.entrypoint`; design each entrypoint's
tree separately in 2c (a crawler validates schemas/tables; a miner validates
its query-history prerequisites, e.g. `pg_stat_statements`). Keep the branches
disjoint — never run the crawler's tables probe on a miner run, or vice versa.

The load-bearing gate-path issue is **credential sourcing per entrypoint**
(bucket 9). If an entrypoint's guid arrives only on the reused connection, lift
it onto the top-level `credential_guid` at **input construction** so the gate —
which runs before the body — resolves it:

```python
class MyMinerInput(QueryExtractionInput):
    @model_validator(mode="after")
    def _lift_credential_guid_from_connection(self):
        # gate reads the top-level triple before any body code runs, so the
        # connection's guid must be lifted here, not in run()
        if not self.credential_guid and self.connection is not None:
            derived = (
                getattr(self.connection.attributes, "default_credential_guid", "")
                or getattr(self.connection.attributes, "defaultCredentialGuid", "")
            )
            if derived:
                self.credential_guid = derived
        return self
```

Precedence — get this exactly, especially for SDR apps:

1. an explicit top-level `credential_guid` / `credential_ref` always wins —
   never overwrite it;
2. `agent_json` (SDR) wins over any guid sitting on the connection;
3. fall back to `connection.attributes.defaultCredentialGuid` only when the
   whole triple is empty.

Then delete the old body-side derivation (coverage diff, corollary 3) so there
is one source of truth. Temporal deserializes the workflow input through
pydantic before dispatching the gate, so a `mode="after"` validator has already
run — verify it (don't assume) by feeding a connection-only input through
`PreflightGateInput.from_extraction_input(input, "<entrypoint>")` and asserting
`.credential_guid` is populated.

This is an app-side stopgap. The connection-carries-its-default-guid pattern is
platform-wide, so the durable fix is the gate resolving it directly (CNCT-92);
when that lands, delete the validator.

**Also normalize `extraction_method` if it isn't router-routable** (phase-0
bucket 10). Lifting the guid is not enough if the router can't route it: the
gate calls `CredentialRef.resolve`, which only accepts `direct` / `agent`. A
manifest that sets `extraction-method` to an extraction *kind* (e.g.
`query_history`) makes `resolve()` raise, and it limps along only on the
deprecated `legacy_credential_ref` fallback. Map it to the routing selector at
construction, mirroring `resolve()`'s own precedence (agent creds win over a
guid):

```python
@model_validator(mode="after")
def _route_extraction_method_for_gate(self):
    if self.extraction_method not in ("direct", "agent"):
        agent = self.agent_json
        if agent is not None and agent.is_populated():
            self.extraction_method = "agent"
        elif self.credential_guid:
            self.extraction_method = "direct"
    return self
```

Leave it untouched when there's nothing to route (no guid, no agent) — let the
gate decide on an empty credential rather than inventing a route. When it bites
if skipped: works today via the caught fallback, but breaks at SDK v4.0 (fallback
removed) or the hard-mode flip (every run aborts). Nothing reads the extraction
*kind* value for behaviour, so remapping is safe — verify with a grep. Same
durable SDK fix: `resolve()` should route "non-agent + guid → GUID" itself
(CNCT-92), after which this validator is deleted too.

## Phase 3 — Tests (mandatory, thorough)

Update or write unit tests so every verdict path is pinned. Minimum matrix:

1. All checks pass → `READY`, full check list, durations set.
2. Each required check failing → `NOT_READY`, short-circuit proven (later
   checks absent from the list), `error` present with the agreed
   category/audience and `suggested_action`.
3. Advisory-only failure → `PARTIAL`, run-proceeding semantics documented in
   the test name, failed advisory check present with `passed=False`.
4. No `blocking=`/`category=`/`suggested_action=` kwargs anywhere (grep-clean
   — those fields no longer exist on `PreflightCheck`).
5. Gate-path input shape: `preflight_check` called with a `PreflightInput`
   built only from contract fields + credentials (no form-only keys) behaves
   correctly — this is the regression test for the phase-0 silent-drift audit.

Then the full app suite green, pre-commit clean, and one final worker boot.
Do not claim done with anything less.

For a multi-entrypoint or late-credential-derivation app, add a gate-path check
the unit suite can't give you: start the real workflow (`uv run main.py`,
provision a guid via `/dev/local-vault`, then `/start?entrypoint=<ep>` with the
guid **only** on `connection.attributes.defaultCredentialGuid`) and read the
gate verdict from the "Preflight gate outcome" event. The console drops the
event's structured extras, so read the local observability logs
(`local/dapr/objectstore/artifacts/apps/observability/non-sdr/logs/**.json.gz`,
gzipped JSONL) and confirm `outcome="proceeded"` with the entrypoint's checks in
`check_matrix`. `no_verdict`/`ActivityError` means the gate activity fail-opened
(e.g. flaky embedded Dapr locally), not a real verdict — re-run.

## Pitfalls (each observed live during the rollout — check all of them)

- `all(c.passed)` status logic → advisory checks silently promoted to
  run-blocking.
- Raise-to-block → does nothing but fail open; blocks are returned, not raised.
- `error` on a passed check → ignored by the gate; remove it.
- Form-only metadata keys → silently absent on the gate path; defensive
  `.get` hides it (checks pass with wrong config), hard access crashes to
  fail-open. Fix the contract, not the symptom.
- Deleting a colliding activity without the coverage diff → checks vanish.
- Wrong audience on an internal failure → customer's SLA split absorbs an app
  bug.
- Skipping the boot check before pushing a bump → collision discovered as a
  prod crash-loop instead of locally.
- Multi-credential app hand-rolling credential resolution + fail-open taxonomy
  in the handler → dangerous duplication (a store outage misread as a bad
  credential blocks healthy runs). Use `preflight_credential_refs` +
  `credentials_by_name` (2g); declare it as a `ClassVar` — a pydantic field
  silently no-ops back to the single-triple path.
- Credential derived from the connection in the entrypoint body → the gate sees
  an empty triple and soft-fails `would_block` while extraction succeeds. Lift
  the guid at input construction, not in `run()` (phase-0 bucket 9, 2h).
- Trusting a green UI Test-Connection as gate readiness → the UI gets creds
  inline; the gate resolves them from the triple, so they can disagree. Read the
  "Preflight gate outcome" event (`outcome`/`reason`/`check_matrix`), not the
  UI, to know what the gate actually did.
- Mis-set `entrypoint` default in `preflight_check` → multi-entrypoint runs
  routed down the wrong branch (a miner silently running the crawler's checks).
- `extraction_method` names an extraction *kind* (e.g. `query_history`), not a
  routing selector → `CredentialRef.resolve` raises and the gate survives only
  on the deprecated `legacy_credential_ref` fallback (removed in v4.0) / hard-
  aborts under hard mode. Reads green today because the error is caught — a
  false all-clear. Normalize to `direct`/`agent` at input construction (phase-0
  bucket 10, 2h).

## Agent protocol — stop points and what to report at each

The skill is a conversation with checkpoints, not a batch job. Three hard
stops:

1. **After phase 0** — report the bucket(s), the consolidation candidates
   (with your three-way classification and reasoning), the eligibility
   verdict, and the silent-drift key list. No edits yet. The developer may
   already know some candidates are intentional; let them say so here.
2. **After 2c/2a-bis** — the agreed target tree and consolidation decisions,
   restated as the plan of record ("these checks, this order, these block,
   these are advisory, these activities fold in, these stay"). Get an explicit
   yes before writing code. This restatement goes verbatim into the PR
   description later.
3. **After phase 3** — the full verify evidence: test matrix results, suite +
   pre-commit output, worker boot confirmation. Then hand off for PR review.

Between stops, work autonomously. If anything contradicts these instructions
(an SDK surface that moved, a pattern that doesn't fit the buckets), STOP and
report rather than improvising — the buckets came from a fleet audit, but the
fleet has ~80 apps and this skill has met seven of them.

## Done means

Bucket reported → bumped and booting → check tree agreed with the developer
and implemented → typed errors on failed checks → test matrix green → suite +
pre-commit green. Summarize the final tree in the PR description so reviewers
see the blocking structure at a glance.
