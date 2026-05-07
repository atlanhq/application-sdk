---
name: toolkit-feature-workflow
description: Use when adding, debugging, or validating a contract-toolkit feature or bug fix. Enforces issue tracking, API-design triage, cross-repo consumer validation against atlan-frontend, blaze playground, heracles, and automation-engine, examples/docs/tests, extended commits, PR triage notes, and independent review.
---

# Toolkit Feature Workflow

Use this before changing `contract-toolkit/src/`, examples, generated output, docs, or tests for any toolkit feature or bug fix.

## Working directory

This skill is installed at the SDK repo root. Treat `contract-toolkit/` as the toolkit root and run toolkit commands from that directory:

```bash
if [ -d contract-toolkit/src ]; then
  cd contract-toolkit
fi
```

All paths and commands below are toolkit-root-relative after that `cd`. If already running from a standalone toolkit checkout, stay in the current directory.

## Non-Negotiables

- Validate before coding whether the feature already exists, is only undocumented, or is a bad toolkit API design forcing app-level workarounds.
- Branch from latest `main`; keep the worktree clean before regeneration and before review.
- Track the work in an issue before implementation unless the user explicitly says not to. If no issue exists, create one or draft one with the exact fields below.
- Check downstream consumers on latest remote branches before deciding the API is valid. If a sibling repo is missing, clone it under `/tmp` and validate there; missing local checkouts are not a reason to skip consumer validation:
  - `../atlan-frontend`: `origin/beta`
  - `../heracles`: `origin/beta`
  - `../blaze`: `origin/main`
  - `../atlan-automation-engine-app`: `origin/main`
- Add or update a production-shaped example and committed generated output.
- Update all three docs when `src/` changes: `README.md`, `docs/reference.md`, `docs/index.html`.
- Add targeted tests for new behavior and backward compatibility.
- Use extended commit descriptions, not just one-line subjects.
- Put the design triage and validation record in the PR body.
- Run two independent review tracks after implementation: implementation correctness and API-design/codebase fit. Use subagents when explicitly authorized by the user/runtime; otherwise run both reviews sequentially and state that subagents were not available.

## 1. Start With Tracking

If the user gave a Linear/GitHub issue, read it first. Otherwise create or draft a tracking issue:

```text
Title:
Problem:
Current workaround:
Why toolkit API is insufficient:
Affected apps/examples:
Expected generated configmap change:
Expected generated manifest change:
Expected _input.py change:
Consumers to validate:
Backward compatibility:
Acceptance criteria:
```

Use the issue as the source of truth. Update it after PR creation and after merge.

## 2. Triage Before Coding

Search the toolkit first:

```bash
rg "<requested field|json key|node arg|widget|class>" src examples tests README.md docs
rg "<workaround key>" ../atlan-*-app/contract ../atlan-*-app/app/generated 2>/dev/null
```

Answer these in the PR body:

```text
Root cause:
Existing toolkit behavior:
Existing app workaround:
Is this already supported but undocumented?
Is this app-specific or generally reusable?
Correct API layer: app-level / node-level / field-level / widget-level / bundle-level
Backward compatibility plan:
Why this avoids raw DAGNode or post-render JSON patching:
```

Prefer these API placements:

- Built-in extract/publish behavior: app-level `NativeApp` knobs.
- Prebuilt reusable DAG behavior: typed node properties.
- Credential fields: `FieldSpec`.
- Workflow UI fields: `Config.pkl` widgets/properties.
- Multi-entrypoint behavior: `NativeAppBundle.pkl`.
- Avoid raw `DAGNode` unless the toolkit cannot model the behavior yet; that is usually the API gap to fix.

## 3. Downstream Consumer Validation

Fetch latest remotes first. Do not discard local dirty changes in sibling repos.
If a sibling checkout is missing, clone the repo into `/tmp/app-contract-toolkit-consumers/<repo>` and use that clone for read-only validation.

```bash
mkdir -p /tmp/app-contract-toolkit-consumers
[ -d ../atlan-frontend/.git ] || git clone https://github.com/atlanhq/atlan-frontend.git /tmp/app-contract-toolkit-consumers/atlan-frontend
[ -d ../heracles/.git ] || git clone https://github.com/atlanhq/heracles.git /tmp/app-contract-toolkit-consumers/heracles
[ -d ../blaze/.git ] || git clone https://github.com/atlanhq/blaze.git /tmp/app-contract-toolkit-consumers/blaze
[ -d ../atlan-automation-engine-app/.git ] || git clone https://github.com/atlanhq/atlan-automation-engine-app.git /tmp/app-contract-toolkit-consumers/atlan-automation-engine-app

FRONTEND_REPO=$([ -d ../atlan-frontend/.git ] && echo ../atlan-frontend || echo /tmp/app-contract-toolkit-consumers/atlan-frontend)
HERACLES_REPO=$([ -d ../heracles/.git ] && echo ../heracles || echo /tmp/app-contract-toolkit-consumers/heracles)
BLAZE_REPO=$([ -d ../blaze/.git ] && echo ../blaze || echo /tmp/app-contract-toolkit-consumers/blaze)
AE_REPO=$([ -d ../atlan-automation-engine-app/.git ] && echo ../atlan-automation-engine-app || echo /tmp/app-contract-toolkit-consumers/atlan-automation-engine-app)

git -C "$FRONTEND_REPO" fetch origin beta
git -C "$HERACLES_REPO" fetch origin beta
git -C "$BLAZE_REPO" fetch origin main
git -C "$AE_REPO" fetch origin main
```

Record each repo's current branch, dirty state, and fetched remote SHA:

```bash
git -C "$FRONTEND_REPO" status --short --branch
git -C "$FRONTEND_REPO" rev-parse origin/beta
git -C "$HERACLES_REPO" status --short --branch
git -C "$HERACLES_REPO" rev-parse origin/beta
git -C "$BLAZE_REPO" status --short --branch
git -C "$BLAZE_REPO" rev-parse origin/main
git -C "$AE_REPO" status --short --branch
git -C "$AE_REPO" rev-parse origin/main
```

Validate only the relevant consumer surfaces:

- Frontend: widget registry, hidden/default behavior, condition handling, configmap schema compatibility.
- Blaze playground: widget support and manifest payload substitution.
- Heracles: `{{param}}` substitution, unresolved placeholder stripping, credential/config handling.
- Automation Engine: DAG node model, accepted fields, dependency references, error handling semantics.
- SDK/app runtime: `_input.py` field names and types, `ExtractionInput` collisions, runtime arg expectations.

If cloning is impossible because network access is unavailable, explicitly mark the validation as blocked and ask for permission to fetch/clone. Do not silently skip it.

## 4. Define Acceptance Criteria

Before editing, write the exact expected value flow:

```text
Configmap field:
Widget/schema:
Manifest arg:
Placeholder/static value:
_input.py field/type:
Runtime consumer:
Docs:
Example:
Tests:
```

This prevents implementing a connector workaround instead of a toolkit primitive.

## 5. Implement

Keep edits scoped:

1. Change the source layer in `src/`.
2. Add/update one production-shaped example.
3. Regenerate generated output.
4. Add targeted tests.
5. Update docs.

Use `apply_patch` for manual edits. Do not hand-edit generated files except by regeneration.

## 6. Validate

Run:

```bash
./scripts/regenerate-all.sh
./scripts/check-invariants.sh
pkl test tests/*.pkl
../application-sdk/.venv/bin/python scripts/test-sdk-import.py
git diff --check
```

Also manually inspect the changed generated files:

```bash
git diff -- examples/**/generated/*.json examples/**/generated/_input.py
```

Confirm:

- Every user-facing configmap field has the intended manifest flow or is intentionally UI-only.
- Every manifest placeholder has a matching configmap field or is intentionally supplied by Heracles/SDK.
- `_input.py` names and types match the widget and runtime expectations.
- Generated output is committed.

## 7. Commit and PR

Use a conventional subject plus an extended body:

```text
fix: short description

Root cause:
<why the previous toolkit API/behavior was insufficient>

Change:
<what changed in source, examples, tests, docs>

Compatibility:
<old behavior preserved or migration note>

Validation:
- <commands>
- <consumer checks>
```

PR body must include:

```text
## Root Cause
## Existing Behavior
## New Behavior
## API Design Decision
## Backward Compatibility
## Affected Apps / Examples
## Cross-Repo Validation
## Value Flow
## Validation Commands
```

## 8. Independent Review

After pushing the PR:

1. Run the `contract-review` skill against the PR and post the report.
2. Run two independent review tracks. Prefer separate subagents when explicitly authorized; otherwise perform both reviews yourself and keep the findings separated.
3. Implementation review prompt:

```text
Review this PR for implementation correctness only.

Inputs:
- PR URL:
- Issue:
- Acceptance criteria:
- Validation commands already run:

Find:
- bugs or behavior regressions
- stale generated output
- missing or weak tests
- docs that do not match source behavior
- configmap -> manifest -> _input.py value-flow mismatches
- downstream consumer mismatches

Do not redesign the API unless it blocks correctness.
```

4. API-design review prompt:

```text
Review this PR for toolkit API design and codebase fit.

Inputs:
- PR URL:
- Issue:
- Current app workaround:
- Proposed toolkit API:

Find:
- whether the feature already existed under another API
- whether the new API belongs at app, node, field, widget, or bundle level
- whether this is too app-specific or should be generalized
- whether it avoids raw DAGNode and post-render JSON patching
- whether names/defaults preserve backward compatibility
- whether examples teach the right future pattern
- whether the change creates hidden coupling with frontend, Heracles, AE, SDK, or app runtime

Focus on bad API design, not local implementation nits.
```

5. Resolve or explicitly acknowledge both review tracks before merge.
6. Watch CI until green. Investigate failures instead of assuming flake.

## 9. Close the Loop

After merge:

- Sync local `main`.
- Update the tracking issue with PR link, release version, and validation summary.
- Draft a short release/Slack note when the change affects app authors.
