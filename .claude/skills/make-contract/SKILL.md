---
name: make-contract
description: Create, migrate, update, generate, or validate Atlan native app Pkl contracts with app-contract-toolkit and the Atlan CLI. Use for contract/app.pkl, contract/PklProject, app/generated artifacts, atlan.yaml ownership, SDK input contracts, and contract validation.
---

# make-contract

Use this skill when the user asks to create, migrate, update, generate, or
validate an Atlan native app contract. The goal is to produce a correct Pkl
contract and regenerated SDK artifacts, not to hand-edit generated JSON.

## Hard Rules

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

If the CLI cannot be used, say why and use the smallest fallback:

```bash
pkl project resolve <app-dir>/contract
pkl eval -m <app-dir>/app/generated <app-dir>/contract/app.pkl
```

If editing `contract/PklProject` manually, use the SDK-hosted package URL:

```pkl
dependencies {
  ["app-contract-toolkit"] {
    uri = "package://atlanhq.github.io/application-sdk/app-contract-toolkit/app-contract-toolkit@<VERSION>"
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

Use the curated open-source examples in this repo:

- `contract-toolkit/examples/postgres`: basic SQL/JDBC datasource.
- `contract-toolkit/examples/clickhouse`: migration-shaped SQL datasource.
- `contract-toolkit/examples/trino`: multi-catalog SQL tree and auth options.
- `contract-toolkit/examples/openapi`: URL/cloud modes and extra credential
  output.
- `contract-toolkit/examples/connection-ref`: selected connection object input.
- `contract-toolkit/examples/fanin`: dependency and fan-in graph behavior.
- `contract-toolkit/examples/publish-controls`: publish toggle behavior.

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
