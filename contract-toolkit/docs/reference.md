# App Contract Toolkit — Reference Documentation

One `app.pkl` contract generates every artifact for an Atlan native app: `atlan.yaml`, `app.yaml`, workflow config, credential config, AE manifest, and typed Python input model. The v0.10.0 canonical template is `App.pkl` — one `amends` line, one `pkl eval`, everything emitted at the repo root.

## Architecture

```
contract/app.pkl (developer writes)
    │
    ├─▶ atlan.yaml                        — Marketplace manifest (repo root)
    ├─▶ app.yaml                          — SDR CI shim (repo root)
    ├─▶ app/generated/{name}.json         — Workflow config (setup form)
    ├─▶ app/generated/atlan-connectors-{name}.json  — Credential config
    ├─▶ app/generated/manifest.json       — AE DAG
    └─▶ app/generated/_input.py           — Python AppInputContract
```

**Consumption flow:**
```
pkl eval -m . contract/app.pkl
    → SDK serves at /workflows/v1/configmap/{id}
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
gh release list --repo atlanhq/application-sdk --limit 50 --json tagName --jq '[.[] | select(.tagName | startswith("contract-toolkit-v"))][0].tagName | sub("^contract-toolkit-v"; "")'
```

```pkl
amends "pkl:Project"

dependencies {
  ["app-contract-toolkit"] {
    uri = "package://atlanhq.github.io/application-sdk/contracts/app-contract-toolkit@<LATEST_VERSION>"
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
cd contract && pkl project resolve && cd ..
pkl eval -m . contract/app.pkl
```

### app.pkl — single amend line (v0.10.0+)

```pkl
amends "@app-contract-toolkit/App.pkl"
import "@app-contract-toolkit/Connectors.pkl"
```

`Connectors.pkl` must be imported explicitly by consuming contracts. All other domain classes and widget types are available directly (no extra `import` lines needed).

### Generate (run from the **app repo root**)

```bash
pkl eval -m . contract/app.pkl
```

Output files carry full paths relative to the repo root (`atlan.yaml`, `app.yaml`, `app/generated/…`) so a single invocation lays everything down in the right place.

---

## App.pkl — Canonical Template (v0.10.0+)

The single entry point for all new native app contracts. Supersedes `NativeApp.pkl` and `NativeAppBundle.pkl`.

### App Metadata

| Property | Type | Default | Description |
|---|---|---|---|
| `name` | String | required | App slug (e.g. `"snowflake"`). Drives configmap names, task queues, file names. |
| `displayName` | String | `name.capitalize()` | UI display name. |
| `connector` | Connectors.Type? | null | Connector from the registry. Required when `hasCredentialConfig = true`. |
| `icon` | String | required | Icon URL. |
| `docsUrl` | String | `""` | Documentation link. |
| `logo` | String | `icon` | Logo URL. |
| `helpdeskLink` | String | `""` | Helpdesk link for credential form. |
| `type` | String | `"connector"` | Marketplace type. |
| `visibility` | String | `"public"` | Marketplace visibility. |
| `buildTag` | String | `"v1"` | Emitted as `build_tag`. |
| `selfDeployedRuntime` | Boolean | `false` | Emitted as `self_deployed_runtime`. |

### Workflow & Manifest

| Property | Type | Default | Description |
|---|---|---|---|
| `workflowType` | String | `""` | Python App class name in PascalCase. Auto-converted to kebab-case. Either this or `workflowTypeOverride` must be set. |
| `workflowTypeOverride` | String? | null | Explicit workflow type (used as-is, overrides `workflowType`). |
| `taskQueuePrefix` | String | `"atlan-{name}"` | Task queue prefix. Override for multi-entrypoint apps sharing a deployment. |

### Pipeline Block

The typed `pipeline` block replaces per-flag properties. Each step is nullable to opt out.

| Property | Type | Default | Description |
|---|---|---|---|
| `pipeline.extract` | ExtractStep? | `new ExtractStep {}` | Extract node. `name` and `displayName` overridable. Null to omit (rare). |
| `pipeline.parseQueries` | ParseQueriesStep? | null | Query Intelligence node (default-off). |
| `pipeline.popularity` | PopularityStep? | null | Popularity node (default-off). |
| `pipeline.lineage` | LineageStep? | null | Lineage app node (default-off). |
| `pipeline.publish` | PublishStep? | `new PublishStep {}` | Publish node (default-on). `errorHandling` defaults to 24h `startToCloseTimeoutSeconds`. Set null for utility apps. |
| `extraNodes` | Mapping<String, DAGNode> | `{}` | Custom nodes outside the typed pipeline. `"publish"` key replaces auto-generated publish. |

**Pipeline step classes:**

```pkl
class ExtractStep {
  name: String = "extract"               // override for non-extract apps (e.g. "apply")
  displayName: String = "Extract"        // compact AE step label
  errorHandling: ErrorHandlingConfig?    // retry / timeout policy
}

class ParseQueriesStep {                 // wraps QueryIntelligenceNode
  // all QueryIntelligenceNode fields available here
  vendorName: String?
  vendorKey: String?
  sqlKey: String                         // required
  // ... (see QueryIntelligenceNode below for full field list)
  errorHandling: ErrorHandlingConfig?
}

class PopularityStep {                   // wraps PopularityNode
  tenantId: String                       // required
  parsedDataPrefix: String               // required
  minedDataPrefix: String                // required
  connectionCachePath: String            // required
  outputPrefix: String                   // required
  // ... (see PopularityNode below for full field list)
  errorHandling: ErrorHandlingConfig?
}

class LineageStep {                      // wraps LineageNode
  connectorName: String                  // required
  sqlUnquotedCase: String                // required
  ignoreAllCase: Boolean                 // required
  // ... (see LineageNode below for full field list)
  errorHandling: ErrorHandlingConfig?
}

class PublishStep {
  executorEnabled: Boolean|String = true
  includeInputFields: Boolean = true     // generates output_dir/load_to_atlan/publish_dry_run in _input.py
  lineagePublish: LineagePublishStep?    // opt-in lineage publish (default-off)
  errorHandling: ErrorHandlingConfig? = new ErrorHandlingConfig {
    startToCloseTimeoutSeconds = 86400  // 24h default — AE's 2h is too tight for large tenants
  }
}

class LineagePublishStep {               // wraps LineagePublishNode
  // ... (see LineagePublishNode below for full field list)
  errorHandling: ErrorHandlingConfig?
}
```

**Migration from old API:**

| Old property | New pipeline equivalent |
|---|---|
| `hasPublishStep = false` | `pipeline.publish = null` |
| `hasPublishInputFields = false` | `pipeline.publish.includeInputFields = false` |
| `publishExecutorEnabled = "{{x}}"` | `pipeline.publish.executorEnabled = "{{x}}"` |
| `extractActivityDisplayName = "Apply"` | `pipeline.extract.displayName = "Apply"` |
| `extractNodeErrorHandling = ...` | `pipeline.extract.errorHandling = ...` |
| `publishNodeErrorHandling = ...` | `pipeline.publish.errorHandling = ...` |
| `extraNodes { ["qi"] = new QueryIntelligenceNode { ... } }` | `pipeline.parseQueries = new ParseQueriesStep { ... }` |
| `extraNodes { ["popularity"] = new PopularityNode { ... } }` | `pipeline.popularity = new PopularityStep { ... }` |
| `extraNodes { ["lineage-app"] = new LineageNode { ... } }` | `pipeline.lineage = new LineageStep { ... }` |
| `extraNodes { ["lineage-publish"] = new LineagePublishNode { ... } }` | `pipeline.publish.lineagePublish = new LineagePublishStep { ... }` |

### Credential Config

| Property | Type | Default | Description |
|---|---|---|---|
| `hasCredentialConfig` | Boolean | `true` | Whether to generate credential JSON. |
| `connectorConfigName` | String | `"atlan-connectors-{name}"` | Credential configmap name. Override to share credentials across entrypoints. |
| `credentialConnectorType` | String | `"rest"` | Default connector type (`"jdbc"`, `"rest"`). |
| `credentialCommonFields` | Listing<CredentialFieldEntry> | `[]` | Fields shared across all auth types. |
| `credentialSharedExtraFields` | Listing<CredentialFieldEntry> | `[]` | Fields under shared top-level `extra`. |
| `credentialAuthOptions` | Mapping<String, AuthOption>? | null | Auth type options. |
| `credentialAuthTitle` | String | `"Authentication"` | Auth radio label. |
| `credentialAuthDefault` | String? | null | Default auth type (defaults to first key). |
| `credentialAuthRules` | Listing<ValidationRule>? | null | Validation rules for auth-type radio. |
| `credentialAuthPlaceholder` | String? | null | Placeholder for auth-type radio. |
| `credentialAuthHelp` | String? | null | Tooltip below auth-type radio. |
| `credentialAuthWidth` | Int? | null | Grid width for auth-type radio. |
| `credentialAuthHiddenEnumListForCreating` | Listing<String>? | null | Auth-type enum values hidden during credential creation. |
| `credentialExtraAnyOfRules` | Listing<UIRule> | `[]` | Extra `anyOf` branches appended to the credential `anyOf` block, beyond the auto-generated auth-type branches. Use for requirements keyed on inputs other than `auth-type`. Appended in both the plain and JDBC-URL flows. See [Extra credential anyOf rules](#extra-credential-anyof-rules). |
| `credentialNamePlaceholder` | String | `"Host Name"` | Placeholder for hidden credential `name` field. |
| `credentialUrlGroup` | AdvancedJDBCUrlGroup? | null | Opt-in JDBC Host↔URL credential form. Requires all `credentialAuthOptions` to be `JDBCUrlAuthOption`. |

The auth-type radio's `ui.hidden` is auto-derived from `credentialAuthOptions.length == 1` — when the connector declares a single auth option, the radio is hidden and the default value is still emitted. No manual flag is required.

### Workflow Config

| Property | Type | Description |
|---|---|---|
| `uiConfig` | UIConfig | Setup form definition with tasks, rules. |

### Deploy Block

The typed `deploy` block replaces the legacy free-form mapping.

| Property | Type | Default | Description |
|---|---|---|---|
| `deploy.executionMode` | String | `"native"` | Execution mode. |
| `deploy.splitDeployment` | Boolean | `true` | Emitted as `splitDeploymentEnabled`. |
| `deploy.replicaCount` | Int? | null | Honoured only when `keda.enabled = false`. |
| `deploy.dapr` | DaprComponents | `new DaprComponents {}` | Dapr sidecar toggles. |
| `deploy.keda` | KedaConfig | `new KedaConfig {}` | KEDA autoscaling config. |
| `deploy.resources` | ResourceConfig? | null | Kubernetes resource requests/limits. |
| `deploy.env` | Mapping<String, String> | `{}` | Static container environment variables. |
| `deployOverrides` | Mapping<String, Any> | `{}` | Deep-merged on top of the rendered deploy section (VPA, extraVolumes, securityContext, etc.). |

**DaprComponents:**

```pkl
class DaprComponents {
  objectstore: Boolean = false
  secretstore: Boolean = false
  statestore: Boolean = false
  eventstore: Boolean = false
  subscription: Boolean = false
  configurationstore: Boolean = false
  lock: Boolean = false
}
```

**KedaConfig:**

```pkl
class KedaConfig {
  enabled: Boolean = true
  minReplicaCount: Int = 0
  temporal: KedaTemporalConfig = new { targetQueueSize = 5 }
}
class KedaTemporalConfig { targetQueueSize: Int }
```

Note: `targetQueueSize` must be set via `keda.temporal.targetQueueSize`, not `keda.targetQueueSize`.

**ResourceConfig:**

```pkl
class ResourceConfig {
  requests: Mapping<String, String>
  limits: Mapping<String, String>
}
```

### Multi-Entrypoint Bundle

Set `entrypoints` to serve multiple marketplace tiles from one deployment. Per-entrypoint contracts are separate files that each `amend App.pkl`.

| Property | Type | Default | Description |
|---|---|---|---|
| `entrypoints` | Listing<Entrypoint> | `[]` | Marketplace card / SDK entrypoint definitions. When non-empty, enables bundle mode. |
| `emitAtlanYaml` | Boolean | `true` | Emit `atlan.yaml`. |
| `emitEntrypoints` | Boolean | `true` | Emit the `entrypoints:` block. |
| `emitGeneratedArtifacts` | Boolean | `true` | Re-export entrypoint contract files. |

**Entrypoint class:**

| Property | Type | Default | Description |
|---|---|---|---|
| `name` | String | required | SDK entrypoint key and generated subfolder (`crawler`, `miner`). |
| `displayName` | String | `name.capitalize()` | Card display name. |
| `description` | String? | null | Card description. |
| `icon` | String? | null | Card icon. |
| `type` | String | `"connector"` | Card type. |
| `source` | String? | null | Source slug. |
| `sourceCategory` | String? | null | Source category. |
| `contract` | Typed? | null | The entrypoint's `App.pkl` contract whose `output.files` are emitted. |

Bundle output layout:
```
atlan.yaml                              # bundle-level (entrypoints listed)
app.yaml
app/generated/
├── atlan-connectors-{shared}.json      # hoisted from entrypoints, deduped by filename
├── {entrypoint1}/
│   ├── __init__.py
│   ├── {name}.json
│   ├── manifest.json
│   └── _input.py
└── {entrypoint2}/
    └── ...
```

Credential files are hoisted by matching `connectorConfigName`. If two entrypoints produce the same filename with different content, generation fails — use unique `connectorConfigName` values for genuinely different credentials.

### Other App.pkl Properties

| Property | Type | Default | Description |
|---|---|---|---|
| `workflowConfigName` | String | `name` | Workflow configmap name / output filename. |
| `credentialFieldName` | String? | `"{name}_credential"` | Credential ref field in Input class. Null to omit. |
| `manifestTopLevelArgs` | Mapping<String, String> | `{"credential_guid": "credential-guid", "connection": "connection"}` | Explicit top-level extract args. |
| `publishTagPipelineEnabled` | Boolean\|String? | auto | Value for `PublishNode`'s `tag_pipeline_enabled`. Auto-wired when `enable-tags` or `enable-tag-sync` is in the form. |
| `publishTagAttachmentsPrefix` | String? | auto | Value for `PublishNode`'s `tag_attachments_prefix`. |

---

## Legacy: NativeApp.pkl — Base Module (pre-v0.10.0)

> **Deprecated.** New contracts should amend `App.pkl`. `NativeApp.pkl` remains resolvable for existing apps during the v0.10.x transition period; the hard cutover is planned for v1.0.

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
| `taskQueuePrefix` | String | `"atlan-{name}"` | Task queue prefix for the extract node. Full queue: `{taskQueuePrefix}-{deployment_name}`. Override for multi-entrypoint apps sharing a deployment (e.g., `taskQueuePrefix = "atlan-teradata"` for both crawler and miner). |
| `hasPublishStep` | Boolean | `true` | Includes the default publish node in the manifest DAG. |
| `hasPublishInputFields` | Boolean | `true` | Appends generated publish scaffolding fields (`output_dir`, `checkpoint_dir`, `load_to_atlan`, `publish_dry_run`) and their `_config_hash_exclude` block to the Input class when publish is enabled. Set `false` when the app has publish but does not implement those fields. |
| `publishExecutorEnabled` | Boolean\|String | `true` | Value for the default publish node's `executor_enabled` arg. Use a Boolean literal or an exact template placeholder string such as `"{{load-to-atlan}}"`. |
| `publishTagPipelineEnabled` | Boolean\|String? | `"{{enable-tags}}"` when the workflow has `enable-tags`; otherwise `"{{enable-tag-sync}}"` when the workflow has legacy `enable-tag-sync`; otherwise null | Value for `PublishNode`'s `tag_pipeline_enabled` arg. The default mirrors the workflow form value so publish-app runs managed classification/tag-attachment processing only when tag sync is enabled. If both fields exist, `enable-tags` wins because native Databricks keeps `enable-tag-sync` as a hidden compatibility alias. Without this flag, publish-app can receive TagAttachment entities while still stripping classifications from Atlas payloads. Set explicitly for a different form field or fixed value; set null to omit. |
| `publishTagAttachmentsPrefix` | String? | `$.extract.outputs.tag_attachments_prefix` when `publishTagPipelineEnabled` is not null; otherwise null | Value for `PublishNode`'s `tag_attachments_prefix` arg. The default matches native Databricks: extract returns the object-store prefix for connector-emitted `{output_path}/transformed/tag_attachments` payloads, and publish-app uses it for typedef resolution and classification enrichment. Override when the extract workflow emits a different output key. |
| `extractActivityDisplayName` | String? | null | Optional override for the built-in extract node's `activity_display_name`. Defaults to `"Extract {displayName} Metadata"`. Use this for long app display names that need compact AE step labels. Only changes the human-readable step label; node id, workflow type, task queue, app name, and args are unchanged. |
| `publishActivityDisplayName` | String? | null | Optional override for the built-in default publish node's `activity_display_name`. Defaults to `"Publish to Atlas"`. Applies only when the toolkit generates the default publish node; for `extraNodes["publish"]`, set `displayName` on that `PublishNode`. |
| `extractNodeErrorHandling` | ErrorHandlingConfig? | null | Optional workflow-safe retry / timeout policy for the built-in extract node. Use `startToCloseTimeoutSeconds` to raise AE's child workflow execution timeout. `heartbeatTimeoutSeconds` is invalid because the built-in extract node renders `activity_name = "execute_workflow"` and AE treats it as a workflow node. |
| `publishNodeErrorHandling` | ErrorHandlingConfig? | `startToCloseTimeoutSeconds = 86400` (24h) | Workflow-safe retry / timeout policy for the toolkit-generated default publish node. Defaults to 24h so connectors don't silently inherit AE's 2h default. Override as needed. For `extraNodes["publish"]`, set `errorHandling` on that `PublishNode` directly (it also defaults to 24h). `heartbeatTimeoutSeconds` is invalid for the default publish node. |
| `credentialFieldName` | String? | `"{name}_credential"` | Credential ref field in Input class. Null to omit. |
| `workflowConfigName` | String | `name` | Workflow configmap name / output filename. |
| `additionalOutputFiles` | Mapping<String, FileOutput> | `{}` | Explicit opt-in files to emit with this contract, for example a customized `AgentConfig.pkl` output. |
| `extraNodes` | Mapping<String, DAGNode> | `{}` | Custom DAG nodes. Use `DAGNode` for arbitrary workflows, `PublishNode` for the standard Atlas publish step, `QueryIntelligenceNode` for native QI, `PopularityNode` for popularity metrics, `LineageNode` for the Lineage app, or `LineagePublishNode` for lineage publish. Key `"publish"` replaces auto-generated publish. |
| `manifestTopLevelArgs` | Mapping<String, String> | `{"credential_guid": "credential-guid", "connection": "connection"}` | Explicit top-level extract args. These remain top-level in either manifest shape. |

### Credential Config

| Property | Type | Default | Description |
|---|---|---|---|
| `hasCredentialConfig` | Boolean | `true` | Whether to generate credential JSON. |
| `connectorConfigName` | String | `"atlan-connectors-{name}"` | Credential configmap name. Override to share credentials across manifests (e.g., `"atlan-connectors-teradata"` for both crawler and miner). |
| `credentialConnectorType` | String | `"rest"` | Default connector type (`"jdbc"`, `"rest"`). |
| `credentialCommonFields` | Listing<CredentialFieldEntry> | `[]` | Fields shared across all auth types. |
| `credentialSharedExtraFields` | Listing<CredentialFieldEntry> | `[]` | Fields emitted under a shared top-level credential `extra` object, outside auth branches. |
| `credentialAuthOptions` | Mapping<String, AuthOption>? | null | Auth type options. |
| `credentialAuthTitle` | String | `"Authentication"` | Auth radio label. |
| `credentialAuthDefault` | String? | null | Default auth type. Defaults to the first `credentialAuthOptions` key when unset. |
| `credentialAuthRules` | Listing<Dynamic>? | null | Validation rules for auth-type radio. |
| `credentialAuthPlaceholder` | String? | null | Placeholder text for auth-type radio. |
| `credentialAuthHelp` | String? | null | Help text under the auth-type radio (tooltip). |
| `credentialAuthWidth` | Int? | null | Grid width for auth-type radio when set. |
| `credentialAuthHiddenEnumListForCreating` | Listing<String>? | null | Auth-type enum values hidden during credential creation. |
| `credentialNamePlaceholder` | String | `"Host Name"` | Placeholder for the hidden credential `name` field. |
| `credentialConnectorDefault` | String? | null | Optional default for the hidden credential `connector` field. |
| `credentialUrlGroup` | AdvancedJDBCUrlGroup? | null | Opt-in JDBC Host↔URL credential form. When set, every `credentialAuthOptions` entry must be a `JDBCUrlAuthOption`. See [AdvancedJDBCUrlGroup](#advancedjdbcurlgroup--hostrlarrowurl-jdbc-credential-form). |

The auth-type radio's `ui.hidden` is auto-derived from `credentialAuthOptions.length == 1` — when the connector declares a single auth option, the radio is hidden and the default value is still emitted. No manual flag is required.

### Workflow Config

| Property | Type | Description |
|---|---|---|
| `uiConfig` | Config.UIConfig | Setup form definition with tasks, rules. |

---

## Legacy: NativeAppBundle.pkl — Multi-Entrypoint Bundle (pre-v0.10.0)

> **Deprecated.** Multi-entrypoint bundle support is now built into `App.pkl` via the `entrypoints` block. `NativeAppBundle.pkl` remains resolvable for existing apps during the v0.10.x transition period; the hard cutover is planned for v1.0.

Use `NativeAppBundle.pkl` when one deployed native app exposes multiple marketplace cards / SDK entrypoints from the same service. The bundle owns deployment and marketplace metadata, while each entrypoint still uses a normal `NativeApp.pkl` contract for workflow config, credentials, manifest, and `_input.py`. Entry contracts can be amended inline in the same `app.pkl`.

### Output Shape

```text
contract/app.pkl
    ├─▶ atlan.yaml
    ├─▶ atlan-connectors-teradata.json
    ├─▶ crawler/{workflow-config}.json
    ├─▶ crawler/manifest.json
    ├─▶ crawler/_input.py
    ├─▶ miner/{workflow-config}.json
    ├─▶ miner/manifest.json
    └─▶ miner/_input.py
```

`atlan-connectors-agent.json` is intentionally not part of this app output shape. Apps reference that shared configmap from `AgentSelector.agentConfigEntries`; the canonical payload is owned by `AgentConfig.pkl` and generated separately.

### Bundle Properties

| Property | Type | Default | Description |
|---|---|---|---|
| `name` | String | required | Deployment/runtime identity (`teradata`). Local marketplace derives namespace/service from this. |
| `appId` | String | required | Global Marketplace UUID emitted as `app_id`. |
| `displayName` | String | `name.capitalize()` | App display name emitted as `display_name`. |
| `creatorOrg` | String? | null | Optional `creator_org`. |
| `type` | String | `"connector"` | Root marketplace type. |
| `iconUrl` | String? | null | Root `icon_url`. |
| `visibility` | String | `"public"` | Marketplace visibility. |
| `buildTag` | String | `"v1"` | Emitted as `build_tag`. |
| `selfDeployedRuntime` | Boolean | `false` | Emitted as `self_deployed_runtime`. |
| `metadata` | Mapping<String, Any> | `{}` | Extra top-level `atlan.yaml` fields. |
| `atlanYamlOverrides` | Mapping<String, Any> | `{}` | Deep overrides applied after typed fields and `metadata`. Use for exact rendered yaml control, including nested `deploy` overrides. |
| `entrypoints` | Listing<Entrypoint> | `[]` | Marketplace card / SDK entrypoint definitions. |
| `emitAtlanYaml` | Boolean | `true` | Emit `atlan.yaml`. Set `false` when using the bundle only for generated artifact layout / credential hoisting. |
| `emitEntrypoints` | Boolean | `true` | Emit the `entrypoints:` block. Set `false` for marketplace paths that do not support entrypoint passthrough. |
| `emitGeneratedArtifacts` | Boolean | `true` | Re-export entrypoint contract files. Credential configmaps are hoisted to the bundle root and deduped by filename; other files are emitted under `{entrypoint.name}/`. |
| `additionalOutputFiles` | Mapping<String, FileOutput> | `{}` | Explicit root-level files to emit with the bundle, for example a customized `AgentConfig.pkl` output. |
| `deploy` | Mapping<String, Any> | `{}` | Current flat deployment config. Unknown keys are passed through by local marketplace. |

### Entrypoint Properties

| Property | Type | Default | Description |
|---|---|---|---|
| `name` | String | required | SDK entrypoint key and generated folder (`crawler`, `miner`). |
| `id` | String? | null | Optional explicit marketplace/card id. Current local marketplace usually derives `{bundle.name}-{entrypoint.name}`. |
| `displayName` | String | `name.capitalize()` | Card display name. |
| `description` | String? | null | Card description. |
| `iconUrl` | String? | null | Card icon; falls back to bundle `iconUrl` when rendered. |
| `type` | String | `"connector"` | Card type (`connector`, `miner`, ...). |
| `source` | String? | null | Source slug (`teradata`). |
| `sourceCategory` | String? | null | Source category (`database`). |
| `categories` | Listing<String> | `[]` | Marketplace categories. |
| `docsUrl` | String? | null | Documentation URL. |
| `metadata` | Mapping<String, Any> | `{}` | Extra per-entrypoint yaml fields. |
| `contract` | Typed? | null | Inline or imported `NativeApp.pkl` contract whose `output.files` should be emitted. |

### Example

```pkl
amends "@app-contract-toolkit/NativeAppBundle.pkl"

import "@app-contract-toolkit/NativeApp.pkl" as NativeApp
import "@app-contract-toolkit/Config.pkl"
import "@app-contract-toolkit/Connectors.pkl"

name = "teradata"
appId = "019c4730-27cd-7a21-8bf7-e85766402e78"
displayName = "Teradata"
creatorOrg = "Atlan"
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
    name = "crawler"
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

For Teradata-style apps, entrypoint contracts should usually set:

```pkl
name = "teradata-crawler"                       // workflow configmap/card id
workflowTypeOverride = "teradata-app:crawler"   // SDK workflow type
connectorConfigName = "atlan-connectors-teradata"
taskQueuePrefix = "atlan-teradata"
```

If two entrypoint contracts emit the same credential config filename, the bundle emits it once at the root. If the same filename has different content, generation fails; use unique `connectorConfigName` values for genuinely different credentials.

Set `emitAtlanYaml = false` to use the bundle only for generated artifact layout and credential hoisting:

```pkl
emitAtlanYaml = false
```

Use `atlanYamlOverrides` for exact rendered yaml control. Overrides are deep-merged, so nested fields can be changed without replacing the whole parent object:

```pkl
atlanYamlOverrides {
  ["display_name"] = "Teradata Custom"
  ["deploy"] {
    ["containerPort"] = 9000
    ["dapr"] {
      ["eventstore"] = true
    }
  }
}
```

---

## AgentConfig.pkl — Shared Secure-Agent Config

`AgentConfig.pkl` owns the shared secure-agent configmap payload that previously lived in `marketplace-packages/packages/atlan/connectors/configmaps/agent.yaml`.

Apps should reference it by name from `AgentSelector.agentConfigEntries`:

```pkl
new Listing { "atlan-connectors-agent" }
```

The module is intentionally not included in every `NativeApp.pkl` or `NativeAppBundle.pkl` output. It represents a shared/global configmap, so app-generated folders only contain app-specific workflow, credential, manifest, and input artifacts unless the app opts in with `additionalOutputFiles`.

### AgentConfig Properties

| Property | Type | Default | Description |
|---|---|---|---|
| `name` | String | `"atlan-connectors-agent"` | Configmap name and output filename prefix. |
| `agentSelectorProperty` | String | `"agent-json"` | Workflow form field used by conditional secure-agent rules. |
| `newAppFrameworkAgentType` | String | `"new-app-framework"` | Agent type value used by secure-agent visibility rules. |
| `docsUrl` | String | `"https://ask.atlan.com/"` | Docs link used by generated banners. |
| `secretManagerValues` / `secretManagerLabels` | Listing<String> | default secure-agent stores | Secret-store values and labels. |
| `awsRegionValues` / `awsRegionLabels` | Listing<String> | supported AWS regions | AWS region values and labels. |
| `awsAuthMethodValues` / `awsAuthMethodLabels` | Listing<String> | IAM, assume-role, access-key | AWS auth method values and labels. |
| `azureAuthMethodValues` / `azureAuthMethodLabels` | Listing<String> | managed identity, service principal | Azure auth method values and labels. |
| `keyTypeValues` / `keyTypeLabels` | Listing<String> | multi-key, single-key | Key type values and labels. |
| `propertyOverrides` | Mapping<String, Any> | `{}` | Deep patches merged into existing secure-agent properties. |
| `extraProperties` | Mapping<String, Any> | `{}` | Additional fields appended under `properties`. |
| `extraAnyOf` | Listing<Any> | `[]` | Additional conditional requirement branches appended to `anyOf`. |
| `extraConfig` | Mapping<String, Any> | `{}` | Additional top-level keys appended to the generated config payload. |
| `configFile` | FileOutput | generated | JSON file output for `additionalOutputFiles` or direct module output. |

Generate the shared artifact directly when packaging or publishing that global configmap:

```bash
pkl eval -m generated src/AgentConfig.pkl
```

Output:

```text
generated/
└── atlan-connectors-agent.json
```

Apps can also amend and emit a customized copy:

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
  extraProperties {
    ["custom-agent-field"] = new Mapping {
      ["type"] = "string"
      ["ui"] = new Mapping {
        ["label"] = "Custom Agent Field"
      }
    }
  }
}

additionalOutputFiles {
  ["\(agentConfig.name).json"] = agentConfig.configFile
}
```

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
  includeRequiredWhenFalse: Boolean = false
  sensitive: Boolean = false             // true → password widget
  fieldType: FieldType = "text"          // text|password|textarea|select|number|checkbox|url|email
  options: Listing<String>?              // For select fields
  optionLabels: Mapping<String, String>? // Display labels for options
  defaultValue: String?                  // Pre-filled value
  validationRegex: String?               // Regex pattern
  width: Int = 8                         // Grid: 8=full, 4=half, 2=quarter
  rows: Int?                             // TextInput rows; set 1 for compact pasted-newline fields
  isHidden: Boolean = false              // true → hidden in UI (for fixed-value fields)
  feedback: Boolean?                     // Show validation feedback indicator
  message: String?                       // Inline validation message below field
  validationRules: Listing<Dynamic>?     // Validation rules (e.g., required, min, max)
  byocDisabled: Boolean?                 // Emit BYOCdisabled for frontend credential UIs
  includeUiWidget: Boolean = true
  includeUiLabel: Boolean = true
  includeUiHidden: Boolean = true
  includeUiPlaceholder: Boolean = true
  includeUiGrid: Boolean = true
  uiClass: String?
  uiRequired: Boolean?
}
```

Use `isHidden = true` for fields with fixed values that users should never see or edit (e.g., a fixed API host):
```pkl
new FieldSpec { name = "host"; defaultValue = "https://api.example.com"; isHidden = true }
new FieldSpec { name = "port"; fieldType = "number"; defaultValue = "443"; isHidden = true }
```

The `rows` option is emitted under `ui.rows` when non-null. Use it with
`fieldType = "textarea"` for compact multiline-capable fields, for example
`rows = 1` for certificate or key inputs that should remain visually short while
still accepting pasted newlines.

The `includeUi*`, `uiClass`, `uiRequired`, and `includeRequiredWhenFalse`
controls are exact-parity escape hatches for existing hand-authored
configmaps. New apps should normally leave them at their defaults.

### FieldType → Widget Mapping

| FieldSpec Config | Generated Widget |
|---|---|
| `sensitive = true` | `password` |
| `fieldType = "number"` | `inputNumber` |
| `fieldType = "select"` (≤4 options) | `radio` |
| `fieldType = "select"` (>4 options) | `select` |
| `fieldType = "checkbox"` | `checkbox` |
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

Each `Config.Condition` carries either `property` / `value` for a simple match (including booleans for switcher-driven fields) or `filter` for a compound `$and` / `$or` tree evaluated against sibling form fields, plus optional overrides for `ui`, `type`, enum values, required state, and default value. `AuthOption.fields`, `AuthOption.extraFields`, and `AuthOption.postExtraFields` accept `FieldSpec`, `ConditionalFieldSpec`, `NamedWidget`, or `NamedProperty` entries.

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

For an executable example, see `examples/full/app.pkl`, where
`token_audience` is a field-level `ConditionalFieldSpec` that appears only for
token auth. For connector-specific condition trees, add a focused example or Pkl
test that pins the generated credential JSON shape.

## AuthOption — Auth Type Definition

```pkl
open class AuthOption {
  label: String                                                   // Radio button label
  fields: Listing<CredentialFieldEntry>                            // FieldSpec, ConditionalFieldSpec, NamedWidget, or NamedProperty
  extraFields: Listing<CredentialFieldEntry> = []                  // Nested under "extra"
  postExtraFields: Listing<CredentialFieldEntry> = []              // Siblings emitted after "extra"
  anyOfRequiredFields: Listing<String> = []                        // Extra keys in this option's anyOf.required
  nestedLabel: String = ""                                        // Custom label for nested section wrapper
  nestedPlaceholder: String?                                      // Custom placeholder for nested section wrapper
  nestedHidden: Boolean = true                                    // Hide nested auth pane by default
  nestedExtraLabel: String = ""                                   // Label on the per-option `extra` nested section
  nestedExtraHeader: String? = "Advanced"                         // Header on `extra`; null omits it
}
```

`CredentialFieldEntry` is `FieldSpec | ConditionalFieldSpec | NamedWidget | NamedProperty`.
Use `NamedWidget` when a workflow-style `Config.UIElement` needs a key inside
the credential form. `fields`, `extraFields`, and `postExtraFields` all preserve
declaration order.

Use `credentialSharedExtraFields` when the same `extra` metadata should be
available regardless of auth type instead of being duplicated in every option.
`nestedExtraHeader` defaults to `"Advanced"`; JDBC-URL consumers override per
option (`"Configuration"` for Azure AD, `"Domain Configuration"` for NTLM).
`AuthOption` is `open` — see `JDBCUrlAuthOption` below for the JDBC-URL subclass.

### Extra credential anyOf rules

`anyOfRequiredFields` keys a conditional requirement off the **auth-type radio**.
When the requirement depends on a different input, use the module-level
`credentialExtraAnyOfRules`.

The credential `anyOf` block is generated from `credentialAuthOptions`: one
branch per key requiring the matching pane when `auth-type == <key>`. Each
`UIRule` in `credentialExtraAnyOfRules` is appended as an additional branch
using the same `whenInputs` → `properties.const` / `required` mapping as
workflow-config rules. Branches are appended after the auth-type branches in
both the plain auth-type flow and the JDBC-URL flow (the JDBC `anyOf` lives
inside the `jdbcUrl` object). Defaults to empty, preserving existing output, and
is only emitted when an `anyOf` block already exists (`credentialAuthOptions`
non-null).

```pkl
credentialCommonFields {
  new FieldSpec {
    name = "enable_ssl"; fieldType = "checkbox"; defaultValue = "true"; required = false
  }
  new FieldSpec { name = "ssl_cert"; fieldType = "textarea"; required = false }
}

credentialExtraAnyOfRules {
  new UIRule {
    whenInputs { ["enable_ssl"] = "true" }
    required { "ssl_cert" }
  }
}
```

Generated branch (appended after the auth-type branches):

```json
{
  "properties": { "enable_ssl": { "const": "true" } },
  "required": ["ssl_cert"]
}
```

### NamedWidget — Config.UIElement in credential forms

```pkl
class NamedWidget {
  name: String
  widget: Config.UIElement
}
```

### NamedProperty — raw credential property

```pkl
class NamedProperty {
  name: String
  property: Mapping<String, Any>
}
```

Use `NamedProperty` sparingly when a legacy credential configmap must emit a
fully-rendered property verbatim, for example a presentational custom widget
with only a `ui` block and no schema `type`.

```pkl
new NamedProperty {
  name = "pre-requisites"
  property = new Mapping {
    ["ui"] = new Mapping {
      ["widget"] = "FivetranPrerequisites"
      ["hidden"] = false
    }
  }
}
```

`FieldSpec` and `ConditionalFieldSpec` carry their JSON property key in
`name`. `Config.UIElement` subclasses normally get their key from
`uiConfig.tasks.*.inputs`; `NamedWidget` supplies the same key when embedding a
widget inside an auth-option credential form.

```pkl
credentialAuthOptions {
  ["platform_connector"] = new AuthOption {
    label = "Fivetran Platform Connector"
    extraFields {
      new FieldSpec { name = "selected_connector"; required = false; isHidden = true }
      new NamedWidget {
        name = "connection-qualified-name"
        widget = new Config.ConnectionSelector {
          title = "Atlan Connection"
          connectorFilters { "snowflake"; "redshift"; "bigquery"; "postgres"; "databricks" }
          selectedConnectorName = "credential-guid.platform_connector.extra.selected_connector"
        }
      }
      new NamedWidget {
        name = "include-filter"
        widget = new Config.SqlTree {
          title = "Fivetran Platform Schema"
          sqlQuery = "show atlan schemas"
          dependsOn = "credential-guid.platform_connector.extra.connection-qualified-name"
          additionalPropertiesToIncludeInCredentialBody {
            "auth-type"
            "credential-guid.platform_connector.extra.selected_connector"
          }
        }
      }
    }
    postExtraFields {
      new NamedProperty {
        name = "pre-requisites"
        property = new Mapping {
          ["ui"] = new Mapping { ["widget"] = "FivetranPrerequisites"; ["hidden"] = false }
        }
      }
    }
  }
}
```

---

## AdvancedJDBCUrlGroup — Host↔URL JDBC Credential Form

Toolkit primitive for SQL-family connectors whose credential form exposes a
Host↔URL toggle (mssql, cloud-sql-postgres, alloydb-postgres). Set at the
module level via `credentialUrlGroup` — switches the credential renderer from
the plain `auth-type` radio flow to the conditional `jdbcUrl` wrapper that
mirrors the argo marketplace configmaps.

### Generated structure

When `credentialUrlGroup != null`, the credential JSON emits:

1. A `connectBy` radio (Host / URL) at the root.
2. A `jdbcUrl: { type: "conditional" }` object containing:
   - **`conditions`** — N×M branches (auth-type × extraction-method) with the
     `AdvancedJDBCUrlGroup` widget config. `urlPartPropertyIdMapping` is
     resolved per-auth-type against the mode's namespace (`credential-guid.*`
     for direct, `agent-json.*` for agent).
   - **`properties.{host, port, extra.compiled_url}`** — conditional rows that
     toggle enabled/disabled based on `connectBy`.
   - **`properties.auth-type`** — conditional radio that may expose different
     enums for direct vs agent (e.g. `workload_identity_federation` agent-only
     on cloudsql).
   - **`properties.<authKey>`** — one nested pane per auth option (via the
     existing auth-pane renderer; labels / headers configurable).
   - **`properties.extra`** — shared section for fields that live outside any
     single auth type (`database`, `__enable_ssl`, `sqlalchemy_url`, …).
   - **`ui`** — default top-level `AdvancedJDBCUrlGroup` widget block pointing
     at `credential-guid.connectBy` / `credential-guid.extra.compiled_url`.
   - **`anyOf`** — enforces the selected auth-option pane is present.

### Classes

```pkl
class AdvancedJDBCUrlGroup {
  protocol: String = ""                      // "" for mssql; "postgresql+asyncpg" for postgres-family
  urlAddonBefore: String                     // Prefix on the URL text input
  connectByDefault: "host"|"url" = "host"    // Initial radio value
  urlLabel: String = "SQLAlchemy Connection URL"
  urlHelp: String?
  urlPlaceholder: String?
  hostField: FieldSpec                       // Shared host row (FieldSpec re-used across auth-types)
  portField: FieldSpec                       // Shared port row
  extraFields: Listing<FieldSpec|ConditionalFieldSpec> = []  // Shared `extra` block
  extraHeader: String = "Advanced"           // Header on the shared extra section
  connectByLabel: String = "Connect by"
  connectByRules: Listing<Dynamic>           // Defaults to [{required:true, message:"Please select..."}]
}

class JDBCUrlAuthOption extends AuthOption {
  urlPartMapping: UrlPartMapping             // Per-auth-type URL mapping
  extractionModes: Listing<"direct"|"agent"> = { "direct"; "agent" }
  nestedLabel: String                        // Required (no default) — distinct from `label`
}

class UrlPartMapping {
  hostname: String?                          // Path from credential-form root (e.g. "host")
  port: String?
  pathname: String?
  searchParams: Mapping<String, UrlValue> = new Mapping {}
}

abstract class UrlValue {}
class UrlLiteral extends UrlValue { value: String }  // emitted verbatim
class UrlRef extends UrlValue { field: String }      // resolved to <namespace>.<field>
```

### Example

Minimal skeleton:

```pkl
credentialUrlGroup = new AdvancedJDBCUrlGroup {
  urlAddonBefore = "mssql+pyodbc://"
  hostField = new FieldSpec { name = "host"; displayName = "Host"; width = 6 }
  portField = new FieldSpec { name = "port"; fieldType = "number"; defaultValue = "1433"; width = 2 }
  extraFields {
    new FieldSpec { name = "database" }
    new FieldSpec { name = "__enable_ssl"; fieldType = "select"; options { "yes"; "no" } }
  }
}

credentialAuthOptions {
  ["basic"] = new JDBCUrlAuthOption {
    label = "Basic"
    nestedLabel = "Basic Authentication"
    urlPartMapping = new UrlPartMapping {
      hostname = "host"; port = "port"; pathname = "extra.database"
      searchParams {
        ["Encrypt"] = new UrlRef { field = "extra.__enable_ssl" }
        ["TrustServerCertificate"] = new UrlLiteral { value = "yes" }
      }
    }
    fields {
      new FieldSpec { name = "username" }
      new FieldSpec { name = "password"; sensitive = true }
    }
  }
}
```

### Agent extraction wiring

JDBC-URL consumers must wire the `agent-json` field through the toolkit helper
`module.generatedAgentJsonEntries(...)` instead of authoring a duplicate
schema. This derives the agent override block (with `"Store Credential Path"`
placeholders + empty defaults) directly from `credentialAuthOptions`:

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

Only auth options with `"agent"` in `extractionModes` appear in the override
block.

### Invariants

Enforced at generation time; all throw with clear messages:

1. `credentialUrlGroup != null` → `credentialAuthOptions` must be non-null.
2. `credentialUrlGroup != null` → `credentialAuthOptions` must be non-empty.
3. `credentialUrlGroup != null` → every option must be a `JDBCUrlAuthOption`
   (not a plain `AuthOption`).

### Parity testing

For connector-specific JDBC URL contracts, add a focused Pkl test that compares
the generated credential JSON against the authoritative marketplace shape.

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
  hidden titleOverride: String?                    // Optional display title override
  hidden idOverride: String?                       // Optional generated step id override
  description: String = ""
  hidden inputs: Mapping<String, UIElement>         // Form fields
  fixed id = idOverride ?? title.replaceAll(" ", "_").toLowerCase()
  fixed properties: List<String>                   // (Generated) field names
}
```

`titleOverride` and `idOverride` are for legacy configmaps whose displayed step
title and stable step id differ from the task map key.

All `UIElement` subclasses inherit exact-parity UI emission controls:
`emitUiLabel`, `emitUiHidden`, `emitUiHelp`, `emitUiGrid`, `uiClass`, and
`uiRequired`, plus `includeInManifest` and `includeInInput` for UI-only fields.
They default to the current generated shape and should only be changed when
matching an existing hand-authored configmap or intentionally rendering static
UI that should not become a runtime input.

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
| `ConnectionSelector` | `connectionSelector` | `str` | `multiSelect`, `connectorFilter` (single), `connectorFilters` (multi — emits `connectorName` as list), `connectionCategories`, `selectedConnectorName`, `selectedCredentialGuid`, `emitMode`, `emitStart` |
| `ConnectionRefInput` | `connectionSelector` | `ConnectionRef \| None` or `list[ConnectionRef]` | Emits `ui.shouldIncludeConnectionInfo = true`. Use when the workflow needs both `attributes.qualifiedName` and `attributes.defaultCredentialGuid` from the selected connection. Supports `multiSelect`, `connectorFilter`, `connectorFilters`, `connectionCategories`, `selectedConnectorName`, `selectedCredentialGuid`, `displayOnlyMultiConnections`, `emitMode`, `emitStart` |
| `CredentialInput` | `credential` | `str` | `credType` (required), `authTypeVsLabel`, `hiddenFields`, `additionalDisplayFields` — triggers credential form fetch |
| `APITokenSelector` | `apiTokenSelect` | `str` | Select existing API token |

`ConnectionRefInput` uses the same frontend widget as `ConnectionSelector`, but
sets `ui.shouldIncludeConnectionInfo = true`. The submitted form value is the
selected connection object (or list of connection objects), not only the
qualified-name string.

```pkl
["connection"] = new Config.ConnectionRefInput {
  title = "Connection"
  connectorFilter = "teradata"
  placeholderText = "Select a connection"
  required = true
}
```

Generated `_input.py`:

```python
connection: ConnectionRef | None = None
```

For multi-select:

```pkl
["connections"] = new Config.ConnectionRefInput {
  title = "Connections"
  connectorFilters { "teradata"; "snowflake" }
  multiSelect = true
}
```

Generated `_input.py`:

```python
connections: Annotated[list[ConnectionRef], MaxItems(1000)] = Field(default_factory=list)
```

#### Trees & Filters

| Class | Widget | Python Type | Notes |
|---|---|---|---|
| `SqlTree` | `sqltree` | `dict[str, Any]` | `sqlQuery`, `cred`, `excludePatterns`, `databaseExcludePatterns`, `desc`, `multiSelect` (emits `ui.multiple`), `dependsOn` (scope to sibling field), `databasesUnselectable` (lock DB-level selection), `additionalPropertiesToIncludeInCredentialBody`, `validationRules`, `includeDefault` |
| `APITree` | `apitree` | `dict[str, Any]` | Legacy API tree. Emits `{}` as the config default; generated input also accepts JSON object strings such as `"{}"` and coerces them to dicts. `credentialType`, `metadataTemplate`, `desc` |
| `ApiTreeSelect` | `apiTreeSelect` | `dict[str, Any]` | Workflows-v2 API tree selector used by Power BI-style metadata pickers. Emits `{}` as the config default; generated input also accepts JSON object strings. `credentialType`, `cred`, `metadataTemplate`, `metadataTransformer`, `strict`, `multiSelect`, `desc` |
| `DsnTreeMap` | `dsnTreeMap` | `dict[str, Any]` | Maps DSN names to connection qualified names. `mapConfig` carries heading/content/input/connection labels. |
| `GlossarySelector` | `GlossarySelector` | `str` | Glossary picker. `selectorMode = "multiple"`, `showGlossaryIcon`, `showGlossaryCount`, `placeholderText` |

#### Complex

| Class | Widget | Python Type | Notes |
|---|---|---|---|
| `NestedInput` | `nested` | `dict[str, Any]` | `inputs` — sub-element map |
| `Sage` / `SageV2` | `sage` / `sageV2` | `str` | `checks` — preflight definitions. `connectorConfig` + `selectedCredentialGuid` route per-dialect checks to a selected connection's configmap. |
| `FileUploader` | `fileUpload` | `FileReference \| None` | `fileTypes` |
| `AgentSelector` | `agent` | `dict[str, Any]` | `agentConfigEntries` — use `Listing<Any>` with `Mapping` for nested objects needing `"default"` keys |
| `InfoBanner` | `infoBanner` | omitted by default | Static markdown banner with `bannerType`, `content`, optional `iconName`, `hideBannerIcon`, and `linkConfig`. Defaults to `includeInManifest=false` and `includeInInput=false`. Use `widgetName = "InfoBanner"` for credential banners that need that casing. |
| `Switcher` | `switcher` | `bool` | Boolean switch with `switchTitle`, `defaultSelection`, optional `begin`, and `toastConfig`. Can be used in credential configs through `NamedWidget`. |
| `ConditionalInput` | configurable | `str` or `dict` | `baseWidgetType` (default `"radio"`), `conditions`, sqltree/connection/credential-specific properties, generic InfoBanner props, and `outputValueType` for object-returning branches |
| `CustomWidget` | `<widgetName>` | `str` | Escape hatch for bespoke frontend components. `widgetName` picks the component; `props` pass through verbatim into the `ui` object. Use sparingly — prefer typed widgets. |

#### Computed

| Function | Widget | Python Type | Notes |
|---|---|---|---|
| `evaluateWidget(val)` | `evaluate` | `str` | Factory function returning a `Widget` for `Condition.overrideUi`. Derives value from other form selections (hidden, no UI) |

`InfoBannerType` is the banner style union accepted by `InfoBanner.bannerType`:
`"info"`, `"warning"`, `"error"`, `"prerequisites"`, or `"side-note"`.
`WidgetLink` is the reusable link object for banner links: `label`, `url`, and
optional `icon`.

When adding workflows-v2 widgets, add a focused example or Pkl test that pins
the frontend renderer shape.

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
  dependsOnCondition: DependencyCondition?      // Explicit AE condition, mutually exclusive with dependsOn
  errorHandling: ErrorHandlingConfig?           // Retry / timeout policy, see below
}
```

`nodeType` is PKL-side only (not emitted to JSON). AE auto-infers `"workflow"` when
`activity_name == "execute_workflow"`, which this toolkit always emits. The field
exists so PKL can reject `heartbeatTimeoutSeconds` (activity-only) at `pkl eval`
time instead of at AE parse time.

`dependsOn` emits two shapes based on list length:

- 1 entry → `"depends_on": {"node_id": "..."}` (fires on the parent's SUCCESS *or* FAILURE)
- 2+ entries → `"depends_on": {"and_conditions": [{"node_id": "...", "tag": "success"}, ...]}`
  (strict AND-fan-in: node is scheduled only when every listed parent reaches SUCCESS)

The `tag: "success"` in the multi-parent shape is honored by AE's condition evaluator
(`automation_engine/workflows/graph.py`), which gates on
`WorkflowRunNodeStatus.SUCCESS`. Because single-parent emission omits `tag`, adding a
second entry to an existing `dependsOn` is not a pure additive change — it switches the
node from "fire on any completion" to "fire only on all-success". Call this out in the
app PR that introduces the second entry. See `examples/fanin/` for a minimal example.

Pkl listing amendment matters for pre-built nodes. Nodes such as `PublishNode`,
`QueryIntelligenceNode`, and `LineageNode` define defaults like
`dependsOn { upstream }`. If you write `dependsOn { "qi"; "publish" }` inside
one of those nodes, Pkl appends to the default instead of replacing it; a
default `LineageNode` would render dependencies on `extract`, `qi`, and
`publish`. Use `dependsOn = new Listing { "qi"; "publish" }` for a clean
replacement. Use `dependsOn = null` plus `dependsOnCondition = new
DependencyCondition { ... }` when replacing the default with explicit tagged or
nested AE logic.

Use `dependsOnCondition` when you need AE's full dependency condition contract:
direct node conditions with tags, `andConditions`, `orConditions`, and nested
condition trees. It is mutually exclusive with `dependsOn`. For pre-built nodes
that define a default `dependsOn`, set `dependsOn = null` before setting
`dependsOnCondition`.

```pkl
class DependencyCondition {
  nodeId: String?
  tag: String?
  andConditions: Listing<DependencyCondition>?
  orConditions: Listing<DependencyCondition>?
}
```

Direct condition:

```pkl
dependsOnCondition = new DependencyCondition {
  nodeId = "extract"
  tag = "success"
}
```

OR condition with built-in failure tags:

```pkl
dependsOnCondition = new DependencyCondition {
  orConditions {
    new DependencyCondition { nodeId = "left"; tag = "failure" }
    new DependencyCondition { nodeId = "right"; tag = "failure" }
  }
}
```

Nested condition:

```pkl
dependsOnCondition = new DependencyCondition {
  andConditions {
    new DependencyCondition { nodeId = "prepare"; tag = "success" }
    new DependencyCondition {
      orConditions {
        new DependencyCondition { nodeId = "primary"; tag = "success" }
        new DependencyCondition { nodeId = "fallback"; tag = "success" }
      }
    }
  }
}
```

Renderer output uses AE's snake_case shape: `node_id`, `tag`, `and_conditions`,
and `or_conditions`. Tags `success` and `failure` are built into AE; other tag
values match custom tags emitted by the upstream node. AE extracts every
referenced `node_id` as a graph predecessor, so `or_conditions` are evaluated
after all referenced predecessors finish or skip. They do not short-circuit
execution as soon as one parent succeeds.

### ErrorHandlingConfig

Maps 1:1 to AE's `ErrorHandlingConfig` (`automation_engine/registry/workflows/models.py`).
All fields are optional; omit the ones you don't need.

```pkl
class ErrorHandlingConfig {
  initialInterval: Int(this >= 1)?                 // Retry initial interval (seconds)
  backoffCoefficient: Int?                          // Retry backoff multiplier
  maximumInterval: Int(this >= 1)?                  // Retry max interval (seconds)
  maximumAttempts: Int?                             // Retry cap
  nonRetryableErrorTypes: Listing<String>?          // Exact error types to skip retry on
  startToCloseTimeoutSeconds: Int(isBetween(1, 259200))?  // Total node time budget
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

For toolkit-managed built-in extract and default publish nodes, use app-level
fields:

```pkl
extractNodeErrorHandling = new ErrorHandlingConfig {
  startToCloseTimeoutSeconds = 14400
}
publishNodeErrorHandling = new ErrorHandlingConfig {
  startToCloseTimeoutSeconds = 14400
}
```

These built-in nodes render `activity_name = "execute_workflow"`, so AE treats
them as workflow nodes. The toolkit rejects `heartbeatTimeoutSeconds` on these
fields at `pkl eval` time. If an app replaces publish via
`extraNodes["publish"]`, configure `errorHandling` on that `PublishNode` instead
of using `publishNodeErrorHandling`.

### PublishNode

Pre-built publish node. Usually specify only `upstream`, plus `executorEnabled`
when dry-run or extract-only control is needed:

```pkl
class PublishNode extends DAGNode {
  hidden upstream: String = "extract"
  executorEnabled: Boolean|String = true
  tagPipelineEnabled: Boolean|String? = null
  tagAttachmentsPrefix: String? = null
  displayName = "Publish to Atlas"
  workflowType = "PublishWorkflow"
  appName = "publish"
  args {
    ["connection_qualified_name"] = "$.<upstream>.outputs.connection_qualified_name"
    ["transformed_data_prefix"] = "$.<upstream>.outputs.transformed_data_prefix"
    ["publish_state_prefix"] = "$.<upstream>.outputs.publish_state_prefix"
    ["current_state_prefix"] = "$.<upstream>.outputs.current_state_prefix"
    ["connection_creation_enabled"] = true
    ["executor_enabled"] = executorEnabled
    ["connection_entity"] = "{{connection}}"
  }
  dependsOn { upstream }
}
```

`executorEnabled` can be a Boolean literal or an exact workflow placeholder string.
For the auto-generated default publish node, set the app-level
`publishExecutorEnabled`; for `extraNodes["publish"] = new PublishNode { ... }`,
set `executorEnabled` on that node.

If the workflow form contains `enable-tags`, `PublishNode` also emits
`tag_pipeline_enabled = "{{enable-tags}}"` and
`tag_attachments_prefix = "$.extract.outputs.tag_attachments_prefix"` by
default. This is inherited by both the toolkit-generated publish node and custom
`extraNodes["publish"] = new PublishNode { ... }` nodes. The extract workflow must return
`tag_attachments_prefix`; native Databricks uses that output to point publish-app
at connector-emitted `{output_path}/transformed/tag_attachments` payloads for
typedef resolution and classification enrichment. Without `tag_pipeline_enabled`,
publish-app can receive TagAttachment entities while still stripping
classifications from Atlas payloads. Set app-level `publishTagPipelineEnabled` or
`publishTagAttachmentsPrefix` only when an app uses a different form field or
extract output key.

If both `enable-tags` and legacy `enable-tag-sync` exist, the toolkit prefers
`enable-tags`. Apps that only expose `enable-tag-sync` still get
`tag_pipeline_enabled = "{{enable-tag-sync}}"`.

For compact Automation Engine step labels, set app-level
`extractActivityDisplayName` or `publishActivityDisplayName`. The extract default
is `"Extract {displayName} Metadata"` and the default publish label is
`"Publish to Atlas"`, so existing manifests do not change unless an app opts in.
These fields only change manifest `activity_display_name`; they do not change
node ids, workflow types, task queues, app names, dependencies, or args. For a
custom `extraNodes["publish"]`, set `displayName` directly on that `PublishNode`
because app-level `publishActivityDisplayName` only applies to the
toolkit-generated default publish node.

For built-in extract/default publish retry and timeout policy, use
`extractNodeErrorHandling` and `publishNodeErrorHandling`. These fields emit
manifest `error_handling` without replacing the generated nodes, and only accept
workflow-safe `ErrorHandlingConfig` values. See `examples/publish-controls/`.

### QueryIntelligenceNode

Pre-built Query Intelligence workflow node. Defaults:
- `upstream = "extract"`
- `displayName = "Parse SQL Queries"`
- `workflowType = "QueryIntelligenceWorkflow"`
- `appName = "automation-engine"`
- `taskQueue = "atlan-query-intelligence-{deployment_name}"`

Required:
- `sqlKey`
- one of `vendorName` or `vendorKey`

The default `inputPrefix` / `outputPrefix` assume the extract node emits
`qi_input_prefix` and `qi_output_prefix`. Override them for connectors that
use different path names such as `transformed_data_prefix` or
`view_lineage_output_prefix`.

```pkl
class QueryIntelligenceNode extends DAGNode {
  hidden upstream: String = "extract"
  connectionQualifiedName: String = "$.<upstream>.outputs.connection_qualified_name"
  inputPrefix: String = "$.<upstream>.outputs.qi_input_prefix"
  outputPrefix: String = "$.<upstream>.outputs.qi_output_prefix"
  vendorName: String?
  vendorKey: String?
  sqlKey: String
  catalogKey: String = "DATABASE_NAME"
  schemaKey: String = "SCHEMA_NAME"
  @Deprecated timestampKey: String = "START_TIME"
  mineOutputType: "parquet"|"json" = "json"
  @Deprecated parsingMode: "fast"|"fallback"|"schema-aware"|"competitive"|"lorien-only" = "fallback"
  extraFilter: String?
  indirectLineage: Boolean|String? = null
  ignoreOrphans: Boolean|String? = null
  @Deprecated columnsToPreserve: String?
  relatedAssetsOutputPrefix: String?
  @Deprecated parserArtifactsKey: String?
  connectionCacheKey: String?
  currentStatePrefix: String?
  @Deprecated instanceName: String?
}
```

Validation enforced at `pkl eval` time:
- `vendorName` or `vendorKey` must be set

Cloud bucket and backend are resolved from the QI app's pod environment, not
per-workflow args. Configure storage at the QI app deployment, not on this node.

**Deprecated fields (kept for back-compat, ignored by the QI app):**

| Field | Reason |
|---|---|
| `timestampKey` | QI no longer filters records by timestamp. |
| `parsingMode` | Parsing strategy is now tuned internally by QI per workload. |
| `columnsToPreserve` | All columns/fields are preserved in JSON mode (now the default). |
| `parserArtifactsKey` | No longer consumed by QI. |
| `instanceName` | Instance name is read from QI app environment variables, not workflow args. |

These fields still emit their corresponding args into `manifest.json` when set,
but the QI app discards them. New contracts should not set them.

**`mineOutputType` default flipped to `"json"`.** New integrations get JSON
automatically. `"parquet"` remains valid for legacy miner-style inputs but
should not be used by new contracts.

Typical crawler-style example:

```pkl
extraNodes {
  ["qi"] = new QueryIntelligenceNode {
    vendorName = "athena"
    sqlKey = "attributes.definition"
    catalogKey = "attributes.databaseName"
    schemaKey = "attributes.schemaName"
    inputPrefix = "$.extract.outputs.transformed_data_prefix"
    outputPrefix = "$.extract.outputs.view_lineage_output_prefix"
  }
}
```

`indirectLineage` and `ignoreOrphans` are `Boolean|String?` (HYP-793 pattern).
Use a Boolean literal for static values, or an exact template placeholder
string when the value must be wired from a workflow form field:

```pkl
extraNodes {
  ["qi"] = new QueryIntelligenceNode {
    vendorName = "snowflake"
    sqlKey = "QUERY_TEXT"
    ignoreOrphans = true                          // static
    indirectLineage = "{{indirect-lineage}}"      // templated form field
  }
}
```

Heracles substitutes exact workflow parameter names only. Dotted placeholders
such as `"{{control_config.indirect_lineage}}"` are not resolved — they are
stripped from the DAG inputs before the workflow starts, so QI silently runs
with its default. The placeholder must match a workflow form input key.

The QI app coerces resolved string values (`"true"`, `"false"`, `"1"`, `"0"`,
`"yes"`/`"no"`, `"on"`/`"off"`, case-insensitive) to bool via Pydantic. Empty
strings and unrecognised values fail Pydantic validation at workflow start —
omit the field instead of emitting `""` to mean "use default".

### PopularityMineColumnMapping

Typed source-specific query-history column mapping for `PopularityNode`.
Defaults mirror `atlan-popularity-app`'s built-in `MineColumnMapping`
(Snowflake-shaped). Override only fields that use different source column
names. The class is purely additive: existing connectors that don't set
`mineColumnMapping` still emit no `mine_column_mapping` block.

```pkl
class PopularityMineColumnMapping {
  queryId: String = "QUERY_ID"
  userName: String = "USER_NAME"
  warehouseName: String = "WAREHOUSE_NAME"
  roleName: String = "ROLE_NAME"
  totalElapsedTime: String = "TOTAL_ELAPSED_TIME"
  creditsCloudServices: String = "CREDITS_USED_CLOUD_SERVICES"
  creditsCompute: String = "CREDITS_USED_COMPUTE"
  queryType: String = "QUERY_TYPE"
  startTime: String = "START_TIME"
  queryHash: String = "md5(QUERY_TEXT)"
  queryText: String = "QUERY_TEXT"
  queryTag: String = "NULL"
  queryTagsSql: String = "NULL"
  sourceQueryTypeQi: String = "SOURCE_QUERY_TYPE"
  parsedDataKeepFilter: String = "(OUTPUT_FLAGS & (33554432 | 67108864)) != 0"
  caseInsensitiveMatch: Boolean = true
  hasNativeInputs: String?
  nativeInputsPayload: String?
}
```

Per-source guidance for the source-shape fields:

- `queryHash` / `queryText`:
  Pop-app pins `query_hash = md5(<text-column>)` across all sources — see
  `tests/test_query_hash_convention.py` in `atlan-popularity-app`. Native
  source hash columns (Snowflake `QUERY_HASH`, Redshift `HASH`) are
  intentionally NOT used: Snowflake's hash is version-bumped/undocumented and
  breaks cross-tenant and cross-source comparability. The toolkit default
  matches pop-app's Snowflake default; non-Snowflake connectors override the
  text-column name.
  - Snowflake: `md5(QUERY_TEXT)` / `QUERY_TEXT` (default).
  - BigQuery: `md5(CLEANED_QUERY)` / `CLEANED_QUERY`.
  - Databricks: `md5(statement_text)` / `statement_text`.
  - Redshift: `md5(QUERYTXT)` / `QUERYTXT`.
  Both are required even on sources where the values are not consumed
  downstream — popularity's canonical `v_mined` projection references both
  unconditionally.
- `sourceQueryTypeQi`: SQL expression projected as `SOURCE_QUERY_TYPE` on the
  parsed-data view (`v_qi`). Snowflake / BigQuery parsed_data carry this
  column natively; Databricks parsed_data does NOT, so set
  `"NULL::VARCHAR"` there. Set `"NULL::VARCHAR"` for any source whose
  parsed_data does not natively expose `SOURCE_QUERY_TYPE`.
- `parsedDataKeepFilter`: SQL predicate applied at `v_qi` build time to keep
  only popularity-relevant parsed rows. Default is QI's `OUTPUT_FLAGS`
  bitmask — bit 25 (Gudusoft, `33554432`) plus bit 26 (sqlglot,
  `67108864`). Sources still pre-dating `OUTPUT_FLAGS` can override to
  `"COALESCE(HAS_ERROR, FALSE) = FALSE"`.
- `caseInsensitiveMatch`: case-folding for asset matching. Default `true`
  matches the miner YAMLs in marketplace-packages
  (Snowflake / BigQuery / Redshift / Databricks).

The rendered manifest uses the snake_case keys expected by
`PopularityParameters.mine_column_mapping`, for example `query_id`,
`credits_cloud_services`, `query_hash`, `query_text`, `source_query_type_qi`,
`parsed_data_keep_filter`, `case_insensitive_match`, and
`native_inputs_payload`.

### PopularityNode

Pre-built Popularity app workflow node. Defaults:
- `displayName = "Compute Popularity"`
- `workflowType = "PopularityWorkflow"`
- `appName = "popularity"`
- `taskQueue = "atlan-popularity-{deployment_name}"`
- waits for `queryIntelligenceNode = "qi"` and `assetPublishNode = "publish"` to succeed
- omits `lake_provider` unless `lakeProvider` is explicitly set
- omits `window_days`, `top_n_queries`, `top_n_users`, `dry_run`, `parsed_data_filter_has_error`, and `relationships_output_attribution` unless explicitly set

Required:
- `tenantId`
- `parsedDataPrefix`
- `minedDataPrefix`
- `connectionCachePath`
- `outputPrefix`

`tenantId` should come from a trusted runtime/session source, not from a
user-editable form value. `connectionCachePath` must be the exact SQLite cache
object key or local file path. For cloud runs the popularity app downloads this
single object; a directory prefix such as `connection-cache` is not sufficient.
By default the node omits `lake_provider` so the Popularity app can use its own
trigger defaults and deployment environment. Set `lakeProvider` only as an
explicit storage override.

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

```pkl
class PopularityNode extends DAGNode {
  queryIntelligenceNode: String? = "qi"
  assetPublishNode: String? = "publish"
  waitForQueryIntelligence: Boolean = true
  waitForAssetPublish: Boolean = true
  connectionQualifiedName: String = "$.extract.outputs.connection_qualified_name"
  tenantId: String
  parsedDataPrefix: String
  minedDataPrefix: String
  connectionCachePath: String
  outputPrefix: String
  accessHistoryDataPrefix: String?
  windowDays: (Int(this >= 1)|String)?
  topNQueries: (Int(this >= 1)|String)?
  topNUsers: (Int(this >= 1)|String)?
  lakeProvider: ("local"|"aws"|"gcp"|"azure")? = null
  mineColumnMapping: PopularityMineColumnMapping?
  includeFilter: Mapping<String, Listing<String>>?
  excludeFilter: Mapping<String, Listing<String>>?
  parsedDataFilterHasError: (Boolean|String)?
  relationshipsOutputAttribution: (Boolean|String)?
  queryTypesToIgnore: Listing<String>?
  dryRun: (Boolean|String)?
  extraArgs: Mapping<String, Any> = new Mapping {}
}
```

Rendered args:

```json
{
  "connection_qualified_name": "$.extract.outputs.connection_qualified_name",
  "tenant_id": "$.extract.outputs.tenant_id",
  "parsed_data_prefix": "$.extract.outputs.qi_output_prefix",
  "mined_data_prefix": "$.extract.outputs.query_history_prefix",
  "connection_cache_path": "$.extract.outputs.connection_cache_path",
  "output_prefix": "$.extract.outputs.popularity_output_prefix"
}
```

Typical query-history DAG shape:

```pkl
extraNodes {
  ["qi"] = new QueryIntelligenceNode {
    vendorName = "snowflake"
    sqlKey = "QUERY_TEXT"
    inputPrefix = "$.extract.outputs.query_history_prefix"
    outputPrefix = "$.extract.outputs.qi_output_prefix"
  }
  ["publish"] = new PublishNode {
    args {
      ["connection_cache_enabled"] = true
      ["connection_cache_via_app_enabled"] = true
    }
  }
  ["popularity"] = new PopularityNode {
    tenantId = "$.extract.outputs.tenant_id"
    parsedDataPrefix = "$.extract.outputs.qi_output_prefix"
    minedDataPrefix = "$.extract.outputs.query_history_prefix"
    connectionCachePath = "$.extract.outputs.connection_cache_path"
    outputPrefix = "$.extract.outputs.popularity_output_prefix"
  }
}
```

Set `waitForQueryIntelligence = false` when `parsedDataPrefix` already points
to parsed QI output. Set `waitForAssetPublish = false` when
`connectionCachePath` points to a pre-existing cache. If either wait flag is
true, the corresponding node id must be non-null.

`queryTypesToIgnore` is omitted by default so the popularity app uses its own
default ignored query types. Set `queryTypesToIgnore {}` only when you
intentionally want no ignored query types.

### LineageNode

Pre-built Lineage app workflow node. Defaults:
- `upstream = "extract"`
- `displayName = "Build Lineage Entities"`
- `workflowType = "LineageWorkflow"`
- `appName = "automation-engine"`
- `taskQueue = "atlan-lineage-{deployment_name}"`
- JSON/view-lineage input: `fileType = "json"`, `inputPath = ""`, `parsedViewsPath = "$.extract.outputs.view_lineage_output_prefix"`
- Native crawler output paths: `lineageOutputPath = "$.extract.outputs.lineage_stage_prefix"`, `cachePath = "connection-cache"`
- Storage settings: `lake_provider` and `cloud_storage_bucket` are omitted by default so the Lineage app can resolve tenant object-store settings from deployment environment. Set `lakeProvider` and `cloudStorageBucket` only as explicit transitional or non-default storage overrides.

Required:
- `connectorName`
- `sqlUnquotedCase` (`"upper"`, `"lower"`, or `"preserve"`)
- `ignoreAllCase`

```pkl
class LineageNode extends DAGNode {
  hidden upstream: String = "extract"
  connectionQualifiedName: String = "$.<upstream>.outputs.connection_qualified_name"
  connectorName: String
  sessionKey: String = "view-lineage"
  sqlUnquotedCase: "upper"|"lower"|"preserve"
  ignoreAllCase: Boolean
  inputPath: String = ""
  parsedViewsPath: String? = "$.<upstream>.outputs.view_lineage_output_prefix"
  lineageOutputPath: String = "$.<upstream>.outputs.lineage_stage_prefix"
  cachePath: String = "connection-cache"
  stagePath: String?
  lakeProvider: ("local"|"aws"|"gcp"|"azure")?
  cloudStorageBucket: String?
  localOutputPrefix: String?
  fileType: "parquet"|"json" = "json"
  azureBlob: Boolean?
  isTestEnv: Boolean?
  excludeFilePattern: String?
  includeFilePattern: String?
  enableSharded: Boolean?
  enableParentJobScoping: Boolean?
  enableFallbackMatch: Boolean?
  includeIndirectColumnRelations: Boolean?
  parsedViewsCurrentState: String?
  processRoutines: Boolean?
  routineMappingsPath: String?
  columnMapping: Mapping<String, String>?
  jsonKeyMapping: Mapping<String, String>?
  sortColumns: String?
  lineageCurrentState: String?
  workflowName: String?
  runName: String?
  tenantId: String?
  packageName: String?
  gudusoftVersion: String?
  startTimeEpoch: Int?
  endTimeEpoch: Int?
  maxParallelChunks: Int?
  maxRowsPerChunk: Int?
  ingestGroupSize: Int?
  maxParallelIngestGroups: Int?
  enableAccuracyTest: Boolean?
  traceDir: String?
  checkpointDir: String?
  resumeFromStage: Int?
  extraArgs: Mapping<String, Any> = new Mapping {}
}
```

Athena-style view lineage example:

```pkl
extraNodes {
  ["qi"] = new QueryIntelligenceNode {
    vendorName = "athena"
    sqlKey = "attributes.definition"
    catalogKey = "attributes.databaseName"
    schemaKey = "attributes.schemaName"
    inputPrefix = "$.extract.outputs.transformed_data_prefix"
    outputPrefix = "$.extract.outputs.view_lineage_output_prefix"
  }
  ["publish"] = new PublishNode {
    args {
      ...super.args
      ["connection_cache_enabled"] = true
      ["connection_cache_via_app_enabled"] = true
      ["current_state_via_app_enabled"] = true
    }
  }
  ["lineage-app"] = new LineageNode {
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
  ["lineage-publish"] = new LineagePublishNode {}
}
```

Use `dependsOn = new Listing { ... }` for shorthand success fan-in. Use
`dependsOn = null` plus `dependsOnCondition` when the dependency should be
documented as an explicit AE condition. The amendment form `dependsOn { ... }`
appends to the class default.

### LineagePublishNode

Pre-built lineage publish node. It emits the same lineage-specific publish
arguments Athena needs after Lineage app processing:
- `displayName = "Publish Lineage to Atlas"`
- `workflowType = "PublishWorkflow"`
- `appName = "publish"`
- `taskQueue = "atlan-publish-{deployment_name}"`
- `transformedDataPrefix = "$.extract.outputs.lineage_stage_prefix"`
- `publishStatePrefix = "$.extract.outputs.lineage_publish_state_prefix"`
- `currentStatePrefix = "$.extract.outputs.lineage_current_state_prefix"`
- `connection_creation_enabled = false`
- `executor_enabled = true`
- `cacheNamespace = "lineage"` rendered as `cache_namespace`
- connection cache and current-state app-cache flags enabled

The default dependency is a strict success fan-in expressed as
`dependsOnCondition`: `lineage-app` AND `publish`, both tagged `success`. This
ensures lineage publish waits for both the Lineage app and the main asset
publish to complete successfully.

Set `waitForAssetPublish = false` when the Lineage app node already waits for
asset publish. In that shape, `LineagePublishNode` emits a direct
`dependsOn { "lineage-app" }` dependency instead of a redundant fan-in.
Set `cacheNamespace = "miner-lineage"` or another unique namespace when a
multi-entrypoint app needs lineage publish state/cache isolation between
crawler, miner, or other lineage-producing workflows.

```pkl
class LineagePublishNode extends DAGNode {
  hidden upstream: String = "lineage-app"
  assetPublishNode: String = "publish"
  waitForAssetPublish: Boolean = true
  hidden extractNode: String = "extract"
  connectionQualifiedName: String = "$.<extractNode>.outputs.connection_qualified_name"
  transformedDataPrefix: String = "$.<extractNode>.outputs.lineage_stage_prefix"
  publishStatePrefix: String = "$.<extractNode>.outputs.lineage_publish_state_prefix"
  currentStatePrefix: String = "$.<extractNode>.outputs.lineage_current_state_prefix"
  connectionEntity: String = "{{connection}}"
  connectionCacheEnabled: Boolean = true
  connectionCacheViaAppEnabled: Boolean = true
  currentStateViaAppEnabled: Boolean = true
  cacheNamespace: String = "lineage"
  extraArgs: Mapping<String, Any> = new Mapping {}
}
```

Use `upstream`, `assetPublishNode`, and `extractNode` only when your DAG uses
non-standard node ids. Use `waitForAssetPublish = false` when the Lineage app
node already depends on asset publish. Use `dependsOn = new Listing { ... }`
when replacing the default fan-in entirely.

### Custom Pipeline Example

Insert QI and enrichment nodes between extract and publish:

```pkl
extraNodes {
  ["qi"] = new QueryIntelligenceNode {
    vendorKey = "datasource/vendor"
    sqlKey = "rendered_source"
    catalogKey = "datasource/database"
    schemaKey = ""
    inputPrefix = "$.extract.outputs.transformed_data_prefix"
    outputPrefix = "$.extract.outputs.qi_output_prefix"
  }
  ["transform"] = new DAGNode {
    displayName = "Transform & Enrich"
    workflowType = "TransformWorkflow"
    taskQueue = "atlan-redshift-{deployment_name}"
    args {
      ["qi_output"] = "$.extract.outputs.qi_output_prefix"
    }
    dependsOn { "qi" }
  }
  ["publish"] = new PublishNode {
    upstream = "transform"
  }
}
```

Result: `extract → qi → transform → publish`

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
| `outputValueType` | String | inferred | Generated `_input.py` value type. Inferred as `"object"` for object base widgets; set to `"object"` when a conditional branch renders `dsnTreeMap` or another object-returning widget. |
| `uiType` / `content` / `iconName` / `hideWidgetIcon` / `linkConfig` | mixed | null | Generic base UI props used by widgets such as `InfoBanner`. |
| `widgetConfig` | Any? | null | Generic base UI config object used by widgets such as `dsnTreeMap`. |
| `sqlQuery` | String? | null | SQL query (when `baseWidgetType = "sqltree"`) |
| `credentialRef` | String? | null | Credential variable name (when sqltree) |
| `connectorConfig` | String? | null | Connector config name (when sqltree) |
| `schemaExcludePatterns` | List<String>? | null | Schema exclude patterns (when sqltree) |
| `databaseExcludePatterns` | List<String>? | null | Database exclude patterns (when sqltree) |
| `desc` | String? | null | Description text (when sqltree) |
| `multiSelect` | Boolean? | null | Multi-select (when sqltree) |
| `dependsOn` | String? | null | Sibling form field whose value scopes the sqltree branch; emits `dependentConnectionField` |
| `databasesUnselectable` | Boolean? | null | Lock database-level selection in sqltree branch; emits `areDatabasesUnselectable` |
| `connOptions` | Boolean? | null | Show connection options (when `baseWidgetType = "connection"`) |

### Condition

Each `Config.Condition` in a `conditions` listing (used by `ConditionalInput` and `ConditionalFieldSpec`) carries one match source plus per-condition overrides.

| Property | Type | Description |
|---|---|---|
| `property` | String? | Simple match: form property name to check. Pair with `value`. |
| `value` | Any? | Simple match: value that triggers this condition. Booleans are supported for widgets such as `switcher`. |
| `labFlag` | String? | Match when a lab flag is set. |
| `featureFlag` | String? | Match when a feature flag is set. |
| `filter` | Dynamic? | Compound `$and` / `$or` tree evaluated against sibling form fields. Emitted verbatim alongside `ui`; `property`/`value` are typically left null. Use for multi-property combos like SAP's `connection-type` × `extraction-method`. |
| `overrideUi` | Widget \| Mapping<String, Any>? | Widget override applied when the match succeeds. Use `Config.Widget` for reusable frontend props, or a mapping for fully custom UI props. |
| `overrideType` | String? | Override property `type` when the match succeeds. Useful when a conditional base wrapper renders as a scalar widget such as `credential`. |
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

When combining `ConditionalInput` and `AgentSelector`, add a targeted example or
Pkl test for the generated workflow JSON.

The `atlan-connectors-agent` entry above is a reference to the shared secure-agent configmap. It does not cause app contract generation to emit `atlan-connectors-agent.json`; use `AgentConfig.pkl` when that global artifact needs to be generated.

---

## CustomWidget — Bespoke Frontend Components

Escape hatch for connector-specific frontend components that don't fit any typed widget (e.g., `FivetranDeprecationWarning`, `FivetranPrerequisites`, `FivetranNoSupportedConnections`). The `widgetName` picks the frontend component; `props` pass through verbatim into the `ui` object.

### Properties

| Property | Type | Default | Description |
|---|---|---|---|
| `widgetName` | String | — (required) | Frontend component name emitted as the `widget` field. |
| `props` | `Mapping<String, Any>?` | null | Arbitrary key/value pairs merged into the `ui` object verbatim. |
| `valueType` | String | `"string"` | JSON-schema `type` for the property. Keep as `"string"` for presentational widgets. |
| `required` | Boolean | `false` | Whether a value must be set to proceed. |

### Example

```pkl
["deprecation-warning"] = new Config.CustomWidget {
  title = ""
  widgetName = "FivetranDeprecationWarning"
  props {
    ["link"] = "https://ask.atlan.com/hc/en-us/articles/8427117425297-How-to-set-up-Fivetran"
    ["endDate"] = 1714521600000
    ["style"] = new Dynamic { marginBottom = "1rem" }
  }
}
```

Emits:

```json
"deprecation-warning": {
  "type": "string",
  "required": false,
  "ui": {
    "widget": "FivetranDeprecationWarning",
    "link": "https://ask.atlan.com/hc/en-us/articles/8427117425297-How-to-set-up-Fivetran",
    "endDate": 1714521600000,
    "style": { "marginBottom": "1rem" }
  }
}
```

### When to use

- Connector-specific frontend components that won't be reused elsewhere.
- Prefer adding a typed widget class to `Config.pkl` if the component is reusable across connectors.
- Presentational widgets (banners, warnings) still emit a Python field on `AppInputContract` (defaulting to `str = ""`). The value is whatever the frontend posts back — usually empty — so the app code can ignore it.

---

## Multi-Entrypoint Apps

For apps serving multiple marketplace tiles from one deployment (e.g., crawler + miner).

### Structure

```pkl
// contract/app.pkl — bundle metadata + atlan.yaml
amends "@app-contract-toolkit/NativeAppBundle.pkl"
import "@app-contract-toolkit/NativeApp.pkl" as NativeApp
import "@app-contract-toolkit/Config.pkl"
import "@app-contract-toolkit/Connectors.pkl"

name = "teradata"
appId = "019c4730-27cd-7a21-8bf7-e85766402e78"

local commonFields: Listing<NativeApp.FieldSpec> = new { /* host, port, etc. */ }
local authOptions: Mapping<String, NativeApp.AuthOption> = new { /* basic, ldap, etc. */ }

local crawlerContract = (NativeApp) {
  name = "teradata-crawler"
  workflowTypeOverride = "teradata-app:crawler"
  connector = Connectors.TERADATA
  icon = "https://assets.atlan.com/assets/Teradata.svg"
  connectorConfigName = "atlan-connectors-teradata"
  taskQueuePrefix = "atlan-teradata"
  credentialCommonFields = commonFields
  credentialAuthOptions = authOptions
  uiConfig { /* crawler setup form */ }
}

local minerContract = (NativeApp) {
  name = "teradata-miner"
  workflowTypeOverride = "teradata-app:miner"
  connector = Connectors.TERADATA
  icon = "https://assets.atlan.com/assets/Teradata.svg"
  connectorConfigName = "atlan-connectors-teradata"  // share creds with crawler
  taskQueuePrefix = "atlan-teradata"                 // share task queue
  credentialCommonFields = commonFields
  credentialAuthOptions = authOptions
  uiConfig { /* miner setup form */ }
}

entrypoints {
  new Entrypoint { name = "crawler"; contract = crawlerContract }
  new Entrypoint { name = "miner"; contract = minerContract }
}
```

### Generate

```bash
pkl eval -m app/generated contract/app.pkl
```

### Key patterns

- **Root bundle**: Use `NativeAppBundle.pkl` to render current-compatible `atlan.yaml` and entrypoint outputs from one `app.pkl`.
- **Shared credentials**: Define fields once as locals in `app.pkl`, reference from both inline contracts.
- **Entrypoint configmap names**: Use names like `teradata-crawler` / `teradata-miner` so filenames match marketplace card ids.
- **Shared credential name**: Override `connectorConfigName` in every entrypoint contract. The bundle emits identical credential configs once at the root.
- **Shared task queue**: Override `taskQueuePrefix` in every entrypoint contract so Temporal routes by workflow type.
- **`__init__.py`**: Auto-generated in each output directory for Python imports

The SDK repo keeps the public example set intentionally small. Add a focused
multi-entrypoint example or Pkl test in the same PR when changing bundle
behavior.

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
          "extraction_method": "{{extraction-method}}",
          "include_filter": "{{include-filter}}"
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

Generated as a Pydantic model extending
`application_sdk.templates.contracts.ExtractionInput`. The parent class supplies
`allow_unbounded_fields=True` (so object-valued UI fields validate without
writing the escape hatch into connector-owned code), AE-payload normalization
(`metadata.{...}` to top level, hyphenated keys to underscores), and typed SDK
fields. UI properties whose Python names collide with parent fields are skipped
so the SDK declarations and validators remain in effect.

```python
# AUTO-GENERATED from app.pkl — DO NOT EDIT MANUALLY.
# To regenerate: make generate
from __future__ import annotations
from typing import Annotated, Any, ClassVar
from pydantic import Field
from application_sdk.contracts.types import ConnectionRef, FileReference, MaxItems
from application_sdk.credentials.ref import CredentialRef
from application_sdk.templates.contracts import ExtractionInput


class AppInputContract(ExtractionInput):
    _config_hash_exclude: ClassVar[set[str]] = {
        "output_dir",
        "checkpoint_dir",
        "load_to_atlan",
        "publish_dry_run",
    }

    # UI fields (from uiConfig.properties, hyphens to underscores).
    # workflow_id / connection / credential_guid / credential_ref /
    # extraction_method / agent_json / output_prefix / output_path /
    # include_filter / exclude_filter / temp_table_regex / source_tag_prefix
    # are provided by ExtractionInput and intentionally not re-emitted here.
    # ...

    # Credential ref (when hasCredentialConfig)
    redshift_credential: CredentialRef | None = None

    # Publish fields (when hasPublishStep=True and hasPublishInputFields=True)
    output_dir: str = ""
    checkpoint_dir: str = ""
    load_to_atlan: bool = True
    publish_dry_run: bool = False
```

**Key differences from earlier versions:**
- Base class: `application_sdk.templates.contracts.ExtractionInput` (carries the
  unbounded-fields opt out, AE payload normalization, and SDK-owned typed fields)
- Connection type: `ConnectionRef` (not `pyatlan.models.connection.Connection`)
- Credential type: `application_sdk.credentials.ref.CredentialRef`
- Dict fields: `Annotated[dict[str, Any], MaxItems(1000)]` with `Field(default_factory=dict)`.
  Object-widget fields also parse JSON object strings such as `"{}"` before
  validation so workflow defaults passed through as strings remain compatible.
- `allow_unbounded_fields=True` is inherited from `ExtractionInput`, never written
  into generated connector contracts.
- SDK-owned fields are skipped in the generated subclass so parent validators
  continue to apply. This includes `include_filter`, `exclude_filter`, and
  `temp_table_regex`.
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
