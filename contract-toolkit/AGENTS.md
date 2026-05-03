# AGENTS.md - app-contract-toolkit

NEVER add `Co-Authored-By` lines to commit messages. All commits are authored by the user.

PKL toolkit for Atlan native app contracts. A contract renders workflow config, credential config, Automation Engine manifest, and typed SDK input models. Single-entrypoint apps amend `NativeApp.pkl`; multi-entrypoint apps amend `NativeAppBundle.pkl` and can render root-compatible `atlan.yaml` plus per-entrypoint generated folders.

## Current Outputs

Single-entrypoint apps normally generate into `app/generated` in consuming app repos:

```text
app/generated/
|-- __init__.py
|-- _input.py
|-- {name}.json
|-- atlan-connectors-{name}.json
`-- manifest.json
```

Bundles generate root artifacts plus per-entrypoint folders:

```text
app/generated/
|-- atlan.yaml
|-- atlan-connectors-{shared}.json
|-- crawler/
|   |-- __init__.py
|   |-- _input.py
|   |-- {crawler-name}.json
|   `-- manifest.json
`-- miner/
    |-- __init__.py
    |-- _input.py
    |-- {miner-name}.json
    `-- manifest.json
```

Root `atlan.yaml` is still required in app repos. A generated `app/generated/atlan.yaml` is not sufficient unless the app has an explicit sync step to root.

## Project Structure

```text
src/
|-- NativeApp.pkl          # Main native app contract module
|-- NativeAppBundle.pkl    # Multi-entrypoint bundle + atlan.yaml renderer
|-- AgentConfig.pkl        # Shared secure-agent configmap contract
|-- Config.pkl             # Workflow UI widget types
|-- Credential.pkl         # Legacy Argo-era credential module
|-- Connectors.pkl         # Connector constants
|-- Renderers.pkl          # Legacy renderers
`-- PklProject             # Package metadata
scripts/
|-- regenerate-all.sh
|-- check-invariants.sh
`-- test-sdk-import.py
examples/
|-- clickhouse/            # Open-source SQL datasource from legacy templates
|-- connection-ref/        # ConnectionRefInput typed output
|-- fanin/                 # DependencyCondition and fan-in
|-- openapi/               # OpenAPI loader with extra object-store credential
|-- postgres/              # Basic open-source SQL datasource
|-- publish-controls/      # Publish toggles
`-- trino/                 # Multi-catalog SQL connector
tests/
`-- *_test.pkl
```

## Key Concepts

- `NativeApp.pkl` is the primary module for new native app contracts.
- `NativeAppBundle.pkl` is for one deployed app exposing multiple marketplace cards or SDK entrypoints.
- `AgentConfig.pkl` owns the shared `atlan-connectors-agent` payload. Apps reference it from `AgentSelector`; it is emitted only when explicitly generated or added through `additionalOutputFiles`.
- Native credentials use `FieldSpec`, `AuthOption`, `ConditionalFieldSpec`, `NamedWidget`, and `NamedProperty` from `NativeApp.pkl`.
- `Credential.pkl` is legacy. Do not use it for new native app credential config.
- Workflow UI uses `Config.pkl` widgets inside `uiConfig.tasks`.
- Generated `_input.py` defines `AppInputContract` extending SDK `ExtractionInput`.
- `flatManifestArgs = true` is the SDK v3 default. Use `flatManifestArgs = false` only for workflows that intentionally read `args.metadata`.
- Default DAG is `extract -> publish`. Use typed nodes (`PublishNode`, `QueryIntelligenceNode`, `PopularityNode`, `LineageNode`, `LineagePublishNode`) before raw `DAGNode`.

## Commands

```bash
# Regenerate all example outputs and format generated Python
./scripts/regenerate-all.sh

# Validate generated output invariants
./scripts/check-invariants.sh

# Run pkl tests
pkl test tests/*.pkl

# SDK import validation for generated _input.py files
python scripts/test-sdk-import.py

# Generate one example
pkl eval -m examples/trino/generated examples/trino/app.pkl
```

`scripts/test-sdk-import.py` requires the application SDK import dependencies to be available in the active Python environment.

## App Playground

For consuming app repos, the app playground belongs under `app/generated/frontend/static` and must not be committed. Ensure it is ignored before installing:

```bash
git check-ignore app/generated/frontend/static
npx @atlanhq/app-playground@3.1.0 install-to app/generated/frontend/static
```

Run final UI validation only after contract generation and static checks pass:

```bash
atlan app run -p .
```

That serves the app and playground UI on port `8000`.

## Feature and Bug Workflow

Use the right repo skill for the job:

| Task | Skill |
|---|---|
| Build, migrate, generate, or validate a consuming app contract | `make-contract` |
| Add, debug, or validate a toolkit feature/API/bug fix | `toolkit-feature-workflow` |
| Review a toolkit PR before merge | `contract-review` |

For toolkit source changes, `toolkit-feature-workflow` is mandatory. It owns the
strict process for issue tracking, API-design triage, cross-repo consumer checks,
examples, docs, tests, extended commits, PR triage, and independent review.

When asked to add a feature or fix a bug, start by proving the current behavior
from the repo. Do not assume the report is correct or complete.

1. Read the issue, linked connector PR/app, and the closest local example.
2. Locate the owning module in `src/`, then trace the generated artifact that
   downstream systems consume:
   - workflow config: `{name}.json`
   - credential config: `atlan-connectors-{name}.json`
   - manifest: `manifest.json`
   - SDK model: `_input.py`
   - bundle metadata: generated `atlan.yaml`, when relevant
3. Check whether the behavior already exists under another property or typed
   node before adding a new API.
4. Validate impact across consumers before editing:
   - SDK serves generated JSON and imports `_input.py`.
   - Frontend/app-playground renders workflow and credential configmaps.
   - Heracles substitutes manifest `{{params}}`.
   - Automation Engine consumes DAG node ids, dependencies, activity labels,
     task queues, workflow types, app names, and args.
   - Consumer repos must be checked on the required remote branch:
     `../atlan-frontend` and `../heracles` on `origin/beta`;
     `../blaze` and `../atlan-automation-engine-app` on `origin/main`.
     If a sibling checkout is missing, clone it into
     `/tmp/app-contract-toolkit-consumers/<repo>` and validate there. Do not
     silently skip consumer validation.
5. Prefer the smallest typed toolkit change that preserves existing output by
   default. New behavior should usually be opt-in unless the issue is a clear
   bug in current output.
6. Add targeted Pkl tests for the new invariant or bug reproduction. Keep a
   default-behavior assertion when backward compatibility matters.
7. Add or update an executable example that shows the intended usage, then
   regenerate and commit generated output.
8. Update `README.md`, `docs/reference.md`, and `docs/index.html` with enough
   detail for app authors to use the feature without reading the implementation.
9. Commit with a conventional subject and an extended body covering root cause,
   change, compatibility, and validation.
10. Open the PR with triage/design/value-flow sections, then run and post
    `contract-review`.
11. Run two independent review tracks before merge: one implementation
    correctness review and one API-design/codebase-fit review. Use separate
    subagents when explicitly available/authorized; otherwise do both reviews
    sequentially and keep the findings separate.

Useful first-pass repo scan:

```bash
git status --short --branch
rg -n "<property|node|widget|manifest key>" src examples tests docs README.md
find examples -maxdepth 2 -name app.pkl | sort
```

## Editing Guidelines

- `NativeApp.pkl`: main native contract renderer. Changes affect most apps.
- `NativeAppBundle.pkl`: bundle layout, root `atlan.yaml`, shared credentials, per-entrypoint output.
- `AgentConfig.pkl`: shared secure-agent config only.
- `Config.pkl`: workflow widgets. Add new widget types here.
- `Credential.pkl`: legacy only; modify for backward compatibility.
- `Renderers.pkl`: legacy renderers; do not add native rendering here.
- `Connectors.pkl`: connector constants.
- Toolkit repo skills live at the SDK repo root for agent compatibility. If
  adding or editing `.agents/skills/<name>/SKILL.md`, make the same change in
  `.claude/skills/<name>/SKILL.md` and verify both files match. Do not add
  nested `.agents/` or `.claude/` directories under `contract-toolkit/`.

When changing any PKL module, update all three docs in the same PR:

- `docs/reference.md`
- `docs/index.html`
- `README.md`

Also add or update an example and generated output for new behavior.

When adding or changing a public toolkit property, write extended descriptions
in the source doc comment and public docs. Cover the default, when to use it,
what generated output changes, what does not change, and any caveats such as
built-in node behavior vs. `extraNodes` overrides.

## Validation Expectations

Before opening a PR that touches `src/`:

```bash
./scripts/regenerate-all.sh
./scripts/check-invariants.sh
pkl test tests/*.pkl
python scripts/test-sdk-import.py
```

For widget, manifest, or SDK input changes, add targeted `tests/*_test.pkl` coverage against the curated examples.

## Generated JSON Consumption

```text
{name}.json
  -> SDK serves configmap at /workflows/v1/configmap/{name}
  -> frontend/app-playground renders setup form

atlan-connectors-{name}.json
  -> SDK serves configmap at /workflows/v1/configmap/atlan-connectors-{name}
  -> frontend/app-playground renders credential form

manifest.json
  -> SDK serves /manifest
  -> Heracles substitutes {{params}}
  -> Automation Engine runs extract -> publish or custom DAG
```

Manifest value types:

- `{{param}}`: substituted by Heracles from form values.
- `$.node.outputs.*`: resolved by Automation Engine from upstream node output.
- Static values: passed through.

## PR and Release Notes

Use conventional PR titles:

| Prefix | Version bump | Label |
|---|---|---|
| `feat!:` or `BREAKING CHANGE:` | major (minor on 0.x) | `type:breaking` |
| `feat:` | minor | `type:feature` |
| `fix:` | patch | `type:bug` |
| `docs:` | patch | `type:docs` |
| `ci:` | patch | `type:ci` |
| `chore:` | patch | `type:chore` |

`CHANGELOG.md` is generated by release automation. Do not hand-edit it.

Releases are manual from the `atlanhq/application-sdk` repository: Actions -> Release Contract Toolkit -> Run workflow -> enter semver version. The release workflow bumps `contract-toolkit/src/PklProject`, regenerates changelog content, publishes under `app-contract-toolkit/` on the `application-sdk` GitHub Pages branch, tags `contract-toolkit-v<version>`, and creates the GitHub Release.

After opening a PR, run the repo `contract-review` skill against the PR and post the report as the self-review record before human review.
