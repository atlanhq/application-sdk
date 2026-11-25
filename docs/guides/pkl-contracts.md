# PKL Contracts

Every Atlan app ships a machine-readable contract — `contract/app.pkl` — that drives the entire deployment lifecycle: the setup form users fill in, the credential form, the Automation Engine DAG, the typed Python `Input` class your workflow receives, and the marketplace manifest.

One file, `app.pkl`, generates everything. This guide is the short path through it. The full authoring reference lives with the toolkit:

- [Toolkit README](https://github.com/atlanhq/application-sdk/blob/main/contract-toolkit/README.md) — quick start, outputs, widget table, curated examples
- [Field reference](https://github.com/atlanhq/application-sdk/blob/main/contract-toolkit/docs/reference.md) — every property, its default and what it renders

## How It Works

Contracts amend the toolkit's `App.pkl`. One `pkl eval`, run from the app repo root, writes every output at its final path:

```
contract/app.pkl
    │
    └── pkl eval --project-dir contract -m . contract/app.pkl
            ├── atlan.yaml                                  (marketplace manifest, repo root)
            ├── app.yaml                                    (SDR CI shim, repo root)
            ├── app/generated/{name}.json                   (setup form)
            ├── app/generated/atlan-connectors-{name}.json  (credential form)
            ├── app/generated/manifest.json                 (AE DAG)
            └── app/generated/_input.py                     (typed Input class)
```

The SDK's handler auto-serves these files from `app/generated/` (override with `ATLAN_CONTRACT_GENERATED_DIR`):

| Generated File | Served At |
|---|---|
| `{name}.json` | `GET /workflows/v1/configmap/{name}` |
| `atlan-connectors-{name}.json` | `GET /workflows/v1/configmap/atlan-connectors-{name}` |
| `manifest.json` | `GET /workflows/v1/manifest` |

No handler overrides needed. Commit the generated files; do not edit them by hand.

## Prerequisites

Use the **pinned** `pkl` — the one CI renders contracts with — not whatever
`brew` last shipped:

```bash
python -m application_sdk.dev.pkl print-version   # the version CI uses
PKL="$(python -m application_sdk.dev.pkl path)"   # download + cache that build
"$PKL" --version
```

The binary is cached under `~/.cache/atlan-sdk/pkl/<version>/`. Each download
attempt has a 60-second socket timeout. A 5xx response or a dropped connection
is retried up to 2 times (3 attempts in total). A 404 means the pin has no
asset for your platform, so it fails after the first attempt. A cached binary
is used only if it runs and reports the pinned version. Otherwise it is
downloaded again.

`pkl` is a language, not just a renderer, so versions disagree about what your
contract *means*: backslash line-continuations inside a multi-line string are
valid from 0.28 on and a hard `Invalid character escape sequence` before it. A
clean render on a different version is therefore not evidence the
`Generated Artifact Freshness` gate will pass — which is the whole reason that
gate exists (FND-1864). The pin lives in one place,
`application_sdk/pkl_version.py`.

Already have a `pkl` you like? Keep it, but check it:

```bash
python -m application_sdk.dev.pkl check   # exit 1 if it has drifted from CI
```

## Step 1 — Declare the Toolkit Dependency

Create `contract/PklProject`:

```
amends "pkl:Project"

dependencies {
  ["app-contract-toolkit"] {
    uri = "package://atlanhq.github.io/application-sdk/contracts/app-contract-toolkit@<LATEST_VERSION>"
  }
}
```

`<LATEST_VERSION>` is the `version` in
[`contract-toolkit/src/PklProject`](https://github.com/atlanhq/application-sdk/blob/main/contract-toolkit/src/PklProject)
on `main` (each release is also tagged `contract-toolkit-v<version>`).

Resolve and lock it from the repo root:

```bash
pkl project resolve contract
# Creates contract/PklProject.deps.json — commit this file
```

## Step 2 — Write `app.pkl`

This is the toolkit's own `minimal` example,
[`contract-toolkit/examples/minimal/app.pkl`](https://github.com/atlanhq/application-sdk/blob/main/contract-toolkit/examples/minimal/app.pkl),
which toolkit CI evaluates on every change:

```pkl
/// Minimal example — demonstrates the smallest possible App.pkl contract.
///
/// Every field not set here takes its documented default. No credentials,
/// no connector, opted-out of publish in the pipeline. Use this as the
/// starting point when scaffolding a new app contract.
amends "../../src/App.pkl"

name = "minimal"
displayName = "Minimal Example"
icon = "https://assets.atlan.com/assets/fullscreen-exit.svg"
hasCredentialConfig = false

pipeline {
  publish = null
}

uiConfig = new UIConfig {
  tasks {
    ["Configuration"] {
      inputs {
        ["target"] = new TextInput {
          title = "Target"
          helpText = "What to process."
          placeholderText = "my-target"
        }
      }
    }
  }
}
```

In your app, replace the first `amends` line (a path inside the toolkit's
source tree) with the package import:

```pkl
amends "@app-contract-toolkit/App.pkl"
```

Everything else comes from the toolkit: set only what differs from the
documented defaults. Do not import `Config.pkl`, `Credential.pkl` or
`NativeApp.pkl` — they are legacy modules, and conformance flags contracts
that use them. Widgets (`TextInput`, `SqlTree`, `CredentialInput`, `SageV2`, …)
are re-exported from `App.pkl` and used unqualified.

For a connector with credentials and a schema filter, start from the
[README quick start](https://github.com/atlanhq/application-sdk/blob/main/contract-toolkit/README.md#quick-start),
then pick the closest [example](#examples).

## Step 3 — Add the Generate Task

The SDK does not ship a built-in `generate` poe task — you add it to your
app's `pyproject.toml`. Use exactly this wiring; it is what the
`Generated Artifact Freshness` gate runs, and any other command produces output
the gate reports as stale:

```toml
[tool.poe.tasks]
generate.shell = """
  PKL="$(python -m application_sdk.dev.pkl path)"
  "$PKL" eval --project-dir contract -m . contract/app.pkl
  uvx ruff check --fix --quiet --force-exclude app/generated/*.py
  uvx ruff format --force-exclude app/generated/*.py
"""
```

Then run:

```bash
uv run poe generate
```

Run it from the repo root. `--project-dir contract` is required: pkl looks
for `PklProject` in the current directory, not next to the module. Do not
move `_input.py` out of `app/generated/` afterwards.

## Generated Python Is Lint-Clean

The toolkit lays out every generated `.py` file exactly as ruff does with
default settings, and toolkit CI fails if ruff would change one. Do **not**
exclude `app/generated/_input.py` from ruff, pyright or pre-commit — the
generated input model is meant to be type-checked with the rest of your app.

## The DAG — How Extraction Flows

By default, `manifest.json` describes a two-node DAG:

```
extract → publish
```

The `extract` node runs your app's main workflow. The `publish` node runs the
Publish App on the output of `extract`. Drop it with `pipeline { publish = null }`
(as the minimal example does) when your app publishes nothing.

### What Publish Reads from Extract

The generated `publish` node reads these keys from the extract workflow's
output, via `$.extract.outputs.<key>`:

- `connection_qualified_name`
- `transformed_data_prefix`
- `publish_state_prefix`
- `current_state_prefix`

Your workflow's output must carry every key the DAG references. Build it on
`PublishInputMixin`, which derives the two state prefixes from
`connection_qualified_name`:

```python
from application_sdk.contracts import Output, PublishInputMixin


class MyExtractionOutput(Output, PublishInputMixin):
    pass


return MyExtractionOutput(
    connection_qualified_name=connection_qualified_name,
    transformed_data_prefix=transformed_data_prefix,
)
```

To publish from a different node, override `publish` in `extraNodes` and set
its `upstream`, as below.

### Adding an Intermediate Node

```pkl
extraNodes {
  ["lineage"] = new DAGNode {
    displayName = "Extract Lineage"
    workflowType = "LineageExtractionWorkflow"
    taskQueue = "atlan-my-connector-{deployment_name}"
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

### Zero-Out Instead of Delete (Cross-Connection Enrichers)

If your app enriches assets another connector owns — writing its own namespaced
attributes onto them rather than creating them — publish's default handling of an
asset that falls out of a run is wrong for you. It issues a hard Atlas DELETE,
destroying a still-live asset along with its downstream lineage and its
user-curated tags and descriptions.

Declare a `ZeroOutSpec` so publish clears just your attributes instead:

```pkl
pipeline {
  publish {
    zeroOutConfig = new ZeroOutSpec {
      // Types your app owns outright — these keep being hard-deleted.
      exclude { "Process"; "ColumnProcess" }
      // attribute -> zero value, written as-is inside entity.attributes.
      attrs {
        ["myConnectorNodeStatus"] = null
        ["myConnectorProjectId"] = null
      }
      // Same shape, for entity-root fields.
      rootAttrs { ["classifications"] = new Listing {} }
    }
  }
}
```

Also settable directly on `PublishNode` when you override the node via
`extraNodes["publish"]`. Leave it unset and the arg is omitted, which is how
zero-out stays off — do not build an empty spec to disable it.

Do **not** hand-write this as a raw JSON string in Pkl: a string is spliced
unparsed, so a typo yields a broken manifest with no error at generation time,
and it needs a post-generate merge script that CI's single hook may not run. The
typed spec type-checks and needs no post-processing. See
[the toolkit reference](https://github.com/atlanhq/application-sdk/blob/main/contract-toolkit/docs/reference.md)
for the full semantics, including why `attrs` is a value map rather than a list
of names.


## Widgets

Widgets are declared under `uiConfig.tasks[<step>].inputs`. The
[README widget table](https://github.com/atlanhq/application-sdk/blob/main/contract-toolkit/README.md)
lists each widget, the UI component it renders and the Python type it adds to
`_input.py`; the [field reference](https://github.com/atlanhq/application-sdk/blob/main/contract-toolkit/docs/reference.md)
covers every widget property.

If your handler overrides `Handler.warmup`, set `warmup = true` on the
`SageV2` widget so the setup UI runs the warmup flow before the warmup-tier
checks. Leave it unset otherwise; the flag must match the handler. See
[SageV2 warmup](https://github.com/atlanhq/application-sdk/blob/main/contract-toolkit/docs/reference.md#sagev2-warmup).

## Bumping the Toolkit Version

When a new toolkit version ships:

1. Update the version in `contract/PklProject`
2. Re-resolve: `pkl project resolve contract`
3. Regenerate: `uv run poe generate`
4. Diff the generated files — review changes before committing

Find the latest version in
[`contract-toolkit/src/PklProject`](https://github.com/atlanhq/application-sdk/blob/main/contract-toolkit/src/PklProject),
or list the release tags:

```bash
git ls-remote --tags https://github.com/atlanhq/application-sdk 'contract-toolkit-v*'
```

Changes per version are in the toolkit
[CHANGELOG](https://github.com/atlanhq/application-sdk/blob/main/contract-toolkit/CHANGELOG.md).

## Verification

Before pushing, reproduce what the freshness gate checks:

```bash
python -m application_sdk.dev.pkl check   # local pkl matches CI's pin
uv run poe generate                        # regenerate from scratch
git status --porcelain                     # empty = generated files are fresh
```

## Examples

Every example under
[`contract-toolkit/examples/`](https://github.com/atlanhq/application-sdk/tree/main/contract-toolkit/examples)
is an executable contract, evaluated by toolkit CI, with its generated output
committed next to it. Start with:

- [`minimal`](https://github.com/atlanhq/application-sdk/tree/main/contract-toolkit/examples/minimal) — smallest possible contract; all defaults
- [`tree-filters`](https://github.com/atlanhq/application-sdk/tree/main/contract-toolkit/examples/tree-filters) — include/exclude filter tree pickers
- [`full`](https://github.com/atlanhq/application-sdk/tree/main/contract-toolkit/examples/full) — every overridable feature: JDBC URL auth, all pipeline steps, diverse widgets, UI rules, `extraNodes`
- [`fanin`](https://github.com/atlanhq/application-sdk/tree/main/contract-toolkit/examples/fanin) — multi-parent fan-in via `dependsOn`
- [`publish-controls`](https://github.com/atlanhq/application-sdk/tree/main/contract-toolkit/examples/publish-controls) — publish toggles, `errorHandling`, `zeroOutConfig`

The [README](https://github.com/atlanhq/application-sdk/blob/main/contract-toolkit/README.md#curated-examples)
describes the rest.

## See Also

- [Contract Toolkit](contract-toolkit.md) — where the toolkit lives and how it is released
- [Deployment Guide](deployment.md) — `atlan.yaml` and Kubernetes topology
- [Multi-App Coordination](multi-app-coordination.md) — complex DAGs across multiple apps
- `make-contract` skill — interactive PKL contract creation and migration
