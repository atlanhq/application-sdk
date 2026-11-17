# CLAUDE.md - app-contract-toolkit

The agent guide for the toolkit, for every agent. `AGENTS.md` points here;
edit this file, not that one.

NEVER add `Co-Authored-By` lines to commit messages.

## What This Repo Is

PKL toolkit for Atlan native app contracts. A single amend line is all a
consuming app needs:

```pkl
amends "@app-contract-toolkit/App.pkl"
```

Contracts generate — from one `pkl eval --project-dir contract -m . contract/app.pkl` at repo root:

- `atlan.yaml` — marketplace deployment manifest (repo root)
- `app.yaml` — SDR CI shim: `app_name`, `app_image`, `app_port` (repo root)
- `app/generated/{name}.json` — workflow setup config
- `app/generated/atlan-connectors-{name}.json` — credential config
- `app/generated/manifest.json` — Automation Engine DAG
- `app/generated/_input.py` — typed SDK input model
- `app/generated/artifact_schemas.json` — data hand-off declarations (only when `artifactSchemas` is non-empty)

## Main Files

- `src/App.pkl`: **canonical template** for all new native app contracts.
- `src/Widgets.pkl`: widget catalog re-exported from `App.pkl`.
- `src/Inputs.pkl`: typed input field classes for `App.inputs` (system apps), re-exported from `App.pkl`.
- `src/InputContract.pkl`: renders a system app's caller node (`contract/generated/input.pkl`) from its `inputs`.
- `src/PyFormat.pkl`: lays every generated `.py` out as ruff does with default settings (import order, 88-column wrapping). Renderers emit plain one-line statements and pass the module through `format`. One exception: `fmtSubsField` keeps the one-argument-per-line layout (trailing comma) for long `_e2e_substitutions.py` fields, because ruff preserves it and apps already commit it; don't replace it with a one-line statement.
- `src/Deployment.pkl`: deployment classes (`DeployConfig`, `Pool`, `KedaConfig`, `DaprComponents`, `ResourceConfig`, `VpaConfig`) re-exported from `App.pkl`.
- `src/Connectors.pkl`: connector type registry (still imported explicitly by consumers).
- `src/SdkNames.pkl`: SDK names `App.pkl` copies: base-class field names and `_input.py` import names it must not shadow, the e2e substitution parents' fields, the class attributes a generated `_e2e_base.py` may assign, and the artifact type and format vocabulary. `scripts/test-sdk-import.py` compares them with the SDK; update them when the SDK's base input classes, e2e harness or artifact vocabulary change.
- Legacy modules (reference only, not for new apps):
  - `src/NativeApp.pkl`, `src/NativeAppBundle.pkl`
  - `src/Config.pkl`, `src/Credential.pkl`, `src/Renderers.pkl`
- `examples/*/app.pkl`: executable reference contracts.
- `tests/*_test.pkl`: Pkl behavior tests.

## Common Commands

```bash
./scripts/regenerate-all.sh
pkl test tests/*.pkl
python scripts/test-sdk-import.py
uv run python scripts/check-ruff-stable.py
```

Generate one example (example dir acts as the consuming app's repo root):

```bash
pkl eval -m examples/full examples/full/app.pkl
```

## Current Examples

Examples serve as toolkit unit tests — no source-specific examples. Each
demonstrates distinct feature surface, verified by `tests/*.pkl`.

- `minimal`: smallest possible contract; uses all defaults. Start here.
- `tree-filters`: include/exclude filter tree pickers with `treeSelection = true`;
  the input class extends `TreeSelectionExtractionInput`.
- `full`: shows every overridable feature: JDBC URL auth, all pipeline steps,
  diverse widgets, UIRules, extraNodes.
- `bundle`: multi-entrypoint app (crawler + miner); shared credential configmap;
  per-entrypoint artifact subfolders.
- `credential-configs`: `additionalCredentialConfigs` — more than one credential
  configmap per entrypoint, including a bare (wrapper-less) one; hoisted and deduped.
- `deploy`: single-pool migration example — KEDA, resources, env, pool-level `overrides`; shows v0.16.x → v0.17.0 migration path.
- `pools`: `pools` map (preferred) — named hot/cold worker pools with per-pool `cooldownPeriod` and resources.
- `system-app`: `type = "system"` — no `entrypoints` block or card, no credential config, no `uiConfig`; typed `inputs` render `_input.py` with no manifest, and `contract/generated/input.pkl`, the caller node (its planned toolkit home, `src/system/<name>.pkl`, does not exist yet).
- `system-bundle`: `type = "system"` with two UI-less system sub-contracts; each renders an importable `app/generated/<ep_snake>/`, `contract/generated/<ep>/input.pkl`, and a `SystemAppE2ETest` e2e base.
- `system-triggers`: `type = "system"` bundle whose entrypoints declare `schedules` / `events`; `atlan.yaml` renders an `entrypoints` block with `marketplace_card: false` so Local Marketplace reconciles the triggers.
- `connection-ref`: `ConnectionRefInput` widget, `pipeline.publish = null`.
- `publish-controls`: publish toggles, `includeInputFields`, `errorHandling`, `zeroOutConfig`.
- `fanin`: multi-parent fan-in via `dependsOn`, explicit `DependencyCondition`.
- `ae-activities`: `FetchEntitiesNode`, `MergeNode` and generic `ActivityNode` in
  `extraNodes`; AE activity nodes mixed with a `DAGNode`, no `extract` step.
- `artifact-schemas`: `artifactSchemas` data hand-off declarations — parquet + NDJSON, nested paths, arrays of structs (`[]` element step), an input artifact.
- `card-split`: two entrypoints where only one is a marketplace UI card.
- `behind-the-scenes`: single entrypoint with `marketplaceCard = false`; routable, no card.
- `agent-e2e`: agent/SDR e2e codegen — direct and agent credential bodies, an
  `extraction-method` ConditionalInput.
- `scheduled`: cron background job via `schedules`.
- `streaming`: streaming dispatch via `streaming.enabled`.

## Generated Output Consumers

```text
{name}.json
  -> SDK serves configmap at /workflows/v1/configmap/{name}
  -> frontend/app-playground renders setup form

atlan-connectors-{name}.json
  -> SDK serves configmap at /workflows/v1/configmap/atlan-connectors-{name}
  -> frontend/app-playground renders credential form

manifest.json
  -> SDK serves /workflows/v1/manifest
  -> Heracles substitutes {{params}}
  -> Automation Engine runs extract -> publish or custom DAG
```

Manifest value types:

- `{{param}}`: substituted by Heracles from form values.
- `$.node.outputs.*`: resolved by Automation Engine from upstream node output.
- Static values: passed through.

Default DAG is `extract -> publish`. Prefer typed nodes (`PublishNode`,
`QueryIntelligenceNode`, `PopularityNode`, `LineageNode`, `LineagePublishNode`)
over a raw `DAGNode` in `extraNodes`.

## Editing Rules

- Prefer typed APIs over raw mappings.
- New workflow widgets belong in `Widgets.pkl`; re-export via typealias in `App.pkl`.
- All native rendering behavior belongs in `App.pkl`.
- Legacy modules (`NativeApp.pkl`, `Config.pkl`, etc.) are frozen reference material.
- If `src/` changes, update `README.md` and `docs/reference.md`.
- Add or update an example when adding behavior.
- Test an eval-time refusal (a `throw` or a type constraint) with a `module.catch`
  fact in `tests/*_test.pkl`, forcing every output file the way `pkl eval -m` does:
  `module.catch(() -> m.output.files.toMap().mapValues((_, f) -> f.text)).contains("<message>")`.
  Assert the message, not just that eval failed, and keep a control fact showing the
  same contract minus the offending value generates.
- Commit regenerated example outputs with the source change.
- Toolkit repo skills live at the SDK repo root. Mirror changes between
  `.agents/skills/<name>/SKILL.md` and `.claude/skills/<name>/SKILL.md`, and do
  not add nested skill directories under `contract-toolkit/`.
- When adding or changing a public toolkit property, write extended descriptions
  in the source doc comment and public docs. Cover the default, when to use it,
  what generated output changes, what does not change, and any caveats such as
  built-in node behavior vs. `extraNodes` overrides.

## Feature/Bug Flow

Use `make-contract` for consuming app contract work. For toolkit source changes,
follow the workflow below for issue tracking, API-design triage, cross-repo
consumer checks, examples, docs, tests, extended commits, PR triage, and
independent review.

For any feature or bug request, prove current behavior before changing code:

1. Read the issue, linked app/connector, closest local example, and relevant tests.
2. Trace source in `src/` to generated output: config JSON, credential JSON,
   `manifest.json`, `_input.py`, and bundle `atlan.yaml` when relevant.
3. Check whether the behavior already exists under another property or typed node.
4. Validate downstream impact: SDK serving/imports, frontend config rendering,
   Heracles manifest substitution, and AE DAG fields. Required consumer branches:
   `atlan-frontend` / `heracles` on `origin/beta`; `blaze` /
   `atlan-automation-engine-app` on `origin/main`. If a sibling repo is missing,
   clone it into `/tmp/app-contract-toolkit-consumers/<repo>` and validate there.
5. Preserve existing output by default unless fixing a confirmed bug.
6. Add targeted tests, update an executable example, regenerate generated output,
   update all three docs, then run validation.
7. Before merge, run two review tracks: implementation correctness and
   API-design/codebase fit. Use separate subagents when explicitly available;
   otherwise keep the two reviews separate yourself.

## App Playground

For consuming app repos, playground output belongs at `app/generated/frontend/static` and must be ignored, never committed:

```bash
git check-ignore app/generated/frontend/static
npx @atlanhq/app-playground@3.1.0 install-to app/generated/frontend/static
atlan app run -p .
```

`atlan app run -p .` serves the app and playground UI on port `8000`. Run it only at the end of validation after generation/static checks pass.

## PR Checklist

Before opening a PR that touches `src/`:

```bash
./scripts/regenerate-all.sh
pkl test tests/*.pkl
python scripts/test-sdk-import.py
uv run python scripts/check-ruff-stable.py
```

Use conventional PR titles: `feat:`, `fix:`, `docs:`, `ci:`, `chore:`, or breaking-change syntax. Do not hand-edit `CHANGELOG.md`; release automation owns it.

## Releases

Automated; no manual version input.

1. Merging a PR that touches `contract-toolkit/**` to `main` fires
   `Release Contract Toolkit` (`.github/workflows/contract-toolkit-release.yml`).
   It runs `.github/scripts/contract_toolkit_release.py`, which reads the
   conventional commits touching `contract-toolkit/**` since the last
   `contract-toolkit-v*` tag: `!:` or a `BREAKING CHANGE:` trailer bumps major
   (minor while on `0.x`), `feat:` bumps minor, anything else patch. It bumps
   `src/PklProject` and prepends `CHANGELOG.md`; the workflow then regenerates
   the conformance toolkit baseline, sanity-builds the package, and pushes the
   `bump-version-contract-toolkit` branch with a PR labeled
   `release:contract-toolkit`. Nothing to release: it exits quietly.
2. Review and merge that release PR.
3. `Publish Contract Toolkit` (`contract-toolkit-publish.yml`) builds the
   package from merged `main`, publishes it under `contracts/` on `gh-pages`,
   and tags `contract-toolkit-v<version>` at the merge commit.

After opening a PR, post the self-review report before human review.
