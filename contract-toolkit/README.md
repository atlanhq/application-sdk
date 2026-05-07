# app-contract-toolkit

PKL toolkit for defining Atlan app contracts. One `app.pkl` file per app generates workflow config, credential config, Automation Engine manifest, and typed Python input models. Multi-entrypoint native apps can use `NativeAppBundle.pkl` to render the current flat `atlan.yaml` and re-export per-entrypoint contract outputs into the SDK layout.

## Quick Start

`contract/PklProject`:

```pkl
amends "pkl:Project"

dependencies {
  ["app-contract-toolkit"] {
    uri = "package://atlanhq.github.io/application-sdk/app-contract-toolkit/app-contract-toolkit@<LATEST_VERSION>"
  }
}
```

```pkl
// contract/app.pkl
amends "@app-contract-toolkit/NativeApp.pkl"

import "@app-contract-toolkit/Config.pkl"
import "@app-contract-toolkit/Connectors.pkl"

name = "trino"
displayName = "Trino"
connector = Connectors.TRINO
workflowTypeOverride = "TrinoMetadataExtractionWorkflow"
icon = "https://assets.atlan.com/assets/trino.svg"
credentialConnectorType = "jdbc"

credentialCommonFields {
  new FieldSpec { name = "host"; displayName = "Host"; width = 6 }
  new FieldSpec { name = "port"; fieldType = "number"; defaultValue = "443"; width = 2 }
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
        ["credential-guid"] = new Config.CredentialInput { title = ""; credType = "atlan-connectors-trino" }
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
cd contract
pkl project resolve
pkl eval -m ../app/generated app.pkl
touch ../app/generated/__init__.py
```

Output:
```
app/generated/
├── __init__.py
├── trino.json                       # Workflow config (UI form)
├── atlan-connectors-trino.json      # Credential config (auth form)
├── manifest.json                    # AE DAG template
└── _input.py                        # Python AppInputContract
```

The SDK auto-serves these from `ATLAN_CONTRACT_GENERATED_DIR`, defaulting to `app/generated`.

## Curated Examples

The `examples/` directory contains a small curated set of executable contracts
that teach stable toolkit patterns. App-specific wiring examples should live
with sample apps or the owning app repository; test-only scenarios belong under
`tests/fixtures/`.

- [`examples/connection-ref/`](examples/connection-ref/) — generic
  `ConnectionRefInput` usage.
- [`examples/fanin/`](examples/fanin/) — custom DAG fan-in and dependency
  conditions.
- [`examples/openapi/`](examples/openapi/) — OpenAPI spec loader contract with
  URL/cloud import modes and an extra object-store credential configmap emitted
  through `additionalOutputFiles`.
- [`examples/postgres/`](examples/postgres/) — basic open-source SQL datasource
  contract with JDBC credentials and SQL-tree filtering.
- [`examples/publish-controls/`](examples/publish-controls/) — explicit
  publish toggles and publish executor templating.
- [`examples/trino/`](examples/trino/) — multi-catalog SQL connector with
  reusable credential config and SQL-tree filtering.

## What Gets Generated

### Workflow Config (`{name}.json`)

UI form schema fetched by the frontend during "Setup Workflow". Contains `properties` (form fields with widget types), `steps` (wizard progression), and `anyOf` (conditional visibility rules).

### Credential Config (`atlan-connectors-{name}.json`)

Auth form schema. Contains common fields (host, port), auth-type radio, and nested input panes per auth type. Auth panes are hidden by default and revealed via `anyOf` rules when the corresponding auth type is selected.

Credentials are shared — a user creates credentials once and reuses them across crawler and miner workflows.

### Manifest (`manifest.json`)

Automation Engine DAG template. Auto-derived from the declared workflow params — fields included in the manifest become `{{param}}` placeholders in the extract node. Heracles substitutes these with form values before sending to AE.

Default pipeline: `extract → publish`. Customizable via `extraNodes`. Set `hasPublishStep = false` for utility apps that don't publish to Atlas. Apps that keep publish but do not implement the generated publish scaffolding fields can set `hasPublishInputFields = false`; apps that need dry-run or extract-only behavior can template `publishExecutorEnabled`.

## Modules

### `NativeApp.pkl` — Base for Native Apps

The primary module for temporal-native apps. Developer amends this and declares:

Generated `_input.py` extends `application_sdk.templates.contracts.ExtractionInput`.
SDK-owned fields such as `workflow_id`, `connection`, `credential_guid`,
`credential_ref`, `extraction_method`, `agent_json`, `include_filter`,
`exclude_filter`, `temp_table_regex`, `output_prefix`, `output_path`, and
`source_tag_prefix` are inherited rather than re-emitted, so SDK validators and
`CredentialRef.resolve(input)` keep their expected behavior.

**App metadata:**
- `name`, `displayName`, `connector`, `icon`, `workflowType`
- `taskQueuePrefix` (optional) — override task queue prefix for multi-entrypoint apps sharing a deployment
- `connectorConfigName` (optional) — override credential configmap name to share credentials across entrypoints

**Credential config (FieldSpec-based):**
- `credentialCommonFields` — fields shared across auth types (Listing of CredentialFieldEntry)
- `credentialSharedExtraFields` — fields shared under top-level credential `extra`
- `credentialAuthOptions` — auth types with per-type fields (Mapping of AuthOption)
- `credentialAuthDefault` / `credentialAuthHiddenEnumListForCreating` — default and creation-time hiding for the auth-type radio
- `credentialAuthPlaceholder` / `credentialAuthHelp` / `credentialAuthWidth` / `credentialAuthIncludeHiddenFlag` — control auth-type radio UI
- `credentialNamePlaceholder` — placeholder for hidden credential `name`
- `credentialConnectorDefault` / `credentialConfigIncludeTopLevelMetadata` — legacy credential-config parity controls
- `credentialUrlGroup` — opt-in `AdvancedJDBCUrlGroup` for Host↔URL JDBC credential forms (mssql, cloud-sql-postgres, alloydb-postgres). When set, every `credentialAuthOptions` entry must be a `JDBCUrlAuthOption`. See [AdvancedJDBCUrlGroup](#advancedjdbcurlgroup--hostrlarrowurl-jdbc-credential-form) below.

**Workflow config:**
- `uiConfig` — form steps and fields using Config.pkl widget types
- `workflowConfigIncludeTopLevelMetadata` — legacy workflow-config parity control
- `additionalOutputFiles` — explicit opt-in files to emit, for example a customized `AgentConfig.pkl` output

**DAG customization:**
- `hasPublishStep` — include publish node in DAG (default: `true`, set `false` for utility apps)
- `hasPublishInputFields` — append generated publish scaffolding fields to `_input.py` when publish is enabled (default: `true`)
- `publishExecutorEnabled` — value for the default publish node's `executor_enabled` arg; use `true`, `false`, or an exact placeholder like `"{{load-to-atlan}}"`
- `publishTagPipelineEnabled` / `publishTagAttachmentsPrefix` — publish-app tag pipeline args; by default these are emitted when the workflow form has `enable-tags` or legacy `enable-tag-sync`, using the visible `enable-tags` value when both exist and `$.extract.outputs.tag_attachments_prefix`
- `extractActivityDisplayName` / `publishActivityDisplayName` — optional compact labels for the built-in extract and default publish AE steps; these affect only manifest `activity_display_name`, not node ids, workflow types, task queues, app names, or args
- `extractNodeErrorHandling` / `publishNodeErrorHandling` — optional workflow-safe retry / timeout policy for the built-in extract and default publish AE steps; use `startToCloseTimeoutSeconds` for long-running child workflows and do not set `heartbeatTimeoutSeconds`
- `flatManifestArgs` / `manifestMetadataArgs` — choose top-level args or an explicit legacy `args.metadata` mapping
- `extraNodes` — additional pipeline nodes (`DAGNode`, `PublishNode`, `QueryIntelligenceNode`, `PopularityNode`, `LineageNode`, or `LineagePublishNode`)

### `NativeAppBundle.pkl` — Multi-Entrypoint App Bundle

Use this for one deployed native app that exposes multiple marketplace cards / SDK entrypoints from the same service, for example Teradata crawler + miner.

`NativeAppBundle.pkl` renders the current flat `atlan.yaml` shape and can render inline `NativeApp.pkl` entrypoint contracts from one root `app.pkl`:

```pkl
amends "@app-contract-toolkit/NativeAppBundle.pkl"

import "@app-contract-toolkit/NativeApp.pkl" as NativeApp
import "@app-contract-toolkit/Config.pkl"
import "@app-contract-toolkit/Connectors.pkl"

name = "teradata"      // deployment/runtime identity
appId = "019c4730-27cd-7a21-8bf7-e85766402e78"
displayName = "Teradata"
type = "connector"
iconUrl = "https://assets.atlan.com/assets/Teradata.svg"
selfDeployedRuntime = true
argoPackageNames {
  "@atlan/teradata"
  "@atlan/teradata-miner"
}

local sharedCredentialFields: Listing<NativeApp.FieldSpec> = new { /* host, port */ }
local sharedCredentialAuthOptions: Mapping<String, NativeApp.AuthOption> = new { /* basic, ldap */ }

local crawlerContract = (NativeApp) {
  name = "teradata-crawler"
  displayName = "Teradata Assets"
  connector = Connectors.TERADATA
  workflowTypeOverride = "teradata-app:crawler"
  icon = "https://assets.atlan.com/assets/Teradata.svg"
  connectorConfigName = "atlan-connectors-teradata"
  taskQueuePrefix = "atlan-teradata"
  credentialCommonFields = sharedCredentialFields
  credentialAuthOptions = sharedCredentialAuthOptions
  uiConfig { /* crawler setup form */ }
}

local minerContract = (NativeApp) {
  name = "teradata-miner"
  displayName = "Teradata Miner"
  connector = Connectors.TERADATA
  workflowTypeOverride = "teradata-app:miner"
  icon = "https://assets.atlan.com/assets/Teradata.svg"
  connectorConfigName = "atlan-connectors-teradata"
  taskQueuePrefix = "atlan-teradata"
  credentialCommonFields = sharedCredentialFields
  credentialAuthOptions = sharedCredentialAuthOptions
  uiConfig { /* miner setup form */ }
}

entrypoints {
  new Entrypoint {
    name = "crawler"   // SDK entrypoint + generated folder
    displayName = "Teradata Assets"
    type = "connector"
    source = "teradata"
    categories { "database"; "connector" }
    contract = crawlerContract
  }
  new Entrypoint {
    name = "miner"
    displayName = "Teradata Miner"
    type = "miner"
    source = "teradata"
    categories { "database"; "miner" }
    contract = minerContract
  }
}

deploy {
  ["execution_mode"] = "native"
  ["splitDeploymentEnabled"] = true
  ["containerPort"] = 8000
}

atlanYamlOverrides {
  ["deploy"] {
    ["replicaCount"] = 2
  }
}
```

Output:

```text
generated/
├── atlan.yaml
├── atlan-connectors-teradata.json
├── crawler/
│   ├── teradata-crawler.json
│   ├── manifest.json
│   └── _input.py
└── miner/
    ├── teradata-miner.json
    ├── manifest.json
    └── _input.py
```

Important defaults and switches:
- `name` is deployment identity, not card identity.
- `Entrypoint.name` is the SDK entrypoint and generated folder.
- The inline entrypoint contract still owns workflow config, manifest, credentials, and `_input.py`.
- Credential configmaps are hoisted to the bundle root and deduped by filename. Duplicate credential names with different content fail generation.
- `emitAtlanYaml = false` suppresses `atlan.yaml` while keeping generated entrypoint artifacts.
- `emitEntrypoints = false` suppresses the `entrypoints:` block when targeting marketplace paths that do not support entrypoint passthrough.
- `emitGeneratedArtifacts = false` renders only `atlan.yaml`.
- `metadata` appends top-level `atlan.yaml` keys; `atlanYamlOverrides` deep-merges exact overrides into the rendered yaml.
- `additionalOutputFiles` emits explicit root-level artifacts, for example a customized `AgentConfig.pkl` output.

### `AgentConfig.pkl` — Shared Secure-Agent Config

`AgentSelector.agentConfigEntries` may reference the shared secure-agent configmap by name:

```pkl
new Listing { "atlan-connectors-agent" }
```

The toolkit owns the canonical payload as an amendable `AgentConfig.pkl` contract. It is not emitted by every app or bundle because it is a shared/global configmap, not an app-specific config artifact.

Customization knobs:
- `propertyOverrides` deep-merges patches into existing secure-agent properties.
- `extraProperties` appends new fields under the generated `properties` map.
- `extraAnyOf` appends conditional requirement branches to the generated `anyOf`.
- `extraConfig` appends top-level metadata to the generated config payload.

Generate it separately when publishing that shared configmap:

```bash
pkl eval -m generated src/AgentConfig.pkl
```

Or opt in from an app and customize nested fields:

```pkl
import "@app-contract-toolkit/AgentConfig.pkl" as AgentConfig

local agentConfig = (AgentConfig) {
  propertyOverrides {
    ["secret-path"] {
      ["ui"] {
        ["label"] = "Secret Name"
        ["placeholder"] = "Enter secret name"
      }
    }
  }
}

additionalOutputFiles {
  ["\(agentConfig.name).json"] = agentConfig.configFile
}
```

### `FieldSpec` — Credential Field Definition

Aligned with the SDK's `application_sdk.credentials.types.FieldSpec`. Single class for all credential fields:

```pkl
new FieldSpec {
  name = "host"              // Internal name (JSON key)
  displayName = "Host Name"  // UI label (auto-generated from name if omitted)
  placeholder = "db.example.com"
  helpText = "Database hostname"
  required = true            // Default: true
  includeRequiredWhenFalse = false
  sensitive = false          // Default: false (true → password widget)
  fieldType = "text"         // text|password|textarea|select|number|checkbox|url|email
  options = null             // For select — allowed values
  defaultValue = null        // Pre-filled value
  validationRegex = null     // Regex validation pattern
  width = 8                  // Grid width (8=full, 4=half)
  rows = null                // TextInput rows; set 1 for compact pasted-newline fields
  isHidden = false           // Default: false (true → hidden in UI)
  feedback = null            // Show validation feedback indicator
  message = null             // Inline validation message
  validationRules = null     // Custom validation rules
  byocDisabled = null        // Emit BYOCdisabled for frontend credential UIs
  includeUiWidget = true
  includeUiLabel = true
  includeUiHidden = true
  includeUiPlaceholder = true
  includeUiGrid = true
  uiClass = null
  uiRequired = null
}
```

`sensitive = true` renders as a password widget. `fieldType = "select"` with ≤4 options renders as radio buttons, >4 as a dropdown. `isHidden = true` hides the field in the UI — use for fields with fixed values (e.g., a fixed API endpoint).
`rows` is emitted as `ui.rows` when non-null. Use it with `fieldType = "textarea"` for compact multiline-capable fields, for example `rows = 1` for certificate or key inputs that should stay visually short while still accepting pasted newlines.

The `includeUi*`, `uiClass`, `uiRequired`, and `includeRequiredWhenFalse` controls exist for legacy configmaps that need exact JSON parity; leave them at defaults for new apps.

### `ConditionalFieldSpec` — Conditional Credential Field

Use `ConditionalFieldSpec` in place of `FieldSpec` when a credential field must emit `type: "conditional"` with a `conditions` array. Each entry is a `Config.Condition` which can match via simple `property` / `value` (including booleans for switcher-driven fields) or via a compound `filter` (`$and` / `$or` tree over sibling form fields), then override `ui`, `overrideType` (emitted as `type`), enum values, required state, or default. `AuthOption.fields`, `AuthOption.extraFields`, and `AuthOption.postExtraFields` accept `FieldSpec`, `ConditionalFieldSpec`, `NamedWidget`, or `NamedProperty`.

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

For production connector-specific condition trees, keep the rendered credential
JSON close to the marketplace configmap you are replacing and add a targeted
Pkl test for the generated shape.

### `AuthOption` — Auth Type Definition

```pkl
new AuthOption {
  label = "Basic"             // Radio button display text
  nestedLabel = ""            // Custom label for auth section wrapper
  nestedPlaceholder = null    // Custom placeholder for auth section wrapper
  nestedHidden = true         // Default: true (set false to show default auth pane)
  nestedExtraLabel = ""        // Label for the per-option `extra` nested section
  nestedExtraHeader = "Advanced"  // Header text on `extra`; set null to omit
  anyOfRequiredFields {}       // Extra required keys in this auth option's anyOf branch
  fields {                     // Primary fields: CredentialFieldEntry
    new FieldSpec { name = "username" }
    new FieldSpec { name = "password"; sensitive = true }
  }
  extraFields {                // Nested under "extra". CredentialFieldEntry.
    new FieldSpec { name = "database" }
    new NamedWidget {          // Embed a Config.UIElement (ConnectionSelector / SqlTree / CustomWidget)
      name = "include-filter"  // supplies the JSON key UIElements don't have
      widget = new Config.SqlTree { title = "Schema"; sqlQuery = "show atlan schemas" }
    }
  }
  postExtraFields {            // Sibling fields emitted after the `extra` group
    new NamedProperty {         // Emit an already-rendered property verbatim
      name = "pre-requisites"
      property = new Mapping {
        ["ui"] = new Mapping { ["widget"] = "FivetranPrerequisites"; ["hidden"] = false }
      }
    }
  }
}
```

`extraFields` maps to the `extra` dict that Heracles stores in the object store alongside Vault secrets.
Use `NamedWidget` when a workflow-style `Config.UIElement` needs to appear in a credential form. `fields`, `extraFields`, and `postExtraFields` all accept `NamedWidget`; the wrapper supplies the JSON key and the wrapped widget serializes structurally.
Use `NamedProperty` only for legacy parity when the exact credential JSON intentionally omits normal schema keys:

```pkl
new NamedProperty {
  name = "pre-requisites"
  property = new Mapping {
    ["ui"] = new Mapping { ["widget"] = "FivetranPrerequisites"; ["hidden"] = false }
  }
}
```

`credentialSharedExtraFields` emits the same `extra` shape, but once at the
top level outside auth branches for metadata shared by all auth types.
`nestedExtraHeader` defaults to `"Advanced"`; JDBC-URL connectors typically
override per option (e.g. `"Configuration"` for Azure AD, `"Domain Configuration"` for NTLM).

### `AdvancedJDBCUrlGroup` — Host↔URL JDBC Credential Form

Opt-in primitive for SQL-family connectors that expose a Host/URL toggle in
their credential form (mssql, cloud-sql-postgres, alloydb-postgres). Set
`credentialUrlGroup` at the module level and author each auth type as a
`JDBCUrlAuthOption`. The toolkit emits:
- A `connectBy` radio (Host / URL).
- A `jdbcUrl` conditional wrapper with one `AdvancedJDBCUrlGroup` widget
  branch per (auth-type × extraction-method), plus per-branch
  `urlPartPropertyIdMapping` resolved against `credential-guid.*` or
  `agent-json.*` namespaces.
- Shared `host` / `port` / `extra.compiled_url` rows (enabled on Host mode,
  disabled on URL mode).
- A per-mode `auth-type` radio that can expose different enums for direct
  vs agent (e.g. `workload_identity_federation` agent-only on cloudsql).
- One nested pane per `JDBCUrlAuthOption` plus a shared `extra` section.
- An `anyOf` block enforcing the selected auth-option pane is present.

```pkl
credentialUrlGroup = new AdvancedJDBCUrlGroup {
  protocol = ""                               // "" for mssql; "postgresql+asyncpg" for postgres
  urlAddonBefore = "mssql+pyodbc://"          // prefix on the URL text input
  urlPlaceholder = "myhost:1433 or myhost.database.windows.net"
  urlHelp = "Format: host:port..."
  hostField = new FieldSpec { name = "host"; displayName = "Host"; width = 6 }
  portField = new FieldSpec { name = "port"; fieldType = "number"; defaultValue = "1433"; width = 2 }
  extraFields {                                // shared across auth-types
    new FieldSpec { name = "database" }
    new FieldSpec { name = "__enable_ssl"; fieldType = "select"; options { "yes"; "no" }; defaultValue = "yes" }
  }
}

credentialAuthOptions {
  ["basic"] = new JDBCUrlAuthOption {
    label = "Basic"
    nestedLabel = "Basic Authentication"       // pane label (distinct from radio label above)
    extractionModes { "direct"; "agent" }       // which branches this option appears in
    urlPartMapping = new UrlPartMapping {
      hostname = "host"                         // credential-form path; renderer prefixes with <namespace>.
      port = "port"
      pathname = "extra.database"
      searchParams {
        ["Encrypt"] = new UrlRef { field = "extra.__enable_ssl" }    // form-ref: resolved per namespace
        ["TrustServerCertificate"] = new UrlLiteral { value = "yes" }  // literal: verbatim
      }
    }
    fields {
      new FieldSpec { name = "username" }
      new FieldSpec { name = "password"; sensitive = true }
    }
  }
  // ... more auth options
}
```

**`UrlValue` sealed union** — disambiguates literal vs form-ref in
`searchParams`. `UrlLiteral { value = "yes" }` emits the string verbatim;
`UrlRef { field = "extra.__enable_ssl" }` emits `credential-guid.extra.__enable_ssl`
for direct branches and `agent-json.extra.__enable_ssl` for agent branches.

**Agent extraction wiring** — when using `credentialUrlGroup`, wire the
`agent-json.agentConfigEntries` field via the toolkit helper instead of
hand-maintaining a duplicate schema:

```pkl
["agent-json"] = new Config.AgentSelector {
  title = ""
  placeholderText = "Agent JSON"
  agentConfigEntries = module.generatedAgentJsonEntries(
    module.credentialUrlGroup!!,
    module.credentialAuthOptions!!,
    module.connectorConfigName
  )
}
```

**Invariants** (enforced at generation time with clear throws):
1. `credentialUrlGroup != null` requires `credentialAuthOptions` to be non-null and non-empty.
2. Every option in `credentialAuthOptions` must be a `JDBCUrlAuthOption`, not a plain `AuthOption`.

Add connector-specific tests before introducing a new JDBC URL contract shape;
the SDK repo intentionally keeps only curated public examples.

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
`startToCloseTimeoutSeconds` (1–259200), `heartbeatTimeoutSeconds` (1–3600, activity-only).
See [`docs/reference.md`](docs/reference.md#errorhandlingconfig) for details.

For toolkit-managed built-in nodes, use app-level fields instead of replacing
the generated node:

```pkl
extractNodeErrorHandling = new ErrorHandlingConfig {
  startToCloseTimeoutSeconds = 14400
}
publishNodeErrorHandling = new ErrorHandlingConfig {
  startToCloseTimeoutSeconds = 14400
}
```

Both built-in extract and default publish render `activity_name = "execute_workflow"`,
so AE treats them as workflow nodes. `heartbeatTimeoutSeconds` is activity-only
and is rejected on these app-level fields at `pkl eval` time. If you replace
publish with `extraNodes["publish"] = new PublishNode { ... }`, set
`errorHandling` on that `PublishNode` directly.

`dependsOn` emits two manifest shapes based on list length. A single entry keeps the
single-parent `"depends_on": {"node_id": "extract"}` form. Two or more entries emit AE's
native fan-in `"depends_on": {"and_conditions": [{"node_id": "left", "tag": "success"},
{"node_id": "right", "tag": "success"}]}` form, so the node waits for all listed parents
to succeed. Single-parent emission omits `tag` (fires on SUCCESS *or* FAILURE); adding a
second entry switches the node to strict-SUCCESS gating, so migrating single → multi
changes error-propagation semantics. See [`examples/fanin/`](examples/fanin/) for a
minimal fan-in example.

Use `dependsOnCondition` when you need AE's full condition object: direct node
conditions with tags, `andConditions`, `orConditions`, or nested combinations.
It is mutually exclusive with `dependsOn`. For pre-built nodes that define a
default `dependsOn`, set `dependsOn = null` before setting `dependsOnCondition`.

Pkl listing amendment matters for pre-built nodes. Nodes such as `PublishNode`,
`QueryIntelligenceNode`, and `LineageNode` define defaults like
`dependsOn { upstream }`. If you write `dependsOn { "qi"; "publish" }` inside
one of those nodes, Pkl appends to the default instead of replacing it; a
default `LineageNode` would render dependencies on `extract`, `qi`, and
`publish`. Use `dependsOn = new Listing { "qi"; "publish" }` for a clean
replacement.

```pkl
dependsOnCondition = new DependencyCondition {
  orConditions {
    new DependencyCondition { nodeId = "left"; tag = "failure" }
    new DependencyCondition { nodeId = "right"; tag = "failure" }
  }
}
```

`tag = "success"` and `tag = "failure"` map to AE's built-in completion tags.
Other tag values are matched against custom tags emitted by the upstream node.
AE extracts every referenced `nodeId` as a graph predecessor, so `orConditions`
are evaluated after all referenced predecessors finish or skip; they are not an
early-scheduling shortcut.

### `PublishNode` — Pre-built Publish Node

```pkl
new PublishNode { upstream = "transform" }  // Just specify which node to read from
new PublishNode { executorEnabled = "{{load-to-atlan}}" }  // Optional dry-run toggle
```

Auto-fills the 4 standard output paths, `connection_entity`, and boolean flags. Defaults to `upstream = "extract"` and `executorEnabled = true`.

### `QueryIntelligenceNode` — Pre-built QI Node

```pkl
new QueryIntelligenceNode {
  vendorName = "athena"
  sqlKey = "attributes.definition"
  catalogKey = "attributes.databaseName"
  schemaKey = "attributes.schemaName"
  timestampKey = ""
  mineOutputType = "json"
  parsingMode = "competitive"
  inputPrefix = "$.extract.outputs.transformed_data_prefix"
  outputPrefix = "$.extract.outputs.view_lineage_output_prefix"
}
```

Pre-built Query Intelligence workflow node. Defaults to `upstream = "extract"`,
`appName = "automation-engine"`, and
`taskQueue = "atlan-query-intelligence-{deployment_name}"`.

Mandatory fields:
- `sqlKey`
- one of `vendorName` or `vendorKey`

Usually overridden:
- `inputPrefix`
- `outputPrefix`
- `mineOutputType`
- `catalogKey`
- `schemaKey`

Cloud bucket and backend are resolved from the QI app's pod environment, so
`lakeProvider` / `storageBucket` are not exposed on this node. Set storage at
the QI app deployment, not in the contract.

`ignoreOrphans` and `indirectLineage` are `Boolean|String?` (HYP-793 pattern).
Use a Boolean literal for a static value, or an exact workflow placeholder
such as `"{{ignore-orphans}}"` to wire the value from a form field. Heracles
substitutes exact workflow parameter names only; dotted placeholders such as
`"{{control_config.indirect_lineage}}"` are not resolved and are stripped
before the workflow starts. Omit the fields to keep the QI app defaults.

### `PopularityNode` — Pre-built Popularity App Node

```pkl
new PopularityNode {
  tenantId = "$.extract.outputs.tenant_id"
  parsedDataPrefix = "$.extract.outputs.qi_output_prefix"
  minedDataPrefix = "$.extract.outputs.query_history_prefix"
  connectionCachePath = "$.extract.outputs.connection_cache_path"
  outputPrefix = "$.extract.outputs.popularity_output_prefix"
}
```

Pre-built Popularity workflow node. Defaults to `workflowType =
"PopularityWorkflow"`, `appName = "popularity"`, and `taskQueue =
"atlan-popularity-{deployment_name}"`. The default dependency is an explicit
AND fan-in on `qi` and `publish`, both tagged `success`, because popularity
needs parsed SQL from Query Intelligence and the SQLite connection cache from
asset publish.

Mandatory fields:
- `tenantId` — use a runtime/session-sourced value, not a user-editable form value
- `parsedDataPrefix` — Query Intelligence output prefix
- `minedDataPrefix` — source query-history miner output prefix
- `connectionCachePath` — exact SQLite cache object key or local file path, not a directory prefix
- `outputPrefix` — dedicated popularity output prefix

Usually overridden:
- `connectionQualifiedName`
- `queryIntelligenceNode` / `assetPublishNode` when your DAG uses different ids
- `waitForQueryIntelligence = false` when `parsedDataPrefix` points to precomputed parsed data
- `waitForAssetPublish = false` when `connectionCachePath` points to an existing cache

Optional popularity knobs:
- `accessHistoryDataPrefix`, `windowDays`, `topNQueries`, `topNUsers`, `lakeProvider`, `dryRun`
- `includeFilter`, `excludeFilter`, `parsedDataFilterHasError`, `relationshipsOutputAttribution`
- `queryTypesToIgnore` (omit for app defaults; set an empty listing to disable the default ignore list)
- `mineColumnMapping = new PopularityMineColumnMapping { ... }` for non-Snowflake miner schemas
- `extraArgs`

The Popularity app owns the runtime defaults for `windowDays`, `topNQueries`,
`topNUsers`, `dryRun`, `parsedDataFilterHasError`, and
`relationshipsOutputAttribution` (`30`, `5`, `10`, `false`, `false`, and
`false`). The toolkit omits these args by default; set them only when the
connector intentionally needs a fixed non-default value or exposes a workflow
setup control for the value. Use literals for fixed non-defaults, for example
`windowDays = 60`.

When a connector exposes Popularity scalar controls, set exact workflow
placeholders such as `"{{popularity-window-days}}"`. The renderer validates
these placeholders against `uiConfig.properties`: numeric properties must
reference an existing `Config.NumericInput`; Boolean properties must reference
an existing `Config.BooleanInput` or `Config.Switcher`. Malformed placeholders,
missing fields, or mismatched widget types fail PKL generation instead of
silently relying on Heracles placeholder stripping.

```pkl
uiConfig {
  tasks {
    ["Popularity"] {
      inputs {
        ["popularity-window-days"] = new Config.NumericInput {
          title = "Window Days"
          default = 60
          validationRules {
            new Dynamic { type = "number"; required = false; min = 1; message = "Window days must be at least 1." }
          }
        }
      }
    }
  }
}

extraNodes {
  ["popularity"] = new PopularityNode {
    // required prefixes omitted for brevity
    windowDays = "{{popularity-window-days}}"
  }
}
```

`extraArgs` is only for new Popularity app args that the toolkit does not model
yet. It cannot set first-class arg keys such as `window_days`; use the typed
`PopularityNode` property instead.

By default the node omits `lake_provider` so the Popularity app can use its own
trigger defaults and deployment environment. Set `lakeProvider` only as an
explicit storage override.

When relying on the standard `PublishNode` to produce the connection cache,
amend that publish node's args:

```pkl
["publish"] = new PublishNode {
  args {
    ["connection_cache_enabled"] = true
    ["connection_cache_via_app_enabled"] = true
  }
}
```

The SDK repository keeps richer popularity wiring scenarios under
`tests/fixtures/` so automated coverage does not turn app-specific contracts
into public examples.

### `LineageNode` — Pre-built Lineage App Node

```pkl
new LineageNode {
  connectorName = "athena"
  sqlUnquotedCase = "lower"
  ignoreAllCase = false
  dependsOn = null
  dependsOnCondition = new DependencyCondition {
    andConditions {
      new DependencyCondition { nodeId = "qi"; tag = "success" }
      new DependencyCondition { nodeId = "publish"; tag = "success" }
    }
  }
}
```

Pre-built Lineage app workflow node. Defaults target the native JSON/view-lineage
flow used by Athena: `upstream = "extract"`, `appName = "automation-engine"`,
`taskQueue = "atlan-lineage-{deployment_name}"`, `fileType = "json"`,
`inputPath = ""`, `parsedViewsPath = "$.extract.outputs.view_lineage_output_prefix"`,
`lineageOutputPath = "$.extract.outputs.lineage_stage_prefix"`, and
`cachePath = "connection-cache"`. By default the node omits `lake_provider` and
`cloud_storage_bucket` so the Lineage app can resolve tenant object-store
settings from its deployment environment. Set `lakeProvider` and
`cloudStorageBucket` only as explicit transitional or non-default storage
overrides.

Mandatory fields:
- `connectorName`
- `sqlUnquotedCase`
- `ignoreAllCase`

Usually overridden:
- `dependsOn` (use `dependsOn = new Listing { ... }` when replacing the default)
- `lakeProvider` / `cloudStorageBucket` only when overriding system-app env storage defaults
- `inputPath` / `fileType` for parquet miner-style lineage
- `parsedViewsPath` / `parsedViewsCurrentState`
- `columnMapping` / `jsonKeyMapping`
- `lineageCurrentState`

Specialized pass-through knobs:
- `connectionQualifiedName`, `sessionKey`, `stagePath`, `localOutputPrefix`
- `azureBlob`, `isTestEnv`, `enableSharded`, `enableParentJobScoping`, `enableFallbackMatch`, `includeIndirectColumnRelations`
- `processRoutines`, `routineMappingsPath`, `sortColumns`, `workflowName`, `runName`, `tenantId`, `packageName`, `gudusoftVersion`
- `startTimeEpoch`, `endTimeEpoch`, `maxParallelChunks`, `maxRowsPerChunk`, `ingestGroupSize`, `maxParallelIngestGroups`
- `enableAccuracyTest`, `traceDir`, `checkpointDir`, `resumeFromStage`, `extraArgs`

### `LineagePublishNode` — Pre-built Lineage Publish Node

```pkl
new LineagePublishNode {}
```

Pre-built publish node for lineage output. Defaults to the lineage-specific
publish app args used by Athena: `workflowType = "PublishWorkflow"`,
`appName = "publish"`, `taskQueue = "atlan-publish-{deployment_name}"`,
`transformedDataPrefix = "$.extract.outputs.lineage_stage_prefix"`,
`publishStatePrefix = "$.extract.outputs.lineage_publish_state_prefix"`,
`currentStatePrefix = "$.extract.outputs.lineage_current_state_prefix"`,
`cacheNamespace = "lineage"` rendered as `cache_namespace`, `connection_creation_enabled = false`, and the
connection/current-state app-cache flags enabled.

The default dependency is an explicit `dependsOnCondition` AND fan-in on
`lineage-app` and `publish`, both tagged `success`, so lineage publish waits
for both the Lineage app output and the main asset publish to succeed. Override
`upstream`, `assetPublishNode`, or `extractNode` only when your DAG uses
different node ids. Set `waitForAssetPublish = false` when your `LineageNode`
already depends on asset publish and lineage publish only needs to wait for
`lineage-app`.

Set `cacheNamespace = "miner-lineage"` or another unique namespace when a
multi-entrypoint app needs lineage publish state/cache isolation between
crawler, miner, or other lineage-producing workflows.

Specialized publish overrides:
- `waitForAssetPublish`, `connectionEntity`, `connectionCacheEnabled`, `connectionCacheViaAppEnabled`, `currentStateViaAppEnabled`
- `cacheNamespace`
- `extraArgs`

### `Config.pkl` — Workflow Widget Types

35+ widget types for the workflow config form. Used inside `uiConfig.tasks`:

`UIStep.titleOverride` / `idOverride` and `UIElement` emission controls
(`emitUiLabel`, `emitUiHidden`, `emitUiHelp`, `emitUiGrid`, `uiClass`,
`uiRequired`, `includeInManifest`, `includeInInput`) exist for exact parity
with older hand-authored configmaps and static UI-only fields.
Leave them at defaults for new apps.

| Widget | Use Case |
|---|---|
| `TextInput` | Text fields |
| `PasswordInput` | Masked text |
| `NumericInput` | Numbers |
| `Radio` | Radio button group |
| `DropDown` | Single/multi-select dropdown |
| `BooleanInput` | Toggle |
| `ConnectionCreator` | Connection creation form |
| `ConnectionSelector` | Connection picker (`connectorFilter`, `connectorFilters` for multi-type, `connectionCategories`, `selectedConnectorName`, `selectedCredentialGuid`, `emitMode`, `emitStart`) |
| `ConnectionRefInput` | Connection picker that returns full `ConnectionRef` object(s), including `attributes.qualifiedName` and `attributes.defaultCredentialGuid` (`multiSelect`, `connectorFilter`, `connectorFilters`, `connectionCategories`) |
| `CredentialInput` | Credential selector (links to credential config; supports view overlays via `authTypeVsLabel`, `hiddenFields`, `additionalDisplayFields`) |
| `SqlTree` | SQL-driven schema/database filter tree (`multiSelect` emits `ui.multiple`, `desc`, `databaseExcludePatterns`, `dependsOn`, `databasesUnselectable`, `additionalPropertiesToIncludeInCredentialBody`, `validationRules`, `includeDefault`) |
| `APITree` | Legacy API-driven resource filter tree (`desc` for inline text); emits `{}` defaults and generated input accepts JSON object strings for start-payload compatibility |
| `ApiTreeSelect` | Workflows-v2 API tree selector (`apiTreeSelect`) used by Power BI-style metadata pickers; emits object filters with `{}` defaults and JSON object string compatibility |
| `InfoBanner` | Static markdown banner (`infoBanner` / `InfoBanner`) with type, icon, and link; omitted from manifest and `_input.py` by default |
| `DsnTreeMap` | DSN-to-connection mapping widget (`dsnTreeMap`); emits object mappings |
| `GlossarySelector` | Glossary picker (`GlossarySelector`); emits selected glossary data as a JSON string |
| `Switcher` | Boolean switch (`switcher`) with optional title and toast feedback; can be embedded in credentials via `NamedWidget` |
| `Sage` / `SageV2` | Preflight check runner (`connectorConfig` + `selectedCredentialGuid` route per-dialect checks to a selected connection's configmap) |
| `NestedInput` | Grouped sub-elements |
| `ConditionalInput` | Conditional widget switching (`baseWidgetType`, `conditions`) — sqltree/text/connection/credential/dsnTreeMap/InfoBanner; use `outputValueType = "object"` when a conditional branch returns an object |
| `AgentSelector` | Secure agent configuration (`agentConfigEntries`; references shared `atlan-connectors-agent`, generated separately via `AgentConfig.pkl`) |
| `CloudProvider` | Cloud provider selector |
| `InputRepeater` | Repeatable text input list — connection strings, tags, etc. |
| `CustomWidget` | Escape hatch for bespoke frontend components — pass `widgetName` + arbitrary `props`; optional `valueType` sets the emitted JSON-schema `type` (defaults to `"string"`) |
| `evaluateWidget()` | Helper function returning a `Widget` for `Condition.overrideUi` — derives value from other form selections (hidden, no UI) |

`InfoBannerType` is the banner style union accepted by `InfoBanner.bannerType`:
`"info"`, `"warning"`, `"error"`, `"prerequisites"`, or `"side-note"`.
`WidgetLink` is the reusable link object for banner links: `label`, `url`, and
optional `icon`.

When adding a workflows-v2 widget, add a focused example or Pkl test for the
frontend shape that the app-playground should render.

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
  ["qi"] = new QueryIntelligenceNode {
    vendorKey = "datasource/vendor"
    sqlKey = "rendered_source"
    catalogKey = "datasource/database"
    schemaKey = ""
    timestampKey = ""
    mineOutputType = "json"
    inputPrefix = "$.extract.outputs.transformed_data_prefix"
    outputPrefix = "$.extract.outputs.qi_output_prefix"
  }
  ["transform"] = new DAGNode {
    displayName = "Transform & Enrich"
    workflowType = "TransformWorkflow"
    appName = "redshift"
    args {
      ["raw_data"] = "$.extract.outputs.raw_data_prefix"
      ["qi_output"] = "$.extract.outputs.qi_output_prefix"
    }
    dependsOn { "qi" }
  }
  ["publish"] = new PublishNode { upstream = "transform" }
}
```

Produces: `extract → qi → transform → publish`

Prefer passing a known QI output prefix forward (for example
`$.extract.outputs.qi_output_prefix`) instead of relying on
`$.qi.outputs.output_prefix`.

If `extraNodes` contains a `"publish"` key, the auto-generated default publish is replaced.

**No publish step (utility apps):**
```pkl
hasPublishStep = false  // Only extract node in manifest, no publish-related fields in _input.py
```

**Publish step without generated publish inputs:**
```pkl
hasPublishInputFields = false
publishExecutorEnabled = "{{load-to-atlan}}"
```

See [`examples/publish-controls/`](examples/publish-controls/) for a minimal app that declares its own `load-to-atlan` boolean and flows it to the publish node.

**Publish-app tag pipeline defaults:**
```pkl
["enable-tags"] = new Config.Radio {
  title = "Import tags"
  possibleValues { ["false"] = "False"; ["true"] = "True" }
  default = "true"
}
```

When the workflow form contains `enable-tags`, `PublishNode` automatically
emits `tag_pipeline_enabled = "{{enable-tags}}"` and
`tag_attachments_prefix = "$.extract.outputs.tag_attachments_prefix"`. This
matches the Databricks native contract: the extract workflow returns
`tag_attachments_prefix` pointing at the connector-emitted
`{output_path}/transformed/tag_attachments` payloads, and publish-app uses it
for typedef resolution and classification enrichment. Without
`tag_pipeline_enabled`, publish-app can receive TagAttachment entities while
still stripping classifications from the Atlas payloads. Override
`publishTagPipelineEnabled` or
`publishTagAttachmentsPrefix` only when an app uses a different form field or
extract output key.

If both `enable-tags` and legacy `enable-tag-sync` exist, the toolkit prefers
`enable-tags`. This matches native Databricks, where `enable-tags` is the
visible user-facing field and `enable-tag-sync` is a hidden compatibility alias.
Apps that only have `enable-tag-sync` still get
`tag_pipeline_enabled = "{{enable-tag-sync}}"`.

**Compact Automation Engine step labels:**
```pkl
extractActivityDisplayName = "Extract"
publishActivityDisplayName = "Publish"
```

Use these when the app `displayName` is intentionally descriptive but too long
for the AE step list. The defaults remain `"Extract {displayName} Metadata"` and
`"Publish to Atlas"` so existing manifests do not change unless an app opts in.
For custom publish nodes declared through `extraNodes["publish"]`, set
`displayName` on the `PublishNode` itself because the app-level
`publishActivityDisplayName` only applies to the toolkit-generated default
publish node.

**Built-in node retry / timeout policy:**
```pkl
extractNodeErrorHandling = new ErrorHandlingConfig {
  startToCloseTimeoutSeconds = 14400
}
publishNodeErrorHandling = new ErrorHandlingConfig {
  startToCloseTimeoutSeconds = 14400
}
```

Use these for long-running built-in extract or default publish child workflows.
They emit manifest `error_handling` without requiring an `extraNodes` override.
Because these nodes are workflow nodes, `heartbeatTimeoutSeconds` is invalid;
use only workflow-safe retry fields and `startToCloseTimeoutSeconds`. See
[`examples/publish-controls/`](examples/publish-controls/) for a compact example.

## App Repo Structure

### Single-manifest app (default)

```
your-app/
├── atlan.yaml                          # Root app metadata; required by app tooling
├── .gitignore                          # Must ignore app/generated/frontend/static
├── contract/
│   ├── app.pkl                          # Single source of truth
│   ├── PklProject                       # Package dependency
│   └── PklProject.deps.json             # Resolved package lock
├── app/
│   ├── generated/                       # ATLAN_CONTRACT_GENERATED_DIR
│   │   ├── __init__.py                  # Auto-generated
│   │   ├── _input.py                    # Input class
│   │   ├── {name}.json
│   │   ├── atlan-connectors-{name}.json
│   │   ├── manifest.json
│   │   └── frontend/static/             # App playground dist; ignored, not committed
│   └── ...
└── main.py
```

### Multi-entrypoint app (crawler + miner sharing one deployment)

For apps that serve two marketplace tiles (e.g. `@atlan/teradata` + `@atlan/teradata-miner`)
from the same pod. Each tile gets its own manifest, UI config, and input contract.

```
your-app/
├── atlan.yaml                          # Root app metadata; generated copy must be synced here
├── .gitignore                          # Must ignore app/generated/frontend/static
├── contract/
│   ├── app.pkl                          # NativeAppBundle + inline entrypoint contracts
│   ├── PklProject
│   └── PklProject.deps.json
├── app/
│   ├── generated/
│   │   ├── __init__.py
│   │   ├── atlan.yaml                   # Generated copy only; root atlan.yaml is required
│   │   ├── atlan-connectors-teradata.json
│   │   ├── frontend/static/             # App playground dist; ignored, not committed
│   │   ├── crawler/
│   │   │   ├── __init__.py              # Auto-generated
│   │   │   ├── _input.py
│   │   │   ├── {name}.json
│   │   │   └── manifest.json
│   │   └── miner/
│   │       ├── __init__.py              # Auto-generated
│   │       ├── _input.py
│   │       ├── {name}.json
│   │       └── manifest.json
│   └── ...
└── main.py
```

Key patterns for multi-entrypoint:
- **Root bundle**: Use `NativeAppBundle.pkl` to define deployment metadata, `entrypoints`, and `deploy`.
- **Shared credentials**: Define shared `credentialCommonFields` and `credentialAuthOptions` as locals in `app.pkl`, then reference them from each inline entrypoint contract.
- **Entrypoint config names**: Use names like `teradata-crawler` / `teradata-miner` so workflow configmap filenames match marketplace card ids.
- **Shared credential name**: Set `connectorConfigName = "atlan-connectors-teradata"` in
  each entrypoint contract. The bundle emits the identical credential config once at the root.
- **Shared task queue**: Set `taskQueuePrefix = "atlan-teradata"` in each entrypoint contract so both
  workflows poll the same queue (Temporal routes by workflow type)

Generate:
```bash
cd contract
pkl project resolve
pkl eval -m ../app/generated app.pkl
```

The SDK repo keeps the public example set intentionally small. Add a focused
multi-entrypoint example or test in the same PR when changing bundle behavior.

## SDK Integration

The SDK v3 handler service reads from `ATLAN_CONTRACT_GENERATED_DIR` (default: `app/generated`):
- `GET /workflows/v1/configmap/{id}` → reads `{id}.json`
- `GET /manifest` → reads `manifest.json`
- `GET /workflows/v1/configmaps` → lists available configmap IDs

For SDK v2, set `ATLAN_CONTRACT_GENERATED_DIR=contract/generated` to read from the original location.

### Atlan CLI generation

In consuming app repos, prefer the Atlan CLI when available:

```bash
atlan app contract validate
atlan app contract generate
```

The CLI writes generated artifacts to `app/generated` by default. If the CLI is unavailable, use the direct `pkl` command shown above.

### Recommended poe task

```toml
[tool.poe.tasks]
generate.shell = "cd contract && pkl project resolve && pkl eval -m ../app/generated app.pkl && touch ../app/generated/__init__.py"
```

### App playground

For local UI validation in consuming app repos, install the app playground into the generated folder. This is built frontend dist and should never be committed.

```bash
git check-ignore app/generated/frontend/static
npx @atlanhq/app-playground@3.1.0 install-to app/generated/frontend/static
atlan app run -p .
```

`atlan app run -p .` serves the app and playground UI on port `8000`. Run it after contract generation and static validation.

If `git check-ignore` does not match, add `app/generated/frontend/static/` to the consuming app's `.gitignore` before installing the playground.

### Flat Manifest Args (v3)

`flatManifestArgs` defaults to `true`, so workflow params are emitted as top-level keys in the manifest `args` (no `metadata` wrapper). This matches generated SDK v3 `_input.py` fields, where the workflow `Input` reads fields directly from args.

Set `flatManifestArgs = false` only for legacy workflows that intentionally read non-top-level fields from `args.metadata`.

## Documentation

Docs live in two places — both must be updated together when changing PKL modules:

| File | Format | Content |
|------|--------|---------|
| `docs/reference.md` | Markdown | Detailed reference (classes, properties, examples) |
| `README.md` | Markdown | Quick start guide and widget summary tables |

## PKL Reserved Keywords in Dynamic Objects

When using `Dynamic` objects (e.g., inside `AgentSelector.agentConfigEntries`), some JSON keys collide with PKL reserved keywords:

| Keyword | Workaround | Notes |
|---------|-----------|-------|
| `hidden` | Use backtick escaping: `` `hidden` = true `` | Idiomatic PKL — the toolkit's `Widget` class uses this pattern |
| `default` | Use `Mapping`: `["default"] = ""` | Backtick escaping does **not** work for `default` in Dynamic — PKL silently drops it. Use `Mapping<String, Any>` instead |

For `AgentSelector.agentConfigEntries`, use `Listing<Any>` with nested `Mapping` objects when you need `"default"` keys in the output JSON.

## Testing Changes

After modifying any PKL module, regenerate all examples and verify no breakage:

```bash
./scripts/regenerate-all.sh            # regenerates every example under examples/
./scripts/check-invariants.sh          # invariant checks on generated output
pkl test tests/*.pkl                   # Pkl behavior tests
python scripts/test-sdk-import.py      # imports every generated _input.py against SDK deps
```

Every PR that touches `src/` must pass generation for all examples.

## Releasing

Releases are **manual** — triggered from the GitHub Actions tab, never on push.

1. Go to **Actions → Release Contract Toolkit → Run workflow** in the
   `atlanhq/application-sdk` repository.
2. Enter the target version (e.g. `1.0.0`). The workflow validates it as
   semver-greater-than-current before bumping.
3. Submit. The workflow bumps `contract-toolkit/src/PklProject`, regenerates
   `contract-toolkit/CHANGELOG.md` via `git-cliff`, publishes the package under
   `app-contract-toolkit/` on the `application-sdk` GitHub Pages branch, tags
   `contract-toolkit-v<version>`, and creates the GitHub Release.

## Requirements

- [PKL](https://pkl-lang.org/) >= 0.25.1
- `brew install pkl` on macOS
- Python 3 for invariant checks and SDK import validation
- `ruff` or `uvx` for formatting generated `_input.py` files during `./scripts/regenerate-all.sh`
- Application SDK import dependencies for `python scripts/test-sdk-import.py`
- Atlan CLI for consuming app workflows (`atlan app contract validate`, `atlan app contract generate`, `atlan app run -p .`)
- Node.js with `npm` / `npx` only when installing the app playground
