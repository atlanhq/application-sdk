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
- [`examples/deploy/`](examples/deploy/) — typed `deploy` block: KEDA, Dapr, resources, env, `deployOverrides`.
- [`examples/connection-ref/`](examples/connection-ref/) — `ConnectionRefInput` widget, `pipeline.publish = null`.
- [`examples/publish-controls/`](examples/publish-controls/) — publish toggles, `includeInputFields`, `errorHandling`.
- [`examples/fanin/`](examples/fanin/) — multi-parent fan-in via `dependsOn`, explicit `DependencyCondition`.

## What Gets Generated

### `atlan.yaml` (repo root)

Marketplace deployment manifest. Contains app identity, entrypoints list, and the typed `deploy` block (KEDA, Dapr, resources, env, and any `deployOverrides`). Emitted with a DO-NOT-EDIT header — edit `contract/app.pkl` instead.

### `app.yaml` (repo root)

Three-line SDR CI shim (`app_name`, `app_image`, `app_port`). Derived entirely from `name`. Emitted with a DO-NOT-EDIT header.

### Workflow Config (`app/generated/{name}.json`)

UI form schema fetched by the frontend during "Setup Workflow". Contains `properties` (form fields with widget types), `steps` (wizard progression), and `anyOf` (conditional visibility rules).

### Credential Config (`app/generated/atlan-connectors-{name}.json`)

Auth form schema. Contains common fields (host, port), auth-type radio, and nested input panes per auth type. Auth panes are hidden by default and revealed via `anyOf` rules when the corresponding auth type is selected.

Credentials are shared — a user creates credentials once and reuses them across crawler and miner workflows.

### Manifest (`app/generated/manifest.json`)

Automation Engine DAG template. Auto-derived from the declared workflow params and the typed `pipeline` block. Heracles substitutes `{{param}}` placeholders with form values before sending to AE.

Default pipeline: `extract → publish`. Opt out of publish with `pipeline.publish = null`. Add parseQueries, popularity, or lineage steps by setting the corresponding `pipeline.*` field.

### `_input.py` (`app/generated/_input.py`)

Typed Python `AppInputContract` dataclass. SDK-owned fields inherited from `ExtractionInput`; app-specific fields generated from `uiConfig.properties`.

## Modules

### `App.pkl` — Canonical Template

The single entry point for all new native app contracts. Amend this module and declare:

- App metadata: `name`, `displayName`, `connector`, `icon`
- Credential config: `credentialCommonFields`, `credentialAuthOptions`, `credentialConnectorType`
- Workflow config: `uiConfig` (form steps and fields using `Widgets.*` types)
- Pipeline: typed `pipeline` block (extract → parseQueries → popularity → lineage → publish)
- Deployment: typed `deploy` block (KEDA, Dapr, resources, env)

All domain classes (`FieldSpec`, `AuthOption`, `UIConfig`, `UIRule`, `Entrypoint`, pipeline step classes, `DeployConfig`, `DaprComponents`, `KedaConfig`, `ResourceConfig`, `ErrorHandlingConfig`, `DependencyCondition`, `DAGNode`, etc.) are defined in `App.pkl` and available directly in amending modules without additional imports.

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
  }
}
```

Dependencies between pipeline steps are **auto-wired** based on position — you do not write `dependsOn` for pipeline steps. Use `extraNodes` for custom nodes outside the typed pipeline.

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

`extraNodes` is still available as an escape hatch for nodes that fall outside the canonical pipeline (e.g. custom fan-in or notification steps).

## Deploy Block

The typed `deploy` block replaces the legacy free-form mapping. The common 80% is typed; anything unmodeled goes in `deployOverrides` (deep-merged last).

```pkl
deploy {
  keda {
    enabled = true
    minReplicaCount = 1
    temporal { targetQueueSize = 10 }  // Note: must be set here, NOT on keda.targetQueueSize
  }

  dapr {
    objectstore = true
    secretstore = true
    // statestore, eventstore, subscription, configurationstore, lock default to false
  }

  resources = new ResourceConfig {
    requests { ["cpu"] = "500m"; ["memory"] = "1Gi" }
    limits   { ["cpu"] = "2";   ["memory"] = "4Gi" }
  }

  env {
    ["LOG_LEVEL"] = "INFO"
    ["MAX_WORKERS"] = "4"
  }
}

// Deep-merged on top of deploy section — for unmodeled marketplace keys:
deployOverrides {
  ["verticalPodAutoscaler"] = new Mapping {
    ["enabled"] = true
    ["updateMode"] = "Auto"
  }
}
```

## Multi-Entrypoint Bundle

Multi-entrypoint apps set `entrypoints` at the root `app.pkl` and keep per-entrypoint contracts as separate files that each `amend App.pkl`.

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
