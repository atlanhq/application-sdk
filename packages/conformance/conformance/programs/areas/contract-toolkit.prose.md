---
kind: responsibility
name: contract-toolkit-area
description: >
  Maintains the current K-series violation-set and drives remediation of
  contract-toolkit findings.  K001 (contract amends NativeApp.pkl or
  NativeAppBundle.pkl instead of App.pkl) and K002 (NativeApp-only APIs present:
  flatManifestArgs, manifestMetadataArgs, workflowTypeOverride, or legacy
  Config/Connectors/Credential/Renderers imports) are guided migration fixes.
  K003 (stale Pkl lock), K004 (missing generated output), and K005 (stripped
  provenance banner) are generated-artifact freshness findings fixed by
  re-resolving / regenerating — all verified by the pkl-eval gate.  K006
  (a manifest.json JSONPath reference the Python Output contract does not
  declare) is fixed on the Python side — declare the field or mix in the SDK
  contract base that supplies it — and verified by the test-suite gate, not
  pkl-eval.  K007 (toolkit version below the latest published), K008 (toolkit
  sourced from a non-canonical base URI), K009 (unresolved scaffold placeholder
  in a generated artifact), and K010 (missing generated E2E scaffolding) are
  toolkit-hygiene findings fixed by re-pointing/bumping the PklProject dependency
  and regenerating — all verified by the pkl-eval gate.  K013 (a DAG node running a
  toolkit-owned workflow but still attributed to automation-engine) is fixed in
  contract/app.pkl -- by switching to the built-in node class or setting appName
  explicitly -- and verified by the pkl-eval gate.  K014 (atlan.yaml declares no
  release_model, so the app inherits the publish-on-merge default) is fixed either
  in the contract or in a hand-owned atlan.yaml, decided by whether the contract
  actually emits that file.  K009, K011, and K012 are
  BLOCK-tier (they fail the gate in default mode); the rest of the K-series is WARN.
---

### Maintains

The current set of unsuppressed K-series (contract-toolkit conformance)
findings in the working tree, classified by disposition and remediability.

#### violations-contract-toolkit

The fingerprint-set of all unsuppressed FAILING/WARNING K-series results in the
current working tree, as reported by `suite.runner --series K`.

All K-series rules are WARN-tier **except K003, K009, K011, and K012 (BLOCK)**.
So in **default** mode this facet is empty *unless* a K003 (pin/lock drift),
K009 (unresolved scaffold placeholder), K011 (missing `app_id`), or K012
(missing `generate` poe task) finding is present — those are FAILING results
that fail the gate and must be remediated in default mode.  In **strict** mode
the fingerprint-set also includes the unsuppressed WARNING results
(K004/K005/K007/K008/K010/K014), which is where the rest of K-series
remediation runs.

The active scope decides which rules can appear: K001–K014 are all `scope=APP`,
so they surface only on consumer app repos.  The runner auto-detects scope, so
the SDK repo sees 0 findings.

This facet's fingerprint moves when any K-series finding is resolved (fixed or
suppressed with justification) or when new ones appear.  An unchanged
fingerprint-set across loop iterations is the oscillation signal.

Postcondition (deterministic validator — never render-attested):

> `atlan-application-sdk-conformance detect --repo . --series K` exits 0
> (zero unsuppressed FAILING results).  In strict mode, additionally: the
> `atlan/summary.warning` count for K-series in the SARIF output is 0 (every
> K-series WARNING was cleared by a real fix or a justified suppression).

### Requires

- `scope` — repository root path (provided by the top-level responsibility at
  expansion time).
- `mode` — `"default"` or `"strict"` (propagated from the top-level entry).

### Continuity

Input-driven: re-render this node when any `contract/**/*.pkl` file, the Pkl lock
(`contract/PklProject`, `contract/PklProject.deps.json`), or a generated artifact
(`atlan.yaml`, `app.yaml`, `app/generated/**`) under `scope` changes.  In the
Claude Code skill path the skill caller re-invokes on demand.

### Execution

```prose
call detect-fix-recheck
  scope: scope
  series: "K"
  mode: mode
  max_attempts: 5
```

### Fix Prescription

_Read by `remediate-finding` when `finding.area == "contract-toolkit"`._

All K-series rules are **WARN-tier except K003, K009, K011, and K012 (BLOCK)** —
the WARN rules surface only under `--strict` mode, while K003 (pin/lock drift),
K009 (unresolved scaffold placeholder), K011 (missing `app_id`), and K012
(missing `generate` poe task) are FAILING results that must be remediated even
in default mode.
Before proposing any edit, read the actual lines around `finding.line` in
`finding.file`.  **Never hand-edit `atlan.yaml`, `app/generated/`, or any other
generated artifact directly** — those are outputs of `pkl eval`, and K004/K005
catch the staleness that hand-editing causes.  The *only* sanctioned way to
change a generated artifact is to regenerate it from the contract.  After every
edit to `contract/**/*.pkl`, `contract/PklProject`, or the Pkl lock (K001–K005,
K007–K011, K013), the `pkl-eval` gate runs `pkl eval` to verify the contract compiled
and regenerated cleanly.  **K006 and K012 are the exceptions**: K006's fix is a
plain Python edit and K012's is a `pyproject.toml` edit (both see below), neither
involving `.pkl` or generated artifacts, so both are verified by the standard
test-suite gate instead of `pkl-eval`.  **K014 is either**, decided per repo: the
contract route regenerates and is verified by `pkl-eval`; the hand-owned-atlan.yaml
route touches no `.pkl` and is verified by the test-suite gate.

The freshness rules (K003/K004/K005) are remediated by running a pkl command
(`pkl project resolve` and/or `pkl eval -m . contract/app.pkl`), so they are
**remediable only when the `pkl` toolchain is available** in the environment.
When `pkl` is not installed, the fix cannot be applied or gate-verified — route
the finding to **residue** with a note to regenerate locally (or let the
self-hosted Renovate runner's `postUpgradeTasks` / CI freshness gate handle it),
rather than hand-editing the artifact.

---

**K001 ContractAmendsLegacyModule** — the contract file's `amends` line points at
a legacy module (`NativeApp.pkl` or `NativeAppBundle.pkl`) instead of `App.pkl`.
`classification = "judgment"` (structural migration; no two contracts are
identical).  After applying the edit, the `pkl-eval` gate determines whether the
migration compiled.

*Procedure:*

1. **Change the amends line** to `amends "@app-contract-toolkit/App.pkl"` (for a
   package-dependency contract) or `amends "../../src/App.pkl"` (for the
   toolkit's own examples/tests).

2. **Fix the workflowType** — NativeApp.pkl used a `workflowType`
   (PascalCase) + optional `workflowTypeOverride` pair with automatic
   kebab-casing.  App.pkl accepts the verbatim string.  Take the string that the
   legacy contract would have emitted (apply PascalCase→kebab-case manually if
   needed), and set it as App.pkl's `workflowType`.  Omit `workflowType`
   entirely when it equals the `name` field (App.pkl defaults to kebab-casing
   `name`).  Drop `workflowTypeOverride` — it does not exist in App.pkl.

3. **Fix the connector field** — `NativeApp.pkl` required `connector` (it typed
   it as `Connectors.Type`).  `App.pkl` makes `connector` nullable; utility apps
   that have no connector type should set `connector = null` or omit the field.

4. **Remove NativeAppBundle wiring** (K001 on `NativeAppBundle.pkl`) — the
   bundle entrypoints should be collapsed into App.pkl's typed `entrypoints`
   block.  Each per-entrypoint contract file (`amends "@app-contract-toolkit/
   NativeApp.pkl"`) must also be migrated (K001 will fire for each file separately).

5. **Stage the `contract/*.pkl` edits** — do NOT run `pkl eval` manually; the
   `pkl-eval` gate does it and captures the result.

If `NativeAppBundle.pkl` is involved and the migration requires restructuring
multiple entrypoint files, `classification = "judgment"` and every migrated file
is individually residue-listed for human review.

---

**K002 LegacyContractApi** — the contract file carries NativeApp-only knobs or
imports that do not exist in `App.pkl`.  `classification = "judgment"` (each
knob requires understanding the intent).

Read the specific knob(s) named in the `finding.message` and apply:

- **`flatManifestArgs`** and **`manifestMetadataArgs`** — remove both properties.
  `App.pkl` always emits flat top-level args; these flags are structural no-ops.

- **`workflowTypeOverride`** — see K001 step 2 above.  Remove the property after
  resolving it into the `workflowType` field.

- **`import "…Config.pkl"`** — remove the import.  Switch any `Config.*` widget
  references to the re-exported `Widgets.*` typealiases provided by `App.pkl`
  directly (e.g. `Config.UIConfig` → `UIConfig`, `Config.TextInput` →
  `TextInput`).

- **`import "…Credential.pkl"`** (Argo-era) and **`import "…Renderers.pkl"`**
  (Argo-era) — remove both; these modules are no longer used by App.pkl.

  (Note: `import "…Connectors.pkl"` is NOT a K002 finding — the Connectors
  registry is still imported explicitly by App.pkl consumers, so it is not
  flagged and needs no fix.)

If the contract file also has a K001 finding (amends a legacy module), address
K001 first — many K002 knobs disappear automatically when the module changes
because App.pkl lacks those properties.

---

**K003 ContractLockDrift** — `contract/PklProject` pins a dependency at a version
the resolved lock `contract/PklProject.deps.json` does not match (or the lock is
missing / lacks the dependency).  `classification = "mechanical"` (the fix is a
deterministic re-resolve), but it **requires `pkl`**.

*Procedure:*

1. Run `pkl project resolve` in the `contract/` directory to rewrite
   `PklProject.deps.json` so the lock matches the pin.
2. Run `pkl eval -m . contract/app.pkl` (or `uv run poe generate` where defined)
   so the regenerated artifacts reflect the newly-locked toolkit version.
3. Stage `contract/PklProject.deps.json` and every regenerated artifact.
4. Set `touched_files` to the union of every path reported changed by
   `git status --porcelain -- contract/ app/generated/ atlan.yaml app.yaml`,
   diffed before step 1 and after step 3 — see the write-scope note in
   `remediate-finding.prose.md` for why this determinism argument (read off
   a tool's own output, never model-judged) applies uniformly here and to
   `bootstrap`'s stdout-prefix extraction for C002. It is what lets
   `detect-fix-recheck` revert the lock plus every regenerated artifact, not
   just `finding.file`, if the `pkl-eval` gate rejects this fix.

If `pkl` is unavailable, do not hand-edit the lock JSON — route to residue with a
note to re-resolve locally (the self-hosted Renovate runner does this
automatically on renovate bumps, via `postUpgradeTasks`).

---

**K004 MissingGeneratedArtifact** — `contract/app.pkl` exists but an expected
output (`atlan.yaml`, `app/generated/manifest.json`, `app/generated/_input.py`)
is absent.  `classification = "mechanical"`; **requires `pkl`**.

*Procedure:*

1. Run `pkl eval -m . contract/app.pkl` (or `uv run poe generate`) to regenerate
   all outputs, then stage the newly-produced files.
2. Set `touched_files` the same deterministic way as K003 step 4 — the union
   of paths `git status --porcelain -- contract/ app/generated/ atlan.yaml
   app.yaml` reports changed, diffed before step 1 and after staging — so a
   `pkl-eval` gate rejection reverts every regenerated artifact this
   invocation produced, not just `finding.file`.
3. If an output is legitimately not produced for this app (e.g. a utility app
   that emits no manifest), the finding is `classification = "judgment"` —
   suppress with `// conformance: ignore[K004] <reason>` on the `amends` line of
   `contract/app.pkl` and route to residue.

If `pkl` is unavailable, route to residue with a regenerate-locally note.

---

**K005 GeneratedArtifactBannerStripped** — a generated text artifact
(`atlan.yaml` / `app.yaml` / an `app/generated/*.py` other than `__init__.py`) is
missing its `DO NOT EDIT` provenance banner, indicating a hand-edit.
`classification = "judgment"` — a stripped banner usually means deliberate
hand-authoring, which the static scanner cannot distinguish from an accidental
edit.

*Procedure:*

1. If the artifact should be generated, run `pkl eval -m . contract/app.pkl` to
   re-emit it with its banner, then stage the result.  **Do not** re-add the
   banner by hand to a file whose *content* was hand-edited — that hides real
   drift from the CI regenerate-and-diff freshness gate.
2. If the app deliberately hand-maintains this artifact, suppress with
   `# conformance: ignore[K005] <reason>` on the first line and route to residue.

K005 never graduates to BLOCK; a hand-maintaining app suppresses per file.

---

**K006 ManifestContractFieldMismatch** — a `$.extract.outputs.<field>` reference
in a committed `app/generated/**/manifest.json` DAG node names a field the
entrypoint's Python `Output` contract does not declare (directly, or via an
inherited base/mixin).  `classification = "mechanical"` when the fix is
"mix in the SDK contract base that already supplies this field" (see below);
`"judgment"` when it requires deciding what value the app itself should
compute and declaring a new field by hand.  **Does not require `pkl`** — this
is a pure Python edit, verified by the test-suite gate.

`finding.message` names the missing field(s), the manifest path, and the
Output contract class. Read the actual `Output` class at `finding.line` in
`finding.file` before proposing an edit.

*Procedure:*

1. **Check whether an SDK contract mixin already supplies the field(s)** named
   in the finding — most commonly `application_sdk.contracts.base.PublishInputMixin`
   for `connection_qualified_name`, `transformed_data_prefix`,
   `publish_state_prefix`, and `current_state_prefix` (the publish-pipeline
   fields; it also derives their values correctly from
   `connection_qualified_name`, so hand-declaring them independently is both
   unnecessary and a second place to drift). If so, add the mixin to the
   `Output` class's base classes — `classification = "mechanical"`.
2. **Otherwise, declare the missing field(s) directly** on the `Output` model
   with an appropriate type and a sensible default, and set its value wherever
   the class is constructed. `classification = "judgment"` — the fix requires
   understanding what the field should actually contain.
3. **Do not edit `app/generated/manifest.json`** to work around the finding —
   it is a `pkl eval` output, not the source of truth for what fields exist. If
   the referenced field is genuinely not needed (the pipeline step that
   consumes it should not be enabled for this app), the real fix is in
   `contract/app.pkl` (e.g. `pipeline.publish = null`) followed by
   `pkl eval -m . contract/app.pkl` — that is a **K001/K002-style contract
   change**, not a K006 fix; route it there instead if this is the actual
   intent.
4. **Verification is the standard test-suite gate**, not `pkl-eval` — a plain
   `uv run pytest` (or the app's configured test command) after staging the
   Python edit. Re-running `atlan-application-sdk-conformance detect --series K`
   also directly re-checks the fixed field is now visible (including through
   the mixin's inheritance chain).
5. If the mismatch is understood and deliberately deferred (e.g. the pipeline
   step is being removed in a separate, already-tracked change), suppress with
   `# conformance: ignore[K006] <reason>` on the `Output` class definition (or
   the comment-only line directly above it) and route to residue.

---

**K007 ToolkitVersionOutdated** — the `app-contract-toolkit` dependency in
`contract/PklProject` resolves (per the lock) to a version below the latest
published one.  `classification = "mechanical"`; **requires `pkl`**.

*Procedure:*

1. Bump the `@<version>` on the `app-contract-toolkit` `uri` line in
   `contract/PklProject` to the latest published version (the finding message
   names it).
2. Run `pkl project resolve` to refresh `contract/PklProject.deps.json`, then
   `pkl eval -m . contract/app.pkl` to regenerate the artifacts against the new
   toolkit.  Do NOT run `pkl eval` manually to fix `touched_files` — the
   `pkl-eval` gate does it; set `touched_files` the deterministic way described
   in K003 step 4 (the union of paths `git status --porcelain` reports changed).
3. If the lag is deliberate, suppress with `// conformance: ignore[K007]
   <reason>` on the `uri` line and route to residue.

If `pkl` is unavailable, route to residue with a bump-and-resolve-locally note
(the self-hosted Renovate runner does this automatically on renovate bumps, via
`postUpgradeTasks`).

---

**K008 ToolkitSourceNonCanonical** — the `app-contract-toolkit` dependency is
pointed at a base URI other than the canonical SDK-published package.
`classification = "mechanical"` when the fix is a straight re-point;
`"judgment"` when the fork is deliberate.  **requires `pkl`**.

*Procedure:*

1. Change the dependency `uri` in `contract/PklProject` to the canonical
   package base named in the finding message
   (`package://atlanhq.github.io/application-sdk/contracts/app-contract-toolkit@<latest>`).
2. Run `pkl project resolve`, then `pkl eval -m . contract/app.pkl`; set
   `touched_files` the K003-step-4 way.
3. If the app deliberately consumes a fork (rare — e.g. an in-flight toolkit
   change under review), `classification = "judgment"`: suppress with
   `// conformance: ignore[K008] <reason>` on the `uri` line and route to residue.

If `pkl` is unavailable, route to residue.

---

**K009 UnresolvedScaffoldPlaceholder** — a committed generated artifact
(`atlan.yaml`, `app.yaml`, or a file under `app/generated/`) still contains a
single-brace scaffold token (`{app_name}`, `{name}`, …).  **This is a BLOCK-tier
finding** — it fails the gate in default mode, because the current toolkit
renders these tokens to literals, so the artifact is wrong as shipped.
`classification = "judgment"` — the leftover is a symptom of a deeper cause
(outdated toolkit, never instantiated, or a legacy `NativeApp.pkl` base), so the
real fix is upstream, not a text substitution.  **requires `pkl`**.

*Procedure:*

1. **Upgrade the toolkit to the latest version first.** A leftover `{app_name}`
   almost always means the app is on an outdated `app-contract-toolkit` (a K007
   finding is usually present alongside): bump the `@<version>` in
   `contract/PklProject` to the latest and `pkl project resolve`. If the contract
   still amends `NativeApp.pkl` (K001), migrate to `App.pkl` first.
   **Never hand-edit the artifact** to delete or fill the placeholder — it is a
   `pkl eval` output; the token must disappear because the toolkit now renders it.
2. Regenerate with `pkl eval -m . contract/app.pkl` and stage the result; set
   `touched_files` the K003-step-4 way so a gate rejection reverts every
   regenerated artifact.  Confirm no `{...}` scaffold token remains.
3. `{{…}}` double-brace tokens are intentional E2E runtime substitutions and are
   never flagged — if one appears in a finding, that is a bug in the rule, not a
   fix target.
4. A placeholder in a `.json` output has no comment syntax to carry an inline
   suppression, so it cannot be suppressed in place — it must be regenerated (or
   the root-cause rule suppressed).  For a text artifact, suppress with
   `# conformance: ignore[K009] <reason>` on the placeholder line and route to
   residue.

If `pkl` is unavailable, route to residue with a regenerate-locally note.

---

**K010 E2EScaffoldingMissing** — a single-entrypoint `contract/app.pkl` exists
but the generated `app/generated/_e2e_base.py` the toolkit always emits for it is
absent.  `classification = "mechanical"`; **requires `pkl`**.

*Procedure:*

1. Run `pkl eval -m . contract/app.pkl` to regenerate all outputs (including
   `_e2e_base.py`), then stage them; set `touched_files` the K003-step-4 way.
2. If the app legitimately ships no E2E scaffolding, `classification =
   "judgment"`: suppress with `// conformance: ignore[K010] <reason>` on the
   `amends` line of `contract/app.pkl` and route to residue.

If `pkl` is unavailable, route to residue with a regenerate-locally note.

---

**K011 AppIdMissingFromContract** — the generated `atlan.yaml` has no top-level
`app_id`.  **This is a BLOCK-tier finding** — it fails the gate in default mode,
because without `app_id` the marketplace publish POSTs an empty identity and the
Global Marketplace returns 404, so the release is cut but never appears.
`classification = "judgment"` — the fix needs the app's correct GM UUID, which
is not derivable from the repo alone.  **requires `pkl`** (the value belongs in
the pkl source, and the artifact is regenerated, never hand-edited).

*Procedure:*

1. Recover the app's UUID.  It is stable per app: read it from the Global
   Marketplace admin UI (the app URL is `/admin/#/apps/<app_id>/versions`), from
   the `app_id` line of a prior successful publish log, or from git history of
   `atlan.yaml` before it was dropped (`git log -S 'app_id' -- atlan.yaml`).
2. Add it to the `metadata` block in `contract/app.pkl` — its entries are
   emitted as top-level `atlan.yaml` keys, exactly as `release_model` already is:

   ```
   metadata {
     ["release_model"] = "semver"
     ["app_id"] = "<your-app-uuid>"
   }
   ```
3. Regenerate with `pkl eval -m . contract/app.pkl` (or `uv run poe generate`)
   and stage the result; set `touched_files` the K003-step-4 way so a gate
   rejection reverts every regenerated artifact.  Confirm `atlan.yaml` now
   carries `app_id:`.  **Never hand-edit `atlan.yaml`** — it is a `pkl eval`
   output (K005 guards its provenance banner).
4. Suppression is almost never correct — a semver app with no `app_id` cannot
   publish.  Only for a non-published app that still ships an `atlan.yaml`:
   `# conformance: ignore[K011] <reason>` on the first line and route to residue.

If the UUID cannot be recovered or `pkl` is unavailable, route to residue with a
note naming what to look up.

---

**K012 GeneratePoeTaskMissing** — `pyproject.toml` defines no
`[tool.poe.tasks.generate]` task, so the SDK Certify step's `uv run poe generate`
aborts the publish (`Unrecognized task 'generate'`).  **This is a BLOCK-tier
finding**.  `classification = "mechanical"`; **does not require `pkl`** (it is a
`pyproject.toml` edit).

*Procedure:*

1. Read the repo's `Makefile` `generate` target (or the `pkl eval` invocation the
   app already uses to regenerate) and mirror it as a poe task:

   ```
   [tool.poe.tasks]
   generate = "pkl eval --project-dir contract -m . contract/app.pkl"
   ```

   Include every `.pkl` the `Makefile` evaluates (e.g. a credential contract such
   as `contract/csa-connectors-objectstore.pkl`) so `poe generate` and
   `make generate` stay equivalent.
2. Verify with `uv run poe generate`: it must succeed and leave the generated
   tree unchanged.  Stage `pyproject.toml` (and any regenerated artifacts).
3. Suppress only for an app never published through the marketplace pipeline:
   `# conformance: ignore[K012] <reason>` on the `[tool.poe.tasks]` header line
   and route to residue.

---

**K013 ManifestNodeAppNameMisattributed** — a DAG node in a committed generated
`manifest.json` declares an `app_name` that disagrees with the app running it,
established by either its toolkit-owned `workflow_type` (paired with the raw
`DAGNode` default `automation-engine`) or a `task_queue` naming a known system
app.  The node's logs are written under one identity and read back under another,
so the step shows no logs in the Workflow Center (CNCT-24).
`classification = "judgment"` — choosing between the built-in node class and an
explicit `appName` depends on why the node was hand-written — and it **requires
`pkl`**, since the finding only clears once the contract is regenerated.

The finding is anchored on the generated `manifest.json`, but **the fix is never
in that file** — it is in `contract/app.pkl`.  There is no suppression path: JSON
carries no comment syntax, so a legitimate exception must be routed to residue.

*Procedure:*

1. Locate the offending node in `contract/app.pkl` — the `finding.message` names
   its DAG node id (e.g. `extraNodes["qi"]`) and its `workflow_type`.
2. **Prefer the built-in node class.**  Replace `new DAGNode { … }` with the
   matching class — `QueryIntelligenceNode`, `PublishNode`, `LineageNode`,
   `PopularityNode`, `NotificationNode` — which sets `appName` *and* the
   consistent `taskQueue` together, and carries the typed args for that workflow.
   This also removes the hand-written `workflowType` line.
3. **Only when the node must stay a raw `DAGNode`** — typically because it pins a
   task queue the built-in class would not emit — set `appName` explicitly to the
   app named in the `finding.message` (e.g. `appName = "query-intelligence"`).
   Leave the pinned `taskQueue` alone: the rule is about log identity, not
   routing, and silently re-deriving a live queue would move where the node runs.
4. Do NOT run `pkl eval` manually — stage the `contract/app.pkl` edit; the
   `pkl-eval` gate regenerates and captures the result.
5. Set `touched_files` to the union of every path reported changed by
   `git status --porcelain -- contract/ app/generated/`, diffed before step 2 and
   after step 4, so a rejected fix reverts the contract *and* every regenerated
   artifact (same determinism argument as K003 step 4).

There is no toolkit-side fix to fall back on: the toolkit renders what the
contract declares, deliberately — silently correcting an author-set `appName`
would make the contract text stop matching its generated output.  The contract is
the only place this is fixable.  If `pkl` is unavailable, route to residue with a
note to regenerate locally; never hand-edit `manifest.json`.

---

**K014 ReleaseModelUndeclared** — `atlan.yaml` declares no top-level
`release_model`, or declares an invalid/deprecated value.  A missing key is read
as `cd`, so the app auto-publishes to `channel='all'` on every merge without
anyone having chosen that.  **WARN-tier** (strict mode only).
`classification = "judgment"` — the *value* is a release-policy decision owned by
the app team, not derivable from the repo.

**The rule takes no side between `cd` and `semver`.**  Never silently pick
`semver` for an app that has not asked for it: switching an app to `semver` stops
merges publishing to tenants, so an app whose team is not yet cutting GitHub
Releases would silently lose its delivery path.  An invalid value is mechanical
(correct it to the intended model); a *missing* value needs the owner's intent, so
prefer residue with the question unless the intent is already recorded in the repo
(e.g. a release policy doc, or the contract already declaring it — see step 1).

*Procedure:*

1. **Check the contract first.**  If `contract/app.pkl` already declares
   `["release_model"]` and only the committed `atlan.yaml` lacks the rendered key,
   the intent is settled: regenerate (step 3) and stop.  This is a pure staleness
   fix with no policy decision.
2. **Determine where the key belongs — by observation, not inference.**  Run
   `pkl eval -m <tmpdir> contract/app.pkl` and check whether `atlan.yaml` appears
   in the output.

   **Do not** infer this from the presence of `contract/app.pkl`, nor from which
   template the contract `amends`.  Both are unreliable: most contracts do *not*
   emit `atlan.yaml`, and a `NativeApp.pkl` contract can still emit one through
   its own `additionalOutputFiles` block.  Getting this backwards is itself the
   bug — putting the key in a generated `atlan.yaml` means the next toolkit bump
   silently reverts it.

   - **The eval emits `atlan.yaml`** → the file is generated.  Declare the key in
     the pkl `metadata` mapping (the same untyped hatch K011 uses for `app_id`):

     ```
     metadata {
       ["release_model"] = "semver"
     }
     ```

     If the contract instead builds `atlan.yaml` itself via an inline
     `additionalOutputFiles["atlan.yaml"]` mapping, add the key inside *that*
     mapping — a top-level `metadata` block will not reach it.  **requires `pkl`.**
   - **The eval does not emit `atlan.yaml`** → the file is hand-owned.  Add
     `release_model:` to it directly, near `build_tag:`.  Does **not** require
     `pkl`, and no regeneration step applies.
3. If the contract route was taken, regenerate with `pkl eval -m . contract/app.pkl`
   (or `uv run poe generate`) and set `touched_files` the K003-step-4 way so a gate
   rejection reverts every regenerated artifact.  **Never hand-edit a generated
   `atlan.yaml`** — K005 guards its provenance banner.
4. **Abort to residue if the committed `atlan.yaml` is already drifted** from a
   fresh eval beyond the one key being added.  Regenerating a drifted file sweeps
   unrelated reversions into a fix labelled "declare release_model" — the same
   failure class K005 and the freshness rules exist to prevent.  Report the drift
   instead; it needs an owner decision per field.
5. Suppression (strict mode): `# conformance: ignore[K014] <reason>` on the first
   line of `atlan.yaml` or the line above the key.  Rarely right — declaring the
   value is one line.  Route to residue.

If `pkl` is unavailable, the emission test in step 2 cannot be run; route to
residue rather than guessing which file to edit.

---

**Suppress outcome (strict mode only, WARNING-tier findings)**: the model may
propose an inline suppression comment — `// conformance: ignore[Kxxx]
<8–40 word justification>` for the `.pkl`-source / `PklProject`-anchored rules
(K001–K005, K007, K008, K010; `//` comments) or `# conformance: ignore[Kxxx]
<8–40 word justification>` for the artifact/Python-anchored rules (K006 Python,
K009 text artifact, K014 `atlan.yaml`; `#` comments) — on the violating line or
the comment-only
line directly above it when a legitimate exception exists (e.g. a K001 finding on
a contract intentionally kept at the legacy module during a phased migration with
a tracked follow-on ticket).  Route every suppression to residue for human audit.
