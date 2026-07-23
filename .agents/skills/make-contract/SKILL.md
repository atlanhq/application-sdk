---
name: make-contract
description: Create, migrate, update, generate, or validate an Atlan native app Pkl contract with app-contract-toolkit and pkl. Use when helping an app author produce contract/app.pkl, contract/PklProject, app/generated artifacts, atlan.yaml decisions, and SDK input contracts — including migrating a legacy app onto the canonical App.pkl template, moving it off the archived toolkit repo, and raising it to the SDK floor that keeps its logs visible.
---

# make-contract

Use this skill when the user asks to create, migrate, update, generate, or
validate an Atlan native app contract. The goal is a correct Pkl contract and
regenerated SDK artifacts — never hand-edited generated JSON.

This skill is **adaptive and incremental**. You inspect the app you are running
in, classify what shape it is already in, propose a small plan, ask only
blocking questions, then apply and verify one step at a time. You do not force a
redesign on an app that does not need one.

## Ground yourself in live sources first — do not trust memory or this file's snapshots

The toolkit moves. Earlier versions of this skill went stale because they
hard-coded facts (which primitive to amend, the example list, a version-lookup
recipe) that later changed. **Derive volatile facts at runtime** from the
authoritative sources in this repo instead of trusting any list baked below:

- Primitive model, widgets, credential shapes, migration notes:
  `contract-toolkit/README.md` and `contract-toolkit/CLAUDE.md` are current.
- Changelog / when something changed: `contract-toolkit/CHANGELOG.md`.
- The real example set: `git ls-files 'contract-toolkit/examples/*/app.pkl'`
  (untracked `generated/` and `__pycache__` leftover dirs are **not** examples).
- Latest toolkit version (bare semver, from the SDK checkout root):
  `git tag --list 'contract-toolkit-v*' | sort -V | tail -1 | sed 's/^contract-toolkit-v//'`.
- The template source of truth: `contract-toolkit/src/App.pkl`.

These `contract-toolkit/...`, `git ls-files`, and `git tag` lookups only resolve
from an `atlanhq/application-sdk` checkout — run them there (or `git -C
<sdk-repo>`). From a consuming app repo they return nothing, silently.

**`contract-toolkit/AGENTS.md` is stale** (still describes the pre-consolidation
`NativeApp.pkl` world and references examples that no longer exist). Do not use
it as the source of truth; trust `README.md` + `CLAUDE.md` + `CHANGELOG.md` +
`src/App.pkl`.

## Hard Rules

- Treat `atlanhq/application-sdk` as the canonical source for this skill. The
  repo-local copies live at `.agents/skills/make-contract/SKILL.md` and
  `.claude/skills/make-contract/SKILL.md`; mirror any edit between them
  byte-for-byte, and do not add nested `make-contract` copies under
  `contract-toolkit/` or app repos.
- **`App.pkl` is the canonical template. `NativeApp.pkl` and
  `NativeAppBundle.pkl` are frozen legacy.** Author every new contract by
  amending `App.pkl`. Only touch the legacy modules when reading an app that
  still uses them (to migrate it, or to preserve it when the user declines).
- Author, validate, and generate with `pkl` from the app repo root — this repo
  guarantees the pkl toolchain, not any external CLI. See Contract Commands.
- Do not hand-edit generated files under `app/generated/` except to inspect or
  compare. Fix `contract/app.pkl`, then regenerate.
- Keep examples public and generic. Prefer the tracked examples under
  `contract-toolkit/examples/`; test-only scenarios live under
  `contract-toolkit/tests/fixtures/`.
- Preserve existing app behavior unless the user explicitly asks for a redesign.
- Never put secrets, real tokens, passwords, or tenant-specific data in
  contracts, examples, generated artifacts, or notes.

## Adaptive Workflow

Work in this loop. Keep the main thread short; state a plan before you edit.

1. **Detect** the app you are in (Discovery below).
2. **Classify** its rollout bucket (Classification below) — fresh, legacy
   primitive, archived toolkit source, or below the version floor. An app can be
   in several buckets at once.
3. **State the plan** in plain terms: app type, primitive, single- vs
   multi-entrypoint, credential form, DAG/publish, generated path, `atlan.yaml`
   ownership, and which migrations (if any) apply. Name the version floor and
   whether the app clears it.
4. **Ask only blocking questions** — usually no more than three at a time, with
   concrete choices and a recommended default. Ask only where local repo
   evidence cannot settle a behavior-preserving choice.
5. **Apply one step at a time** in `contract/app.pkl` / `contract/PklProject`,
   then regenerate.
6. **Verify** the value flow and the `app_name` invariant (below).
7. **Finish** with changed source files, generated artifacts, validation
   commands run, and any assumption or skipped check.

When the user is unsure, choose the simplest behavior-preserving contract:
single entrypoint, `contract/app.pkl` amending `App.pkl`, generated output in
`app/generated`, root `atlan.yaml` as packaging source of truth, and default
`extract -> publish` only for asset-producing connectors.

## Contract Commands (pkl-native)

This repo guarantees the `pkl` toolchain — there is no dependency on an external
CLI. Run every command from the **app repo root** (not from inside `contract/`).
Prereq: `pkl >= 0.25.1` (`brew install pkl`).

```bash
# Resolve / refresh the dependency lock (writes contract/PklProject.deps.json):
( cd contract && pkl project resolve )

# Validate AND generate — the same command. A clean eval IS the validation
# (pkl type-checks the whole contract graph and fires every constraint), and it
# writes atlan.yaml, app.yaml, and app/generated/*.
pkl eval -m . contract/app.pkl
```

- `-m .` sets the module output root to the app root so each output file lands at
  its natural path; running from inside `contract/` or with a different `-m`
  writes artifacts to the wrong tree.
- There is no separate validate-only verb — `pkl eval` is both validate and
  generate.
- **Scaffold: there is no init command.** To start a new contract, copy
  `contract-toolkit/examples/minimal/app.pkl` to `contract/app.pkl`, change its
  amend line to `amends "@app-contract-toolkit/App.pkl"`, and create a
  `contract/PklProject` with the package dependency (see Migrations -> Archived
  toolkit URL for the `uri`). Then resolve and generate. The tracked examples
  amend a relative `../../src/App.pkl` and ship no `PklProject`; a real app repo
  uses the package amend plus its own `contract/PklProject`.
- **Update the toolkit version:** edit the `["app-contract-toolkit"].uri` version
  in `contract/PklProject`, then re-run resolve + generate.

## Discovery

Before editing, inspect the app shape:

```bash
git status --short --branch
find . -maxdepth 3 \( -name app.pkl -o -name PklProject -o -name atlan.yaml \)
# Which primitive does the contract amend?
grep -rn "amends" contract/*.pkl 2>/dev/null
# Toolkit dependency + URL
grep -rn "app-contract-toolkit" contract/PklProject contract/PklProject.deps.json 2>/dev/null
# SDK version the app pins
grep -REh "atlan-application-sdk" pyproject.toml uv.lock requirements*.txt 2>/dev/null | head
# Runtime wiring
rg -n "ATLAN_CONTRACT_GENERATED_DIR|app/generated|AppInputContract|@entrypoint|@task|workflowType|manifest.json"
```

Also answer:

- New contract, or migration from an existing one?
- Single-entrypoint or multi-entrypoint/bundle?
- Which generated directory does the app actually serve?
- Is root `atlan.yaml` present and intentionally maintained?
- Does it need credential config, preflight, publish controls, connection
  reference, multiple auth modes, or custom runtime steps?

## Classification (rollout buckets)

Route the app by what Discovery found:

- **Fresh** — no contract yet, or already amends `App.pkl` on a current toolkit
  and clears the version floor. Author/edit directly; no migration.
- **Legacy primitive** — `contract/app.pkl` amends `NativeApp.pkl` or
  `NativeAppBundle.pkl`. Propose migration to `App.pkl` (see Migrations).
- **Archived toolkit source** — `PklProject` points at the old standalone
  toolkit URL (`package://atlanhq.github.io/app-contract-toolkit/...`) instead
  of the SDK-hosted one. Propose switching the URL and bumping the version.
- **Below the version floor** — SDK `< 3.21.0` or toolkit `< 0.17.0`. Propose
  raising both (this is what keeps logs visible — see the `app_name` invariant).

Migrations are opt-in and incremental. Detect, propose, get a yes, then apply
one at a time — never silently redesign.

## Version Floor (why it exists)

**Enforce SDK `>= 3.21.0` and contract-toolkit `>= 0.17.0`.** Below this, an
app's failure logs can silently fail to surface in the Workflow Center. Hard-fail
generation/validation guidance if the app is below the floor and the user has
not explicitly opted to stay.

Why 3.21.0 / 0.17.0 (not 3.18):

- `app_name` is the app's **log-correlation identity**. Logs are *written*
  tagged with `app_name` = `ATLAN_APPLICATION_NAME` (from `atlan.yaml`). The UI
  *reads* logs back by filtering on the `app_name` value baked into the served
  manifest.
- Pre-fix, the manifest shipped a literal `"{app_name}"` placeholder that the
  SDK substituted **at serve time from the runtime class name** (`GlueApp` ->
  `glue-app`), not the contract `name` (`glue`). Write-identity != read-identity
  -> the log query matches zero rows -> the panel shows "no logs" though logging
  worked. This is the failure signature the fix addresses.
- The fix bakes `app_name` from the contract `name` **at generate time** and
  removes the runtime placeholder, so read-identity equals write-identity by
  construction:
  - SDK `3.19.0` / toolkit `0.14.2` — core bake for `App.pkl` apps.
  - SDK `3.20.2` — output-path derivation prefers the resolved app name.
  - SDK `3.21.0` / toolkit `0.17.0` — same bake applied to the legacy
    `NativeApp.pkl` crossover template.
- `3.21.0` / `0.17.0` is the single floor that covers both the `App.pkl` and
  `NativeApp.pkl` families. `3.18.0` carries none of these fixes.

## The `app_name` invariant (same name everywhere)

The contract `name` (kebab-case) is the single root identity. The toolkit
derives everything from it, and every surface must stay byte-identical:

| Surface | Must equal |
|---|---|
| contract `name` in `contract/app.pkl` | the root value |
| generated `{name}.json` and `atlan-connectors-{name}.json` filenames | `name` |
| manifest `app_name` (top-level and `inputs.app_name`) | `name` |
| task-queue prefix `atlan-{name}-...` | `name` |
| `atlan.yaml` app name -> `ATLAN_APPLICATION_NAME` -> the log tag | `name` |
| runtime app identity (`_app_name` / resolved app name) | `name` |

If `atlan.yaml` name diverges from the contract `name`, the log write-tag and
the manifest read-filter disagree and **logs stop surfacing**. Fix the divergent
surface to match the intended kebab-case identity — correct `contract/app.pkl`
`name` or `atlan.yaml`, never hand-edit `app/generated/`.

Verify after every generate:

```bash
NAME=$(grep -E '^\s*name\s*=' contract/app.pkl | head -1 | sed -E 's/.*"([^"]+)".*/\1/')
echo "contract name = $NAME"
# No literal placeholder may survive in generated manifests (smoking gun):
grep -rn '"{app_name}"' app/generated/ && echo "FAIL: unbaked placeholder — regenerate on toolkit >= 0.17.0"
# Every baked app_name must equal $NAME:
grep -rn '"app_name"' app/generated/ 2>/dev/null
# atlan.yaml packaging name must equal $NAME:
grep -rn "name" atlan.yaml app/generated/atlan.yaml 2>/dev/null
# Generated filenames must be {name}.json / atlan-connectors-{name}.json:
ls app/generated | grep -E "^(${NAME}|atlan-connectors-${NAME})\.json$"
```

## Migrations

Each migration is small and mechanical. Propose, confirm, apply, regenerate,
verify.

### Archived toolkit URL -> SDK-hosted package

The toolkit was once a standalone repo. It is now hosted from the SDK repo. If
`contract/PklProject` points at the old standalone URL, switch it and pin the
latest version:

```pkl
dependencies {
  ["app-contract-toolkit"] {
    uri = "package://atlanhq.github.io/application-sdk/contracts/app-contract-toolkit@<LATEST>"
  }
}
```

Then re-resolve and regenerate (see Contract Commands) — `pkl project resolve`
rewrites `PklProject.deps.json`. Get the bare-semver `<LATEST>` (from the SDK
checkout) with
`git tag --list 'contract-toolkit-v*' | sort -V | tail -1 | sed 's/^contract-toolkit-v//'`.

### `NativeApp.pkl` / `NativeAppBundle.pkl` -> `App.pkl`

- Change the amend line to `amends "@app-contract-toolkit/App.pkl"`.
- Credential field primitives (`FieldSpec`, `AuthOption`, `ConditionalFieldSpec`,
  `NamedWidget`, `NamedProperty`) are now defined in `App.pkl`, and the
  `Widgets.CredentialInput` widget is re-exported by it — all reachable by name,
  no separate import. (`CredentialInput` is a `uiConfig` widget, not a credential
  field entry — don't use it inside `credentialAuthOptions`.)
- Widgets come from `Widgets.*` (re-exported by `App.pkl`); `Connectors.pkl`
  still needs an explicit `import`.
- Single-entrypoint: leave `entrypoints` empty; set `uiConfig` + `pipeline`.
  `App.pkl` synthesises the implicit entrypoint.
- Multi-entrypoint / bundle: replace the `NativeAppBundle.pkl` root plus N
  `NativeApp.pkl` per-entrypoint contracts with one `App.pkl` root that
  populates `entrypoints`, each pointing at a per-entrypoint contract that also
  amends `App.pkl`.

Confirm the exact current field names against `contract-toolkit/README.md` and
`src/App.pkl` before writing — do not trust this summary over the live source.

### Raise to the version floor

Bump the SDK dependency to `>= 3.21.0` and the toolkit to `>= 0.17.0`, then
regenerate so the manifest bakes a literal `app_name`. Re-run the `app_name`
invariant checks.

## Primitive Model (App.pkl)

- One `amends "@app-contract-toolkit/App.pkl"` line is all any contract needs.
- **Single- vs multi-entrypoint is `entrypoints` empty vs populated** — not two
  different modules.
- Marketplace-card presence: single-entrypoint uses `marketplaceCard: Boolean`;
  multi-entrypoint controls each card via the entrypoint's `packageId` (set it
  to make the entrypoint a card; omit to keep it routable-only).
- `emitEntrypoints` is **deprecated** and slated for removal — do not adopt it in
  new work; use per-entrypoint `packageId` instead. (An existing app that sets it
  is a pattern to migrate off, not to copy.)
- Keep `flatManifestArgs = true` unless an existing app intentionally reads
  metadata-wrapped args; changing it changes the runtime payload shape — call it
  out.

Prefer typed toolkit APIs over raw JSON: typed widgets from `Widgets.*`, typed
credential primitives from `App.pkl`, `Connectors.pkl` constants, SQL/JDBC
helpers (`JDBCUrlAuthOption`, `AdvancedJDBCUrlGroup`, `SqlTree`), and runtime
graph nodes (`PublishNode`, `QueryIntelligenceNode`, `PopularityNode`,
`LineageNode`, `LineagePublishNode`), dropping to raw `DAGNode` only when a typed
node does not fit.

> **FileReference and App.upload():** If your contract carries a `FileReference`
> that must reach a downstream Atlan system app (publish, lineage, quality), the
> connector must call `App.upload()` explicitly from `run()` — the task-to-task
> activity interceptor only writes to the customer-owned `objectstore`. See
> [ADR-0014](../../../docs/adr/0014-two-store-storage-architecture.md).

## Value-Flow Rules

Before finishing, trace every user-provided value through the generated
artifacts:

```text
workflow field -> widget type -> _input.py field/type -> manifest placeholder -> runtime arg
credential field -> credential config -> workflow credential reference -> runtime credential lookup
DAG node id -> dependency condition -> manifest node -> runtime workflow/activity
```

- Prefer typed properties over arbitrary string keys; use a node's typed
  property instead of `extraArgs` or raw `DAGNode.args`.
- Do not expose a runtime default as a setup field just to pass the same value
  through; omit it and let the runtime own its default.
- Placeholders must be exact workflow field names like `"{{include-filter}}"`;
  no dotted placeholders unless the consuming runtime supports them.
- Every manifest placeholder must have a matching workflow/credential field, or
  be intentionally supplied by the SDK/runtime and documented in the final note.
- Boolean and numeric controls use typed widgets and typed node properties —
  avoid stringly-typed contracts a user can typo into a runtime-only failure.

## Examples (derive the current set at runtime)

The tracked examples all amend `App.pkl` and are the public reference. **List
them live** — do not trust a baked list:

```bash
git ls-files 'contract-toolkit/examples/*/app.pkl' | sed 's#.*/examples/##;s#/app.pkl##'
```

At the time of writing the tracked set is: `minimal` (smallest contract, start
here), `full` (every overridable feature), `bundle` (multi-entrypoint), `card-split`
(multi-entrypoint where only one entrypoint is a card), `connection-ref`
(ConnectionRefInput), `publish-controls` (publish toggles), `fanin` (fan-in via
`dependsOn`), `deploy` (single-pool KEDA/resources), `pools` (named worker
pools), `scheduled` (cron background job), `behind-the-scenes`
(`marketplaceCard = false`), `agent-e2e` (agent-mode codegen). Re-run the command
above to catch additions or removals.

Node-specific coverage (popularity, query-intelligence placeholders) lives in
`contract-toolkit/tests/fixtures/*.pkl`, not in `examples/`.

## Canonical Apps (public first-party references)

When you need a real, end-to-end reference, inspect these public connector apps.
Note each one's caveat — they are pattern references, not version references
(none is on the current floor yet):

- **`atlanhq/atlan-mysql-app`** — canonical SQL/JDBC single-entrypoint with
  multi-mode auth (basic + IAM user + IAM role) and a full lineage-publish DAG.
  Caveat: still on legacy `NativeApp.pkl` and the archived toolkit URL — it is
  itself a migration candidate, so read it for the SQL/DAG patterns, not the
  primitive or the dependency URL.
- **`atlanhq/atlan-metabase-app`** — best modern `App.pkl` reference: REST auth,
  a rich multi-node DAG, and the correct "two code `@entrypoint`s, one marketplace
  card" shape via single-entrypoint contract mode (no `entrypoints` listing, so
  the `marketplaceCard` default applies). It has already migrated off the
  deprecated `emitEntrypoints = false` workaround — copy this pattern, not that.
- **`atlanhq/atlan-openapi-app`** — URL-vs-cloud import modes plus an app-owned
  external object-store credential contract (a `Credential.pkl` amend). Caveat:
  task-only (no DAG/publish) and on an older toolkit.

## Validation

Validate and generate with `pkl` from the app root (see Contract Commands) — a
clean `pkl eval` is the validation and writes the artifacts in one step:

```bash
( cd contract && pkl project resolve )
pkl eval -m . contract/app.pkl
```

Then verify generated output and invariants:

```bash
ls app/generated
python -m py_compile app/generated/_input.py
find app/generated -name '*.json' -print0 | xargs -0 -I{} python -c 'import json,sys; json.load(open(sys.argv[1])); print("OK", sys.argv[1])' {}
```

- Generated JSON filenames match the contract `name`.
- No `"{app_name}"` literal survives in `app/generated/`, and every baked
  `app_name` plus the `atlan.yaml` name equals the contract `name`.
- `manifest.json` placeholders match fields emitted by workflow and credential
  config.
- Credential config name matches the credential input type the workflow uses.
- SQL tree credential type and field names line up.
- Optional publish controls match the app's expected output behavior.
- Root `atlan.yaml` and generated artifacts agree on entrypoint names.
- `app/generated/_input.py` imports cleanly in the app's active environment.

For toolkit changes under `contract-toolkit/`, use the validation commands in
`contract-toolkit/CLAUDE.md` and keep examples, tests, and docs in sync.

## Finish

Summarize:

- What contract source changed and which migrations were applied.
- Which generated artifacts changed.
- Which `pkl` commands ran (resolve / eval), and the `app_name` invariant result.
- The SDK/toolkit versions before and after, relative to the floor.
- Any skipped validation and the concrete reason.
