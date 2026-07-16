---
name: adopt-preflight-gate
description: >
  Bump a v3 app to the latest application-sdk and adopt the SDK-native
  preflight gate in its soft-fail observation window (CNCT-81). The gate runs
  the app's preflight_check handler as the mandatory first activity of every
  extraction workflow and aborts the run when the verdict is NOT_READY. During
  the observation window the app never returns overall NOT_READY: blocking
  intent is recorded per-check via PreflightCheck.status while the aggregate
  is softened to PARTIAL so runs proceed and the check matrix is collected in
  connector-pulse; the hard-fail flip later is a one-line aggregate revert.
  Classifies the app's rollout bucket, fixes name collisions, audits the
  handler's status logic against the new semantics, runs an interactive
  check-design session with the developer (visualized as a decision tree:
  which checks block, which are advisory, what is missing), hunts for hidden
  preflight logic by behavior (not name) and consolidates it into the handler
  so both surfaces run one implementation, adopts typed check errors, and
  updates tests. Interactive: every blocking/advisory/consolidation decision
  belongs to the developer; the skill proposes, never imposes.
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
last_updated: "2026-07-17"
staleness_days: 90
inputs:
  - app_root: "auto-detected — the directory containing app/ and pyproject.toml"
outputs:
  - uv.lock (SDK upgraded to a gate-capable release; pyproject floor left unchanged unless it excluded that release)
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

Reference implementation for everything below: **atlan-mysql-app** — PR #340
(gate adoption: short-circuiting auth check with typed error, advisory tables
check) and PR #414 (soft-fail pattern: per-check `status` stamps, softened
aggregate with the revert comment). Read its `app/handler.py` before proposing
changes.

## What changed (context you state to the developer up front)

- The SDK injects `{app}:preflight` as the first activity of every extraction
  workflow. It calls the app's one `Handler.preflight_check`.
- `PreflightOutput.status` is the gate decision: `NOT_READY` **aborts the run**
  (typed `PreflightFailed`, red activity); `READY` and `PARTIAL` proceed.
- **Per-check verdict is a contract field**: `PreflightCheck.status` reuses the
  same enum per check — `NOT_READY` = failed and was supposed to block,
  `PARTIAL` = failed, advisory by design, `READY` = passed (derived
  automatically for passed checks). Mandatory on failed checks: a failed check
  without a stamp is emitted as `"unset"` in the gate's check matrix and logs a
  warning — the app shows up as an adoption gap in connector-pulse.
  Contradictions (`READY` on a failed check, `NOT_READY`/`PARTIAL` on a passed
  one) raise at construction.
- **The soft-fail observation window (CNCT-81)**: during the window the
  handler NEVER returns overall `NOT_READY`. Would-block paths keep their
  short-circuit control flow but return overall `PARTIAL`, with the check
  stamped `status=NOT_READY` so blocking intent is preserved in the data. The
  gate emits the per-check matrix (`check_matrix` on the "Preflight gate
  outcome" event) to connector-pulse, which correlates it with workflow
  outcomes to harden checks. The hard-fail flip later is per-app: revert the
  aggregate lines back to `NOT_READY`; per-check statuses never change.
- Ordering is still expressed by **control flow**: required checks
  short-circuit (return early), advisory checks run after and only influence
  the aggregate. The `status` field records intent; the short-circuit records
  dependency.
- **Raising does not block.** A raise from the handler is a gate plumbing
  failure → the SDK fails open (logs loudly, run proceeds). Blocks happen only
  via the returned status.
- A failed check should carry `error=<SDK leaf>(...).to_failure_details()` —
  category/code/audience/suggested_action flow to the Automation Engine,
  dashboards, and the check matrix's `error_code`. Untyped failures fall back
  to the `PREFLIGHT_CHECK_FAILED` sentinel.

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

## Phase 1 — Bump and boot

1. Make the `pyproject.toml` constraint span the release that ships
   `PreflightCheck.status`: raise the floor to that release if the current one
   excludes it, and keep the upper bound at `<4` (the feature ships within the
   current major). Then `uv lock --upgrade-package atlan-application-sdk` and
   `uv sync --all-extras --all-groups`.
2. **Verify the installed SDK actually has the field before touching handler
   code**:
   `uv run python -c "from application_sdk.handler.contracts import PreflightCheck; print('status' in PreflightCheck.model_fields)"`
   must print `True`. Pydantic silently drops unknown kwargs — handler code
   written with `status=` stamps against an older SDK does not error, it just
   emits `"unset"` for every check and quietly guts the observation-window
   data. This check is the difference between a working adoption and an
   invisible no-op.
3. **Boot the worker.** Boot is the collision detector.
4. Collision fix (if bucket 1): delete the app's `@task preflight`, its call
   site in the workflow, and any now-orphaned private preflight input/output
   contracts. THEN — mandatory — **diff the deleted activity's checks against
   `Handler.preflight_check`**: every check the activity performed must exist
   in the handler (the gate runs the handler, so coverage moves — it must not
   vanish). Fold gaps into the handler before proceeding.
5. Run the app's existing test suite; fix fallout from the bump before
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

[connectivity]  ──fail──> check status=not_ready, stop  (error=SourceUnavailableError)
     │ pass
[auth]          ──fail──> check status=not_ready, stop  (error=AuthError)
     │ pass
[read access]   ──fail──> check status=not_ready, stop  (error=AppPermissionDeniedError)
     │ pass
     ├─[server version]   ──fail──> check status=partial, continue   (advisory)
     └─[optional feature] ──fail──> check status=partial, continue   (advisory)

overall status, observation window (CNCT-81):
        PARTIAL on any failure path — including the short-circuits above;
        READY only when everything passed. Never NOT_READY.
overall status, after the hard-fail flip (one-line revert per short-circuit):
        NOT_READY via the short-circuits; PARTIAL if only advisory failed;
        READY otherwise. Per-check statuses identical in both eras.
```

The blocking/advisory decision the developer makes here lands in TWO places:
the short-circuit position (ordering/dependency) and the per-check
`status=` stamp (recorded intent). The stamp is what connector-pulse's
pattern analysis reads when correlating "would have blocked" against actual
workflow outcomes — it is the data that earns the check its blocking status
back at flip time.

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

- **One implementation, no forks.** The softened aggregate applies inside the
  single `preflight_check` — never a gate-only or UI-only variant. Handler
  (Test-Connection), gate activity, and SDR all execute the same function and
  must return the same verdict for the same source state. Any "if gate then X
  else Y" branching is the drift anti-pattern this feature exists to kill.
- Checks append to a list as they run; short-circuits return the list built so
  far. Every check carries its `status=` stamp; every would-block path returns
  the softened aggregate with the canonical revert comment:

  ```python
  checks.append(PreflightCheck(
      name="auth", passed=False,
      status=PreflightStatus.NOT_READY,   # intent: this was fatal
      error=AuthError(...).to_failure_details(),
  ))
  # Observation window (CNCT-81): revert to NOT_READY at hard-fail flip;
  # per-check statuses stay as they are.
  return PreflightOutput(status=PreflightStatus.PARTIAL, checks=checks)
  ```

  The comment is the flip's grep target — keep the wording, one comment per
  softened return site.
- Advisory failures stamp `status=PreflightStatus.PARTIAL`; passed checks need
  no stamp (the contract derives `READY`).
- Window invariant: the aggregate is never `NOT_READY` — regardless of how
  many checks fail.
- Status logic is explicit — never `all(c.passed)` when any check is advisory.
- Time each check and set `duration_ms`; the UI shows it and pulse charts
  check-cost distributions from it.
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

## Phase 3 — Tests (mandatory, thorough)

Update or write unit tests so every verdict path is pinned. Minimum matrix:

1. All checks pass → `READY`, full check list, every check `status == READY`,
   durations set.
2. Each required check failing → overall `PARTIAL` (observation window), the
   failed check `status == NOT_READY`, short-circuit proven (later checks
   absent from the list), `error` present with the agreed category/audience
   and `suggested_action`.
3. Advisory-only failure → `PARTIAL`, run-proceeding semantics documented in
   the test name, failed advisory check present with `passed=False` and
   `status == PARTIAL`.
4. **Window invariant** — the worst case (every check failing) still returns
   overall `status != NOT_READY`. This is also the guard against a premature
   de-pin: handler code stamping `status=` against a pre-feature SDK fails
   these assertions instead of silently emitting `"unset"`.
5. No `blocking=`/`category=`/`suggested_action=` kwargs anywhere (grep-clean
   — those fields no longer exist on `PreflightCheck`).
6. Gate-path input shape: `preflight_check` called with a `PreflightInput`
   built only from contract fields + credentials (no form-only keys) behaves
   correctly — this is the regression test for the phase-0 silent-drift audit.

At the hard-fail flip, only case 2 and 4's expected aggregates change
(`PARTIAL` → `NOT_READY`; invariant test deleted) — per-check assertions stay
untouched. Write the tests so that revert is exactly that small.

Then the full app suite green, pre-commit clean, and one final worker boot.
Do not claim done with anything less.

## Pitfalls (each observed live during the rollout — check all of them)

- Returning overall `NOT_READY` during the observation window → blocks
  scheduled customer runs and defeats the data-collection window. The window
  invariant test exists to make this impossible to ship.
- Failed check without a `status=` stamp → the gate warns and emits `"unset"`
  in the check matrix; the app shows up as an adoption gap in pulse and its
  rows are useless for the hardening analysis.
- Handler code with `status=` stamps against a pre-feature SDK → pydantic
  drops the kwarg silently; nothing errors, everything emits `"unset"`. The
  phase-1 field check and the test-matrix status assertions are the two
  guards.
- Forking gate behavior from UI behavior (a gate-only softened aggregate, a
  UI-only verdict tweak) → the drift anti-pattern in new clothes. One
  function, one verdict.
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

Bucket reported → bumped and booting (installed SDK carries
`PreflightCheck.status`) → check tree agreed with the developer and
implemented → every check stamped, every softened return carrying the
CNCT-81 revert comment, window invariant holding → typed errors on failed
checks → test matrix green → suite + pre-commit green. Summarize the final
tree in the PR description — including which checks are stamped blocking vs
advisory — so reviewers see the intended hard-fail structure at a glance even
though the aggregate is soft.
