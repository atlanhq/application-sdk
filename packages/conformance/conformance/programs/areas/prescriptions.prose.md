---
kind: responsibility
name: prescriptions-area
description: >
  Maintains the current P-series violation-set and drives SUGGEST-ONLY
  remediation: for each finding the model drafts a proposed fix, but the
  proposal is recorded for human review and never auto-applied — because
  P-series rules currently have no orthogonal gate that can validate a fix.
---

### Maintains

The current set of unsuppressed P-series (prescription) conformance findings
in the working tree, as reported by `suite.runner --series P`, each paired
with a model-drafted **proposed** fix for human review.

#### violations-prescriptions

The fingerprint-set of all unsuppressed FAILING P-series results.  Extends to
include WARNING results in strict mode.

Postcondition (suggest-only — the loop proposes but does not apply):

> Every P-series finding routes to the residue report with a drafted fix
> attached.  The working tree is left unchanged by this area; a human reviews
> each proposal and applies it (or rejects it) manually.  The deterministic
> `suite.runner --series P` exit code is therefore unchanged by this area —
> only humans clear P-series findings.

**Why suggest-only, not auto-applied (not an oversight):** P001
`UnboundedContractFields` is suppress-only, and its only fix that clears the
detector is adding `Annotated[..., MaxItems(N)]` or an inline suppression.
`MaxItems` is a **declarative marker — not runtime-enforced** — so (a)
`recheck-narrowest` is satisfied by *any* bound, including an absurd one, and
(b) the orthogonal test gate is structurally blind: no behaviour changes with
the bound, so no test can catch a hollow fix.  Per design §6.1, a rule whose
gaming move no gate can catch must **not** be auto-applied — that would
normalise exactly the gaming the gate exists to prevent.  The safe form is
**propose, don't apply**: the model drafts a concrete diff, a human is the gate.
When a gate that validates the bound exists (a runtime-enforced `MaxItems`, or
a payload-size behavioural check), this area can graduate to the full
`detect-fix-recheck` loop.

P002/P003 are BLOCK-tier and their fixes *are* validated by the orthogonal test
gate (`orthogonal_gate = "tests"`), unlike P001.  They are suggest-only here for
architectural consistency with the rest of this area, but they are strong
candidates to graduate to the `detect-fix-recheck` loop once the area
differentiates sub-groups by gate status.

The orchestration-seam rules (P004–P007, BLDX-1417) are also P-series and also
suggest-only here, for their own reasons: the app-side import rewrites (P004/P005)
land mostly under `tests/` (an integration harness) where `remediate-finding` may
not write, and they carry judgment (whether a public twin exists, and the
`Client`-annotation hole that depends on P007 being closed first); the SDK-side
rules (P006/P007) describe refactors (relocate Temporal behind the adapter; wrap a
raw type in an opaque SDK type) that no import edit can perform.  All four draft a
proposal for human review and never auto-apply.  (These rules are backed by a
separate, test-scanning `suite.checks.orchestration` check — see its module docs.)

The storage-seam rules (P008–P012) are also P-series and suggest-only: they
describe structural workflow refactors (data-flow topology, store-routing
contracts, durability-field ownership) that no local import rewrite can perform,
and the orthogonal gate is non-protective for structural regressions not yet
covered by tests.  All five draft a proposal for human review.  (These rules are
backed by `suite.checks.prescriptions` alongside P001–P003.)

The client-seam rule (P019, BLDX-1430) is also P-series and suggest-only: the fix
replaces a hand-rolled raw-HTTP call to an Atlan service with the equivalent
`pyatlan` call, which is a semantic rewrite (the right pyatlan method depends on
the endpoint's meaning) that no mechanical import edit can perform, and the choice
of which pyatlan surface to use — or whether to suppress when no equivalent exists
— is the developer's call.  It drafts a proposal for human review and never
auto-applies.  (This rule is backed by a separate `suite.checks.client_seam` check
— see its module docs.)

The determinism / async-correctness rules (P020–P024) are also P-series
and suggest-only.  P020 (non-deterministic primitive in workflow context) has a
concrete mechanical proposal for time/uuid/sleep — swap to the SDK seam — but its
randomness case has no seam target, and P021 (workflow I/O) / P023 (blocking call
in an async def) describe structural moves into a `@task` that no local edit can
safely perform; P022 (un-awaited coroutine) proposes adding `await`, and P024
(sync pyatlan client) proposes the async client via the SDK seam — both
semantically load-bearing.  All five draft a proposal for human review and never
auto-apply.  (These rules are backed by a separate `suite.checks.determinism`
check — see its module docs.)

The typed-boundary / state-seam / asset-modeling rules (P026–P028) are also
P-series and suggest-only.  P026 (getattr-with-default on a typed contract param)
has a concrete mechanical proposal — replace `getattr(input, "f", default)` with
attribute access `input.f` — but whether the field is genuinely optional (and the
default intended) is the developer's call.  P027 (app_state read with no
populating writer) describes a structural fix — route the data through the typed
entrypoint/task contract — that no local edit can perform, and the writer may be
external to the scanned source.  P028 (hand-built qualifiedName f-string) proposes
constructing assets via the pyatlan `.creator()` factories, a semantic rewrite
gated on the SDK exposing a qualifiedName seam.  All three draft a proposal for
human review and never auto-apply.  (These rules are backed by
`suite.checks.prescriptions` alongside P001–P003.)

### Requires

- `scope` — repository root path.
- `mode` — `"default"` or `"strict"`.

### Continuity

Input-driven: re-render when any `*.py` file under `scope` changes.

### Execution

```prose
# Suggest-only: detect, draft a fix per finding, route to residue WITHOUT
# applying.  No gate can validate a P-series fix, so the human is the gate —
# this area never mutates the working tree (contrast detect-fix-recheck, which
# applies and keeps edits that pass their gates).
let violations = call detect-violations
  scope: scope
  series: "P"
  target: if mode == "strict" then "failing+warning" else "failing"

for each finding in violations:
  let proposal = call remediate-finding
    finding: finding
    mode: mode

  # The proposal is recorded, never applied.  classification is always
  # "judgment" for P-series, so it lands in the human-review residue.
  add { finding, proposal } to residue with note "P-series suggest-only: proposed fix drafted for human review; NOT applied (no orthogonal gate validates a MaxItems bound or suppression)"
```

### Fix Prescription

_Read by `remediate-finding` when `finding.area == "prescriptions"`._

Drafts a **proposed** fix for human review.  This area is suggest-only: the
proposal is recorded in residue and **never applied** — see **Why suggest-only**
above.  `classification` is always `"judgment"` for all P-series rules.

- **P001 UnboundedContractFields** — the contract opts out of payload safety
  via the `allow_unbounded_fields=True` class keyword.  Read the contract's
  fields around `finding.line`, then draft, in order of preference:

  1. **The real fix (preferred)** — remove `allow_unbounded_fields=True` and
     bound each field the payload-safety validator would reject: wrap an
     unbounded `list[T]` as `Annotated[list[T], MaxItems(N)]` and an unbounded
     `dict[K, V]` as `Annotated[dict[K, V], MaxItems(N)]`, choosing `N` from
     the field's realistic cardinality and **stating that assumption** in the
     proposal (e.g. ~10000 ≈ ~1MB JSON, well under Temporal's 2MB limit).  A
     scalar-only contract needs only the opt-out removed.  Add
     `from typing import Annotated` and
     `from application_sdk.contracts.types import MaxItems` if missing.
     Return `outcome = "fix"`.

  2. **Fallback** — if a field is genuinely unbounded with no sensible cap,
     draft an inline `# conformance: ignore[P001] <concise justification>` on
     the declaration line, where the justification explains *why* unbounded
     fields are unavoidable here (not merely that the rule is suppressed).
     Return `outcome = "suppress"`.

- **P002 CategoryFieldOverride** — a non-canonical subclass of `AppError` (or
  any of its 15 categorical leaves) redeclares the `category` ClassVar in its
  own body.  Read the class definition around `finding.line`, then:

  1. Verify that the class inherits from a canonical leaf and that the parent's
     `category` value is semantically correct for this class's failure mode.
     (It almost always is — the redeclaration is typically a copy-paste artifact.)
  2. Delete the redeclaring `category` assignment from the class body.  The
     class will then inherit `category` from its canonical-leaf ancestor.

  If the redeclaring class appears to need a category that genuinely differs
  from its parent (i.e. it belongs under a different leaf), do **not** rename
  the field value — instead, note in residue that the class hierarchy may be
  wrong and name the leaf that better fits the semantic.

  The orthogonal test gate (`orthogonal_gate = "tests"`) validates the fix.

- **P003 ErrorCodePrefixMismatch** — a (transitive) subclass of an
  `application_sdk.errors` leaf either omits its own `code: ClassVar[str]`
  declaration or declares one that does not start with the leaf's category
  prefix.  The finding message names the leaf and the required prefix (e.g.
  `AuthError` → `AUTH_`).  Two cases:

  - **No `code` declaration** — add `code: ClassVar[str] = "<PREFIX>_<SUFFIX>"`
    to the class body (just below any docstring, or at the top of the body).
    Choose `<SUFFIX>` from the class name or its semantic intent: a class named
    `CredentialExpiredError` under `AuthError` becomes `AUTH_CREDENTIAL_EXPIRED`.
    Add `from typing import ClassVar` if not already imported.

  - **Code declared but wrong prefix** — if the value is otherwise sensible
    (e.g. `EXPIRED` instead of `AUTH_EXPIRED`), prepend the prefix.  If the
    value clashes with a different leaf's prefix (e.g. `INTERNAL_TIMEOUT` on an
    `AuthError` subclass), note in residue that the class may belong under the
    wrong leaf hierarchy.

  The orthogonal test gate (`orthogonal_gate = "tests"`) validates the fix.

**Orchestration-seam rules (P004–P007)** — also suggest-only; `classification`
is always `"judgment"`:

- **P004 DirectTemporalImport** (app) — the app imports `temporalio` directly.
  Draft a rewrite to the SDK seam by mapping the imported symbol:
  - workflow primitives `now`/`sleep`/`uuid4`/`wait_condition` and the
    interaction decorators `signal`/`query`/`update` (and `task` in place of
    `activity`) → `from application_sdk.app import …`;
  - `temporalio.client.Client` / `Client.connect(...)` →
    `from application_sdk.execution import create_temporal_client`
    (`client = await create_temporal_client(host=...)`);
  - `temporalio.worker.Worker` →
    `from application_sdk.execution import AppWorker, create_worker`;
  - `temporalio.converter` data-converter use →
    `from application_sdk.execution import create_data_converter`.

  **Annotation hole — route to residue, do not fabricate a fix:** if the only
  use of a `temporalio` symbol is to *annotate* a value the public seam returns
  (e.g. `Client` for the result of `create_temporal_client`), there is no public
  opaque type to swap to yet — this is the P007 leak the SDK must close first.
  Note the P007 dependency in residue rather than inventing an import.

- **P005 PrivateOrchestrationInternalImport** (app) — the app reaches into an
  SDK-private module. Draft a rewrite to the public re-export when one exists:
  - `application_sdk.execution._temporal.worker.{create_worker,AppWorker}` →
    `application_sdk.execution.{create_worker,AppWorker}`;
  - `application_sdk.execution._temporal.backend.create_temporal_client` →
    `application_sdk.execution.create_temporal_client`;
  - `application_sdk.execution._temporal.converter.create_data_converter` →
    `application_sdk.execution.create_data_converter`.

  **No public twin — route to residue:** some internals have no public
  equivalent today (e.g. `create_data_converter_for_app`,
  `TemporalExecutorBackend`). Do **not** invent a public import; note that the
  SDK must expose a public equivalent (or the app must drop the dependency).

- **P006 TemporalImportOutsideAdapter** (sdk) — `temporalio` is imported outside
  the `execution/_temporal/` adapter. The fix is a structural relocation of the
  Temporal usage behind the adapter, which no import rewrite can perform. Route
  to residue with a note that an SDK refactor is required. Do not attempt a
  mechanical edit.

- **P007 RawTemporalInPublicSurface** (sdk) — a public API re-exports or exposes
  a raw `temporalio` type. The fix is to wrap the value in an opaque SDK type (or
  stop re-exporting it) — a public-contract refactor. Route to residue with that
  guidance. Do not attempt a mechanical edit.

**Storage-seam rules (P008–P012)** — all suggest-only, scope=app, WARN-tier;
`classification` is always `"judgment"`.  Read the full function/class context
around `finding.line` before drafting any proposal.

- **P008 FrameworkTransferInsideTask** — `self.upload(...)` or
  `self.download(...)` is called inside a `@task`-decorated method.
  `App.upload`/`download` are themselves framework tasks; nesting them violates
  Temporal's activity-within-activity constraint.  Draft a workflow restructure:
  - **Producing side**: remove the nested `self.upload()` call from the `@task`
    method; return the data (or a local file path) to the workflow layer instead.
    The workflow layer calls `App.upload()` as a separate task and passes the
    resulting `FileReference` onward.
  - **Consuming side**: accept a `FileReference` input field rather than calling
    `self.download()` inside the task body; the workflow layer calls
    `App.download()` before scheduling the task.
  Do not attempt a mechanical rewrite — the topology change requires
  understanding the full workflow.  Draft the refactored shape and route to
  residue.

- **P009 ManualObjectStoreConstruction** — app code constructs a cloud client or
  object store directly: `boto3.client(...)`, `S3Store(...)`, `GCSStore(...)`,
  `AzureStore(...)`, or any `create_store_from_binding*(...)` call.  The SDK
  provides a correctly routed store (including SDR mode) via
  `get_infrastructure().storage` (import from `application_sdk.framework`).
  Draft a replacement that obtains the store through the SDK seam.  If the
  original construction passes configuration parameters (region, endpoint,
  credentials) that may not be available through the SDK, note those in residue
  as a follow-up for the SDK team — do not silently drop them.

- **P010 ManualFileReferenceConstruction** — a `FileReference(...)` constructor
  call sets SDK-owned durability fields: `storage_path`, `is_durable`, or
  `file_count`.  These are populated by the activity interceptor at persist time;
  setting them manually bypasses the persist/materialize contract.  Draft a fix
  that removes the SDK-managed fields from the constructor, leaving only
  caller-owned fields (e.g. `name`, `file_type`, metadata).  If the intent is to
  produce a pre-materialized `FileReference` from a known cloud URI, note in
  residue that the SDK may need a factory (`FileReference.from_uri(...)` or
  similar) — do not silently omit the URI.

- **P011 RawBytesInContract** — a `bytes`, `bytearray`, or `memoryview` field
  on an `Input`/`Output` contract embeds raw binary data across the Temporal
  payload boundary and risks hitting the 2 MB limit.  Draft a `FileReference`
  replacement:
  - **Producing side**: write the bytes to a local temp file, call `App.upload()`
    to transfer it, put the resulting `FileReference` in the contract field.
  - **Consuming side**: receive the `FileReference`, call `App.download()` to
    materialize the file, then read the local bytes.
  If the data is demonstrably ≤ 1 KB and truly inline (not file-like), propose
  a `str` field (base64-encoded bytes) with a `# conformance: ignore[P011]`
  suppression and state the size justification explicitly.

- **P012 FilePathStringInContract** — a `str` field whose name or docstring
  signals a filesystem path (`input_path`, `output_dir`, `file`, `directory`,
  etc.) carries a worker-local reference that is invalid on a different worker.
  Draft a `FileReference` replacement using the same producing/consuming pattern
  as P011.  If the path is always an object-store URI (not a worker-local path),
  propose renaming the field to clarify the semantics (e.g. `storage_uri`) and
  suppressing P012 with justification; state why the value is stable across
  workers.

**Client-seam rule (P019)** — suggest-only, scope=both, WARN-tier;
`classification` is always `"judgment"`.  Read the full function/class context
around `finding.line` before drafting any proposal — the proposal is a
**suggestion left to the developer's call**, never auto-applied.

- **P019 RawHttpToAtlan** — a raw HTTP call (`httpx`/`requests`/`aiohttp`/`urllib`)
  targets an Atlan service: its URL carries `/api/meta` (Atlas) or `/api/service`
  (Heracles).  `pyatlan` is the supported client and a core dependency; the SDK
  exposes it through `application_sdk.credentials`.  Draft a proposal in two parts:

  1. **Obtain the client through the SDK seam** (never hand-roll one):
     - inside an `App` subclass →
       `client = await self.get_or_create_async_atlan_client(credential)`
       (the `AtlanClientMixin`);
     - ad-hoc / outside an App →
       `client = create_async_atlan_client(cred)`
       (`from application_sdk.credentials import create_async_atlan_client`).

  2. **Replace the raw call with the matching pyatlan surface**, mapped by the
     endpoint marker in the flagged URL:
     - `…/api/meta/entity/…` (get an asset) →
       `await client.asset.get_by_guid(...)` / `get_by_qualified_name(...)`;
     - `…/api/meta/…` search / typedefs →
       `client.asset.search(FluentSearch…)`, `client.typedef.get(...)`;
     - `…/api/service/…` (workflows, packages, admin) →
       the matching pyatlan surface (`client.workflow…`, admin/token clients).
     If the offending call constructed a client object directly
     (`httpx.AsyncClient(base_url="…atlan…")`), the proposal is to delete it and
     obtain the pyatlan client from the seam in step 1.

  **No pyatlan equivalent — route to residue, do not fabricate a call:** if the
  endpoint has no pyatlan method (raise it with the SDK team), propose an inline
  `# conformance: ignore[P019] <reason>` instead, where the justification names
  the missing surface.  Either way the proposal is recorded for the developer to
  apply or reject; this area never mutates the working tree.

**Determinism / async-correctness rules (P020–P024)** — all suggest-only,
scope=both, WARN-tier; `classification` is always `"judgment"`.  Read the enclosing
method around `finding.line` first, and confirm it is workflow context (`run` /
`@entrypoint` / `@signal` / `@query` / `@update`) versus a `@task` activity before
drafting.

- **P020 NonDeterministicPrimitiveInWorkflow** — a wall-clock/uuid/sleep/random
  call runs in a workflow-context method.  Draft, by category:
  - **time** (`datetime.now`/`utcnow`/`today`, `time.time`/`monotonic`/…) → replace
    with `self.now()` (or `from application_sdk.app import now`).
  - **uuid** (`uuid.uuid1`/`uuid.uuid4`) → replace with `self.uuid()` (or
    `from application_sdk.app import uuid4`).
  - **sleep** (`time.sleep`/`asyncio.sleep`) → replace with `await sleep(...)`
    from `application_sdk.app`.
  - **randomness** (`random.*`/`secrets.*`/`os.urandom`) → **route to residue, do
    not fabricate a swap**: the SDK exposes no deterministic-random seam.  Note
    that the randomness must move into a `@task`, or that the SDK should expose a
    deterministic-random primitive (raise a seam request).
  Verify the receiver before proposing — `self.now()` / `now()` are already the
  sanctioned forms and must never be rewritten.

- **P021 SideEffectIoInWorkflow** — file/network/env/process I/O runs in a
  workflow-context method.  The fix is structural: extract the I/O into a `@task`
  activity and have the workflow `await` it.  No local edit can perform this
  safely (it changes the workflow/activity topology) — draft the refactored shape
  (which call becomes a task, what the task returns) and route to residue.

- **P022 UnawaitedCoroutine** — a bare `self.<async-method>(...)` statement drops a
  coroutine.  Propose adding `await` (or wrapping in `asyncio.create_task`/`gather`
  if concurrency is intended).  State which intent you assumed: a missing `await`
  is the common case, but if the surrounding code suggests fire-and-forget, say so
  and propose `create_task` instead.  The change is load-bearing, so route to
  residue for human confirmation.

- **P023 BlockingCallInAsyncDef** — an event-loop re-entry bridge (`asyncio.run`/
  `run_until_complete`) or a blocking sync call (`requests.*`, `time.sleep`) runs
  inside an `async def`.  Draft: for a bridge, `await` the coroutine directly
  instead of re-entering a loop; for blocking I/O, `await` an async equivalent or
  offload it via `App.run_in_thread()` inside a `@task`.  Both are restructures —
  route to residue with the proposed shape.

- **P024 SyncAtlanClientInApp** — app code constructs pyatlan's sync `AtlanClient`
  (or a factory like `AtlanClient.from_token(...)`).  Draft a swap to the async
  client through the SDK seam: inside an `App` that mixes in `AtlanClientMixin`,
  `client = await self.get_or_create_async_atlan_client(credential)`; ad-hoc /
  outside an App, `client = create_async_atlan_client(cred)`
  (`from application_sdk.credentials import create_async_atlan_client`).  The
  downstream calls on the client then become `await`-ed, so this is a restructure
  — route to residue with the proposed shape; do not mechanically rename the
  class.  Leave `AsyncAtlanClient` usage untouched.

**SDR-readiness rules (P029/P030, P037/P038/P039)** — all suggest-only,
scope=app; `classification` is always `"judgment"`.  All gate on
`self_deployed_runtime: true` in `atlan.yaml`.

- **P029 SdrManifestMissingAgentJson** (BLOCK) — a `manifest.json` under
  `app/generated/` is missing the `agent_json` key in `dag.extract.inputs.args`.
  Without this slot the SDR platform cannot inject credentials at dispatch time;
  the workflow runs to "success" but the extraction agent receives no credentials
  and writes zero assets (the MSSQL regression pattern, atlan-mssql-app#177).
  The finding is anchored at line 1 of the manifest file — JSON has no comment
  syntax and inline suppression is not available.  The only remedy is a Pkl-layer
  change: add `agent_json` to the extract inputs in `contract/app.pkl` and
  re-run `pkl eval` to regenerate the manifest.  Do not hand-edit the generated
  JSON.  Draft the required `app.pkl` addition and route to residue for the
  developer to apply.

- **P030 SdrUploadNotCalled** (WARN) — no `self.upload(` call exists in any app
  source file outside `tests/`, making the `ENABLE_ATLAN_UPLOAD` gate structurally
  unreachable.  The finding is anchored at line 1 of `atlan.yaml` — the check
  builds its `Finding` directly and does not call `_parse_directives`, so inline
  YAML suppression is not honoured.  Draft a proposal that adds
  `await self.upload(output_key)` in the appropriate `@entrypoint`-decorated
  method or `run()` method, after extraction completes.  Read the app's workflow
  structure first — some apps delegate upload to a base class or a helper method
  that the scanner cannot see; if that is the case, note it in residue rather
  than adding a redundant call.  Route to residue for human confirmation.

- **P037 SdrAgentJsonNotConsumed** (WARN) — the app performs custom credential
  resolution (a bare `CredentialRef(credential_guid=...)` construction or a
  `resolve_credential_raw(...)` call) but never routes through an agent-aware
  resolver entry point (`CredentialRef.resolve(input)` /
  `CredentialRef.from_workflow_args(workflow_args)`, or a `CredentialRef` built
  with an `agent_spec`/`agent_json` kwarg).  Resolving strictly by
  `credential_guid` ignores the forwarded `agent_json`, so in agent (SDR) mode
  the credential never resolves and the workflow writes zero assets while
  reporting "success".  The finding is app-level, anchored at
  the first custom-resolution call site.  Apps that lean on the SDK's transparent
  resolution (no `CredentialRef` / `resolve_credential_raw`) are not gated in.
  Draft a proposal that
  routes resolution through `CredentialRef.resolve(input)` /
  `CredentialRef.from_workflow_args(workflow_args)`, keeping the direct
  `credential_guid` path only as a fallback; route to residue for confirmation.

- **P038 SdrArtifactMisrooted** (WARN) — the object-store output path/prefix
  (`artifacts/apps/<identity>/...`) is rooted from the *workflow-input*
  `application_name` field (read as `input_data.get("application_name", ...)`,
  `input_data["application_name"]`, or `input.application_name`) instead of the
  SDK app identity (`APPLICATION_NAME` / `self._app_name`).  That field's contract
  default is `""` and AE forwards only manifest-declared args, so it stays empty
  and artifacts land under `artifacts/apps//workflows/...` (empty app segment);
  `self.upload()` succeeds but the publish app finds 0 assets (complementary to
  P030 — the upload IS called, but mis-rooted).  The
  finding is anchored at the offending f-string.  The heuristic is deliberately
  narrow (it keys on the `application_name` input field feeding an
  `artifacts/apps` literal); it does not catch every mis-rooting — an app that
  forwards an empty `output_prefix` input field without an `artifacts/apps`
  literal is statically indistinguishable from a correct app and is left to
  runtime/e2e detection.  Draft a proposal that roots the prefix from
  `APPLICATION_NAME` / `self._app_name` (or `WORKFLOW_OUTPUT_PATH_TEMPLATE`);
  route to residue for confirmation.

- **P039 SdrAgentJsonDroppedByInputContract** (WARN) — the generated manifest
  declares `{{agent-json}}` at the extract-args top level (P029 passes), but the
  generated extract-input contract model (`AppInputContract` in a generated
  `_input.py`) subclasses the bare `Input` base, declares no `agent_json` field,
  and rejects extra fields — so Pydantic silently drops the forwarded
  `agent_json` at model construction.  The extract input's `credential_ref` is
  then `None` and extraction fails with `PipelineContractError` / 0 assets even
  though the manifest and connector code look correct.  This is
  orthogonal to P029 (manifest side) and P037 (code resolves by guid only) — all
  three must be clean.  The finding is anchored at the `AppInputContract` class.
  Contracts that subclass the SDK `*ExtractionInput` family (which declares
  `agent_json`) or set `allow_unbounded_fields=True` / `extra="allow"` are exempt.
  The remedy is a
  Pkl-layer change: declare `agent_json` on the extract-input contract in
  `contract/app.pkl` (or set `allow_unbounded_fields=True`) and regenerate; do
  not hand-edit the generated `_input.py`.  Route to residue for confirmation.
