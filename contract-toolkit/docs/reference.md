# App Contract Toolkit — Reference Documentation

One `.pkl` contract generates all configuration artifacts for an Atlan native app: workflow config, credential config, AE manifest, and typed Python dataclass. Multi-manifest apps (e.g., crawler + miner) use multiple `.pkl` files with shared credential definitions.

## Architecture

```
contract/app.pkl (developer writes)
    │
    ├─▶ {name}.json                  — Workflow config (setup form)
    ├─▶ atlan-connectors-{name}.json — Credential config (auth form)
    ├─▶ manifest.json                — AE DAG (extract → publish)
    └─▶ input.py                     — Python Pydantic Input class
```

**Consumption flow:**
```
PKL generates → SDK serves at /workflows/v1/configmap/{id}
             → SDK wraps in K8s ConfigMap format
             → Frontend JSON.parse(data.data.config)
             → Renders setup wizard with steps/properties/anyOf
             → User submits → Heracles substitutes {{params}} in manifest
             → AE orchestrates DAG
```

---

## Package Setup

### PklProject

Always use the latest release. Check for the latest version:
```bash
gh release list --repo atlanhq/app-contract-toolkit --limit 1 --json tagName --jq '.[0].tagName'
```

```pkl
amends "pkl:Project"

dependencies {
  ["app-contract-toolkit"] {
    uri = "package://atlanhq.github.io/app-contract-toolkit/app-contract-toolkit@<LATEST_VERSION>"
  }
}
```

Resolve after creating or updating the version:
```bash
cd contract && pkl project resolve
```

To bump an existing project to the latest toolkit version, update the version in `PklProject`, then re-resolve:
```bash
# Update version in PklProject, then:
cd contract && pkl project resolve && pkl eval -m generated app.pkl
```

### app.pkl Imports

```pkl
amends "@app-contract-toolkit/NativeApp.pkl"
import "@app-contract-toolkit/Config.pkl"
import "@app-contract-toolkit/Connectors.pkl"
```

### Generate

```bash
cd contract && pkl eval -m generated app.pkl
```

---

## NativeApp.pkl — Base Module

Developers amend this module. It defines the app's identity, credentials, workflow form, and manifest.

### App Metadata

| Property | Type | Default | Description |
|---|---|---|---|
| `name` | String | required | App slug (e.g., `"redshift"`). Drives configmap names, task queues, file names. |
| `displayName` | String | `name.capitalize()` | UI display name. |
| `connector` | Connectors.Type | required | Connector from the registry. |
| `icon` | String | required | Icon URL. |
| `docsUrl` | String | `""` | Documentation link. |
| `logo` | String | `icon` | Logo URL. |
| `helpdeskLink` | String | `""` | Helpdesk link for credential form. |

### Workflow & Manifest

| Property | Type | Default | Description |
|---|---|---|---|
| `workflowType` | String | `""` | Python App class name in PascalCase (e.g., `"TeradataCrawlerApp"`). Auto-converted to kebab-case in the manifest (`teradata-crawler-app`), matching the SDK's `_pascal_to_kebab` convention. Either this or `workflowTypeOverride` must be set. |
| `workflowTypeOverride` | String? | null | Explicit workflow type for the manifest, used as-is (no conversion). Use when the Python class sets a custom `name` ClassVar that doesn't follow PascalCase→kebab convention. When set, takes priority over `workflowType`. |
| `taskQueuePrefix` | String | `"atlan-{name}"` | Task queue prefix for the extract node. Full queue: `{taskQueuePrefix}-{deployment_name}`. Override for multi-manifest apps sharing a deployment (e.g., `taskQueuePrefix = "atlan-teradata"` for both crawler and miner). |
| `hasPublishStep` | Boolean | `true` | Includes publish node in manifest DAG and appends publish-related fields to Input class. |
| `credentialFieldName` | String? | `"{name}_credential"` | Credential ref field in Input class. Null to omit. |
| `workflowConfigName` | String | `name` | Workflow configmap name / output filename. |
| `extraNodes` | Mapping<String, DAGNode> | `{}` | Custom DAG nodes. Key `"publish"` replaces auto-generated publish. |
| `manifestTopLevelArgs` | Mapping<String, String> | `{"credential_guid": "credential-guid", "connection": "connection"}` | Top-level extract args (rest go under `metadata`). |
| `flatManifestArgs` | Boolean | `false` | When `true`, all workflow params are emitted as top-level keys in `args` with no `metadata` wrapper. |

### Credential Config

| Property | Type | Default | Description |
|---|---|---|---|
| `hasCredentialConfig` | Boolean | `true` | Whether to generate credential JSON. |
| `connectorConfigName` | String | `"atlan-connectors-{name}"` | Credential configmap name. Override to share credentials across manifests (e.g., `"atlan-connectors-teradata"` for both crawler and miner). |
| `credentialConnectorType` | String | `"rest"` | Default connector type (`"jdbc"`, `"rest"`). |
| `credentialCommonFields` | Listing<FieldSpec> | `[]` | Fields shared across all auth types. |
| `credentialAuthOptions` | Mapping<String, AuthOption>? | null | Auth type options. |
| `credentialAuthTitle` | String | `"Authentication"` | Auth radio label. |
| `credentialAuthRules` | Listing<Dynamic>? | null | Validation rules for auth-type radio. |
| `credentialAuthPlaceholder` | String? | null | Placeholder text for auth-type radio. |

### Workflow Config

| Property | Type | Description |
|---|---|---|
| `uiConfig` | Config.UIConfig | Setup form definition with tasks, rules. |

---

## FieldSpec — Credential Field Definition

Aligned with `application_sdk.credentials.types.FieldSpec`.

```pkl
class FieldSpec {
  name: String                           // Internal name (JSON key)
  displayName: String                    // UI label (auto from name)
  placeholder: String?                   // Placeholder text
  helpText: String?                      // Tooltip
  required: Boolean = true
  sensitive: Boolean = false             // true → password widget
  fieldType: FieldType = "text"          // text|password|textarea|select|number|checkbox|url|email
  options: Listing<String>?              // For select fields
  optionLabels: Mapping<String, String>? // Display labels for options
  defaultValue: String?                  // Pre-filled value
  validationRegex: String?               // Regex pattern
  width: Int = 8                         // Grid: 8=full, 4=half, 2=quarter
  isHidden: Boolean = false              // true → hidden in UI (for fixed-value fields)
  feedback: Boolean?                     // Show validation feedback indicator
  message: String?                       // Inline validation message below field
  validationRules: Listing<Dynamic>?     // Validation rules (e.g., required, min, max)
}
```

Use `isHidden = true` for fields with fixed values that users should never see or edit (e.g., a fixed API host):
```pkl
new FieldSpec { name = "host"; defaultValue = "https://api.example.com"; isHidden = true }
new FieldSpec { name = "port"; fieldType = "number"; defaultValue = "443"; isHidden = true }
```

### FieldType → Widget Mapping

| FieldSpec Config | Generated Widget |
|---|---|
| `sensitive = true` | `password` |
| `fieldType = "number"` | `inputNumber` |
| `fieldType = "select"` (≤4 options) | `radio` |
| `fieldType = "select"` (>4 options) | `select` |
| `fieldType = "checkbox"` | `boolean` |
| `fieldType = "textarea"` | `TextInput` |
| `fieldType = "inputRepeater"` | `inputRepeater` |
| Default | `input` |

## ConditionalFieldSpec — Conditional Credential Field

Use for credential fields whose label / placeholder / visibility depends on a compound boolean match over sibling form fields. Emits a `type: "conditional"` property with a `conditions` array (same shape used by sap-s4 / sap-ecc credential configmaps).

```pkl
class ConditionalFieldSpec {
  name: String                           // Internal field name (JSON property key)
  required: Boolean = false
  conditions: Listing<Config.Condition>  // Per-condition overrides (filter or property/value)
  additionalProperties: Dynamic?         // Optional sub-schema for array-valued conditional fields
  defaultValue: Any?                     // Optional default (emitted verbatim)
}
```

Each `Config.Condition` carries either `property` / `value` for a simple match or `filter` for a compound `$and` / `$or` tree evaluated against sibling form fields, plus `overrideUi` for the widget override.

Example — port whose label/visibility depends on `connection-type` × `extraction-method`:

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

See `examples/sap-s4/app.pkl` for the full connection-type / extraction-method combo.

## AuthOption — Auth Type Definition

```pkl
class AuthOption {
  label: String                                                   // Radio button label
  fields: Listing<FieldSpec|ConditionalFieldSpec>                  // Primary credential fields (stored in Vault)
  extraFields: Listing<FieldSpec|ConditionalFieldSpec> = []        // Metadata fields (stored in object store under "extra")
  nestedLabel: String = ""                                        // Custom label for nested section wrapper
  nestedPlaceholder: String?                                      // Custom placeholder for nested section wrapper
}
```

Both `fields` and `extraFields` accept `FieldSpec` and `ConditionalFieldSpec` — mix them freely in the same listing.

---

## Config.pkl — Widget Types

### UIConfig

```pkl
class UIConfig {
  hidden tasks: Mapping<String, UIStep>           // Wizard steps
  fixed properties: Map<String, UIElement>         // (Generated) all fields flattened
  hidden rules: Listing<UIRule> = []               // Conditional visibility
  fixed anyOf: List<UIRule>?                       // (Generated) rules as list
  fixed steps: List<UIStep>                        // (Generated) steps from tasks
}
```

### UIStep

```pkl
class UIStep {
  title: String                                    // Step name (auto from task key)
  description: String = ""
  hidden inputs: Mapping<String, UIElement>         // Form fields
  fixed id = title.replaceAll(" ", "_").toLowerCase()
  fixed properties: List<String>                   // (Generated) field names
}
```

### UIRule — Conditional Visibility

```pkl
class UIRule {
  hidden whenInputs: Mapping<String, String>       // condition: field → value
  required: Listing<String>                        // fields to show when condition matches
}
```

### Widget Types

#### Text & Input

| Class | Widget | Python Type | Notes |
|---|---|---|---|
| `TextInput` | `input` | `str` | `placeholderText`, `defaultValue`, `validationRules` |
| `TextBoxInput` | `TextInput` | `str` | Multi-line |
| `PasswordInput` | `password` | `str` | Masked input |
| `NumericInput` | `inputNumber` | `int` | `default`, `placeholderValue` |
| `InputRepeater` | `inputRepeater` | `list[str]` | Repeatable text inputs. `placeholderText`, `validationRules` |

#### Selection

| Class | Widget | Python Type | Notes |
|---|---|---|---|
| `Radio` | `radio` | `str` | `possibleValues` (required), `default` (required) |
| `DropDown` | `select` | `str` or `list[str]` | `possibleValues`, `multiSelect`, `default` |
| `TagsInput` | `select` (mode=tags) | `list[str]` | Free-form tags |
| `BooleanInput` | `boolean` | `bool` | `defaultSelection` |

#### Connection & Credential

| Class | Widget | Python Type | Notes |
|---|---|---|---|
| `ConnectionCreator` | `connection` | `Connection \| None` | `placeholderText` |
| `ConnectionSelector` | `connectionSelector` | `str` | `multiSelect`, `connectorFilter`, `connectionCategories` |
| `CredentialInput` | `credential` | `str` | `credType` (required) — triggers credential form fetch |
| `APITokenSelector` | `apiTokenSelect` | `str` | Select existing API token |

#### Trees & Filters

| Class | Widget | Python Type | Notes |
|---|---|---|---|
| `SqlTree` | `sqltree` | `dict[str, Any]` | `sqlQuery`, `cred`, `excludePatterns`, `databaseExcludePatterns`, `desc` |
| `APITree` | `apitree` | `dict[str, Any]` | `credentialType`, `metadataTemplate`, `desc` |

#### Complex

| Class | Widget | Python Type | Notes |
|---|---|---|---|
| `NestedInput` | `nested` | `dict[str, Any]` | `inputs` — sub-element map |
| `SageV2` | `sageV2` | `str` | `checks` — preflight check definitions |
| `FileUploader` | `fileUpload` | `FileReference \| None` | `fileTypes` |
| `AgentSelector` | `agent` | `dict[str, Any]` | `agentConfigEntries` — use `Listing<Any>` with `Mapping` for nested objects needing `"default"` keys |
| `ConditionalInput` | configurable | `str` or `dict` | `baseWidgetType` (default `"radio"`), `conditions`, sqltree/connection-specific properties |

#### Computed

| Function | Widget | Python Type | Notes |
|---|---|---|---|
| `evaluateWidget(val)` | `evaluate` | `str` | Factory function returning a `Widget` for `Condition.overrideUi`. Derives value from other form selections (hidden, no UI) |

#### Other

| Class | Widget | Python Type | Notes |
|---|---|---|---|
| `DateInput` | `date` | `int` | Epoch seconds. `past`, `future`, `defaultDay` |
| `KeygenInput` | `keygen` | `str` | Generates unique key |
| `Schedule` | `schedule` | `str` | Cron schedule picker |
| `CloudProvider` | `CloudProvider` | `str` | AWS/GCP/Azure |

---

## Connectors.pkl — Connector Registry

90+ connector constants. Each has `value` (string identifier) and `category`.

```pkl
class Type {
  value: String
  category: AtlanConnectionCategory
}

typealias AtlanConnectionCategory =
  "warehouse"|"bi"|"custom"|"ObjectStore"|"SaaS"|"lake"|
  "queryengine"|"elt"|"database"|"API"|"eventbus"|
  "data-quality"|"schema-registry"|"app"
```

Common connectors:

| Constant | Value | Category |
|---|---|---|
| `SNOWFLAKE` | `"snowflake"` | warehouse |
| `REDSHIFT` | `"redshift"` | warehouse |
| `POSTGRES` | `"postgres"` | database |
| `BIGQUERY` | `"bigquery"` | warehouse |
| `DATABRICKS` | `"databricks"` | lake |
| `TRINO` | `"trino"` | database |
| `TABLEAU` | `"tableau"` | bi |
| `SALESFORCE` | `"salesforce"` | SaaS |
| `S3` | `"s3"` | ObjectStore |
| `KAFKA` | `"kafka"` | eventbus |
| `DBT` | `"dbt"` | elt |
| `AIRFLOW` | `"airflow"` | elt |
| `MONGODB` | `"mongodb"` | database |
| `MSSQL` | `"mssql"` | warehouse |
| `MYSQL` | `"mysql"` | warehouse |
| `ORACLE` | `"oracle"` | warehouse |
| `SAP_S4_HANA` | `"sap-s4-hana"` | erp |
| `SAP_ECC` | `"sap-ecc"` | erp |

---

## DAG Nodes — Manifest Customization

### Default Pipeline

`extract → publish` (auto-generated, no configuration needed).

### DAGNode

```pkl
open class DAGNode {
  nodeType: "activity"|"workflow" = "workflow"  // Discriminator — gates heartbeatTimeoutSeconds
  displayName: String
  workflowType: String
  appName: String = "automation-engine"
  taskQueue: String?                            // Default: "atlan-{appName}-{deployment_name}"
  args: Mapping<String, Any>
  dependsOn: Listing<String>?
  errorHandling: ErrorHandlingConfig?           // Retry / timeout policy, see below
}
```

`nodeType` is PKL-side only (not emitted to JSON). AE auto-infers `"workflow"` when
`activity_name == "execute_workflow"`, which this toolkit always emits. The field
exists so PKL can reject `heartbeatTimeoutSeconds` (activity-only) at `pkl eval`
time instead of at AE parse time.

### ErrorHandlingConfig

Maps 1:1 to AE's `ErrorHandlingConfig` (`automation_engine/registry/models.py`).
All fields are optional; omit the ones you don't need.

```pkl
class ErrorHandlingConfig {
  initialInterval: Int(this >= 1)?                 // Retry initial interval (seconds)
  backoffCoefficient: Int?                          // Retry backoff multiplier
  maximumInterval: Int(this >= 1)?                  // Retry max interval (seconds)
  maximumAttempts: Int?                             // Retry cap
  nonRetryableErrorTypes: Listing<String>?          // Exact error types to skip retry on
  startToCloseTimeoutSeconds: Int(isBetween(1, 86400))?   // Total node time budget
  heartbeatTimeoutSeconds: Int(isBetween(1, 3600))?       // Activity-only
}
```

Enforced at `pkl eval` time:
- `heartbeatTimeoutSeconds < startToCloseTimeoutSeconds` (when both set)
- `heartbeatTimeoutSeconds` is rejected on nodes with `nodeType = "workflow"`

AE remaps `startToCloseTimeoutSeconds` server-side: it becomes `execution_timeout`
for workflow nodes (default 2h) and `start_to_close_timeout` for activity nodes
(default 30m).

Example — override the default publish node with a 4-hour timeout and retry policy:

```pkl
extraNodes {
  ["publish"] = new PublishNode {
    errorHandling = new ErrorHandlingConfig {
      startToCloseTimeoutSeconds = 14400
      maximumAttempts = 3
      initialInterval = 2
      backoffCoefficient = 2
      maximumInterval = 60
    }
  }
}
```

### PublishNode

Pre-built publish node. Only specify `upstream`:

```pkl
class PublishNode extends DAGNode {
  hidden upstream: String = "extract"
  displayName = "Publish to Atlas"
  workflowType = "PublishWorkflow"
  appName = "publish"
  args {
    ["connection_qualified_name"] = "$.<upstream>.outputs.connection_qualified_name"
    ["transformed_data_prefix"] = "$.<upstream>.outputs.transformed_data_prefix"
    ["publish_state_prefix"] = "$.<upstream>.outputs.publish_state_prefix"
    ["current_state_prefix"] = "$.<upstream>.outputs.current_state_prefix"
    ["connection_creation_enabled"] = true
    ["executor_enabled"] = true
    ["connection_entity"] = "{{connection}}"
  }
  dependsOn { upstream }
}
```

### Custom Pipeline Example

Insert a lineage node between extract and publish:

```pkl
extraNodes {
  ["lineage"] = new DAGNode {
    displayName = "Extract Lineage"
    workflowType = "LineageExtractionWorkflow"
    taskQueue = "atlan-redshift-{deployment_name}"
    args {
      ["credential"] = "{{credential}}"
      ["connection"] = "{{connection}}"
    }
    dependsOn { "extract" }
  }
  ["publish"] = new PublishNode {
    upstream = "lineage"
  }
}
```

Result: `extract → lineage → publish`

### DAG Value Types

| Type | Example | Resolved By |
|---|---|---|
| `{{param}}` | `"{{credential}}"` | Heracles (at submission) |
| `$.node.outputs.*` | `"$.extract.outputs.connection_qualified_name"` | AE (at runtime) |
| Static | `true`, `"jdbc"` | Passed through |

---

## ConditionalInput — Conditional Widget Switching

Renders a `type: "conditional"` property whose base widget can be any type (not just radio). Used when a form field needs to switch between widget types based on other form values — e.g., sqltree in direct mode, text input in agent mode.

### Properties

| Property | Type | Default | Description |
|---|---|---|---|
| `baseWidgetType` | String | `"radio"` | Base widget type (`"sqltree"`, `"connection"`, `"radio"`, etc.) |
| `baseEnum` | Listing<String>? | null | Base enum values (for radio/select base widgets) |
| `baseEnumNames` | Listing<String>? | null | Base enum display names |
| `default` | Any? | null | Default value |
| `conditions` | Listing<Condition>? | null | Conditions that override the base rendering |
| `additionalProperties` | Dynamic? | null | Extra properties (e.g., `{ type = "array" }` for sqltree) |
| `sqlQuery` | String? | null | SQL query (when `baseWidgetType = "sqltree"`) |
| `credentialRef` | String? | null | Credential variable name (when sqltree) |
| `connectorConfig` | String? | null | Connector config name (when sqltree) |
| `schemaExcludePatterns` | List<String>? | null | Schema exclude patterns (when sqltree) |
| `databaseExcludePatterns` | List<String>? | null | Database exclude patterns (when sqltree) |
| `desc` | String? | null | Description text (when sqltree) |
| `multiSelect` | Boolean? | null | Multi-select (when sqltree) |
| `connOptions` | Boolean? | null | Show connection options (when `baseWidgetType = "connection"`) |

### Condition

Each `Config.Condition` in a `conditions` listing (used by `ConditionalInput` and `ConditionalFieldSpec`) carries one match source plus per-condition overrides.

| Property | Type | Description |
|---|---|---|
| `property` | String? | Simple match: form property name to check. Pair with `value`. |
| `value` | String? | Simple match: value that triggers this condition. |
| `labFlag` | String? | Match when a lab flag is set. |
| `featureFlag` | String? | Match when a feature flag is set. |
| `filter` | Dynamic? | Compound `$and` / `$or` tree evaluated against sibling form fields. Emitted verbatim alongside `ui`; `property`/`value` are typically left null. Use for multi-property combos like SAP's `connection-type` × `extraction-method`. |
| `overrideUi` | Widget? | Widget override applied when the match succeeds. |
| `overrideEnum` / `overrideEnumNames` | Listing<String>? | Override enum values / display names when the match succeeds. |
| `overrideRequired` | Boolean? | Override `required` when the match succeeds. |
| `overrideDefault` | Any? | Override default value when the match succeeds. |

### Example: SqlTree with Agent Mode Fallback

```pkl
["include-filter"] = new Config.ConditionalInput {
  title = "Include Metadata"
  helpText = "Only selected databases and schemas will be extracted."
  baseWidgetType = "sqltree"
  sqlQuery = "show atlan schemas"
  credentialRef = "credential-guid"
  schemaExcludePatterns = List("pg_*", "information_*")
  width = 4
  additionalProperties = new Dynamic { type = "array" }
  conditions {
    new Config.Condition {
      property = "extraction-method"
      value = "agent"
      overrideUi = new Config.Widget {
        widget = "input"
        label = "Include Filter"
        help = "Enter filter as JSON"
        placeholder = "{\"^DB1$\": [\"^SCHEMA1$\"]}"
        grid = 8
      }
    }
  }
}
```

Generates `type: "conditional"` with `ui.widget: "sqltree"` as the base, and a condition that switches to a plain text input when `extraction-method = "agent"`.

---

## AgentSelector — PKL Reserved Keywords

`AgentSelector.agentConfigEntries` accepts `Listing<Any>` to support nested objects that need `"default"` or `"hidden"` as JSON keys.

**`hidden` keyword** — use backtick escaping (idiomatic PKL):
```pkl
new Dynamic {
  ui = new Dynamic {
    `hidden` = true    // renders as "hidden": true
  }
}
```

**`default` keyword** — backtick escaping does NOT work (PKL silently drops it). Use `Mapping` instead:
```pkl
new Mapping {
  ["default"] = ""     // renders as "default": ""
  ["ui"] = new Mapping {
    ["placeholder"] = "Store Credential Path"
  }
}
```

### Example: Agent Config with Credential Overrides

```pkl
["agent-json"] = new Config.AgentSelector {
  title = ""
  placeholderText = "Agent JSON"
  agentConfigEntries {
    new Listing {
      "atlan-connectors-teradata"
      new Mapping {
        ["properties"] = new Mapping {
          ["agent-type"] = new Mapping {
            ["type"] = "string"
            ["ui"] = new Mapping { ["hidden"] = true }
          }
          ["basic"] = new Mapping {
            ["properties"] = new Mapping {
              ["username"] = new Mapping {
                ["default"] = ""
                ["ui"] = new Mapping { ["placeholder"] = "Store Credential Path" }
              }
            }
          }
        }
      }
    }
    new Listing { "atlan-connectors-agent" }
  }
}
```

See `examples/teradata/credentials.pkl` for a full working example combining both `ConditionalInput` and `AgentSelector`.

---

## Multi-Manifest Apps

For apps serving multiple marketplace tiles from one deployment (e.g., crawler + miner).

### Structure

```pkl
// contract/credentials.pkl — shared credential definitions
module credentials
import "@app-contract-toolkit/NativeApp.pkl"

commonFields: Listing<NativeApp.FieldSpec> = new { /* host, port, etc. */ }
authOptions: Mapping<String, NativeApp.AuthOption> = new { /* basic, ldap, etc. */ }

// contract/crawler.pkl
amends "@app-contract-toolkit/NativeApp.pkl"
import "./credentials.pkl"

name = "teradata"
workflowType = "TeradataCrawlerApp"          // auto → "teradata-crawler-app"
credentialCommonFields = credentials.commonFields
credentialAuthOptions = credentials.authOptions

// contract/miner.pkl
amends "@app-contract-toolkit/NativeApp.pkl"
import "./credentials.pkl"

name = "teradata-miner"
workflowType = "TeradataMinerApp"            // auto → "teradata-miner-app"
connectorConfigName = "atlan-connectors-teradata"  // share creds with crawler
taskQueuePrefix = "atlan-teradata"                 // share task queue
credentialCommonFields = credentials.commonFields
credentialAuthOptions = credentials.authOptions
```

### Generate

```bash
pkl eval -m app/generated/crawler contract/crawler.pkl
pkl eval -m app/generated/miner contract/miner.pkl
```

### Key patterns

- **Shared credentials**: Define fields once in `credentials.pkl`, import from both contracts
- **Shared credential name**: Override `connectorConfigName` in miner to match crawler
- **Shared task queue**: Override `taskQueuePrefix` in miner so Temporal routes by workflow type
- **`__init__.py`**: Auto-generated in each output directory for Python imports

See `examples/teradata/` for the complete multi-manifest example.

---

## Generated Output Shapes

### Workflow Config (`{name}.json`)

```json
{
  "id": "redshift",
  "name": "Redshift",
  "logo": "https://assets.atlan.com/assets/redshift.svg",
  "config": {
    "properties": {
      "extraction-method": {
        "type": "string",
        "required": true,
        "ui": {
          "widget": "radio",
          "label": "Extraction method",
          "hidden": false,
          "help": "...",
          "grid": 8
        },
        "default": "direct",
        "enum": ["direct", "s3"],
        "enumNames": ["Direct", "Offline"]
      },
      "credential-guid": {
        "type": "string",
        "ui": {
          "widget": "credential",
          "credentialType": "atlan-connectors-redshift"
        }
      },
      "connection": {
        "type": "string",
        "ui": { "widget": "connection" }
      },
      "include-filter": {
        "type": "object",
        "ui": {
          "widget": "sqltree",
          "credential": "credential-guid",
          "sql": "show atlan schemas",
          "schemaExcludePattern": ["pg_*", "information_*"]
        }
      }
    },
    "steps": [
      { "title": "Credential", "id": "credential", "properties": ["extraction-method", "credential-guid"] },
      { "title": "Connection", "id": "connection", "properties": ["connection"] },
      { "title": "Metadata", "id": "metadata", "properties": ["include-filter", "exclude-filter", "..."] }
    ],
    "anyOf": [
      {
        "properties": { "extraction-method": { "const": "direct" } },
        "required": ["credential-guid", "include-filter"]
      }
    ]
  }
}
```

### Credential Config (`atlan-connectors-{name}.json`)

```json
{
  "icon": "https://assets.atlan.com/assets/redshift.svg",
  "helpdeskLink": "https://...",
  "logo": "https://...",
  "connector": "redshift",
  "defaultConnectorType": "jdbc",
  "config": {
    "properties": {
      "name": { "type": "string", "ui": { "hidden": true } },
      "connector": { "type": "string", "ui": { "hidden": true } },
      "connectorType": { "type": "string", "ui": { "hidden": true } },
      "host": { "type": "string", "ui": { "widget": "input", "label": "Host Name" } },
      "port": { "type": "number", "default": 5439 },
      "auth-type": {
        "type": "string",
        "enum": ["basic", "iam", "role"],
        "enumNames": ["Basic", "IAM User", "IAM Role"],
        "ui": { "widget": "radio", "label": "Authentication" }
      },
      "basic": {
        "type": "object",
        "ui": { "widget": "nested", "hidden": true, "nestedValue": false },
        "properties": {
          "username": { "ui": { "widget": "input" } },
          "password": { "ui": { "widget": "password" } },
          "extra": {
            "type": "object",
            "ui": { "widget": "nested", "header": "Advanced" },
            "properties": { "database": {}, "deployment_type": {} }
          }
        }
      }
    },
    "anyOf": [
      { "properties": { "auth-type": { "const": "basic" } }, "required": ["basic"] },
      { "properties": { "auth-type": { "const": "iam" } }, "required": ["iam"] }
    ]
  }
}
```

### Manifest (`manifest.json`)

```json
{
  "execution_mode": "automation-engine",
  "dag": {
    "extract": {
      "activity_name": "execute_workflow",
      "activity_display_name": "Extract Redshift Metadata",
      "app_name": "automation-engine",
      "inputs": {
        "workflow_type": "RedshiftMetadataExtractionWorkflow",
        "task_queue": "atlan-redshift-{deployment_name}",
        "args": {
          "credential": "{{credential}}",
          "credential_guid": "{{credential-guid}}",
          "connection": "{{connection}}",
          "metadata": {
            "extraction-method": "{{extraction-method}}",
            "include-filter": "{{include-filter}}"
          }
        }
      }
    },
    "publish": {
      "activity_name": "execute_workflow",
      "activity_display_name": "Publish to Atlas",
      "app_name": "publish",
      "inputs": {
        "workflow_type": "PublishWorkflow",
        "task_queue": "atlan-publish-{deployment_name}",
        "args": {
          "connection_qualified_name": "$.extract.outputs.connection_qualified_name",
          "transformed_data_prefix": "$.extract.outputs.transformed_data_prefix",
          "connection_creation_enabled": true,
          "connection_entity": "{{connection}}"
        }
      },
      "depends_on": { "node_id": "extract" }
    }
  }
}
```

### Input Contract (`app/generated/_input.py`)

Generated as a Pydantic model extending `application_sdk.contracts.base.Input` (SDK v3).
When any field uses `dict[str, Any]` (from APITree, SqlTree, NestedInput, or AgentSelector widgets),
the class automatically includes `allow_unbounded_fields=True` to satisfy the SDK v3 payload-safety validator.

```python
# AUTO-GENERATED from app.pkl — DO NOT EDIT MANUALLY.
# To regenerate: make generate
from __future__ import annotations
from typing import Annotated, Any, ClassVar
from pydantic import Field
from application_sdk.contracts.base import Input
from application_sdk.contracts.types import ConnectionRef, FileReference, MaxItems
from application_sdk.credentials.ref import CredentialRef


class AppInputContract(Input, allow_unbounded_fields=True):
    _config_hash_exclude: ClassVar[set[str]] = {
        "output_dir",
        "checkpoint_dir",
        "load_to_atlan",
        "publish_dry_run",
    }

    # UI fields (from uiConfig.properties, hyphens → underscores)
    extraction_method: str = "direct"
    credential_guid: str = ""
    connection: ConnectionRef | None = None
    include_filter: Annotated[dict[str, Any], MaxItems(1000)] = Field(default_factory=dict)
    exclude_filter: Annotated[dict[str, Any], MaxItems(1000)] = Field(default_factory=dict)
    # ...

    # Credential ref (when hasCredentialConfig)
    redshift_credential: CredentialRef | None = None

    # Publish fields (when hasPublishStep=True)
    output_dir: str = ""
    checkpoint_dir: str = ""
    load_to_atlan: bool = True
    publish_dry_run: bool = False
```

**Key differences from earlier versions:**
- Base class: `application_sdk.contracts.base.Input` (Pydantic BaseModel, not dataclass)
- Connection type: `ConnectionRef` (not `pyatlan.models.connection.Connection`)
- Credential type: `application_sdk.credentials.ref.CredentialRef`
- Dict fields: `Annotated[dict[str, Any], MaxItems(1000)]` with `Field(default_factory=dict)`
- `allow_unbounded_fields=True`: auto-added when any field uses `dict[str, Any]` (required by SDK v3 payload safety)
- Publish fields simplified: `publish_dry_run` replaces the loader-specific fields

---

## Credential.pkl — Legacy Module

For Argo-era apps only. Uses `Config.pkl` widget classes instead of `FieldSpec`. Do not use for new native apps.

```pkl
class NestedCredentialInput extends Config.NestedInput {
  hidden optionLabel: String       // Auth radio label
  title = ""
  hide = true
  hidden inputs: Mapping<CredentialAttribute, Config.UIElement>
}

typealias CredentialAttribute = "host"|"port"|"connection"|"username"|"password"|"extra"|"name"|"connector"|"connectorType"
```

---

## Renderers.pkl — Legacy Renderers

Output generators for legacy formats. NativeApp.pkl has its own rendering pipeline.

| Function | Output | Purpose |
|---|---|---|
| `getConfigMap(m)` | YAML | K8s ConfigMap for workflow config |
| `getConfigMapJson(m)` | JSON | Plain JSON workflow config |
| `getCredentialConfigMapJson(m)` | JSON | Plain JSON credential config |
| `getWorkflowTemplate(m)` | YAML | Argo WorkflowTemplate |
| `getConfigClassKt(m, className)` | Kotlin | Typed config class |
| `getConfigClassPy(m)` | Python | Pydantic BaseModel config |
| `getPackageJson(m)` | JSON | NPM package.json metadata |
