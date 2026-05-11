---
name: typed-failures
description: >
  Convert an app's untyped exceptions (ValueError, RuntimeError, bare Exception,
  ActivityError) to SDK-typed AppError subclasses so every classified failure
  carries category / code / audience / retryable on the wire. The Automation
  Engine attributes failures from those typed fields without parsing exception
  strings. Interactive and incremental: surveys the app first, asks the
  developer to confirm categories at ambiguous sites, applies edits one batch
  at a time, then reports remaining blind-spot (silent-swallow) sites as a
  phase-2 punch list for owner approval.
mandatory_triggers:
  - "/typed-failures"
  - "add typed failures to this app"
  - "onboard typed errors"
  - "wire app to failure attribution"
optional_triggers:
  - "convert exceptions to AppError"
  - "add typed exceptions"
  - "make my errors typed"
owner: connector-platform-team
last_updated: "2026-05-11"
staleness_days: 90
inputs:
  - app_root: "auto-detected — the directory containing app/ and pyproject.toml"
outputs:
  - app/failures.py (typed classes + optional classifier)
  - edited raise sites across app/ (ValueError/RuntimeError → typed classes)
  - tests/unit/test_failures.py (wire-shape tests + classifier dispatch tests)
  - pyproject.toml (SDK pin bump to >= v3.7.0 if needed)
  - blind-spot report (printed, not committed — phase-2 punch list)
gates:
  - "All 14 SDK-provided leaves come from application_sdk.errors. Apps may not invent new categories — only subclass leaves."
  - "Codes follow <CATEGORY>_<SPECIFIC>, connector-agnostic. No vendor name in code."
  - "No logic / control-flow changes unless explicitly approved. Silent-swallow paths report as phase-2 unless owner approves promotion."
---

# typed-failures

Onboard an app onto the SDK's typed `AppError` hierarchy so its failures surface as
structured events through the Automation Engine. The end state: every existing
`raise` site in the app raises a typed `AppError` subclass; bare exception handlers
that re-raise classify-or-passthrough; the wire payload (`ApplicationError.details`)
carries `FailureDetails(category, code, audience, retryable, evidence)`; the AE
parser surfaces those onto `DAGNode.error_details` and the emit pipeline lands them
in `otel_logs.service_logs` as queryable typed attributes.

The canonical reference example is `atlan-athena-app` PR #57 — read it before
running this skill on a new app to see the end-state shape and the conventions
in practice.

---

## Mission

For the app being onboarded:

1. **Discover** every existing exception raise / except / silent-swallow site.
2. **Convert** existing raises to typed `AppError` subclasses with the right
   (category, code, audience, retryable) — interactive Q&A only at ambiguous
   sites, auto-classify obvious ones.
3. **Report** the blind spots — silent-swallow paths and bare-passthrough sites
   that would benefit from typing but require logic changes the owner must
   approve.

**Zero logic / control-flow changes by default.** Only swap classes at existing
raise sites. Promoting a silent-swallow to a raise is a logic change requiring
owner sign-off — surface it in the phase-2 report instead.

---

## When to use this skill

- Onboarding a new app onto failure attribution for the first time.
- Auditing an app that's already partially typed (e.g. some clients are typed
  but the activity layer isn't).
- After an SDK version bump that adds new typed leaves (refresh the app's
  failures.py if the new leaves cover existing raise sites better).

**Not** the right tool when:
- The app has no Atlan SDK dependency (this only helps SDK-based apps).
- The owner has explicitly opted out of failure attribution for this app.
- The app is a one-off script with no Temporal workflows behind it.

---

## SDK contract — the 14 leaves you may subclass

All leaves are defined in `application_sdk.errors`. **You may not invent new
categories.** Apps subclass these and override `code` (per the convention below)
and optionally `audience` (when the locus differs from the SDK base default).

| Leaf class | Category | Default `audience` | Default `retryable` | Typical trigger |
|---|---|---|---|---|
| `CancelledError` | CANCELLED | APP_OWNER | False | Caller / workflow cancellation |
| `AppTimeoutError` | TIMEOUT | APP_OWNER | **True** | Bounded wait elapsed (network read, activity start-to-close) |
| `RateLimitedError` | RATE_LIMITED | USER | **True** | 429 from remote, per-key quota signal |
| `AuthError` | AUTH | USER | False | Credentials missing / expired / invalid |
| `AppPermissionDeniedError` | PERMISSION | USER | False | Authenticated but no permission for the action |
| `NotFoundError` | NOT_FOUND | USER | False | Entity doesn't exist |
| `AlreadyExistsError` | ALREADY_EXISTS | USER | False | Idempotent-create conflict |
| `InvalidInputError` | INVALID_INPUT | USER | False | Argument / payload malformed regardless of system state |
| `PreconditionError` | PRECONDITION | USER | False | Inputs valid but state forbids action (schema mismatch, version conflict) |
| `DependencyUnavailableError` | DEPENDENCY_UNAVAILABLE | PLATFORM | **True** | Required service down (Dapr, Temporal, object store, source DB) |
| `ResourceExhaustedError` | RESOURCE_EXHAUSTED | PLATFORM | **True** | Local resource limit (OOM, disk full, file handles) |
| `DataIntegrityError` | DATA_INTEGRITY | APP_OWNER | False | Returned data corrupt / violates invariants |
| `InternalError` | INTERNAL | APP_OWNER | False | App / SDK bug, invariant broken |
| `UnimplementedError` | UNIMPLEMENTED | APP_OWNER | False | Known capability gap |

**Litmus tests for ambiguous categories:**

- `DEPENDENCY_UNAVAILABLE` vs `PRECONDITION`: if retrying the same call without
  any state change is expected to succeed → DEPENDENCY_UNAVAILABLE. If explicit
  state must change first → PRECONDITION.
- `RATE_LIMITED` vs `RESOURCE_EXHAUSTED`: 429 from a remote endpoint →
  RATE_LIMITED. Local OOM / disk full / worker-slot exhaustion →
  RESOURCE_EXHAUSTED.
- `ALREADY_EXISTS` vs `PRECONDITION`: entity already exists on an
  idempotent-create path → ALREADY_EXISTS. Resource exists but in the wrong
  state for the operation → PRECONDITION.
- `UNIMPLEMENTED` vs `INTERNAL`: known capability gap (feature not built) →
  UNIMPLEMENTED. Unexpected invariant violation (bug) → INTERNAL.
- `INVALID_INPUT` vs `PRECONDITION`: payload itself is malformed (irrespective
  of system state) → INVALID_INPUT. Payload syntactically valid but state
  blocks → PRECONDITION.

---

## Code naming convention — `<CATEGORY>_<SPECIFIC>`

Codes are app-owned strings. The grammar is closed; the vocabulary is open
within the grammar.

**Rules:**

1. **Always start with the category name** (`AUTH_`, `PERMISSION_`,
   `DEPENDENCY_UNAVAILABLE_`, etc.) so the code is self-describing without
   joining against the category column.
2. **No vendor / connector name in the code.** Vendor identity rides in the AE
   DAG node `app_name` label (set by AE, single source of truth) and in
   `evidence`. Write `AUTH_INVALID_CREDENTIALS`, not `ATHENA_AUTH_INVALID_CREDENTIALS`.
3. **Reuse subtypes across connectors.** Every connector with bad credentials
   should emit `AUTH_INVALID_CREDENTIALS` — same code, different `app_name`.
   This is what lets dashboards group by `code` to surface cross-connector
   patterns.
4. **Stay generic.** `PERMISSION_ASSUME_ROLE_FAILED` (works for any cloud-IAM
   connector), not `PERMISSION_STS_ASSUME_ROLE_FAILED` (AWS-specific).
   `NOT_FOUND_COMPUTE_RESOURCE`, not `NOT_FOUND_WORKGROUP` (Athena-specific).

**Examples drawn from the Athena reference:**

| Code | Why this shape |
|---|---|
| `AUTH_INVALID_CREDENTIALS` | category prefix; "invalid credentials" is the cross-connector subtype |
| `PERMISSION_DENIED` | category prefix; no further subtype needed (this IS the common case) |
| `DEPENDENCY_UNAVAILABLE_NETWORK` | category prefix; "network" subtype distinguishes from other unavailability modes |
| `INVALID_INPUT_CONFIG` | category prefix; "config" distinguishes from data / query-shape input errors |
| `INTERNAL_QUERY_PREP_FAILED` | category prefix; "query_prep_failed" is specific enough to be a runbook entry |

---

## Property usage matrix

Every typed class produces a `FailureDetails` wire envelope via the SDK's
`AppError.to_failure_details()`. Here's what each property is for and whether
to set it.

| Property | Layer | Use it? | How |
|---|---|---|---|
| `category` | ClassVar | ✅ always | Inherited from each leaf's SDK base class |
| `code` | ClassVar | ✅ always | Override per leaf following `<CATEGORY>_<SPECIFIC>` |
| `audience` | ClassVar | ✅ override only when needed | Inherit from SDK base; override per leaf only when locus differs |
| `default_retryable` | ClassVar | ⚪ rarely override | Inherit from SDK base; override only for unusual retry semantics |
| `message` | instance | ✅ always | Set at every raise site with concrete human-readable text |
| `cause` | instance | ✅ where applicable | Set via `from exc` chain at classifier sites; not needed when there is no upstream exception |
| **evidence fields** (dataclass attrs you add) | instance | ✅ when useful | Add fields specific to the leaf (e.g. `field` on InvalidInput-style errors, `db_name`/`schema_name` on a query failure). These land in `FailureDetails.evidence` automatically. |
| `app_name` | instance | ❌ deliberately NOT set | AE attributes via DAG node label — single source of truth. Setting it on every leaf creates a second source of truth and confuses Grafana cuts. |
| `run_id` | instance | ❌ deliberately NOT set | AE attaches from Temporal context at ingest. The producer doesn't carry it. |
| `suggested_action` | instance | ⚠️ optional, at raise site | Free to set at the raise site when there's specific guidance to convey ("regrant Glue read access"). Don't pre-default per leaf — canned text is rarely better than the activity-specific context the call site has. |

Document the three deliberate non-uses in the app's `failures.py` module
docstring (see the Athena reference for the exact wording).

---

## Audience routing — who acts on a failure

The `audience` field is what AE uses to route alerts and SLA cuts. Closed
three-value enum:

- **USER** — the customer must act. Credentials, permissions, source config,
  source-side network, customer-controlled resources.
- **PLATFORM** — infra ops must act. A shared Atlan service is unavailable
  (Dapr, Temporal, object store, pod OOM).
- **APP_OWNER** — the app team that wrote the failing code must act. Connector
  bugs, SDK bugs, unimplemented capabilities, data-integrity failures.

**The deliberate override pattern:** when an SDK base class's default
`audience` doesn't fit your leaf's actual locus, override it. Example from
Athena:

```python
@dataclass(kw_only=True)
class AthenaSourceUnreachable(DependencyUnavailableError):
    """Athena endpoint not reachable.

    Audience overridden to USER (the SDK base defaults to PLATFORM) because
    source-side reachability is the customer's network / firewall, not the
    platform's.
    """
    code: ClassVar[str] = "DEPENDENCY_UNAVAILABLE_NETWORK"
    audience: ClassVar[Audience] = Audience.USER
```

If you're tempted to override `audience` more than once or twice per app,
re-check the SDK base default — most apps don't need to override at all.

---

## Phase 1 — Discovery (no edits)

When invoked, do this first. **Make zero changes during discovery.** End
discovery with a structured report and explicit user confirmation to proceed.

### Step 1.1 — Verify SDK version

The typed-errors infrastructure (`application_sdk.errors`) requires SDK
≥ v3.7.0. Check `pyproject.toml` `[tool.uv.sources]` block.

If the pin is older, propose a bump:

> SDK pin is currently v3.0.1. Typed errors require >= v3.7.0. The bump pulls
> in temporalio 1.27 and OpenTelemetry 1.41. OK to bump?

If the user declines, abort — the skill cannot proceed without the typed
errors package.

### Step 1.2 — Survey raise sites

Grep / read every `*.py` under `app/`. Categorise every `raise` statement:

- **Bare-builtin raises** (`raise ValueError("...")`, `raise RuntimeError(...)`,
  `raise Exception(...)`) — in-scope, must be converted.
- **SDK ActivityError / legacy errors** — in-scope, convert.
- **Already-typed AppError subclasses** — out of scope (already done).
- **Re-raises in except blocks** (`except X: raise`, `except X as e: raise e`,
  `except X: raise e from X`) — in-scope for classify-or-passthrough wrap if
  the underlying exception comes from a known source (boto, requests, db
  driver, file I/O).

For each in-scope site, capture:
- file:line
- 5 lines of context (the code before/after the raise)
- The current exception class + message
- The control-flow position (inside `__init__`, inside an activity body, inside
  a helper function, inside a `finally` block, etc.)

### Step 1.3 — Survey except blocks

Categorise every `except` block:

- **Re-raises** (terminates with `raise` / `raise e` / `raise X(...) from e`)
  → in-scope for classifier wrap.
- **Logs and continues** (`except X: logger.warning(...)` with no raise) →
  **silent-swallow blind spot**. Capture for phase-2 report. Do NOT touch in
  phase 1.
- **Returns / sets-default and continues** → also silent-swallow. Capture.
- **Per-record swallows** (`for row in batch: try: transform(row) except: skip`)
  → intentional best-effort, low severity. Capture but mark as "likely fine".

### Step 1.4 — Map the app's activity surface

Read the app's entry points to understand the architecture:

- `app/<connector>_app.py` or `app/connector.py` — the main App class.
- `app/handlers/*.py` — HTTP-side preflight / test_auth.
- `app/clients/*.py` — connection / query layer (likeliest source of typed
  raises).
- `app/transformers/*.py` — usually per-record swallows; rarely raise.
- Any `@activity.defn` or `@task` decorated functions — activity boundaries.

Note which raise sites are inside `@task`-decorated bodies (these reach AE
directly via `ApplicationError.details`) vs inside HTTP handlers (these never
reach AE — they surface as HTTP responses; typed classification still helps
preflight UX).

### Step 1.5 — Report and confirm

Print the discovery summary as a single message with three sections:

```
## Discovery summary

App: <name>
SDK pin: <current> → <target if bump needed>

### Existing raises (will be converted)
- N bare ValueError raises (likely InvalidInputError for missing-config patterns)
- M bare RuntimeError raises
- K legacy SDK error raises
- (sites listed by file:line with one-line context each)

### Except blocks that re-raise (candidates for classifier)
- Site list with the underlying exception source (boto / requests / db driver)

### Silent-swallow blind spots (phase-2 — owner approval needed)
- High severity: <count> — silent swallows in activity-level code that may
  mask production failures (activity returns success despite real failure)
- Medium: <count> — bare exception handlers that bubble untyped
- Low: <count> — per-record swallows, likely intentional best-effort

Ready to proceed with phase 2? (y/n)
```

Wait for explicit user confirmation before any edit.

---

## Phase 2 — Conversion (interactive, batched)

Apply edits in this order: **failures module first, then raise sites, then
tests, then SDK pin bump, then verification.**

### Step 2.1 — Build app/failures.py

Create or update `app/failures.py`. Always start with the module docstring
explaining the convention and the deliberate non-uses. Use the Athena
reference's docstring as a template — adapt the connector name.

For each typed class you're going to need, define it in `failures.py` with:
- A docstring describing what triggers it and who acts
- `code: ClassVar[str]` following the convention
- `audience: ClassVar[Audience]` only if overriding the SDK base
- Evidence fields as dataclass attributes (kw_only, with `None` or sensible
  defaults)

If the app has marker-string-based exception classification (common in apps
wrapping boto, requests, db drivers), define marker tuples and a
`classify_<app>_exception(exc) -> AppError | None` function at the bottom of
`failures.py`. Pattern from Athena:

```python
AUTH_MARKERS: tuple[str, ...] = (...)
PERMISSION_MARKERS: tuple[str, ...] = (...)
NETWORK_MARKERS: tuple[str, ...] = (...)

def classify_<app>_exception(exc: BaseException) -> AppError | None:
    """Return a typed failure for exc, or None if no marker matches.

    Returns None when exc is already an AppError — don't reclassify.
    Caller pattern:
        classified = classify_<app>_exception(e)
        if classified is not None:
            raise classified from e
        raise
    """
    if isinstance(exc, AppError):
        return None
    msg = str(exc)
    if any(m in msg for m in AUTH_MARKERS):
        return <App>AuthFailed(message=msg, cause=exc)
    # ...
    return None
```

### Step 2.2 — Convert raise sites (auto-classify obvious, ask on ambiguous)

**Auto-classify (no question needed):**

- `raise ValueError("X is required")` / `raise ValueError("X must be provided")`
  → `<App>InvalidConfig(message=..., field="X")` — INVALID_INPUT / USER
- `raise ValueError("Engine not initialized")` / `raise ValueError("X must be
  called first")` → `<App>EngineNotInitialized(message=...)` / similar
  → INTERNAL / APP_OWNER
- `raise RuntimeError("Failed to prepare ...")` / template / formatting errors
  → `<App>QueryPrepFailed(message=...)` / similar → INTERNAL / APP_OWNER
- `raise ActivityError(ATLAN_UPLOAD_ERROR ...)` →
  `AtlanUploadFailed(message=..., failed_count=N, total_count=M)`
  → DEPENDENCY_UNAVAILABLE / PLATFORM
- `except <ConnectionError|...>: raise` after boto / requests / db calls
  → wrap with `classify_<app>_exception(e)` + passthrough

**Ask the developer when:**

- The same message could be one of two categories. Example: "Cursor is not
  supported" could be `InternalError` (programmer error) or `UnimplementedError`
  (known feature gap). Ask which.
- A new class name doesn't match an existing one. Show suggested name, ask to
  confirm or rename.
- The audience would differ from the SDK base default. Show the default, ask
  if override is intended.
- Evidence field naming. Show suggested fields, ask for additions.

**Question format** — keep it tight. One question per ambiguous site, with
context and a default:

> Site: `app/clients/__init__.py:196` — `raise ValueError("Cursor is not supported")`
>
> Context: SQLAlchemy returned a cursor we can't iterate. This is either:
> a) **InternalError** (default — PyAthena version mismatch / programmer error)
> b) UnimplementedError (we know this cursor type exists but haven't implemented it)
>
> Suggested class: `<App>CursorUnsupported(InternalError)`
> Suggested code: `INTERNAL_CURSOR_UNSUPPORTED`
> Audience: APP_OWNER
>
> a / b / customize?

**Batch similar sites** when they share the same pattern. Don't ask the same
question 4 times for missing-config raises in the same file.

### Step 2.3 — Apply edits

Use `Edit` for each site. Preserve the original message string verbatim
(append it as the `message=` kwarg). Add evidence kwargs where the call site
has obvious context.

Update imports — every modified file needs `from app.failures import <classes>`
at the top.

For classifier-wrapped except blocks, change the pattern:

```python
# before:
except Exception as e:
    logger.error("...", e)
    raise e

# after:
except Exception as e:
    logger.error("...", e)
    classified = classify_<app>_exception(e)
    if classified is not None:
        raise classified from e
    raise
```

### Step 2.4 — Write tests

Create or update `tests/unit/test_failures.py` with two test classes:

**`TestTypedClasses`** — one test per leaf, asserting the wire shape:

```python
def test_<app>_auth_failed_payload(self):
    fd = <App>AuthFailed(message="bad creds").to_failure_details()
    assert fd.category is FailureCategory.AUTH
    assert fd.code == "AUTH_INVALID_CREDENTIALS"
    assert fd.audience is Audience.USER
    assert fd.retryable is False
```

Cover audience overrides explicitly (a test that asserts the override took
effect, e.g. `AthenaSourceUnreachable.audience is Audience.USER` not PLATFORM).

**`TestClassifier`** — parametrised tests over every marker in each tuple,
asserting dispatch to the right typed class. Plus:
- `test_unknown_message_returns_none` — unrecognised exception → None
- `test_does_not_reclassify_existing_app_error` — AppError → None
- `test_classified_carries_original_as_cause` — `from exc` chain preserved

Update any existing tests that asserted bare `ValueError` / `RuntimeError`
to assert the new typed class + check evidence fields.

### Step 2.5 — Bump SDK pin (if needed)

If `pyproject.toml` `[tool.uv.sources]` pins the SDK to `< v3.7.0`, bump:

```toml
atlan-application-sdk = { git = "https://github.com/atlanhq/application-sdk", tag = "v3.7.0" }
```

Run `uv lock` and `uv sync --all-extras --quiet`. The lock-file diff will be
large but mechanical.

### Step 2.6 — Verify

Run the test suite:

```bash
uv run pytest tests/unit/ -q --no-header -p no:cacheprovider
```

All tests must pass (excluding pre-existing collection errors unrelated to
this change). If a previously-asserting-ValueError test fails, update it —
the test was right, the assertion just needs to be the new typed class.

For each new typed class, run the wire-payload check:

```bash
uv run python -c "
from app.failures import <App>AuthFailed
print(<App>AuthFailed(message='test').to_failure_details().model_dump())
"
```

Verify category / code / audience / retryable match the documented spec.

---

## Phase 3 — Blind-spot report (the phase-2 punch list)

After all edits land and tests pass, produce the final report. This is the
deliverable the app owner uses to decide what to address in the next PR.

**Format** — one section per severity, sites listed with file:line + one-line
description + the typed class that would apply if the swallow were promoted
to a raise:

```
## Blind-spot report — phase 2 punch list

These sites are currently silent-swallows or bare-passthrough. Closing them
requires adding try/except wrappers (logic change). They are intentionally
out of scope for this PR. The list is ranked by customer impact, not by
code change size.

### High severity — activity reports success despite real failure

- `app/<connector>_app.py:<line>` — extract loop catches all exceptions and
  logs; activity returns success with 0 records on auth failures. Would
  classify as <App>AuthFailed / <App>PermissionDenied / <App>SourceUnreachable
  depending on which marker matches. **This is the highest-impact gap.**

- (other high-severity sites)

### Medium — bare exception handlers bubble untyped

- `app/<connector>_app.py:<line>` — Glue API call has no error handling; bare
  botocore ClientError bubbles to AE as untyped ApplicationError.
- (other medium sites)

### Low — intentional per-record swallows

- `app/transformers/*.py:<line>` — per-record transform errors logged and
  skipped. Likely correct as-is; only worth promoting if every record fails
  (would classify as DataIntegrityError).

## Recommendation

Address the high-severity gaps first in a phase-2 PR. The pattern is small
per site (≤5 lines) but does change control flow, so each gap is an owner
decision: "do we want this activity to fail loudly, or silently report
green-with-zero?"
```

**Do not commit this report.** Print it as the final skill output so the
developer can paste it into the PR description or share with the owner for
phase-2 sign-off.

---

## Anti-patterns to avoid

1. **Inventing new categories.** The SDK enum is closed. If the existing
   category doesn't fit, the right move is to refine the leaf's audience or
   evidence — not to add a new category. If you genuinely believe a new
   category is needed, that's a Chris/SDK-team conversation, not a skill
   output.

2. **Putting the vendor name in `code`.** `ATHENA_AUTH_INVALID_CREDENTIALS` is
   wrong. `AUTH_INVALID_CREDENTIALS` is right. Vendor identity rides on the
   DAG node label.

3. **Pre-populating `app_name` / `suggested_action` / `run_id`.** All three
   have documented reasons to stay `None` by default. Read the property
   matrix above and the Athena `failures.py` module docstring.

4. **Promoting silent-swallow paths to raises in this skill's output.** That's
   a logic change. It goes in phase 2 after owner approval. Even if you're
   confident the swallow is wrong (e.g. activity reports green-with-zero on
   auth failure), it goes in the report, not the diff.

5. **Adding granular evidence for every leaf.** Evidence is for context that
   helps debugging or routes downstream. `field` on an InvalidInput-style
   class is useful (tells customer which field to fix). `created_at` on
   every class is noise. Match the Athena reference's restraint.

6. **Mixing classifier markers across categories.** AUTH markers and PERMISSION
   markers are distinct on the wire (different categories). If a marker is
   genuinely ambiguous (`AccessDeniedException` could be auth or permission),
   pick the more specific one (PERMISSION — authenticated but not authorised)
   and document the choice.

7. **Skipping the discovery report.** Always show the user what was found
   before making edits. Different apps have different exception landscapes —
   the discovery report is what makes this skill non-blind.

8. **Auto-overriding audience without asking.** The SDK base default is
   usually right. If you're tempted to override on more than one or two
   leaves per app, double-check — you're probably misreading the locus.

---

## Reference: the canonical example

`atlan-athena-app` PR #57 (branch `feat/typed-failure-classes-develop`,
base `develop`) is the canonical reference. It includes:

- `app/failures.py` — 7 typed classes + classifier with marker tuples
- Raise sites converted across `clients/__init__.py`,
  `clients/glue_api_client.py`, `handlers/athena_handler.py`,
  `<connector>_app.py`
- `tests/unit/test_failures.py` — 35 tests covering wire shape + dispatch
- Handler refactor: `_classify_and_raise` delegates to
  `classify_athena_exception` and handles both bare and already-typed inputs
- PR description with the convention, the wire-flow trace, the blind-spot
  punch list, and the open questions for the owner

Read it before running the skill on a new app to anchor on the end-state
shape and the level of restraint expected (e.g. how many leaves to create,
how rich the evidence fields should be, how to phrase docstrings).

---

## Final output checklist

When the skill completes, you should have produced:

- [ ] `app/failures.py` — typed classes + (optional) classifier
- [ ] Edited raise sites — every in-scope `raise` swapped for typed
- [ ] Updated imports across modified files
- [ ] `tests/unit/test_failures.py` — wire-shape + dispatch tests, all passing
- [ ] Updated existing tests that asserted `ValueError` / `RuntimeError`
- [ ] `pyproject.toml` SDK pin >= v3.7.0 (if needed)
- [ ] All unit tests pass (`uv run pytest tests/unit/ -q`)
- [ ] Blind-spot report printed for the developer
- [ ] PR description draft (template the Athena PR's structure)

If any item is incomplete, say so explicitly to the developer. Do not declare
the skill done if anything in the checklist failed.
