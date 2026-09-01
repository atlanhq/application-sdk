# app-contract-toolkit

PKL toolkit for defining Atlan app contracts. One `app.pkl` file generates every artifact needed to deploy a native app — workflow config, credential config, Automation Engine manifest, typed Python input model, `atlan.yaml`, and `app.yaml`.

## Quick Start

`contract/PklProject`:

```pkl
amends "pkl:Project"

dependencies {
  ["app-contract-toolkit"] {
    uri = "package://atlanhq.github.io/application-sdk/contracts/app-contract-toolkit@<LATEST_VERSION>"
  }
}
```

```pkl
// contract/app.pkl
amends "@app-contract-toolkit/App.pkl"

import "@app-contract-toolkit/Connectors.pkl"

name = "snowflake"
displayName = "Snowflake"
connector = Connectors.SNOWFLAKE
icon = "https://assets.atlan.com/assets/snowflake.svg"
credentialConnectorType = "jdbc"

credentialCommonFields {
  new FieldSpec { name = "host"; displayName = "Host"; width = 6 }
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

uiConfig = new UIConfig {
  tasks {
    ["Credential"] {
      inputs {
        ["credential-guid"] = new Widgets.CredentialInput { credType = "atlan-connectors-snowflake" }
      }
    }
    ["Connection"] {
      inputs {
        ["connection"] = new Widgets.ConnectionCreator { placeholderText = "Connection Name" }
      }
    }
    ["Metadata"] {
      inputs {
        ["include-filter"] = new Widgets.SqlTree {
          title = "Include schemas"
          sqlQuery = "show atlan schemas"
          credentialType = "atlan-connectors-snowflake"
          cred = "credential-guid"
        }
      }
    }
  }
}
```

Generate (run from the **app repo root** — not from inside `contract/`):

```bash
cd contract && pkl project resolve && cd ..
pkl eval -m . contract/app.pkl
```

Output (emitted at the repo root):

```
atlan.yaml                               # Marketplace manifest (DO NOT EDIT — generated)
app.yaml                                 # SDR CI shim (DO NOT EDIT — generated)
app/generated/
├── __init__.py
├── snowflake.json                       # Workflow config (UI form)
├── atlan-connectors-snowflake.json      # Credential config (auth form)
├── manifest.json                        # AE DAG template
└── _input.py                            # Python AppInputContract
```

The SDK auto-serves these from `ATLAN_CONTRACT_GENERATED_DIR`, defaulting to `app/generated`.

## Curated Examples

The `examples/` directory contains executable contracts that teach stable toolkit patterns.

- [`examples/minimal/`](examples/minimal/) — smallest possible contract; uses all defaults. Start here.
- [`examples/full/`](examples/full/) — every overridable feature: JDBC URL auth, all pipeline steps, diverse widgets, UIRules, extraNodes.
- [`examples/bundle/`](examples/bundle/) — multi-entrypoint app (crawler + miner); shared credential configmap; per-entrypoint artifact subfolders.
- [`examples/card-split/`](examples/card-split/) — two entrypoints where only one is a marketplace UI card (`packageId` on the card entrypoint; route-only entrypoint has none).
- [`examples/behind-the-scenes/`](examples/behind-the-scenes/) — single-entrypoint app with `marketplaceCard = false`; routable but no marketplace card or `package_id` emitted.
- [`examples/deploy/`](examples/deploy/) — single-pool deployment: KEDA, resources, env, and per-pool `overrides` under `deploy.pools["default"]`.
- [`examples/pools/`](examples/pools/) — `pools` map (preferred): named hot/cold worker pools with per-pool KEDA `cooldownPeriod` and resources.
- [`examples/connection-ref/`](examples/connection-ref/) — `ConnectionRefInput` widget, `pipeline.publish = null`.
- [`examples/publish-controls/`](examples/publish-controls/) — publish toggles, `includeInputFields`, `errorHandling`.
- [`examples/fanin/`](examples/fanin/) — multi-parent fan-in via `dependsOn`, explicit `DependencyCondition`.
- [`examples/agent-e2e/`](examples/agent-e2e/) — agent/SDR e2e codegen: `_e2e_credential.py` emits both `<Name>CredentialBody` (direct) and `<Name>AgentCredentialBody` (lightweight), plus an `extraction-method` ConditionalInput whose `overrideEnum` widens the substitutions `Literal` to `["direct", "agent"]`.
- [`examples/scheduled/`](examples/scheduled/) — cron background job via `schedules`; renders `triggers.schedules` into `manifest.json` (multiple schedules, non-UTC timezone, a `PAUSED` one). See [Schedules](docs/reference.md#schedules-background-jobs).
- [`examples/artifact-schemas/`](examples/artifact-schemas/) — data hand-off declarations via `artifactSchemas`; renders `app/generated/artifact_schemas.json` (parquet + NDJSON, nested paths, arrays of structs via the `[]` element step, an input artifact). See [Artifact Schemas](docs/reference.md#artifact-schemas-data-hand-off-declarations).

## What Gets Generated

### `atlan.yaml` (repo root)

Marketplace deployment manifest. Contains app identity, marketplace metadata (`short_description`, `long_description`, `docs_url`, `tags`), the `entrypoints` list, and (when `deploy` is configured) a typed `deploy:` block — app-level `dapr` and `overrides` plus a `pools:` map carrying per-pool keda/resources/env/overrides. The top-level `deploy:` block is synthesised from the first pool for backward compatibility. Emitted with a DO-NOT-EDIT header — edit `contract/app.pkl` instead.

### `app.yaml` (repo root)

Three-line SDR CI shim (`app_name`, `app_image`, `app_port`). Derived entirely from `name`. Emitted with a DO-NOT-EDIT header.

### Workflow Config (`app/generated/{name}.json`)

UI form schema fetched by the frontend during "Setup Workflow". Contains `properties` (form fields with widget types), `steps` (wizard progression), and `anyOf` (conditional visibility rules).

### Credential Config (`app/generated/atlan-connectors-{name}.json`)

Auth form schema. Contains common fields (host, port), auth-type radio, and nested input panes per auth type. Auth panes are hidden by default and revealed via `anyOf` rules when the corresponding auth type is selected.

Credentials are shared — a user creates credentials once and reuses them across crawler and miner workflows.

Credential `FieldSpec` values render to frontend widget schemas. For boolean
credential checkboxes, use `fieldType = "checkbox"`; the generated JSON keeps
`type = "boolean"` and emits `ui.widget = "checkbox"`. For credential file
uploads, use `fieldType = "fileUpload"` with `fileTypes { ".crt"; ".pem" }`.
For frontend's credential file-reference input, use
`fieldType = "credentialFileInput"`; uploaded files are stored as JSON upload
references, while typed reference values (for example secret-store keys or
`objectstore://` paths) remain plain strings. The generated JSON emits the
selected upload widget plus `ui.accept`, `ui.fileMetadata`, and opt-in
`ui.removeBeforeUpload` when the widget is emitted. When
`credentialAuthOptions` has a single entry, the auth-type radio is auto-hidden —
the generated default is still emitted, so no form input is required.
For `credentialUrlGroup` forms, the visible auth panes live inside
`jdbcUrl.properties`; root-level auth panes are hidden merge targets for agent
credential overrides. For sources whose JDBC URL separates its parameters with
`;` rather than the `?a=&b=` query-string form (Hive, for example, connects
with `jdbc:hive2://host:port/db;k=v;k2=v2`), set
`isDelimitedBySemicolons = true` on the `credentialUrlGroup`. The generated
config then emits `ui.isDelimitedBySemicolons` on the base `jdbcUrl.ui` block
and every per-mode/per-auth condition, and the frontend's
`AdvancedJDBCUrlGroup.vue` rewrites the separators before parsing the URL. The
flag is opt-in, defaults to `false`, and is omitted (not emitted as `false`)
when unset, so existing generated output is unchanged.

### Manifest (`app/generated/manifest.json`)

Automation Engine DAG template. Auto-derived from the declared workflow params and the typed `pipeline` block. Heracles substitutes `{{param}}` placeholders with form values before sending to AE.

Default pipeline: `extract → publish`. Opt out of publish with
`pipeline.publish = null`. Add parseQueries, popularity, or lineage steps by
setting the corresponding `pipeline.*` field. Opt in to notifications
with `notifications = true`.

A run-level **notification node** (`notifications`) is appended when `notifications = true`. It depends on the reserved run-level `workflow_complete` tag — Automation Engine runs it once when the workflow run reaches any terminal state (success or failure) — and dispatches the `notification-app`, which fans alerts out to the tenant's enabled integrations (Teams, etc.) and decides delivery per integration (`failureOnly`: failure-only vs. all runs). By default the node is not emitted.

Every DAG node also carries its own `app_name` inside `inputs.args` — the value the
SDK stamps on that node's logs (CNCT-93; metric labels stay connector-level).
**`app_name` is framework-owned:**
set it at contract time via `name` or `DAGNode.appName`, never as a form field. A
`uiConfig` property named `app_name` (or `app-name`) is a **generation error**, because
the extract node bakes the contract `name` so failure logs stay attributable — a
tenant-typed value would render a widget that reaches nothing. See
[`docs/reference.md`](docs/reference.md) → *Per-node `app_name` in `inputs.args`*.

A `legacy_workflow_types` block is emitted when the contract declares
`legacyWorkflowTypes` — inbound-only Temporal workflow type aliases that keep the worker
answering a pre-migration type while every SDK-initiated dispatch still emits the
canonical one. The block is the contracted declaration site: the SDK's
`App.legacy_workflow_types` class attribute must declare the same pairs, and conformance
`K015` fails the app on any disagreement. Nothing is emitted when no alias is declared.
See [`docs/reference.md`](docs/reference.md) → *Legacy Workflow Type Aliases*.

### `_input.py` (`app/generated/_input.py`)

Typed Python `AppInputContract` dataclass. SDK-owned fields inherited from `ExtractionInput`; app-specific fields generated from `uiConfig.properties`. Inherited `include_filter` and `exclude_filter` fields accept APITree object selections by normalizing them to the SDK filter-map shape before validation.

Framework-populated fields (`workflow_id`, `correlation_id`, `app_name`) are declared by
the SDK's base `Input` and are never regenerated from a `uiConfig` property — redeclaring
one would shadow the base field with the author's type and fail only at workflow dispatch.

### Artifact Schemas (`app/generated/artifact_schemas.json`)

Declared shapes for the files this app hands off, keyed by the contract `FileReference`
field the runtime materialises — never by a storage path. The SDK checks the real
artifact against the declaration at the hand-off, so a column that has quietly become a
string where the consumer expects a timestamp is reported instead of flowing on.

The toolkit is transport, not owner: it fixes the logical type vocabulary
(`string`/`int`/`float`/`bool`/`timestamp`/`date`/`json`/`any`, plus the additive
`decimal`/`binary`/`time`/`array`/`struct`/`map`) and the file shape; every field is
authored by the app. Nested payloads are paths plus a container type, not a recursive
grammar: `.name` descends into a container's named member and `[]` descends into an
array's element, so an array of structs is `columns` (`array`) → `columns[]` (`struct`)
→ `columns[].name` (`string`). Every path prefix must be declared, with a type that can
hold what the path descends into — otherwise a producer that flattened the container
away would still pass on the leaf assertion alone.

Every field must carry a non-empty `description`. A declaration is read by whoever is
debugging the hand-off that just failed, and a bare `name` + `type` pair states the
assertion without stating why it holds.

**Opt-in and emitted only when declared.** An app with no `artifactSchemas` block
generates byte-identical output to before the block existed — no new file. Unlike the
workflow config and manifest, this file does not depend on `uiConfig`. Multi-entrypoint
apps declare it per entrypoint and each copy lands under
`app/generated/{entrypoint}/`. See
[`docs/reference.md`](docs/reference.md#artifact-schemas-data-hand-off-declarations).

## Modules

### `App.pkl` — Canonical Template

The single entry point for all new native app contracts. Amend this module and declare:

- App metadata: `name`, `displayName`, `connector`, `icon`
- Marketplace metadata: `shortDescription`, `longDescription`, `tags` (emitted top-level into `atlan.yaml`; consumed by the Atlan CLI on `atlan app register` and forwarded to the Global Marketplace App row); `docsUrl` (emitted top-level into `atlan.yaml`)
- Credential config: `credentialCommonFields`, `credentialAuthOptions`, `credentialConnectorType`
- Workflow config: `uiConfig` (form steps and fields using `Widgets.*` types)
- Pipeline: typed `pipeline` block (extract → parseQueries → popularity → lineage → publish)
- Deployment: `deploy` (typed `DeployConfig?` — app-level `dapr` / `overrides` plus a named `pools` map; pool keys must match `@task(pool="…")` strings) — see `Deployment.pkl`

All domain classes (`FieldSpec`, `AuthOption`, `UIConfig`, `UIRule`, `Entrypoint`, pipeline step classes, `Pool`, `DeployConfig`, `DaprComponents`, `KedaConfig`, `ResourceConfig`, `ErrorHandlingConfig`, `DependencyCondition`, `DAGNode`, etc.) are available directly in amending modules without additional imports. Deployment classes (`Pool`, `DeployConfig`, `DaprComponents`, `KedaConfig`, `KedaTemporalConfig`, `ResourceConfig`) are defined in `Deployment.pkl` and re-exported by `App.pkl`.

Widget types are re-exported via `Widgets` (itself re-exported from `App.pkl`). Amending modules use `Widgets.TextInput`, `Widgets.SqlTree`, etc.

`Connectors.pkl` must still be imported explicitly by consuming contracts.

### `Widgets.pkl` — Widget Catalog

Hand-picked set of workflow widget classes. Re-exported from `App.pkl` so consuming apps write:

```pkl
amends "@app-contract-toolkit/App.pkl"
// Widgets available as Widgets.TextInput, Widgets.SqlTree, etc.
```

See the [Widget Types](#widget-types) reference below.

### `Connectors.pkl` — Connector Registry

90+ connector type constants:

```pkl
import "@app-contract-toolkit/Connectors.pkl"
connector = Connectors.SNOWFLAKE  // { value = "snowflake"; category = "warehouse" }
```

### Legacy modules (reference only)

The following modules are frozen and are not used by new contracts. They remain resolvable so existing apps on `NativeApp.pkl` continue to work without changes during the v0.10.x transition period.

| Module | Status |
|---|---|
| `NativeApp.pkl` | Legacy — superseded by `App.pkl` |
| `NativeAppBundle.pkl` | Legacy — bundle support now built into `App.pkl` |
| `Config.pkl` | Legacy — widget catalog superseded by `Widgets.pkl` |
| `Credential.pkl` | Legacy — Argo-era credential module |
| `Renderers.pkl` | Legacy — Argo-era output renderers |
| `AgentConfig.pkl` | Legacy — agent config emission; opt-in via `agentConfig` block in `App.pkl` |

## Pipeline Block

The `pipeline` block replaces per-flag properties (`hasPublishStep`, `extraNodes["qi"]`, etc.) with a typed 5-step sequence. Each step is nullable to opt out.

```pkl
pipeline {
  // extract: default-on. Override name/displayName for non-extract apps.
  // e.g. for model-caster: pipeline.extract.name = "apply"
  extract = new ExtractStep {
    displayName = "Discover"
    errorHandling = new ErrorHandlingConfig { startToCloseTimeoutSeconds = 14400 }
  }

  // default-off steps — set to opt in:
  parseQueries = new ParseQueriesStep {
    vendorName = "snowflake"
    sqlKey = "QUERY_TEXT"
  }
  popularity = new PopularityStep {
    tenantId = "$.extract.outputs.tenant_id"
    parsedDataPrefix = "$.extract.outputs.qi_output_prefix"
    minedDataPrefix  = "$.extract.outputs.query_history_prefix"
    connectionCachePath = "$.extract.outputs.connection_cache_path"
    outputPrefix = "$.extract.outputs.popularity_output_prefix"
  }
  lineage = new LineageStep {
    connectorName = "snowflake"
    sqlUnquotedCase = "lower"
    ignoreAllCase = false
  }

  // publish: default-on. Set null to opt out (utility apps).
  publish = new PublishStep {
    executorEnabled = "{{load-to-atlan}}"
    includeInputFields = true
    errorHandling = new ErrorHandlingConfig { startToCloseTimeoutSeconds = 14400 }
    // connectionEntity: default "{{connection}}". Set null to omit
    // connection_entity AND disable connection creation (see below).
    connectionEntity = null
    // transformedNonDataPrefix: default null (arg omitted). Set it when the
    // extract workflow emits non-data payloads (see below).
    transformedNonDataPrefix = "$.extract.outputs.transformed_nondata_prefix"
  }
}
```

Dependencies between pipeline steps are **auto-wired** based on position — you do not write `dependsOn` for pipeline steps. Use `extraNodes` for custom nodes outside the typed pipeline.

Every node ships with a default `errorHandling.startToCloseTimeoutSeconds`: **1 day (86400s)** for every node, except `publish` and `lineage-publish` at **3 days (259200s)** (both write to Atlas and can be heavy on large tenants). This keeps non-trivial extract/lineage/publish work from silently inheriting Automation Engine's tight 2h workflow default. The `errorHandling` overrides shown above win over the default; set `errorHandling = null` on a node to fall back to AE's own default.

`PublishStep.connectionEntity` (also settable directly on `PublishNode`) controls the publish node's `connection_entity` arg — the full connection entity JSON used for connection creation. It defaults to the `"{{connection}}"` form placeholder. Setting it to `null` omits `connection_entity` from the generated args entirely, and because the field is **linked** to `connection_creation_enabled` (which defaults to `connectionEntity != null`), a `null` entity also disables connection creation — publish then targets an already-existing connection and creates nothing. Override `connectionCreationEnabled` explicitly on the node to disable creation even when an entity is present.

`PublishStep.transformedNonDataPrefix` (also settable directly on `PublishNode`) controls the publish node's optional `transformed_nondata_prefix` arg — the object-store prefix holding the run's transformed **non-data** payloads, alongside the asset rows publish reads from `transformed_data_prefix`. It defaults to `null`, which omits the arg entirely, so apps that do not set it generate byte-identical manifests. Set it — normally to an output reference such as `"$.extract.outputs.transformed_nondata_prefix"`, though a literal prefix or form placeholder is passed through verbatim — only when the upstream node actually emits non-data payloads. It leaves `transformed_data_prefix` and every other publish arg untouched.

The toolkit can append a run-level notification node when an app opts in:

```pkl
notifications = true
```

When enabled, the generated manifest includes `notifications`, a
`NotificationNode` that dispatches `NotificationWorkflow` in `notification-app`.
It depends on the reserved run-level `workflow_complete` tag, so Automation
Engine runs it once after the workflow run reaches any terminal state (success
or failure) and resolves the payload from `$.workflow.*` and `$.failure.*`
context (carrying the real status). The notification app then fans the alert out
to the tenant's enabled integrations and decides delivery per integration
(`failureOnly`: failure-only vs. all runs).

To replace the generated node, define `extraNodes["notifications"]`.

**Mapping from old API:**

| Old | New |
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

`extraNodes` is still available as an escape hatch for nodes that fall outside the canonical pipeline (e.g. custom fan-in steps).

## Pools Map

The `pools` map declares named worker pools. It is **fully optional and explicit-overrides-only** — an empty map (the default) emits nothing. Declare named pools to configure per-pool KEDA, resources, or env.

The pool key must exactly match the string passed to `@task(pool="…")` in Python. Order matters: the first key in insertion order drives the synthesised top-level `deploy:` block — put the primary always-on pool first.

```pkl
deploy = new DeployConfig {
  pools {
    ["hot"] = new Pool {
      keda {
        minReplicaCount = 1        // always-on
        cooldownPeriod = 300       // lenient cooldown — don't kill warm workers between bursts
        temporal { targetQueueSize = 10 }
      }
    }
    ["cold"] = new Pool {
      keda {
        minReplicaCount = 0        // scale-to-zero
        cooldownPeriod = 30        // aggressive cooldown — reclaim resources fast
      }
    }
  }
}
```

For the single-pool case, use the key `"default"`. See `examples/pools/` for a full two-pool example.

> **Migrating from v0.16.x?** The old `deploy { keda/resources/env }` flat fields and `deployOverrides` escape hatch are removed. See `docs/reference.md` § Migration Notes → v0.17.0.

## Multi-Entrypoint Bundle

Multi-entrypoint apps set `entrypoints` at the root `app.pkl` and keep per-entrypoint contracts as separate files that each `amend App.pkl`.

All entrypoints are always routable via `?entrypoint=`. Set `packageId` on entrypoints that should appear as user-facing marketplace cards; entrypoints without a `packageId` are routable but invisible in the marketplace (e.g. a lineage workflow invoked downstream by a DAG node rather than directly by a user).

For multi-entrypoint apps, `packageId` also pins the stable card ID (e.g. `"@atlan/qlik-sense"`) so legacy Argo workflows that reference the app by their old `app_id` continue to resolve correctly, and existing connection references in the UI do not go blank after migration.

```pkl
// app.pkl
amends "@app-contract-toolkit/App.pkl"
import "crawler.pkl" as Crawler
import "miner.pkl"   as Miner

name = "teradata"
displayName = "Teradata"
icon = "https://assets.atlan.com/assets/Teradata.svg"
hasCredentialConfig = false

entrypoints {
  new Entrypoint {
    name = "crawler"
    displayName = "Teradata Assets"
    source = "teradata"
    sourceCategory = "database"
    contract = Crawler
  }
  new Entrypoint {
    name = "miner"
    displayName = "Teradata Miner"
    source = "teradata"
    sourceCategory = "database"
    contract = Miner
  }
}
```

Output layout:

```
atlan.yaml                                   # bundle-level (entrypoints listed)
app.yaml
app/generated/
├── atlan-connectors-teradata.json           # hoisted from crawler, deduped
├── crawler/
│   ├── __init__.py
│   ├── teradata-crawler.json
│   ├── manifest.json
│   └── _input.py
└── miner/
    ├── __init__.py
    ├── teradata-miner.json
    ├── manifest.json
    └── _input.py
```

**Shared credentials:** Set `connectorConfigName = "atlan-connectors-teradata"` in each entrypoint contract. The bundle hoists the credential config to `app/generated/` root and deduplicates by filename. Duplicate names with different content fail generation.

See [`examples/bundle/`](examples/bundle/) for a runnable example.

## Widget Types

Used inside `uiConfig.tasks` — reference them as `Widgets.*`:

### Text & Input

| Class | Widget | Python Type |
|---|---|---|
| `Widgets.TextInput` | `input` | `str` |
| `Widgets.TextBoxInput` | `TextInput` | `str` |
| `Widgets.PasswordInput` | `password` | `str` |
| `Widgets.NumericInput` | `inputNumber` | `int` |
| `Widgets.InputRepeater` | `inputRepeater` | `list[str]` |
| `Widgets.Repeater` | `Repeater` | `list[dict]` |

`TextInput` and `TextBoxInput` support an optional `validation` block for opt-in
JSON or regex validation (`new { type = "json"; formatOnBlur = true }` or
`new { type = "regex"; pattern = "^[a-z0-9_]+$" }`). It renders into `ui.validation`
and is UI-only — separate from `validationRules`. See `docs/reference.md` and the
[`full`](examples/full/) example.

### Selection

| Class | Widget | Python Type |
|---|---|---|
| `Widgets.Radio` | `radio` | `str` |
| `Widgets.DropDown` | `select` | `str` or `list[str]` |
| `Widgets.TagsInput` | `select` (mode=tags) | `list[str]` |
| `Widgets.BooleanInput` | `boolean` | `bool` |
| `Widgets.Switcher` | `switcher` | `bool` |

### Connection & Credential

| Class | Widget | Python Type |
|---|---|---|
| `Widgets.ConnectionCreator` | `connection` | `Connection \| None` |
| `Widgets.ConnectionSelector` | `connectionSelector` | `str` |
| `Widgets.ConnectionRefInput` | `connectionSelector` | `ConnectionRef \| None` |
| `Widgets.CredentialInput` | `credential` | `str` |
| `Widgets.APITokenSelector` | `apiTokenSelect` | `str` |

### Trees & Filters

| Class | Widget | Python Type |
|---|---|---|
| `Widgets.SqlTree` | `sqltree` | `dict[str, str]` |
| `Widgets.APITree` | `apitree` | `dict[str, Any]` |
| `Widgets.ApiTreeSelect` | `apiTreeSelect` | `dict[str, Any]` |
| `Widgets.DsnTreeMap` | `dsnTreeMap` | `dict[str, Any]` |
| `Widgets.GlossarySelector` | `GlossarySelector` | `str` |

### Complex & Utility

| Class | Widget | Python Type |
|---|---|---|
| `Widgets.NestedInput` | `nested` | `dict[str, Any]` |
| `Widgets.InfoBanner` | `infoBanner` | omitted |
| `Widgets.ConditionalInput` | configurable | `str` or `dict` |
| `Widgets.AgentSelector` | `agent` | `dict[str, Any]` |
| `Widgets.CloudProvider` | `CloudProvider` | `str` |
| `Widgets.CustomWidget` | `<widgetName>` | `str` |
| `Widgets.Sage` / `Widgets.SageV2` | `sage`/`sageV2` | `str` |

## Field Lifecycle — Deprecating and Sunsetting Fields

Every `UIElement` carries `lifecycle` and `lifecycleMessage` properties that
support backwards-compatible field retirement:

| Property | Type | Default | Description |
|---|---|---|---|
| `lifecycle` | `"active"\|"deprecated"\|"sunset"` | `"active"` | Lifecycle state of this field. |
| `lifecycleMessage` | `String?` | `null` | Optional human-readable note. Ignored when `lifecycle = "active"`. |

**Lifecycle states and their generated Python:**

| State | Python output | Meaning |
|---|---|---|
| `active` | `field: T = <default>` | In active use. |
| `deprecated` | `field: T = Field(default=..., deprecated=True)` | Accepted but consumers should migrate away. Pydantic v2 emits a `DeprecationWarning` when set. |
| `sunset` | `field: T = Field(default=..., deprecated=True, json_schema_extra={"x-lifecycle": "sunset"})` | Retained only for backwards-compatibility; no longer consumed by the app. |

**Backwards-compatibility rules (enforced by the `B005`/`B006` conformance gate):**

- A field's lifecycle may only _advance_ (`active → deprecated → sunset`).
- A field is **never removed** and its **type never changes** — these are breaking
  contract changes.
- To "rename" a field: deprecate/sunset the old field and add a new field with the
  new name and a default value.

```pkl
// Mark a field as deprecated — still accepted, consumers should migrate
["legacy_timeout"] = new NumericInput {
  title = "Legacy timeout"
  default = 60
  lifecycle = "deprecated"
  lifecycleMessage = "Use the standard pipeline timeout instead."
}

// Mark a field as sunset — retained for serialization compatibility only
["old_batch_mode"] = new BooleanInput {
  title = "Old batch mode"
  default = false
  lifecycle = "sunset"
}
```

After changing field lifecycle in `app.pkl`, run
`uvx atlan-application-sdk-conformance==<version> gen-contract-ledger` in the app
repo and commit the updated `contract_schema.lock.json` in the same PR. Use the
version-pinned `uvx` form, not a bare `uv run`: CI checks the ledger with an
unpinned `uvx` install (always the latest release) while `uv run` resolves the
app's *locked* conformance dev dependency, and the generator's output depends on
its own version — so regenerating with an older locked version can rewrite the
ledger byte-identically and leave a B006 finding standing with no diff to commit
(FND-607). Any B005/B006 finding names the exact command to run.

## App Repo Structure

```
your-app/
├── atlan.yaml                          # Generated — DO NOT EDIT MANUALLY
├── app.yaml                            # Generated — DO NOT EDIT MANUALLY
├── .gitignore                          # Must ignore app/generated/frontend/static
├── contract/
│   ├── app.pkl                          # Single source of truth
│   ├── PklProject                       # Package dependency declaration
│   └── PklProject.deps.json             # Resolved package lock
├── app/
│   ├── generated/                       # ATLAN_CONTRACT_GENERATED_DIR
│   │   ├── __init__.py
│   │   ├── _input.py
│   │   ├── {name}.json
│   │   ├── atlan-connectors-{name}.json
│   │   ├── manifest.json
│   │   └── frontend/static/             # App playground dist; ignored, not committed
│   └── ...
└── main.py
```

## SDK Integration

The SDK v3 handler serves files from `ATLAN_CONTRACT_GENERATED_DIR` (default: `app/generated`):

- `GET /workflows/v1/configmap/{id}` → reads `{id}.json`
- `GET /manifest` → reads `manifest.json`
- `GET /workflows/v1/configmaps` → lists available configmap IDs

### Recommended poe task

```toml
[tool.poe.tasks]
generate-contract.shell = """
  cd contract && pkl project resolve && cd ..
  pkl eval -m . contract/app.pkl
"""
```

### App playground

For local UI validation in consuming app repos:

```bash
git check-ignore app/generated/frontend/static
npx @atlanhq/app-playground@3.1.0 install-to app/generated/frontend/static
atlan app run -p .
```

`atlan app run -p .` serves the app and playground UI on port `8000`. Run after contract generation.

## PKL Reserved Keywords in Dynamic Objects

When using `Dynamic` objects (e.g., inside `AgentSelector.agentConfigEntries`), some JSON keys collide with PKL reserved keywords:

| Keyword | Workaround |
|---|---|
| `hidden` | Backtick escaping: `` `hidden` = true `` |
| `default` | Use `Mapping`: `["default"] = ""` — backtick escaping does **not** work for `default` in Dynamic |

## Testing Changes

After modifying any PKL module, regenerate all examples and verify no breakage:

```bash
./scripts/regenerate-all.sh            # regenerates every example under examples/
./scripts/check-invariants.sh          # invariant checks on generated output
pkl test tests/*.pkl                   # Pkl behavior tests
python scripts/test-sdk-import.py      # imports every generated _input.py against SDK deps
```

Every PR that touches `src/` must pass generation for all examples.

## Documentation

Docs live in two places — both must be updated together when changing PKL modules:

| File | Content |
|---|---|
| `README.md` | Quick start, overview, widget summary tables |
| `docs/reference.md` | Detailed reference (classes, properties, examples) |

## Releasing

Releases are **automatic** — no manual trigger required.

1. Merge any PR that touches `contract-toolkit/` files. The Release Contract Toolkit workflow fires automatically, computes the next semver from unreleased conventional commits via `git-cliff`, and opens (or updates) a `bump-version-contract-toolkit` PR.
2. Review and merge the release PR.
3. The Publish Contract Toolkit workflow fires on merge: bumps `PklProject`, publishes the package, and tags `contract-toolkit-v<version>`.

## Requirements

- [PKL](https://pkl-lang.org/) >= 0.25.1 (`brew install pkl` on macOS)
- Python 3 for invariant checks and SDK import validation
- `ruff` or `uvx` for formatting generated `_input.py` files during `./scripts/regenerate-all.sh`
- Application SDK import dependencies for `python scripts/test-sdk-import.py`
- Atlan CLI for consuming app workflows (`atlan app contract validate`, `atlan app contract generate`, `atlan app run -p .`)
- Node.js with `npm` / `npx` only when installing the app playground
