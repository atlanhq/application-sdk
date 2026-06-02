# `ae-workflow` composite action

Register a connector app's Automation Engine workflow **straight from its
manifest** (`app/generated/manifest.json`, generated from `contract/app.pkl`).
The DAG topology, queues, `parsing_mode`, timeouts, and output wiring all come
from the manifest, so apps never carry a hand-copied DAG that can drift.

One implementation for every connector — replaces the per-app
`.github/scripts/ae-workflow.sh` copies (sigma, mssql, mysql, saphana, …).

## Usage

In a connector app's deploy workflow, after checkout + `atlan app deploy`:

```yaml
- name: Register AE workflow
  id: ae
  uses: atlanhq/application-sdk/.github/actions/ae-workflow@<ref>
  with:
    ae-url:          "https://${{ steps.cfg.outputs.tenant }}/automation"
    token:           ${{ secrets.ATLAN_API_KEY }}
    name:            "Sigma Extract - ${{ steps.cfg.outputs.connection_name }}"
    deployment:      ${{ inputs.deployment }}
    cred-guid:       ${{ steps.cfg.outputs.credential_guid }}
    connection-qn:   ${{ steps.cfg.outputs.connection_qn }}
    connection-name: ${{ steps.cfg.outputs.connection_name }}
    connection-creation-enabled: ${{ steps.cfg.outputs.connection_creation_enabled }}
    # CI dry-run for publish side-effects:
    executor-enabled: "false"
    connection-cache-enabled: "false"
    connection-cache-via-app-enabled: "false"

- run: echo "slug=${{ steps.ae.outputs.slug }}"
```

Pin `@<ref>` to a tag or commit SHA.

## How values are filled

A single generic pass over the manifest — no per-app or node-id logic:

| Manifest token | Filled from |
|---|---|
| `{app_name}` | `name:` in `atlan.yaml` |
| `{deployment_name}` (in `task_queue`) | `deployment` input — so per-app queue flags are unnecessary |
| `{{credential-guid}}` | `cred-guid` |
| `{{connection}}` | `{connection-name, connection-qn}` |
| `{{extraction-method}}` | `extraction-method` |
| `{{include-filter}}` / `{{exclude-filter}}` | `{}` (override via `values`) |
| any other `{{token}}` | `values` JSON file, else **dropped** (e.g. the UI-only `{{credential}}`) |

`node_type` and `app_task_queue` are derived for each child-workflow node.
Publish-side flags override existing keys on the node whose
`workflow_type == "PublishWorkflow"`.

## Requirements on the connector

- `app/generated/manifest.json` present in the checkout (path overridable via `manifest`).
- `atlan.yaml` with a `name:` field (path overridable via `atlan-yaml`).
- Per-deploy values that aren't manifest tokens (e.g. `exclude_workbook_regex`,
  publish flags) should be tokenized in `contract/app.pkl` so they ship in the
  manifest; until then, pass them via `values` / the dedicated inputs.
