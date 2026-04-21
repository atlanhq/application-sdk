---
name: contract-review
description: Review changes in the app-contract-toolkit PKL repo end-to-end — validates widget dispatch, unbounded-flag coverage, workflowType/credential alignment, traces values from PKL to configmap JSON to manifest to _input.py, verifies regenerated output matches committed output, checks widget compatibility against the atlan-frontend registry, and checks docs completeness and semantic accuracy. Use when the user says "review this branch", "review this PR", "/review the contract", "check my PKL changes", or similar before opening/merging a PR in this repo.
---

# Contract Review

Specialized review for `app-contract-toolkit`. Catches the recurring failure patterns seen in past PRs and validates end-to-end contract alignment across PKL, generated JSON, the Python input class, and downstream consumers (atlan-frontend, Heracles, Automation Engine).

## Output rules (strict)

- No emojis anywhere. No status icons, checkmarks, warning glyphs, or lock/bolt icons. Use plain text only: `PASS`, `FAIL`, `OK`, `MISSING`, `CRITICAL`, `HIGH`, `MEDIUM`, `LOW`, `YES`, `NO`.
- Markdown tables only. Never Unicode box-drawing characters.
- Mermaid diagrams must be fenced with ```` ```mermaid ```` … ```` ``` ````.
- Use repo-relative paths in the report (`src/NativeApp.pkl:673`, not absolute paths).
- The normative project doc is `AGENTS.md`. `CLAUDE.md` is a sync copy — treat them as equivalent.

## Canonical skill location

This skill lives in two places: `.claude/skills/contract-review/` and `.agents/skills/contract-review/`. They are intentional duplicates so Claude Code and other agent runners both find the skill. Any edit to one must be mirrored to the other in the same commit.

## Flow

Work steps 0-7 in order. Use Bash, Read, Grep, Glob freely. Produce the report in Step 7. Do not narrate steps as you go.

---

## Step 0 — PR metadata and base branch

If the input is a PR URL or number, resolve it. Otherwise assume the current branch.

```bash
# If the user passed a PR URL/number:
PR_NUM=<extracted>
gh pr view "$PR_NUM" --repo <owner/repo> --json title,body,baseRefName,headRefName,url,mergeable,statusCheckRollup
gh pr checkout "$PR_NUM"

# Otherwise, infer base branch (default: main; trust gh view if available)
BASE_BRANCH="main"
```

Capture for later use in Step 6 and Step 7:

- `title` — validate against the prefixes the release workflow understands (per `AGENTS.md` section "Commit conventions"): `feat:` (minor bump), `fix:` / `chore:` / `docs:` / `ci:` (patch bump), `feat!:` or `BREAKING CHANGE` (minor while on 0.x, major otherwise). Other prefixes (`refactor:`, `perf:`, `test:`, etc.) are not documented for this repo — warn LOW and ask which bump the author intends.
- `body` — sanity-check against the actual diff in Step 1 (e.g., does the body claim a connector is "new" when it already existed on base?).
- `baseRefName` — use this as `$BASE_BRANCH` in every subsequent step. Do not hardcode `main`.
- `mergeable` / `statusCheckRollup` — mention in the report; do not duplicate what CI already surfaces.

---

## Step 1 — Establish scope via merge-base

Never use `git diff <base-branch>` unqualified. That conflates main-only drift with PR changes. Always compute the merge base and diff that range.

```bash
git fetch origin "$BASE_BRANCH"
BASE=$(git merge-base HEAD "origin/$BASE_BRANCH")
git diff --name-only "$BASE"..HEAD
git diff --stat "$BASE"..HEAD
git log --oneline "$BASE"..HEAD
```

**Rule: review only changes in `BASE..HEAD`.** Do not flag paths that only main touched after `$BASE` unless the PR itself modified those paths in `BASE..HEAD`. Main-only drift is not this PR's concern.

Classify each changed path:

- `src/*.pkl` — PKL source change, triggers full semantic review (Step 3)
- `examples/**/*.pkl` (non-generated) — example change, verify generated output stayed in sync (Step 2)
- `examples/**/generated/**` — generated output change, trace in Step 4
- `tests/**/*.pkl` — test change, run in Step 2
- `README.md`, `docs/reference.md`, `docs/index.html` — doc change
- `scripts/**`, `.github/**`, `PklProject` — tooling change, lighter review
- `AGENTS.md`, `CLAUDE.md` — normative doc change

**Narrow exit, not early exit.** If the changed set contains only CI/workflow files, keep the review but focus narrowly on CI security (action pins, permissions, injection-in-`run:`, secrets). Never skip a security-sensitive diff just because `src/` is untouched.

---

## Step 2 — Regenerate and invariants (smoke test)

**Pre-check: worktree must be clean before running the script.** If it isn't, stop and ask the user to stash or commit first — do not let the script mutate a dirty tree.

```bash
# Require clean state on changed scope
git status --short -- examples/ src/
```

If clean, regenerate and verify:

```bash
./scripts/regenerate-all.sh
git diff --stat -- examples/     # use diff, not status — git status can miss mode-only changes
./scripts/check-invariants.sh
```

Rules:

- If `git diff --stat -- examples/` is non-empty after regenerating, flag HIGH: "generated output is stale; PR forgot to commit regenerated output. Run `./scripts/regenerate-all.sh` and commit."
- If `check-invariants.sh` exits non-zero, flag HIGH per failing check. The four invariants are: `allow_unbounded_fields=True` on classes using `dict[str, Any]`, valid Python syntax, sibling `__init__.py`, valid JSON output.

**Run changed PKL tests.** If any `tests/**/*.pkl` file was added or modified in `BASE..HEAD`:

```bash
git diff --name-only "$BASE"..HEAD -- 'tests/**/*.pkl' | while read -r t; do
  pkl test "$t"
done
```

Any test failure is HIGH — tests reflect the PR author's stated invariants.

**Do not blindly reset** `examples/` with `git checkout --`. If regen produced an unintended diff, report it and stop. Only reset if the worktree was clean before Step 2.

---

## Step 3 — Semantic PKL review

Only for `src/*.pkl` and `examples/**/*.pkl` changed in `BASE..HEAD`. Read each fully before judging.

### 3a. Widget dispatch coverage — new vs pre-existing

Locate the dispatcher:

```bash
grep -n "^local function getInputPyField" src/NativeApp.pkl
grep -oE "u is Config\.[A-Za-z]+" src/NativeApp.pkl | sort -u   # currently-handled widgets
```

For each changed example PKL, collect widget classes used:

```bash
grep -oE "new Config\.[A-Za-z]+" <example>.pkl | sort -u
```

Split findings by provenance (this is the key refinement):

- **HIGH** — widget introduced by *this PR* (either a new `Config.*` class added in `src/Config.pkl`, or a first use in a changed example) and missing from the dispatcher. Silent fallthrough to `str = ""` is invisible to review and must be made explicit.
- **LOW note** — widget already used on `$BASE_BRANCH` by other examples, still falling through. Pre-existing dispatcher debt; worth a follow-up cleanup, not a merge blocker for this PR.
- **Escalation trigger** — if the PR modifies `getInputPyField` or `needsUnboundedFlag`, every widget falling through becomes in-scope again; bump all to HIGH for this PR.

To determine provenance:

```bash
# "Is this Config class new in BASE..HEAD?"
git diff "$BASE"..HEAD -- src/Config.pkl | grep -E "^\+class "

# "Was this widget used anywhere on the base branch?"
git grep "new Config\.Sage" "origin/$BASE_BRANCH" -- 'examples/**/*.pkl'
```

### 3b. Unbounded-flag coverage (HIGH if gap)

Every widget whose `getInputPyField` branch emits `dict[str, Any]` must appear in the `needsUnboundedFlag` predicate:

```bash
grep -n "needsUnboundedFlag" src/NativeApp.pkl
```

Mismatch means `_input.py` will lack `allow_unbounded_fields=True`. `check-invariants.sh` catches the symptom; flag the root cause in `src/NativeApp.pkl`.

### 3c. `workflowType` / `workflowTypeOverride` (CRITICAL if both empty)

For each changed app PKL, grep both fields. At least one must be non-empty. `workflowType` converts PascalCase to kebab-case; `workflowTypeOverride` is verbatim (often used for legacy parity).

### 3d. `ErrorHandlingConfig` rules (HIGH if violated)

PKL invariants on `DAGNode` and `ErrorHandlingConfig` enforce numeric ranges, so a file that compiles has valid numbers. The remaining traps:

- `heartbeatTimeoutSeconds` is valid only on `nodeType = "activity"`. Set on a workflow-type node, flag HIGH.
- `heartbeatTimeoutSeconds` must be less than `startToCloseTimeoutSeconds`. PKL catches this via `_timeoutOrderingCheck`.

**Scope: only apply 3d when the PR touches `extraNodes`, `errorHandling`, `manifest`-generation code, or defines custom `DAGNode` subclasses. Otherwise skip — adding noise to unrelated PRs is worse than missing an edge case.**

### 3e. `activity_name = "execute_workflow"` auto-infers `nodeType = WORKFLOW`

AE's `_infer_node_type` (`registry/models.py`, see Consumer anchors) auto-sets `node_type = WORKFLOW` when `activity_name == "execute_workflow"`, overriding whatever the PKL declared. So a PKL setting `nodeType = "activity"` while the rendered manifest will carry `activity_name = "execute_workflow"` will pass PKL's `_errorHandlingCheck` but still be rejected by AE if `heartbeat_timeout_seconds` is set.

Flag MEDIUM when you see this combination on a changed node. Scope: same as 3d — only when touching manifest-generation code.

### 3f. `depends_on.node_id` must reference an existing node (HIGH if broken)

AE's `_validate_node_references` (`workflows/dag_validation.py`, see Consumer anchors) raises `DAGValidationErrorCode.INVALID_NODE_REFERENCE` for `depends_on.node_id` pointing at a node not present in the DAG. PKL does not catch typos here.

Check every `dependsOn` target in changed PKL against the set of DAG keys (default `extract` + `publish` plus any `extraNodes` keys).

### 3g. Multi-manifest credential sharing (MEDIUM if mismatch)

If an example directory has multiple PKL files (e.g. `teradata/crawler.pkl` and `teradata/miner.pkl`):

- `connectorConfigName` must match if they share the credential form — MEDIUM if not.
- `taskQueuePrefix` may intentionally differ (separate queues) or match (shared). State which, do not flag.

### 3h. `credentialFieldName` consistency (MEDIUM if mismatch)

The PKL's `credentialFieldName` (or default `{name.replace('-', '_')}_credential`) must match the `CredentialRef | None` field name emitted in `_input.py`. Read both to confirm.

---

## Step 4 — End-to-end value flow trace

For each modified example, read three files. **Note the paths — single-manifest and multi-manifest differ:**

Single-manifest:
```
examples/<name>/generated/<name>.json          # configmap
examples/<name>/generated/manifest.json
examples/<name>/generated/_input.py
```

Multi-manifest (e.g. teradata):
```
examples/<name>/generated/<manifest>/<manifest>.json
examples/<name>/generated/<manifest>/manifest.json
examples/<name>/generated/<manifest>/_input.py
```

If `src/NativeApp.pkl` changed, trace every example, not just modified ones. Dispatcher changes affect all.

Build a table of configmap fields → manifest placeholders → `_input.py` fields. Use the inlined widget→Python type mapping below for expected types.

### Flag rules

- **Configmap field with no matching `{{param}}` in the manifest — HIGH.** User input goes nowhere; the app never receives it.
- **Manifest `{{param}}` with no configmap counterpart — tiered severity.** Heracles's `stripUnresolvedTemplateVars` (`handler/workflow.go`, see Consumer anchors) *silently deletes* unresolved keys before handing the DAG to AE. The severity depends on how the app uses the field:
  - HIGH if the field is user-controlled or required by the app runtime — its absence will crash or misbehave.
  - MEDIUM if the field is clearly optional, defaulted in `_input.py`, or dead code.
  - Default to HIGH if unsure; leave a note asking the PR author to confirm.
- **`_input.py` type does not match widget (per the inlined table below) — HIGH.**
- **`credentialFieldName` mismatch — MEDIUM** (also caught in 3h; do not double-count).
- **Heracles substitution gotcha (informational):** Heracles uses string replacement, not regex (see `substituteValue` in `handler/workflow.go`). A value that is *exactly* `{{param}}` preserves the underlying type (dict/list/bool); `"prefix_{{param}}_suffix"` stringifies. Note this in the report if the manifest mixes literal content with placeholders.
- **`credential` vs `credential_guid` in the manifest (informational, not a smell):** The current `NativeApp.pkl` intentionally emits both `["credential"] = "{{credential}}"` (line 673) and `["credential_guid"] = "{{credential-guid}}"` (via `manifestTopLevelArgs`, line 311). Heracles upserts the credential body to StateStore via `UpsertCredentialConfig` at the same time. Both keys coexisting in a native-app manifest is normal — do not flag. Only flag MEDIUM if a native-app manifest emits `{{credential}}` *without* `{{credential-guid}}` — that's an Argo-legacy remnant where the app will only get the body and not the GUID.

---

## Step 5 — UI widget compatibility

Collect widget values from every changed configmap JSON in `BASE..HEAD`:

```bash
grep -hoE '"widget":\s*"[^"]+"' \
  $(git diff --name-only "$BASE"..HEAD -- 'examples/**/generated/*.json') | sort -u
```

Cross-check each against the inlined "confirmed widgets" table below:

- In table → `CONFIRMED`
- Not in table, but found in other committed generated JSON on `$BASE_BRANCH` → `CONFIRMED (precedent)` (cite the example)
- Otherwise → `UNCONFIRMED` — note "verify with #app-framework / atlan-frontend before merge"

---

## Step 5.5 — Cross-repo consumer validation (conditional)

Three downstream consumers parse the artifacts this toolkit generates. Validate when their checkouts are available locally. Skip with a report note if not.

```bash
for repo in ../atlan-frontend ../heracles ../atlan-automation-engine-app; do
  [ -d "$repo" ] && echo "FOUND: $repo" || echo "MISSING: $repo"
done
```

If any is missing, emit this row in the Report section: `Cross-repo check (<repo>): SKIPPED — repo not present in parent dir. To enable: git clone git@github.com:atlanhq/<repo>.git ../<repo>`.

When a repo is present, run the targeted check below using the anchors in the "Consumer anchors" section at the bottom of this file.

### Frontend (atlan-frontend, branch: `beta`)

For every *new* widget string introduced by this PR (detected in Step 5), grep the frontend registry:

```bash
grep -n '"<widget>"' ../atlan-frontend/src/workflowsv2/components/dynamicForm2/formBlock.vue
```

If the widget string is absent, flag HIGH: "frontend will render as unknown widget". If present, mark CONFIRMED against the `formBlock.vue` `componentName` registry (see Consumer anchors).

For any new compound-filter feature (`Config.Condition.filter`), verify `getMatchingCondition.ts` (see Consumer anchors) still uses `sift` — the `sift` library handles `$and`/`$or` natively. If the PR introduces a filter operator not in `sift`'s MongoDB subset, flag HIGH.

### Heracles (heracles, branch: `beta`)

Mostly informational. Use the anchors to confirm:

- Manifest placeholders are substituted by `substituteTemplateVars` / `substituteValue` (see Consumer anchors) — string-based, exact-vs-partial match semantics described in Step 4.
- Orphans get silently dropped by `stripUnresolvedTemplateVars` (see Consumer anchors).
- Credential body is upserted to StateStore by `UpsertCredentialConfig` (see Consumer anchors), not the DAG.

### Automation Engine (atlan-automation-engine-app, branch: `main`)

For changed manifests or `extraNodes` modifications:

- Open `registry/models.py` (`ErrorHandlingConfig`, `DAGNode` — see Consumer anchors). Confirm fields emitted by the PKL are accepted by the pydantic model (extra fields will be rejected).
- Open `workflows/dag_validation.py` (`_validate_node_references` — see Consumer anchors). Confirm every `dependsOn` target in the rendered manifest exists as a DAG key.
- If timeouts changed: values within `ErrorHandlingConfig`'s ranges (`1..86400` for start-to-close, `1..3600` for heartbeat). Already enforced PKL-side for compile-time values, but the AE code is the source of truth for the ranges.

---

## Step 6 — Docs and commit convention

### Docs accuracy (not just presence)

When `src/*.pkl` changed in `BASE..HEAD`:

```bash
git diff "$BASE"..HEAD -- README.md docs/reference.md docs/index.html
```

Rules:

- If any of the three shows no diff while `src/` changed — MEDIUM, `<file> not updated when src/ changed`.
- For each *new PKL identifier* (class, property, constant) added in `BASE..HEAD`, grep all three docs. Missing identifier — MEDIUM with the specific item.
- **Verify semantic accuracy against source**, not just presence. Open the new doc section and compare field types, category values, default values against `src/`. Docs updated with the wrong values (seen before: SAP connectors documented as `warehouse` while source said `erp`, or the reverse) is MEDIUM.

### Commit convention

```bash
git log "$BASE"..HEAD --oneline
```

Only the prefixes defined in `AGENTS.md` "Commit conventions" get a version bump: `feat:` → minor, `fix:`/`chore:`/`docs:`/`ci:` → patch, `feat!:` or `BREAKING CHANGE` → minor on 0.x / major otherwise. Other prefixes (`refactor:`, `perf:`, `test:`, etc.) are not documented for this repo — warn LOW and ask which bump the author intends. Mismatched or undocumented prefixes still run CI but produce the wrong release tag (or none).

If `feat:` is used, confirm the PR genuinely adds a user-facing capability (not just refactoring). Misusing `feat:` leads to unwarranted minor-version bumps.

---

## Step 7 — Produce the report

No emojis. Plain-text markers. The report must be self-contained and PR-comment-ready (copy-paste into GitHub should render correctly).

### Rule 1 — Every issue must include a concrete Fix block

Not a description of the fix — the actual code change as a unified diff, a shell command, or a new snippet to paste. Use fenced code blocks with the correct language tag (`pkl`, `python`, `json`, `bash`, `diff`).

**Fix-path verification: before writing a Fix diff, confirm the target file exists on the PR branch:**

```bash
git cat-file -e HEAD:<path>       # exits non-zero if missing
```

If the target is only on `$BASE_BRANCH` (e.g., a sidecar file that doesn't exist on this PR branch), note the dependency ("apply after rebase") instead of writing an un-applicable diff.

### Rule 2 — Include a Mermaid sequence diagram when the change touches end-to-end flow

Required if any is true:

- `src/NativeApp.pkl` changed (dispatcher, rendering, or manifest-generation logic)
- A new widget class was added to `src/Config.pkl`
- A new `extraNodes` entry or manifest topology change was made
- Multi-manifest credential sharing changed (`connectorConfigName`, shared credential fields)

Otherwise omit the diagram. Do not add one for trivial example edits or doc-only PRs.

**The diagram must cite real file paths with line anchors** (from this repo and the sibling repos covered in Step 5.5), not generic abstractions like `User → Frontend → Heracles → AE → SDK → App`. Participants are files; each arrow is captioned with what the file does. If a segment cannot be cited because the sibling repo isn't checked out, omit that segment.

Template (adapt to actual change):

```mermaid
sequenceDiagram
    participant PKL as examples/<name>/app.pkl
    participant Eval as pkl eval
    participant Cfg as <name>.json
    participant Manifest as manifest.json
    participant Input as _input.py
    participant FE as formBlock.vue componentName
    participant Filter as getMatchingCondition.ts sift()
    participant Heracles as workflow.go substituteTemplateVars
    participant Strip as workflow.go stripUnresolvedTemplateVars
    participant AE as registry/atlan/models.py atlan_entity_to_workflow
    participant App as app runtime

    PKL->>Eval: amends NativeApp.pkl
    Eval->>Cfg: render configmap (widget dispatch)
    Eval->>Manifest: render DAG (placeholders {{<field>}})
    Eval->>Input: render AppInputContract
    FE->>FE: user fills form; widget renders <widget-string>
    FE->>Filter: evaluate conditions[].filter via sift
    FE->>Heracles: POST /workflows/v1/create with form values
    Heracles->>Heracles: substituteTemplateVars — string replace {{<field>}}
    Heracles->>Strip: stripUnresolvedTemplateVars — silent delete
    Heracles->>AE: POST resolved DAG to aeClient
    AE->>AE: atlan_entity_to_workflow → pydantic DAGNode
    AE->>App: spawn activity with typed args
```

### Rule 3 — Claim verification for merge hazards

Do not flag a "branch is behind main" or "merge will regress X" issue without producing the actual diff that shows PR touched paths that also changed on base:

```bash
git diff --name-only "$BASE"..HEAD > /tmp/pr_paths
git diff --name-only "$BASE"..origin/$BASE_BRANCH > /tmp/base_paths
comm -12 <(sort /tmp/pr_paths) <(sort /tmp/base_paths)
```

Empty intersection = not a hazard, do not flag. Non-empty = include the intersection in the Fix block.

### Report template

```
## Contract Review — <branch> @ <short-sha>

### Summary
<N issues: C critical, H high, M medium, L low>  or  "No issues found — ready to merge."

### End-to-end value flow
| Configmap field | Widget | Manifest placeholder | _input.py field | Python type | Match |
|---|---|---|---|---|---|

### Flow diagram
(omit if Rule 2 is not triggered)

### Issues

Issue (CRITICAL): <what>
Location: <repo-relative path>:<line>
Risk: <deployment-blocking consequence>
Fix:
```diff
<unified diff>
```

Issue (HIGH): <what>
Location: <repo-relative path>:<line>
Risk: <runtime consequence citing the consumer file>
Fix:
```diff
<unified diff>
```

Issue (MEDIUM): <what>
Location: <repo-relative path>:<line>
Risk: <review-time or follow-up consequence>
Fix:
```diff
<unified diff or exact snippet/command>
```

Note (LOW): <one-liner with inline fix>

### Widget compatibility
| Widget | Used in | Status | Frontend anchor |
|---|---|---|---|

### Cross-repo validation
| Consumer | Check | Result |
|---|---|---|
| atlan-frontend (beta) | widget registry | CONFIRMED / UNCONFIRMED / SKIPPED |
| heracles (beta) | manifest placeholders | CONFIRMED / SKIPPED |
| automation-engine (main) | DAG model | CONFIRMED / SKIPPED |

### Docs status (only if src/ changed)
| File | Updated | Accurate |
|---|---|---|
| README.md | YES/NO | YES/NO |
| docs/reference.md | YES/NO | YES/NO |
| docs/index.html | YES/NO | YES/NO |

### Pre-merge checklist
- [ ] ./scripts/regenerate-all.sh clean (no diff)
- [ ] ./scripts/check-invariants.sh passes
- [ ] pkl test on changed tests passes
- [ ] All three docs updated and semantically accurate (if src/ changed)
- [ ] Unconfirmed widgets verified with #app-framework
- [ ] Value-flow table shows no orphans or type mismatches
- [ ] Conventional commit title matches intended version bump
```

Produce only the report. Do not restate the steps.

---

## Reference tables (inlined)

### Widget → Python type mapping (from `getInputPyField`)

Source of truth: `src/NativeApp.pkl`, function `getInputPyField`. Derive live with:

```bash
grep -nE "u is Config\.[A-Za-z]+|baseWidgetType ==" src/NativeApp.pkl
```

| JSON signal | PKL class | Python type in `_input.py` |
|---|---|---|
| `input` | `Config.TextInput` | `str` |
| `textarea` | `Config.TextBoxInput` | `str` |
| `password` | `Config.PasswordInput` | `str` |
| `radio` | `Config.Radio` | `str` |
| `select` (single) | `Config.DropDown` (multiSelect=false) | `str` |
| `select` (multi) | `Config.DropDown` (multiSelect=true) | `Annotated[list[str], MaxItems(1000)]` |
| `credential` | `Config.CredentialInput` | `str` |
| `connectionSelector` | `Config.ConnectionSelector` | `str` |
| `boolean` | `Config.BooleanInput` | `bool` |
| `inputNumber` | `Config.NumericInput` | `int` |
| `date` | `Config.DateInput` | `int` |
| `sqltree` | `Config.SqlTree` | `Annotated[dict[str, Any], MaxItems(1000)]` |
| `apitree` | `Config.APITree` | `Annotated[dict[str, Any], MaxItems(1000)]` |
| `nested` | `Config.NestedInput` | `Annotated[dict[str, Any], MaxItems(1000)]` |
| `agent` | `Config.AgentSelector` | `Annotated[dict[str, Any], MaxItems(1000)]` |
| `type: "conditional"` + `ui.widget` in `{sqltree, apitree}` | `Config.ConditionalInput` | `Annotated[dict[str, Any], MaxItems(1000)]` |
| `type: "conditional"` + any other `ui.widget` | `Config.ConditionalInput` | `str` |
| `inputRepeater` | `Config.InputRepeater` | `Annotated[list[str], MaxItems(1000)]` |
| `tagsInput` | `Config.TagsInput` | `Annotated[list[str], MaxItems(1000)]` |
| `connection` | `Config.ConnectionCreator` | `ConnectionRef \| None` |
| `fileUpload` | `Config.FileUploader` | `FileReference \| None` |
| `sage` | `Config.Sage` | `str` (explicit branch post-v0.4.0; before that, fallthrough) |
| `sageV2` | `Config.SageV2` | `str` (explicit branch post-v0.4.0; before that, fallthrough) |
| `CloudProvider` | `Config.CloudProvider` | `str` (explicit branch) |

All types producing `dict[str, Any]` require the containing class to have `allow_unbounded_fields=True`. The `needsUnboundedFlag` predicate in `src/NativeApp.pkl` must list every one of them.

### Confirmed widget values

Widgets verified to dispatch cleanly in both the PKL toolkit and `atlan-frontend/src/workflowsv2/components/dynamicForm2/formBlock.vue`. Unknown widgets are not automatically broken — they need human confirmation via `#app-framework` or by grepping the frontend registry.

| Widget | Confirmed in examples |
|---|---|
| `input` | redshift, cosmos, mode, sap-s4 |
| `inputNumber` | redshift, sap-s4 |
| `password` | redshift, cosmos, sap-s4 |
| `textarea` | redshift |
| `radio` | redshift, cosmos, sap-s4 |
| `select` | monte-carlo, trino, sap-s4 |
| `boolean` | redshift, trino |
| `nested` | redshift, cosmos, sap-s4 |
| `credential` | redshift, cosmos, monte-carlo, sap-s4 |
| `connection` | redshift, sap-s4 |
| `connectionSelector` | monte-carlo |
| `sqltree` | redshift, trino |
| `apitree` | monte-carlo, sap-s4 |
| `inputRepeater` | cosmos |
| `sage` | monte-carlo, cosmos, sap-s4 |
| `sageV2` | redshift, mode, trino, teradata |
| `evaluate` | redshift |
| `agent` | teradata, sap-s4 |
| `CloudProvider` | sap-s4 |

### Severity rubric

| Severity | Criteria (contract-toolkit specific) | Header to use |
|---|---|---|
| CRITICAL | App will not deploy: empty `workflowType` and `workflowTypeOverride`, generated JSON invalid, auth/credential totally missing | `Issue (CRITICAL)` |
| HIGH | Contract break at runtime: new-in-PR widget dispatch gap, unbounded-flag miss, orphaned configmap field, manifest placeholder without source (user-controlled), Python type mismatch, stale generated output committed, new widget unsupported by frontend, `depends_on.node_id` targets missing node | `Issue (HIGH)` |
| MEDIUM | Review-time gaps: docs not updated / inaccurate when `src/` changed, multi-manifest `connectorConfigName` mismatch, `credentialFieldName` mismatch, unconfirmed widget without sign-off, optional-field manifest placeholder without source, native-app manifest emitting `{{credential}}` without `{{credential-guid}}` (Argo-legacy remnant), `nodeType` / `activity_name` auto-inference mismatch | `Issue (MEDIUM)` |
| LOW | Style/convention: non-conventional commit title, comment drift, unused imports in example, pre-existing dispatcher debt exposed (not introduced) by this PR | `Note (LOW)` |

Do not opt out of CRITICAL or HIGH. MEDIUM can ship as a follow-up with a tracking note. LOW is a one-liner.

---

## Consumer anchors (cross-repo, for Step 5.5)

Required sibling checkouts in parent dir (`../`):

- `atlan-frontend` on branch `beta`
- `heracles` on branch `beta`
- `atlan-automation-engine-app` on branch `main`

If any is missing, emit `SKIPPED` for that consumer and continue.

**Symbol is the primary anchor; line numbers are current as of 2026-04 and drift with every commit.** If the listed line does not match the listed symbol, grep the symbol (function/class name) to find the real location and cite *that* in the report. Do not guess.

### Frontend (atlan-frontend, `beta`)

```
src/workflowsv2/components/dynamicForm2/formBlock.vue
  ~line 273: components: { … } registry — widget-string → Vue component map
  ~line 434: const componentName = (property) => { … } — dispatch switch

src/workflowsv2/utils/getMatchingCondition.ts
  ~line 39: sift(condition.filter) — compound $and/$or evaluation (sift library)

src/api-vue/generated/heracles/configmaps/useGetConfigMapByName.ts
  ~line 8: useGetConfigMapByName — configmap fetcher wrapper hook

src/workflowsv2/components/dynamicForm2/widget/credential.vue
  credential form entry — consumes atlan-connectors-<name> configmap

src/workflowsv2/components/dynamicForm2/composables/utils.ts
  buildHeraclesCredentialBody — credential POST payload shape
```

### Heracles (heracles, `beta`)

```
handler/configmap.go
  ~line 91: GetConfigMapByName — proxies /workflows/v1/configmap/{name} to app SDK

pkg/app/client.go
  ~line 480: (*App).GetManifest — fetches /manifest from app

handler/workflow.go
  ~line 541: CreateWorkflow — POST /workflows, routes native vs Argo
  ~line 1848: UpsertCredentialConfig — stores credential body in StateStore (not DAG)
  ~line 1855: aeClient.CreateVersion — DAG handoff to AE
  ~line 1860: aeClient.PublishVersion — publishes the version
  ~line 1879: aeClient.SubmitWorkflow — submits, returns run_id
  ~line 1934: substituteTemplateVars — walks map/slice, calls substituteValue
  ~line 1946: substituteValue — string-based {{param}} substitution
             exact match preserves typed value; partial match stringifies
  ~line 1980: stripUnresolvedTemplateVars — SILENT DELETE of unresolved placeholders
```

### Automation Engine (atlan-automation-engine-app, `main`)

```
automation_engine/registry/atlan/models.py
  ~line 413: atlan_entity_to_workflow — manifest → DAGNode pydantic deserialization

automation_engine/registry/models.py
  ~line 247: class ErrorHandlingConfig — field ranges
  ~line 288: validate_timeouts — enforces heartbeat < start_to_close
  ~line 327: class DAGNode — required and optional fields
  ~line 406: _infer_node_type — auto-sets node_type=WORKFLOW when activity_name=="execute_workflow"
  ~line 420: _validate_error_handling_for_node_type — rejects heartbeat_timeout_seconds on workflow nodes

automation_engine/workflows/dag_validation.py
  ~line 448: _validate_node_references — depends_on.node_id existence check, INVALID_NODE_REFERENCE

automation_engine/workflows/workflow.py
  ~line 70: _resolve_jsonpath_value — $.node.outputs.* runtime resolution (no pre-validation)

  ~line 733: task_queue=node.app_task_queue — expects pre-substituted {deployment_name}
```

---

Produce only the report. Every Fix block must be directly applicable. Include the Mermaid diagram only when Rule 2 triggers.
