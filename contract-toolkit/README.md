# app-contract-toolkit

PKL toolkit for defining Atlan app contracts. One `app.pkl` file per app generates all configuration artifacts — workflow config, credential config, and Automation Engine manifest.

## Quick Start

```pkl
// contract/app.pkl
amends "path/to/app-contract-toolkit/src/NativeApp.pkl"

import "path/to/app-contract-toolkit/src/Config.pkl"
import "path/to/app-contract-toolkit/src/Connectors.pkl"

name = "redshift"
displayName = "Redshift"
connector = Connectors.REDSHIFT
workflowType = "RedshiftMetadataExtractionWorkflow"
icon = "https://assets.atlan.com/assets/redshift.svg"
credentialConnectorType = "jdbc"

credentialCommonFields {
  new FieldSpec { name = "host"; displayName = "Host Name"; width = 6 }
  new FieldSpec { name = "port"; fieldType = "number"; defaultValue = "5439"; width = 2 }
}

credentialAuthOptions {
  ["basic"] = new AuthOption {
    label = "Basic"
    fields {
      new FieldSpec { name = "username" }
      new FieldSpec { name = "password"; sensitive = true }
    }
  }
}

uiConfig {
  tasks {
    ["Credential"] {
      inputs {
        ["credential-guid"] = new Config.CredentialInput { title = ""; credType = "atlan-connectors-redshift" }
      }
    }
    ["Connection"] {
      inputs {
        ["connection"] = new Config.ConnectionCreator { title = "" }
      }
    }
    ["Metadata"] {
      inputs {
        ["include-filter"] = new Config.SqlTree { title = "Include Metadata"; sqlQuery = "show atlan schemas" }
      }
    }
  }
}
```

Generate:
```bash
pkl eval -m contract/generated contract/app.pkl
```

Output:
```
contract/generated/
├── atlan-redshift.json              # Workflow config (UI form)
├── atlan-connectors-redshift.json   # Credential config (auth form)
├── manifest.json                    # AE DAG template
└── input.py                         # Python Pydantic Input class
```

The SDK auto-serves these at `GET /workflows/v1/configmap/{id}` and `GET /manifest`.

## What Gets Generated

### Workflow Config (`atlan-{name}.json`)

UI form schema fetched by the frontend during "Setup Workflow". Contains `properties` (form fields with widget types), `steps` (wizard progression), and `anyOf` (conditional visibility rules).

### Credential Config (`atlan-connectors-{name}.json`)

Auth form schema. Contains common fields (host, port), auth-type radio, and nested input panes per auth type. Auth panes are hidden by default and revealed via `anyOf` rules when the corresponding auth type is selected.

Credentials are shared — a user creates credentials once and reuses them across crawler and miner workflows.

### Manifest (`manifest.json`)

Automation Engine DAG template. Auto-derived from the declared workflow params — every form field becomes a `{{param}}` placeholder in the extract node. Heracles substitutes these with form values before sending to AE.

Default pipeline: `extract → publish`. Customizable via `extraNodes`. Set `hasPublishStep = false` for utility apps that don't publish to Atlas.

## Modules

### `NativeApp.pkl` — Base for Native Apps

The primary module for temporal-native apps. Developer amends this and declares:

**App metadata:**
- `name`, `displayName`, `connector`, `icon`, `workflowType`
- `taskQueuePrefix` (optional) — override task queue prefix for multi-manifest apps sharing a deployment
- `connectorConfigName` (optional) — override credential configmap name to share credentials across manifests

**Credential config (FieldSpec-based):**
- `credentialCommonFields` — fields shared across auth types (Listing of FieldSpec)
- `credentialAuthOptions` — auth types with per-type fields (Mapping of AuthOption)

**Workflow config:**
- `uiConfig` — form steps and fields using Config.pkl widget types

**DAG customization:**
- `hasPublishStep` — include publish node in DAG (default: `true`, set `false` for utility apps)
- `extraNodes` — additional pipeline nodes (DAGNode or PublishNode)

### `FieldSpec` — Credential Field Definition

Aligned with the SDK's `application_sdk.credentials.types.FieldSpec`. Single class for all credential fields:

```pkl
new FieldSpec {
  name = "host"              // Internal name (JSON key)
  displayName = "Host Name"  // UI label (auto-generated from name if omitted)
  placeholder = "db.example.com"
  helpText = "Database hostname"
  required = true            // Default: true
  sensitive = false          // Default: false (true → password widget)
  fieldType = "text"         // text|password|textarea|select|number|checkbox|url|email
  options = null             // For select — allowed values
  defaultValue = null        // Pre-filled value
  validationRegex = null     // Regex validation pattern
  width = 8                  // Grid width (8=full, 4=half)
  isHidden = false           // Default: false (true → hidden in UI)
  feedback = null            // Show validation feedback indicator
  message = null             // Inline validation message
  validationRules = null     // Custom validation rules
}
```

`sensitive = true` renders as a password widget. `fieldType = "select"` with ≤4 options renders as radio buttons, >4 as a dropdown. `isHidden = true` hides the field in the UI — use for fields with fixed values (e.g., a fixed API endpoint).

### `ConditionalFieldSpec` — Conditional Credential Field

Use `ConditionalFieldSpec` in place of `FieldSpec` when a credential field must emit `type: "conditional"` with a `conditions` array. Each entry is a `Config.Condition` which can match via simple `property` / `value` or via a compound `filter` (`$and` / `$or` tree over sibling form fields). `AuthOption.fields` and `AuthOption.extraFields` accept either.

```pkl
new ConditionalFieldSpec {
  name = "sap-port"
  conditions {
    new Config.Condition {
      filter = new Dynamic {
        `$or` {
          new Dynamic {
            `$and` {
              new Dynamic { ["connection-type"] = "application-server" }
              new Dynamic { ["extraction-method"] = "direct" }
            }
          }
        }
      }
      overrideUi = new Config.Widget { widget = "input"; label = "Port"; `hidden` = false }
    }
  }
}
```

See `examples/sap-s4/app.pkl` for the full SAP connection-type × extraction-method pattern.

### `AuthOption` — Auth Type Definition

```pkl
new AuthOption {
  label = "Basic"             // Radio button display text
  nestedLabel = ""            // Custom label for auth section wrapper
  nestedPlaceholder = null    // Custom placeholder for auth section wrapper
  fields {                     // Primary credential fields (FieldSpec or ConditionalFieldSpec)
    new FieldSpec { name = "username" }
    new FieldSpec { name = "password"; sensitive = true }
  }
  extraFields {                // Nested under "extra" in credential payload (FieldSpec or ConditionalFieldSpec)
    new FieldSpec { name = "database" }
  }
}
```

`extraFields` maps to the `extra` dict that Heracles stores in the object store alongside Vault secrets.

### `DAGNode` — Custom Pipeline Node

```pkl
new DAGNode {
  displayName = "Transform Data"
  workflowType = "TransformWorkflow"
  appName = "my-app"                              // Task queue: atlan-{appName}-{deployment_name}
  args { ["input"] = "$.extract.outputs.data" }   // JSONPath refs or {{param}} vars
  dependsOn { "extract" }
  errorHandling = new ErrorHandlingConfig {       // Optional — retry / timeout policy
    startToCloseTimeoutSeconds = 14400            // AE remaps to execution_timeout (workflow) / start_to_close (activity)
    maximumAttempts = 3
  }
}
```

Full `ErrorHandlingConfig` surface (all fields optional, maps 1:1 to AE): `initialInterval`,
`backoffCoefficient`, `maximumInterval`, `maximumAttempts`, `nonRetryableErrorTypes`,
`startToCloseTimeoutSeconds` (1–86400), `heartbeatTimeoutSeconds` (1–3600, activity-only).
See [`docs/reference.md`](docs/reference.md#errorhandlingconfig) for details.

### `PublishNode` — Pre-built Publish Node

```pkl
new PublishNode { upstream = "transform" }  // Just specify which node to read from
```

Auto-fills the 4 standard output paths, `connection_entity`, and boolean flags. Defaults to `upstream = "extract"`.

### `Config.pkl` — Workflow Widget Types

30 widget types for the workflow config form. Used inside `uiConfig.tasks`:

| Widget | Use Case |
|---|---|
| `TextInput` | Text fields |
| `PasswordInput` | Masked text |
| `NumericInput` | Numbers |
| `Radio` | Radio button group |
| `DropDown` | Single/multi-select dropdown |
| `BooleanInput` | Toggle |
| `ConnectionCreator` | Connection creation form |
| `ConnectionSelector` | Connection picker (`connectorFilter`, `connectionCategories`) |
| `CredentialInput` | Credential selector (links to credential config) |
| `SqlTree` | SQL-driven schema/database filter tree (`desc`, `databaseExcludePatterns` for inline text) |
| `APITree` | API-driven resource filter tree (`desc` for inline text) |
| `SageV2` | Preflight check runner |
| `NestedInput` | Grouped sub-elements |
| `ConditionalInput` | Conditional widget switching (`baseWidgetType`, `conditions`) — sqltree/text/connection |
| `AgentSelector` | Secure agent configuration (`agentConfigEntries`) |
| `CloudProvider` | Cloud provider selector |
| `InputRepeater` | Repeatable text input list — connection strings, tags, etc. |
| `evaluateWidget()` | Helper function returning a `Widget` for `Condition.overrideUi` — derives value from other form selections (hidden, no UI) |

### `Connectors.pkl` — Connector Registry

90+ connector type constants:
```pkl
Connectors.REDSHIFT   // { value = "redshift"; category = "warehouse" }
Connectors.SNOWFLAKE  // { value = "snowflake"; category = "warehouse" }
Connectors.POSTGRES   // { value = "postgres"; category = "database" }
```

### `Credential.pkl` — Legacy Credential Module

Used by `Config.pkl`-based (Argo-era) apps. Native apps use FieldSpec in NativeApp.pkl instead. Retained for backward compatibility.

### `Renderers.pkl` — Output Generators

Legacy renderers for Argo-era apps (K8s ConfigMap YAML, Argo WorkflowTemplate, Pydantic classes, Kotlin classes). NativeApp.pkl has its own rendering logic built-in.

## DAG Pipeline Examples

**Default (no configuration needed):**
```
extract → publish
```

**With custom nodes:**
```pkl
extraNodes {
  ["query-intelligence"] = new DAGNode {
    displayName = "Process Query Intelligence"
    workflowType = "QueryIntelligenceWorkflow"
    appName = "query-intelligence"
    args {
      ["connection_qualified_name"] = "$.extract.outputs.connection_qualified_name"
      ["query_data_prefix"] = "$.extract.outputs.query_data_prefix"
    }
    dependsOn { "extract" }
  }
  ["transform"] = new DAGNode {
    displayName = "Transform & Enrich"
    workflowType = "TransformWorkflow"
    appName = "redshift"
    args {
      ["raw_data"] = "$.extract.outputs.raw_data_prefix"
      ["qi_output"] = "$.query-intelligence.outputs.qi_output_prefix"
    }
    dependsOn { "query-intelligence" }
  }
  ["publish"] = new PublishNode { upstream = "transform" }
}
```

Produces: `extract → query-intelligence → transform → publish`

If `extraNodes` contains a `"publish"` key, the auto-generated default publish is replaced.

**No publish step (utility apps):**
```pkl
hasPublishStep = false  // Only extract node in manifest, no publish-related fields in input.py
```

## App Repo Structure

### Single-manifest app (default)

```
your-app/
├── contract/
│   ├── app.pkl                          # Single source of truth
│   └── PklProject                       # Package dependency
├── app/
│   ├── generated/                       # ATLAN_CONTRACT_GENERATED_DIR
│   │   ├── __init__.py                  # Auto-generated
│   │   ├── _input.py                    # Input class
│   │   ├── {name}.json
│   │   ├── atlan-connectors-{name}.json
│   │   └── manifest.json
│   └── ...
├── frontend/static/
└── main.py
```

### Multi-manifest app (crawler + miner sharing one deployment)

For apps that serve two marketplace tiles (e.g. `@atlan/teradata` + `@atlan/teradata-miner`)
from the same pod. Each tile gets its own manifest, UI config, and input contract.

```
your-app/
├── contract/
│   ├── credentials.pkl                  # Shared credential definitions
│   ├── crawler.pkl                      # Crawler contract (imports credentials.pkl)
│   ├── miner.pkl                        # Miner contract (imports credentials.pkl)
│   └── PklProject
├── app/
│   ├── generated/
│   │   ├── __init__.py
│   │   ├── crawler/
│   │   │   ├── __init__.py              # Auto-generated
│   │   │   ├── _input.py
│   │   │   ├── {name}.json
│   │   │   ├── atlan-connectors-{name}.json
│   │   │   └── manifest.json
│   │   └── miner/
│   │       ├── __init__.py              # Auto-generated
│   │       ├── _input.py
│   │       ├── {name}.json
│   │       ├── atlan-connectors-{name}.json
│   │       └── manifest.json
│   └── ...
└── main.py
```

Key patterns for multi-manifest:
- **Shared credentials**: Define `credentialCommonFields` and `credentialAuthOptions` in
  `credentials.pkl`, import from both contracts
- **Shared credential name**: Set `connectorConfigName = "atlan-connectors-teradata"` in
  miner to match crawler (otherwise toolkit derives `atlan-connectors-{name}`)
- **Shared task queue**: Set `taskQueuePrefix = "atlan-teradata"` in miner so both
  workflows poll the same queue (Temporal routes by workflow type)

Generate:
```bash
pkl eval -m app/generated/crawler contract/crawler.pkl
pkl eval -m app/generated/miner contract/miner.pkl
```

See `examples/teradata/` for a working multi-manifest example.

## SDK Integration

The SDK v3 handler service reads from `ATLAN_CONTRACT_GENERATED_DIR` (default: `app/generated`):
- `GET /workflows/v1/configmap/{id}` → reads `{id}.json`
- `GET /workflows/v1/manifest` → reads `manifest.json`
- `GET /workflows/v1/configmaps` → lists available configmap IDs

For SDK v2, set `ATLAN_CONTRACT_GENERATED_DIR=contract/generated` to read from the original location.

### Recommended poe task

```toml
[tool.poe.tasks]
generate.shell = "cd contract && pkl eval --ca-certificates ~/.pkl-ca-bundle.pem -m generated app.pkl && mkdir -p ../app/generated && cp generated/input.py ../app/generated/_input.py && cp generated/*.json ../app/generated/"
```

### Flat Manifest Args (v3)

Set `flatManifestArgs = true` in `app.pkl` to emit all workflow params as top-level keys in the manifest `args` (no `metadata` wrapper). This is the recommended pattern for SDK v3 apps where the workflow `Input` reads fields directly from args.

## Documentation

Docs live in three places — all must be updated together when changing PKL modules:

| File | Format | Content |
|------|--------|---------|
| `docs/reference.md` | Markdown | Detailed reference (classes, properties, examples) |
| `docs/index.html` | HTML | Standalone docs page (hand-written, not generated from reference.md) |
| `README.md` | Markdown | Quick start guide and widget summary tables |

## PKL Reserved Keywords in Dynamic Objects

When using `Dynamic` objects (e.g., inside `AgentSelector.agentConfigEntries`), some JSON keys collide with PKL reserved keywords:

| Keyword | Workaround | Notes |
|---------|-----------|-------|
| `hidden` | Use backtick escaping: `` `hidden` = true `` | Idiomatic PKL — the toolkit's `Widget` class uses this pattern |
| `default` | Use `Mapping`: `["default"] = ""` | Backtick escaping does **not** work for `default` in Dynamic — PKL silently drops it. Use `Mapping<String, Any>` instead |

For `AgentSelector.agentConfigEntries`, use `Listing<Any>` with nested `Mapping` objects when you need `"default"` keys in the output JSON. See `examples/teradata/credentials.pkl` for a working example.

## Testing Changes

After modifying any PKL module, regenerate all examples and verify no breakage:

```bash
pkl eval -m examples/redshift/generated examples/redshift/app.pkl
pkl eval -m examples/trino/generated examples/trino/app.pkl
pkl eval -m examples/monte-carlo/generated examples/monte-carlo/app.pkl
pkl eval -m examples/cosmos/generated examples/cosmos/app.pkl
pkl eval -m examples/mode/generated examples/mode/app.pkl
pkl eval -m examples/teradata/generated/crawler examples/teradata/crawler.pkl
pkl eval -m examples/teradata/generated/miner examples/teradata/miner.pkl
```

Every PR that touches `src/` must pass generation for all examples.

## Requirements

- [PKL](https://pkl-lang.org/) >= 0.25.1
- `brew install pkl` on macOS
