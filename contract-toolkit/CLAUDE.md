# CLAUDE.md - app-contract-toolkit

Lean reference for Claude Code. See `AGENTS.md` for the full repo guide.

## What This Repo Is

PKL toolkit for Atlan native app contracts. A single amend line is all a
consuming app needs:

```pkl
amends "@app-contract-toolkit/App.pkl"
```

Contracts generate — from one `pkl eval -m . contract/app.pkl` at repo root:

- `atlan.yaml` — marketplace deployment manifest (repo root)
- `app.yaml` — SDR CI shim: `app_name`, `app_image`, `app_port` (repo root)
- `app/generated/{name}.json` — workflow setup config
- `app/generated/atlan-connectors-{name}.json` — credential config
- `app/generated/manifest.json` — Automation Engine DAG
- `app/generated/_input.py` — typed SDK input model

## Main Files

- `src/App.pkl`: **canonical template** for all new native app contracts.
- `src/Widgets.pkl`: widget catalog re-exported from `App.pkl`.
- `src/Connectors.pkl`: connector type registry (still imported explicitly by consumers).
- Legacy modules (reference only, not for new apps):
  - `src/NativeApp.pkl`, `src/NativeAppBundle.pkl`, `src/AgentConfig.pkl`
  - `src/Config.pkl`, `src/Credential.pkl`, `src/Renderers.pkl`
- `examples/*/app.pkl`: executable reference contracts.
- `tests/*_test.pkl`: Pkl behavior tests.

## Common Commands

```bash
./scripts/regenerate-all.sh
./scripts/check-invariants.sh
pkl test tests/*.pkl
python scripts/test-sdk-import.py
```

Generate one example (example dir acts as the consuming app's repo root):

```bash
pkl eval -m examples/full examples/full/app.pkl
```

## Current Examples

Examples serve as toolkit unit tests — no source-specific examples. Each
demonstrates distinct feature surface, verified by `tests/*.pkl`.

- `minimal`: smallest possible contract; uses all defaults. Start here.
- `full`: shows every overridable feature: JDBC URL auth, all pipeline steps,
  diverse widgets, UIRules, extraNodes.
- `bundle`: multi-entrypoint app (crawler + miner); shared credential configmap;
  per-entrypoint artifact subfolders.
- `deploy`: typed `deploy` block — KEDA, Dapr, resources, env, `deployOverrides`.
- `connection-ref`: `ConnectionRefInput` widget, `pipeline.publish = null`.
- `publish-controls`: publish toggles, `includeInputFields`, `errorHandling`.
- `fanin`: multi-parent fan-in via `dependsOn`, explicit `DependencyCondition`.

## Editing Rules

- Prefer typed APIs over raw mappings.
- New workflow widgets belong in `Widgets.pkl`; re-export via typealias in `App.pkl`.
- All native rendering behavior belongs in `App.pkl`.
- Legacy modules (`NativeApp.pkl`, `Config.pkl`, etc.) are frozen reference material.
- If `src/` changes, update `README.md` and `docs/reference.md`.
- Add or update an example when adding behavior.
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
./scripts/check-invariants.sh
pkl test tests/*.pkl
python scripts/test-sdk-import.py
```

Use conventional PR titles: `feat:`, `fix:`, `docs:`, `ci:`, `chore:`, or breaking-change syntax. Do not hand-edit `CHANGELOG.md`; release automation owns it.

After opening a PR, post the self-review report before human review.
