# app-contract-toolkit

NEVER add `Co-Authored-By` lines to commit messages. All commits are authored by the user.

PKL toolkit for Atlan native app contracts. One `.pkl` generates workflow config, credential config, AE manifest, and typed Python `_input.py`. Multi-manifest apps use multiple `.pkl` files with shared credentials.

## Project Structure

```
src/
├── NativeApp.pkl      # Base module for native apps — amend this
├── Config.pkl         # 28+ UI widget types for workflow forms
├── Credential.pkl     # Legacy credential module (Argo-era, backward compat)
├── Connectors.pkl     # 90+ connector type constants
├── Renderers.pkl      # Legacy renderers (K8s YAML, Argo, Pydantic, Kotlin)
└── PklProject         # Package metadata (version managed by CI)
scripts/
├── regenerate-all.sh  # Auto-discover + regenerate all examples + ruff format
├── check-invariants.sh # Validate generated output invariants
└── test-sdk-import.py  # SDK import validation for _input.py
examples/
├── redshift/          # JDBC warehouse connector (3 auth types, SqlTree, SageV2)
├── trino/             # Multi-catalog connector (SqlTree with databaseExcludePatterns)
├── monte-carlo/       # REST data-quality connector (APITree, ConnectionSelector)
├── cosmos/            # CosmosDB (ConditionalInput, vcore auth)
├── mode/              # Mode analytics (REST, basic auth)
└── teradata/          # Multi-manifest example (crawler + miner)
    ├── credentials.pkl    # Shared credential definitions
    ├── crawler.pkl        # Crawler contract
    ├── miner.pkl          # Miner contract
    └── generated/         # Output in crawler/ and miner/ subdirs
```

## Key Concepts

**NativeApp.pkl** is the entry point. Apps amend it and declare metadata, credentials, workflow config.

**Two credential models exist:**
- `FieldSpec` (in NativeApp.pkl) — for native apps. Aligned with SDK's `application_sdk.credentials.types.FieldSpec`. Single class: `name`, `displayName`, `sensitive`, `fieldType`, `options`, `validationRegex`, `isHidden`.
- `Credential.pkl` — for legacy Argo-era apps. Uses Config.pkl widget classes. Don't use for new native apps.

**Workflow config** uses Config.pkl widget classes (TextInput, Radio, SqlTree, ConnectionCreator, etc.) inside `uiConfig.tasks`.

**Manifest** is auto-generated from workflow params. `{{param}}` placeholders derived from uiConfig properties. `credential` always emitted. Default extract → publish pipeline. Custom nodes via `extraNodes`.

## Commands

```bash
# Regenerate ALL examples (auto-discovers, formats Python output)
./scripts/regenerate-all.sh

# Run invariant checks
./scripts/check-invariants.sh

# Generate a single example
pkl eval -m examples/redshift/generated examples/redshift/app.pkl
```

## Documentation

When changing any PKL module (adding/modifying properties, classes, or widget types), always update **all three** docs in the same PR:
- `docs/reference.md` — detailed reference documentation
- `docs/index.html` — standalone HTML docs page (hand-written, not generated from reference.md)
- `README.md` — quick start guide and widget summary tables

## Editing Guidelines

- `NativeApp.pkl` — the main module developers interact with. Changes here affect all native apps.
- `Config.pkl` — widget types. Add new widget types here. Changing existing widget properties affects all apps.
- `Credential.pkl` — legacy module. Modify only for backward compat fixes. New credential features go in NativeApp.pkl's FieldSpec.
- `Renderers.pkl` — legacy renderers. NativeApp.pkl has its own rendering. Don't add native rendering here.
- `Connectors.pkl` — add new connectors as needed. Simple constants.

## How the Generated JSON is Consumed

```
{name}.json
  → SDK serves at GET /workflows/v1/configmap/{name}
  → Heracles proxies to frontend → renders the setup form

atlan-connectors-{name}.json
  → SDK serves at GET /workflows/v1/configmap/atlan-connectors-{name}
  → Frontend renders the credential form

manifest.json
  → SDK serves at GET /manifest
  → Heracles fetches, substitutes {{params}}, sends to Automation Engine
  → AE orchestrates extract → publish (or custom pipeline)
```

## DAG Value Types in Manifest

- `{{param}}` — Heracles substitutes from user's form values
- `$.node.outputs.*` — AE resolves at runtime from upstream node outputs
- Static values (`true`, `false`) — passed through

## Making PRs

### Commit conventions

Use **conventional commits** — CI uses the PR title (squash-merge message) to determine version bump:
- `feat:` → **minor** bump (0.3.x → 0.4.0)
- `fix:` / `chore:` / `docs:` / `ci:` → **patch** bump (0.3.x → 0.3.y)
- `feat!:` or `BREAKING CHANGE` → **major** bump (for 0.x, bumps minor)

Releases only trigger when `src/` changes. Non-package commits (CI, docs, scripts) skip release.

### Before opening a PR

1. **Regenerate**: `./scripts/regenerate-all.sh` — commit output with your `src/` changes
2. **Check invariants**: `./scripts/check-invariants.sh`
3. **Update docs** (all three) if `src/` changed
4. **Add/update an example** if you added a feature

### After opening a PR

**Run the `contract-review` skill in a sub-agent against the PR.** Once the PR is opened (or updated with new commits), spawn a sub-agent that invokes the `contract-review` skill with the PR URL or number. The sub-agent validates widget dispatch, unbounded-flag coverage, end-to-end value flow (configmap → manifest → `_input.py`), docs accuracy, and cross-repo consumer compatibility (`atlan-frontend`, `heracles`, `atlan-automation-engine-app`). Its report is the self-review record — post it as a PR comment before requesting human review.

### CI checks

| Check | Blocks? | What |
|---|---|---|
| Verify generated output | Yes | Regenerates all, fails if stale |
| Check invariants | Yes | unbounded_fields, syntax, __init__.py, JSON |
| Lint Python | Yes | ruff format + check on _input.py |
| SDK import test | Yes | Imports _input.py against SDK main |
| Docs check | No | Comments reminder if src/ changed without docs |

## Testing Changes

After modifying any PKL module, regenerate **all** examples and verify no breakage:
```bash
./scripts/regenerate-all.sh
./scripts/check-invariants.sh
```

Every PR that touches `src/` must pass generation for all examples. If adding a new feature, also add or update an example that exercises it.
