---
name: make-contract
description: Create, migrate, update, generate, or validate Atlan native app Pkl contracts with app-contract-toolkit and the Atlan CLI. Use when an agent is helping an app author produce contract/app.pkl, contract/PklProject, app/generated artifacts, atlan.yaml decisions, SDK input contracts, and contract validation.
---

# make-contract

Use this skill when the user asks to create, migrate, update, generate, or
validate an Atlan native app contract. The goal is to produce a correct Pkl
contract and regenerated SDK artifacts, not to hand-edit generated JSON.

## Hard Rules

- Treat `atlanhq/application-sdk` as the canonical source for this skill. The
  repo-local copies live at `.agents/skills/make-contract/SKILL.md` and
  `.claude/skills/make-contract/SKILL.md`; mirror any edits between them and do
  not add nested `make-contract` skill copies under `contract-toolkit/` or app
  repos.
- Use the Atlan CLI contract commands first. Fall back to direct `pkl` commands
  only when the CLI is missing, too old, or cannot support the requested change.
- Do not expose internal service, repository, or implementation details in
  generated guidance, examples, comments, or docs.
- Do not hand-edit generated files under `app/generated/` except to inspect or
  compare output. Fix `contract/app.pkl`, then regenerate.
- Keep examples public and generic. Prefer examples in this SDK repo under
  `contract-toolkit/examples/`.
- Preserve existing app behavior unless the user explicitly asks for a contract
  redesign.
- Never put secrets, real tokens, passwords, or tenant-specific data in
  contracts, examples, generated artifacts, or validation notes.

## Agent Workflow for App Authors

Most users are not asking for a PKL lecture; they are asking the agent to get a
working contract into their app. Work in this order:

1. Inspect the repo and infer the app shape before asking questions.
2. State the inferred contract plan in plain terms: app type, generated path,
   credential form, workflow setup fields, DAG shape, publish behavior, and
   `atlan.yaml` ownership.
3. Ask only blocking questions, usually no more than three at a time. Prefer
   concrete choices with a recommended default over broad open-ended prompts.
4. Implement in `contract/app.pkl` and `contract/PklProject`, then regenerate.
5. Verify the generated value flow from workflow config to manifest to
   `_input.py` to runtime expectations.
6. Finish with changed source files, generated artifacts, validation commands,
   and any assumptions or skipped checks.

When the user is unsure, choose the simplest behavior-preserving contract:
single entrypoint, `contract/app.pkl`, generated output in `app/generated`, root
`atlan.yaml` as the packaging source of truth, and default `extract -> publish`
only for asset-producing connectors.

## Expected Layout

For a normal single-entrypoint app:

```text
contract/
|-- PklProject
|-- PklProject.deps.json
`-- app.pkl
app/generated/
|-- __init__.py
|-- _input.py
|-- {name}.json
|-- atlan-connectors-{name}.json
`-- manifest.json
```

Bundle contracts can also render `app/generated/atlan.yaml` and per-entrypoint
folders. The root `atlan.yaml` in the app repo remains the packaging source of
truth unless the app has an explicit sync step.

## CLI Commands

Check the installed CLI before making assumptions:

```bash
command -v atlan
atlan app contract --help
atlan app contract init --help
atlan app contract generate --help
atlan app contract validate --help
atlan app contract update-toolkit --help
```

Common commands:

```bash
# Create a new contract scaffold in an app repo.
atlan app contract init <app-name> -p <app-dir> -c <connector-name>

# Validate Pkl without writing generated artifacts.
atlan app contract validate -p <app-dir>

# Generate app/generated artifacts from contract/app.pkl.
atlan app contract generate -p <app-dir>

# Update the toolkit dependency and refresh PklProject.deps.json.
atlan app contract update-toolkit -p <app-dir>
atlan app contract update-toolkit -p <app-dir> --version <semver>
```

The toolkit package is named `app-contract-toolkit`, but latest-version lookup
must come from the SDK repo releases, not the old standalone toolkit repo:

```bash
gh release list --repo atlanhq/application-sdk --limit 50 --json tagName --jq '[.[] | select(.tagName | startswith("contract-toolkit-v"))][0].tagName | sub("^contract-toolkit-v"; "")'
```

If the CLI cannot be used, say why and use the smallest fallback:

```bash
pkl project resolve <app-dir>/contract
pkl eval -m <app-dir>/app/generated <app-dir>/contract/app.pkl
```

If editing `contract/PklProject` manually, use the SDK-hosted package URL:

```pkl
dependencies {
  ["app-contract-toolkit"] {
    uri = "package://atlanhq.github.io/application-sdk/contracts/app-contract-toolkit@<VERSION>"
  }
}
```

## Discovery Checklist

Before editing, inspect the app shape:

```bash
git status --short --branch
find . -maxdepth 3 -name app.pkl -o -name PklProject -o -name atlan.yaml
rg -n "ATLAN_CONTRACT_GENERATED_DIR|app/generated|contract/generated|AppInputContract|ExtractionInput|CredentialInput|workflowType|workflow_type|manifest.json"
rg -n "get_configmap|get_manifest|/workflows/v1/configmap|/manifest" app application_sdk tests
```

Also check:

- Is this a new contract or migration from existing templates?
- Is it single-entrypoint or bundle-shaped?
- Which generated directory does the app actually serve?
- Is root `atlan.yaml` already present and intentionally maintained?
- Does the app need credential config, preflight, publish controls, connection
  reference, multiple auth modes, or custom runtime steps?

Ask the user only when local repo evidence cannot answer a behavior-preserving
choice.

## Value-Flow Rules

Before finishing, trace every user-provided value through the generated
artifacts:

```text
workflow field -> widget type -> _input.py field/type -> manifest placeholder -> runtime arg
credential field -> credential config -> workflow credential reference -> runtime credential lookup
DAG node id -> dependency condition -> manifest node -> runtime workflow/activity
```

Rules for stable contracts:

- Prefer typed properties over arbitrary string keys. If a node has a typed
  property, use it instead of `extraArgs` or raw `DAGNode.args`.
- Do not expose runtime defaults as setup fields just to pass through the same
  value. Omit them and let the runtime app own its defaults.
- Placeholders must be exact workflow field names like `"{{include-filter}}"`.
  Do not use dotted placeholders unless the consuming runtime explicitly
  supports them.
- Every manifest placeholder must have a matching workflow or credential field,
  or be intentionally supplied by the SDK/Heracles/runtime and documented in the
  final note.
- Boolean and numeric controls should use typed widgets and typed node
  properties. Avoid “stringly typed” contracts where the user can typo a key and
  only discover it at runtime.
- Keep user-facing labels clear and app-author friendly. Avoid exposing internal
  system names unless they are the stable public contract.

## Authoring Rules

Prefer typed toolkit APIs over raw JSON:

- `NativeApp.pkl` for standard single-entrypoint contracts.
- `NativeAppBundle.pkl` for apps with multiple entrypoints.
- `Config.pkl` widgets for workflow form fields.
- `Connectors.pkl` constants where available.
- Native credentials from `NativeApp.pkl`: `CredentialInput`, `FieldSpec`,
  `AuthOption`, `ConditionalFieldSpec`, `NamedWidget`, and `NamedProperty`.
- SQL/JDBC helpers where applicable: `JDBCUrlAuthOption`,
  `AdvancedJDBCUrlGroup`, `SqlTree`, and related filter widgets.
- Runtime graph helpers where applicable: `PublishNode`,
  `QueryIntelligenceNode`, `PopularityNode`, `LineageNode`,
  `LineagePublishNode`, then raw `DAGNode` only when typed nodes do not fit.

Do not use legacy credential modules for new native credentials unless preserving
an existing contract requires it.

Keep `flatManifestArgs = true` unless an existing app intentionally reads
metadata-wrapped args. If changing this, call it out explicitly because it
changes the runtime payload shape.

## Public Examples

Use the curated open-source examples in this repo. They are intentionally
generic and stable; app-specific wiring examples should live with sample apps or
the owning app repo, and test-only scenarios should live under
`contract-toolkit/tests/fixtures`.

- `contract-toolkit/examples/connection-ref`: selected connection object input.
- `contract-toolkit/examples/fanin`: dependency and fan-in graph behavior.
- `contract-toolkit/examples/openapi`: URL/cloud modes and extra credential
  output.
- `contract-toolkit/examples/postgres`: basic SQL/JDBC datasource.
- `contract-toolkit/examples/publish-controls`: publish toggle behavior.
- `contract-toolkit/examples/trino`: multi-catalog SQL tree and auth options.

For node-specific or app-wiring coverage, inspect fixtures instead of copying a
real app contract into public examples:

- `contract-toolkit/tests/fixtures/popularity_default.pkl`
- `contract-toolkit/tests/fixtures/popularity_dynamic_overrides.pkl`
- `contract-toolkit/tests/fixtures/qi_node_placeholders.pkl`

Do not point users at private app repos or internal consumer repos from this
skill.

## Validation

For app repos, run the CLI validation and generation:

```bash
atlan app contract validate -p .
atlan app contract generate -p .
```

Then verify generated output:

```bash
ls app/generated
python -m py_compile app/generated/_input.py
ruby -rjson -e 'ARGV.each { |f| JSON.parse(File.read(f)); puts "OK #{f}" }' app/generated/*.json
```

Check contract-specific invariants before finishing:

- Generated JSON filenames match the contract `name`.
- `manifest.json` placeholders match fields emitted by workflow and credential
  config.
- Credential config name matches the credential input type used by the workflow.
- SQL tree credential type and credential field names line up.
- Optional publish controls match the app's expected output behavior.
- Root `atlan.yaml` and generated artifacts agree on entrypoint names.
- `app/generated/_input.py` imports cleanly in the app's active environment.

For toolkit repo changes under `contract-toolkit/`, use the toolkit validation
commands from `contract-toolkit/AGENTS.md` and keep examples, tests, and docs in
sync.

## Finish

Summarize:

- What contract source changed.
- Which generated artifacts changed.
- Which CLI validation and generation commands ran.
- Any skipped validation and the concrete reason.
