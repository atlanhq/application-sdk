---
name: migrate-to-native
description: "[v2-only] Migrate a v2 connector app from Argo to native orchestration. For v3 migrations, use /migrate-v3."
---

> **⚠ v2-only skill — NOT for `refactor-v3`.**
> This skill targets the Argo → native orchestration migration for v2 connectors.
> The classes, decorators, and directory layout referenced below (`BaseSQLMetadataExtractionApplication`, `@activity.defn`, `get_workflow_args`, `app/activities/metadata_extraction/`, etc.) **do not exist on the `refactor-v3` branch** — invoking this skill against a v3 codebase will fail.
> For v3 migrations, use `/migrate-v3` instead.

# Skill: Migrate App to Native Orchestration

You are helping migrate an Atlan connector app from Argo-based orchestration to **native orchestration** — where workflows run via Temporal through the Automation Engine instead of Argo Workflows.

## Context

Read the full migration guide before starting:
- **Migration guide**: https://linear.app/atlan-epd/document/migrating-a-connector-app-to-native-orchestration-f0065f5675a5
- **Sequence diagram** (full flow): https://linear.app/atlan-epd/document/native-orchestration-sequence-diagram-cc3683aa8554
- **Local copy**: `docs/native-migration-guide.md` in `application-sdk`

Read the reference implementation:
- `atlan-redshift-app` — the complete reference for all changes

The `atlan-application-sdk` base classes handle: connection normalization, hybrid credential resolution (inline + Vault), output path computation. Your job is to wire up the app-specific layer on top.

---

## Instructions

Work through each section below in order. After each section, verify the change compiles or parses correctly before moving on.

---

### Step 1 — Discover the app structure

Read these files to understand the current state:

1. `app/handlers/{connector}.py` — look for `get_configmap`, `test_auth`, credential handling
2. `app/activities/metadata_extraction/{connector}.py` — look for `get_workflow_args`, `_set_state`
3. `app/workflows/metadata_extraction/{connector}.py` — look for `run()` return value
4. `app/templates/` — check what template files exist (if any)
5. `pyproject.toml` — check current `atlan-application-sdk` rev/version

Identify:
- The connector name (used in file names and configmap IDs)
- Any connector-specific arg reshaping in `get_workflow_args`
- Whether `_set_state` is overridden
- Whether `run()` returns anything
- Whether template files already exist

---

### Step 2 — Update SDK dependency

In `pyproject.toml`, ensure `atlan-application-sdk` points to a commit that includes:
- `HandlerInterface._wrap_configmap()`
- Hybrid credential resolution in `BaseSQLMetadataExtractionActivities._set_state()`
- `BaseSQLMetadataExtractionWorkflow.run()` returning `Dict[str, Any]`

The minimum required commit is `75a48a934bf0b43751a63779879330e30f0028b8`.

If the rev is older, update it and run `uv lock` to regenerate the lockfile.

---

### Step 3 — Create credential form template

Create `app/templates/atlan-connectors-{connector}.json`.

This file defines the **Config tab** credential form. It must have:
- `"config"` at the top level (no `"id"` or `"name"`)
- Hidden fields: `name`, `connector`, `connectorType`
- Visible connection fields: `host`, `port`
- Auth type radio: `auth-type` with enum values matching the connector's auth methods
- Nested credential objects per auth type (e.g. `basic`, `iam`, `role`)
- `extra` nested object inside each auth type for connector-specific fields (database, deployment_type, etc.)
- `anyOf` array to require the correct nested object based on `auth-type`

Use `atlan-redshift-app/app/templates/atlan-connectors-redshift.json` as the reference structure.

---

### Step 4 — Create workflow form template

Create `app/templates/workflow.json`.

This file defines the **Schedule tab** workflow form. It must have:
- Top-level `"id"`: the connector name (e.g. `"redshift"`)
- Top-level `"name"`: display name
- Top-level `"logo"`: URL to connector logo
- `config.properties` with:
  - `connection` — widget `"connection"`
  - `credential-guid` — widget `"credential"`, `credentialType` = `"atlan-connectors-{connector}"`
  - `include-filter` / `exclude-filter` — widget `"sqltree"` with `"sql": "show atlan schemas"`
  - `preflight-check` — widget `"sage"` with connector-specific checks array
  - Any connector-specific params (extraction method, temp table regex, etc.)
- `config.steps` with at minimum: `credential`, `connection`, `metadata`

Use `atlan-redshift-app/app/templates/workflow.json` as the reference structure.

---

### Step 5 — Update the handler

In `app/handlers/{connector}.py`, add or replace `get_configmap`:

```python
@staticmethod
async def get_configmap(config_map_id: str) -> Dict[str, Any]:
    base = Path().cwd() / "app" / "templates"
    if config_map_id == "atlan-connectors-{connector}":
        path = base / "atlan-connectors-{connector}.json"
    else:
        path = base / "workflow.json"
    with open(path) as f:
        raw = json.load(f)
    return {ClassName}._wrap_configmap(config_map_id, raw)
```

Add `import json` and `from pathlib import Path` if not already present.

Remove any previous `get_configmap` implementation that read from Elasticsearch or K8s.

---

### Step 6 — Slim down activities

In `app/activities/metadata_extraction/{connector}.py`:

**Remove** any `_set_state` override — the SDK handles both native (inline + Vault) and Argo (full Vault fetch) paths automatically.

**Replace** the full `get_workflow_args` override with a slim version that:
1. Calls `await super().get_workflow_args(workflow_config)`
2. Handles only connector-specific reshaping (e.g. flattening a `metadata` dict)
3. Ensures `credential` is forwarded if present in `workflow_config` but not yet in `workflow_args`

Example:
```python
@activity.defn
async def get_workflow_args(self, workflow_config: Dict[str, Any]) -> Dict[str, Any]:
    workflow_args = await super().get_workflow_args(workflow_config)

    # Connector-specific: flatten metadata keys
    metadata = workflow_args.get("metadata") or {}
    for key, value in metadata.items():
        if key not in workflow_args:
            workflow_args[key] = value

    if "credential" not in workflow_args and "credential" in workflow_config:
        workflow_args["credential"] = workflow_config["credential"]

    return workflow_args
```

Remove all imports that are no longer used after this change (e.g. `SecretStore`, `get_workflow_id`, `DEPLOYMENT_NAME`, `build_output_path`, etc.).

---

### Step 7 — Update the workflow

In `app/workflows/metadata_extraction/{connector}.py`:

Change `await super().run(workflow_config)` to `output_paths = await super().run(workflow_config)` and return `output_paths`.

Remove any manual output path computation that was previously done in `run()` (the SDK now computes these from `workflow_args["output_path"]`, `workflow_args["output_prefix"]`, and `workflow_args["connection"]`).

Remove unused imports (`json`, `Path`, etc.) that are no longer needed.

---

### Step 8 — Check if the manifest needs customisation

The SDK generates the app manifest automatically via `BaseSQLMetadataExtractionApplication.get_manifest()`. This produces a two-node DAG — `extract` (runs the connector's Temporal workflow) followed by `publish` (runs the Publish App) — covering the full extraction + publish pipeline. **No manifest file needs to be created.**

Check whether the default manifest covers the connector's needs:
- If all workflow form parameters map to the standard set (`connection`, `credential-guid`, `credential`, `include-filter`, `exclude-filter`, `temp-table-regex`, `extraction-method`, `advanced-config`, etc.) → no change needed.
- If the connector has **additional form parameters** that need to reach the Temporal workflow → override `get_manifest()` to add them to `dag["extract"]["inputs"]["args"]`.
- If the connector needs a **completely different DAG** (e.g. no publish step, extra intermediate nodes) → return a fully custom dict from `get_manifest()`.

Example override for adding an extra parameter:
```python
class MyConnectorApplication(BaseSQLMetadataExtractionApplication):
    def get_manifest(self):
        base = super().get_manifest()
        base["dag"]["extract"]["inputs"]["args"]["my-extra-param"] = "{{my-extra-param}}"
        return base
```

---

### Step 9 — Verify type annotations

The SDK's `WorkflowInterface.run()` base method returns `Optional[Dict[str, Any]]`. Ensure your workflow's `run()` return type annotation matches: `-> Dict[str, Any]` (or `Optional[Dict[str, Any]]`).

Run the project's type checker if available:
```bash
uv run pyright app/
```

Fix any reported type errors before continuing.

---

### Step 9 — Run linting and formatting

```bash
uv run ruff check app/ --fix
uv run ruff format app/
```

Re-stage any reformatted files before committing.

---

### Step 10 — Verify locally

If local dev infrastructure is available (Dapr + Temporal), run the app and verify:

1. `GET /workflows/v1/configmap/atlan-connectors-{connector}` — returns credential form wrapped in `{ success, message, data: ConfigMap }`
2. `GET /workflows/v1/configmap/{connector}` — returns workflow form
3. `POST /workflows/v1/auth` with a test credential — returns `{ success: true }`

---

### Step 11 — Summary

After completing all steps, report:

1. Files changed (list each file and the nature of the change)
2. Files created (templates, manifest)
3. Imports removed from activities
4. Whether `_set_state` was present and has been removed
5. SDK version/rev that is now in use
6. Any connector-specific logic that was preserved in `get_workflow_args`
7. Any open questions or items that need manual review

---

## What the SDK Handles (Do Not Re-implement)

- Connection normalization from AE entity object → `{ connection_qualified_name, connection_name }`
- Stripping sensitive fields from inline credential in production
- Fetching secrets from Vault via `SecretStore.get_secret(credential_guid)`
- Merging Vault secrets into the stripped inline credential
- Computing `transformed_data_prefix`, `publish_state_prefix`, `current_state_prefix` from `output_path`
- Wrapping configmap response in `{ kind: ConfigMap, apiVersion, metadata, data }`
- Wrapping configmap in `{ success, message, data }` server envelope

## What Each App Must Implement

- Template JSON files (connector-specific form fields)
- `get_configmap()` — file path routing by `config_map_id`
- `get_workflow_args()` — connector-specific arg reshaping only
- `run()` — return `output_paths` from `super().run()`
- Manifest — SDK auto-generates the extract + publish DAG; override `get_manifest()` only if the connector needs extra params or a different DAG shape
