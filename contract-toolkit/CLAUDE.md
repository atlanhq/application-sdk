# CLAUDE.md - app-contract-toolkit

Lean reference for Claude Code. See `AGENTS.md` for the full repo guide.

## What This Repo Is

PKL toolkit for Atlan native app contracts. Contracts generate:

- workflow setup config: `{name}.json`
- credential config: `atlan-connectors-{name}.json`
- Automation Engine manifest: `manifest.json`
- typed SDK input model: `_input.py`
- bundle metadata: optional generated `atlan.yaml`

Default consuming-app output path is `app/generated`. Root `atlan.yaml` is still required; generated `app/generated/atlan.yaml` must be synced to root if PKL owns it.

## Main Files

- `src/NativeApp.pkl`: single-entrypoint native app contracts.
- `src/NativeAppBundle.pkl`: multi-entrypoint apps and `atlan.yaml`.
- `src/AgentConfig.pkl`: shared secure-agent config.
- `src/Config.pkl`: workflow UI widgets.
- `src/Credential.pkl`: legacy credential module.
- `src/Renderers.pkl`: legacy renderers.
- `examples/*/app.pkl`: executable reference contracts.
- `tests/*_test.pkl`: Pkl behavior tests.

## Common Commands

```bash
./scripts/regenerate-all.sh
./scripts/check-invariants.sh
pkl test tests/*.pkl
python scripts/test-sdk-import.py
```

Generate one example:

```bash
pkl eval -m examples/trino/generated examples/trino/app.pkl
```

## Current Examples

- `openapi`: OpenAPI loader with extra object-store credential output.
- `postgres`: basic open-source SQL datasource.
- `trino`: multi-catalog SQL tree.
- `connection-ref`: `ConnectionRefInput`.
- `publish-controls`: publish toggles.
- `fanin`: dependency conditions.

## Editing Rules

- Prefer typed APIs over raw mappings.
- New native credential behavior belongs in `NativeApp.pkl`, not `Credential.pkl`.
- New workflow widgets belong in `Config.pkl`.
- Native rendering belongs in `NativeApp.pkl` / `NativeAppBundle.pkl`, not `Renderers.pkl`.
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
