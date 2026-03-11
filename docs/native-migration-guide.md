# Migrating a Connector App to Native Orchestration

This guide walks through every change required to migrate an existing Argo-based connector app (e.g. `atlan-redshift-app`) to the **native orchestration** style, where workflows are executed via Temporal through the Automation Engine instead of Argo Workflows.

---

## What Changes and Why

| Concern | Argo / Old Style | Native / New Style |
|---|---|---|
| **Workflow execution** | Argo `WorkflowTemplate` in K8s | Temporal via Automation Engine |
| **Credential storage** | Full credential in Argo params | Sensitive fields in Vault only; non-sensitive passed inline |
| **Form schemas** | Read from Elasticsearch / K8s ConfigMap | Served by the app itself via `/configmap/{id}` |
| **Preflight / auth test** | Heracles proxy with K8s ConfigMap lookup | Heracles proxy directly to app; no K8s read |
| **Workflow outputs** | Not used across steps | Returned by `run()` as dict; AE chains via JSONPath |
| **App service discovery** | Not applicable | Marketplace → ENV override → K8s DNS |

---

## Prerequisites

- App is built on `atlan-application-sdk` >= `75a48a93`
- App runs as a standalone FastAPI service (server + Temporal worker)
- App is registered in the Atlan Marketplace with `execution_mode: native`

---

## 1. Templates — Add Form Schema Files

Create two JSON files under `app/templates/`:

### `app/templates/atlan-connectors-{connector}.json`
Defines the **credential form** shown on the Config tab. Fields include connection parameters (`host`, `port`, auth type, credentials, deployment type).

Structure:
```json
{
  "config": {
    "properties": {
      "name":          { "type": "string", "ui": { "hidden": true } },
      "connector":     { "type": "string", "ui": { "hidden": true } },
      "connectorType": { "type": "string", "ui": { "hidden": true } },
      "host": {
        "type": "string", "required": true,
        "ui": { "label": "Host Name", "help": "…", "feedback": true }
      },
      "port": { "type": "number", "default": 5439, "required": true },
      "auth-type": {
        "type": "string",
        "enum": ["basic", "iam", "role"],
        "enumNames": ["Basic", "IAM User", "IAM Role"],
        "ui": { "widget": "radio", "label": "Authentication" }
      },
      "basic": {
        "type": "object",
        "properties": {
          "username": { "type": "string", "required": true },
          "password": { "type": "string", "required": true, "ui": { "widget": "password" } },
          "extra": { /* connector-specific fields: database, deployment_type, etc. */ }
        },
        "ui": { "widget": "nested", "hidden": true }
      }
      /* … other auth types … */
    },
    "anyOf": [
      { "properties": { "auth-type": { "const": "basic" } }, "required": ["basic"] }
    ]
  }
}
```

> **Note**: No `"id"` or `"name"` at the top level — this file is keyed by the `config_map_id` path param.

### `app/templates/workflow.json`
Defines the **workflow form** shown on the Schedule tab. Fields include connection picker, credential picker, include/exclude filters, extraction method, preflight checks.

Structure:
```json
{
  "id": "{connector-name}",
  "name": "{Connector Display Name}",
  "logo": "https://…",
  "config": {
    "properties": {
      "connection": {
        "type": "string",
        "ui": { "widget": "connection", "label": "" }
      },
      "credential-guid": {
        "type": "string",
        "ui": { "widget": "credential", "credentialType": "atlan-connectors-{connector}" }
      },
      "include-filter": {
        "type": "object",
        "ui": { "widget": "sqltree", "sql": "show atlan schemas", "credential": "credential-guid" }
      },
      "preflight-check": {
        "type": "string",
        "ui": {
          "widget": "sage",
          "config": [
            { "id": 1, "name": "schemasCheck", "title": "Databases and schemas" },
            { "id": 2, "name": "tablesMetadataCheck", "title": "Tables metadata" }
          ]
        }
      }
      /* … other fields … */
    },
    "steps": [
      { "id": "credential", "title": "Credential", "properties": ["credential-guid"] },
      { "id": "connection", "title": "Connection", "properties": ["connection"] },
      { "id": "metadata", "title": "Metadata", "properties": ["include-filter", "preflight-check"] }
    ]
  }
}
```

---

## 2. Handler — Serve Templates via `get_configmap`

Override `get_configmap` in your handler class to read the correct template file based on `config_map_id`.

```python
# app/handlers/{connector}.py
import json
from pathlib import Path
from typing import Any, Dict

from application_sdk.handlers import HandlerInterface


class MyConnectorHandler(HandlerInterface):

    @staticmethod
    async def get_configmap(config_map_id: str) -> Dict[str, Any]:
        base = Path().cwd() / "app" / "templates"

        if config_map_id == "atlan-connectors-{connector}":
            path = base / "atlan-connectors-{connector}.json"
        else:
            path = base / "workflow.json"

        with open(path) as f:
            raw = json.load(f)

        return MyConnectorHandler._wrap_configmap(config_map_id, raw)
```

`_wrap_configmap` (provided by the SDK base class) wraps the raw JSON into the K8s ConfigMap shape that Heracles expects:

```python
# SDK implementation (no need to copy this):
@staticmethod
def _wrap_configmap(config_map_id: str, raw: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "kind": "ConfigMap",
        "apiVersion": "v1",
        "metadata": {"name": config_map_id},
        "data": {"config": json.dumps(raw.get("config", raw))},
    }
```

The response is further wrapped by the SDK server in `{ success, message, data: <ConfigMap> }`. Heracles unwraps this before returning to the frontend.

---

## 3. Activities — Slim Down `get_workflow_args`

The SDK base class now handles:
- State store lookup for `workflow_id`
- Connection normalization (AE entity object, flat dict, or JSON string → `{ connection_qualified_name, connection_name }`)
- Hybrid credential resolution (inline stripped config + Vault secrets)

### What you still need to override

Override only if you have **connector-specific argument reshaping**. For example, Redshift flattens a nested `metadata` key:

```python
# app/activities/metadata_extraction/{connector}.py
from application_sdk.activities.metadata_extraction.sql import BaseSQLMetadataExtractionActivities


class MyConnectorActivities(BaseSQLMetadataExtractionActivities):

    @activity.defn
    async def get_workflow_args(self, workflow_config: Dict[str, Any]) -> Dict[str, Any]:
        # Delegate all base logic to SDK
        workflow_args = await super().get_workflow_args(workflow_config)

        # Connector-specific: flatten metadata dict into top-level keys
        metadata = workflow_args.get("metadata") or {}
        for key, value in metadata.items():
            if key not in workflow_args:
                workflow_args[key] = value

        # Ensure AE-supplied credential flows through
        if "credential" not in workflow_args and "credential" in workflow_config:
            workflow_args["credential"] = workflow_config["credential"]

        return workflow_args
```

### What the SDK base handles automatically

**Connection normalization** — AE sends `connection` as a full entity object with `attributes`. The SDK normalizes it to:
```python
workflow_args["connection"] = {
    "connection_qualified_name": attrs.get("qualifiedName") or ...,
    "connection_name": attrs.get("name") or ...,
}
```

**Hybrid credential resolution** — The SDK detects which path to use:

| Condition | Path | What happens |
|---|---|---|
| `credential_guid` present **and** `credential` dict present | **Native (AE)** | Non-sensitive fields from `credential` dict + secrets fetched from Vault by GUID |
| `credential_guid` present, `credential` **not** present | **Argo** | Full credential fetched from Vault by GUID |
| Neither present | **Local dev** | Credential from state store (full inline) |

In production, the SDK automatically strips `password`, `username`, and any `extra` keys containing `"secret"`, `"private_key"`, `"passphrase"`, or `"password"` from the inline credential before merging — Vault provides those values.

### Remove the old `_set_state` override

If your app had a `_set_state` override that called `SecretStore.get_credentials()`, **remove it**. The SDK base now handles both paths. You only need the slim `get_workflow_args` override above.

**Before:**
```python
# DELETE THIS — now handled by SDK
async def _set_state(self, workflow_args):
    credentials = await SecretStore.get_credentials(workflow_args["credential_guid"])
    await self.sql_client.load(credentials)
    ...
```

---

## 4. Workflow — Return Output Paths from `run()`

The SDK's `BaseSQLMetadataExtractionWorkflow.run()` already returns the output paths dict. You don't need to compute them yourself.

### What the SDK returns automatically

```python
{
    "transformed_data_prefix": "{output_path_without_prefix}/transformed",
    "connection_qualified_name": "{connection.connection_qualified_name}",
    "publish_state_prefix": "persistent-artifacts/apps/atlan-publish-app/state/{connection_qn}/publish-state",
    "current_state_prefix": "argo-artifacts/{connection_qn}/current-state",
}
```

These are referenced by AE via JSONPath in the DAG manifest:
```json
{ "name": "transformed_data_prefix", "path": "$.extract.outputs.transformed_data_prefix" }
```

### What to change in your workflow class

Capture the return value from `super().run()` instead of discarding it:

```python
# app/workflows/metadata_extraction/{connector}.py
from application_sdk.workflows.metadata_extraction.sql import BaseSQLMetadataExtractionWorkflow
from typing import Any, Dict


class MyConnectorMetadataExtractionWorkflow(BaseSQLMetadataExtractionWorkflow):

    @workflow.run
    async def run(self, workflow_config: Dict[str, Any]) -> Dict[str, Any]:
        # Run all base extraction activities
        output_paths = await super().run(workflow_config)

        # Connector-specific post-processing (if any) goes here

        return output_paths  # Must be returned for AE JSONPath chaining
```

**Before (wrong — discards output):**
```python
async def run(self, workflow_config):
    await super().run(workflow_config)  # return value ignored
    # compute output paths manually...
```

---

## 5. App Manifest — Expose DAG Template

The app must expose a `GET /manifest` endpoint returning the DAG template. Heracles fetches this when submitting a workflow and substitutes all `{{param-name}}` placeholders with resolved form values before sending to AE.

**The SDK generates this manifest automatically.** `BaseSQLMetadataExtractionApplication.get_manifest()` produces a two-node DAG — `extract` (runs your Temporal workflow) → `publish` (runs the Publish App) — with all standard form parameters already wired via `{{placeholders}}` and the publish node wired to the extract node via JSONPath. For most connectors you do not need to do anything extra; the manifest is registered with the server automatically when you call `application.start(...)`.

The default DAG covers the full extraction + publish pipeline end-to-end:

```json
{
  "execution_mode": "automation-engine",
  "dag": {
    "extract": {
      "activity_name": "execute_workflow",
      "activity_display_name": "Extract {Connector} Metadata",
      "app_name": "automation-engine",
      "inputs": {
        "workflow_type": "{ConnectorMetadataExtractionWorkflow}",
        "task_queue": "{connector}-task-queue",
        "args": {
          "credential_guid": "{{credential-guid}}",
          "credential": "{{credential}}",
          "connection": "{{connection}}",
          "metadata": {
            "extraction-method": "{{extraction-method}}",
            "include-filter": "{{include-filter}}",
            "exclude-filter": "{{exclude-filter}}",
            "temp-table-regex": "{{temp-table-regex}}",
            "advanced-config": "{{advanced-config}}",
            "control-config-strategy": "{{control-config-strategy}}",
            "control-config": "{{control-config}}",
            "use-source-schema-filtering": "{{use-source-schema-filtering}}",
            "use-jdbc-internal-methods": "{{use-jdbc-internal-methods}}"
          }
        }
      }
    },
    "publish": {
      "activity_name": "execute_workflow",
      "activity_display_name": "Publish to Atlas",
      "app_name": "automation-engine",
      "inputs": {
        "workflow_type": "PublishWorkflow",
        "task_queue": "publish-task-queue",
        "args": {
          "connection_qualified_name": "$.extract.outputs.connection_qualified_name",
          "transformed_data_prefix": "$.extract.outputs.transformed_data_prefix",
          "publish_state_prefix": "$.extract.outputs.publish_state_prefix",
          "current_state_prefix": "$.extract.outputs.current_state_prefix",
          "connection_creation_enabled": true,
          "executor_enabled": true,
          "connection_entity": "{{connection}}"
        }
      },
      "depends_on": { "node_id": "extract" }
    }
  }
}
```

> **Parameter naming**: Placeholder names match the workflow form field names (kebab-case from `workflow.json`). Heracles substitutes `{{param-name}}` using values extracted from the submitted form.

### Overriding the manifest

If your connector needs a different DAG shape — extra nodes, additional parameters, a different publish strategy, or no publish step at all — override `get_manifest()` in your application class:

```python
# app/application.py  (or inline in main.py)
from application_sdk.application.metadata_extraction.sql import BaseSQLMetadataExtractionApplication

class RedshiftApplication(BaseSQLMetadataExtractionApplication):

    def get_manifest(self):
        base = super().get_manifest()
        # Add connector-specific args to the extract node
        base["dag"]["extract"]["inputs"]["args"]["extra_param"] = "{{extra-param}}"
        return base
```

Or return a fully custom manifest dict if the default shape doesn't fit at all.

---

## 6. Marketplace Registration

Go to **Global Marketplace** (`globalmarketplaceui.atlan.com/admin`) and either create a new app or edit the existing one.

**When creating the app**, the registration form will ask for the execution mode — select **`native`**. This is what tells Heracles to route all credential, configmap, and workflow requests to the app's own service instead of Kubernetes / Elasticsearch.

Once the app exists, create a new version pointing to your image and publish a release to the target tenant(s). The `app_service_url` is optional — if not provided, Heracles derives it automatically as:

```
http://{appName}.{appName}-app.svc.cluster.local:8000
```

For local development, set the env var instead:
```
APP_SERVICE_URL_{APPNAME_UPPER}=http://localhost:8000
# e.g. APP_SERVICE_URL_REDSHIFT=http://localhost:8000
```

---

## 7. Checklist

### Code changes

- [ ] `app/templates/atlan-connectors-{connector}.json` — credential form schema
- [ ] `app/templates/workflow.json` — workflow form schema
- [ ] `app/handlers/{connector}.py` — `get_configmap()` reads templates and calls `_wrap_configmap()`
- [ ] `app/activities/{connector}.py` — slim `get_workflow_args()` delegates to `super()`; old `_set_state` removed
- [ ] `app/workflows/{connector}.py` — `run()` captures and returns `output_paths = await super().run()`
- [ ] Manifest — SDK generates the default extract + publish DAG automatically; override `get_manifest()` only if you need a custom DAG shape

### Infrastructure

- [ ] App created/updated in Global Marketplace with `execution_mode: native` selected
- [ ] New marketplace version created with correct image tag
- [ ] New marketplace release published to target tenant(s)

### Validation

- [ ] Config tab loads credential form (GET `/configmaps/atlan-connectors-{connector}`)
- [ ] Test connection succeeds (POST `/credentials/test` → app `/auth` endpoint)
- [ ] Preflight checks pass (POST `/credentials/{guid}/sage` → app `/check` endpoint)
- [ ] Workflow tab loads form (GET `/configmaps/{connector}`)
- [ ] Full workflow run completes end-to-end and assets appear in Atlan

---

---

## Reference: Redshift App as Example

The `atlan-redshift-app` is the reference implementation for this migration. Diff summary:

| File | Change |
|---|---|
| `app/templates/atlan-connectors-redshift.json` | Added (credential form) |
| `app/templates/workflow.json` | Added (workflow form) |
| `app/handlers/redshift.py` | `get_configmap()` reads templates + `_wrap_configmap()` |
| `app/activities/metadata_extraction/redshift.py` | Slim `get_workflow_args()` only; `_set_state` deleted |
| `app/workflows/metadata_extraction/redshift.py` | `run()` returns `output_paths` from `super()` |
| `pyproject.toml` | SDK rev updated to include hybrid credential support |
