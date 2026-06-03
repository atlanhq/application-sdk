---
name: add-entrypoint
description: >
  Add a new @entrypoint method to an existing App subclass. Creates the
  typed Input/Output contracts, wires up tasks, and generates the per-entry-point
  manifest subfolder in app/generated/ if contracts are PKL-driven.
mandatory_triggers:
  - "/add-entrypoint"
  - "add entrypoint"
  - "add a new entrypoint"
optional_triggers:
  - "add new workflow"
  - "add mine queries"
  - "add query history"
owner: connector-platform-team
last_updated: "2026-04-30"
staleness_days: 90
inputs:
  - entrypoint_name: string (snake_case Python method name, e.g. "mine_queries"; the SDK converts it to kebab-case for dispatch, e.g. "mine-queries")
  - description: string (human description of what this entrypoint does)
outputs:
  - Modified app/contracts.py (new Input/Output models)
  - Modified app/connector.py (@entrypoint method + tasks)
  - Optional: app/generated/manifest.json update
gates: []
---

# add-entrypoint

Add a new `@entrypoint` method to an existing `App` subclass, giving it a
second independently-triggerable workflow.

## When to Use

Use `@entrypoint` when the same connector needs multiple independently-triggerable
operations that share credentials, handler, and infrastructure but have different
input/output contracts. Common patterns:

- `extract_metadata` + `mine_queries` (metadata + query history)
- `full_sync` + `incremental_sync`
- `extract` + `validate` + `publish` (if keeping them as one app vs AE DAG)

## Steps

### 1. Gather inputs

Ask for:
- `entrypoint_name` (snake_case Python method name): e.g. `mine_queries` — the SDK converts this to kebab-case (`mine-queries`) for the dispatch identity and workflow type
- A brief description of what it does

Read the existing `app/connector.py` and `app/contracts.py` to understand the current structure.

### 2. Add Input/Output contracts to `app/contracts.py`

```python
class {EntrypointName}Input(Input):
    connection_id: str
    credential_guid: str
    # Add entrypoint-specific fields with defaults for backwards compat
    days_back: int = 7

class {EntrypointName}Output(Output):
    record_count: int
```

**Evolution rule:** all new fields must have defaults. Never remove fields from existing contracts.

### 3. Add `@entrypoint` and tasks to `app/connector.py`

```python
from application_sdk.app import App, entrypoint, task

class MyConnector(App):
    # ... existing methods ...

    @entrypoint
    async def {entrypoint_name}(self, input: {EntrypointName}Input) -> {EntrypointName}Output:
        out = await self.{fetch_task_name}(
            {FetchTask}Input(connection_id=input.connection_id, ...)
        )
        # If this entrypoint hands artifacts to a downstream Atlan system app
        # (publish, lineage, quality), call App.upload() here — not from inside @task.
        # The @task FileReference interceptor writes only to the customer-owned
        # objectstore; App.upload() routes to atlan-objectstore in SDR deployments.
        # Omitting this call is a silent failure: the DAG succeeds but Atlan's
        # publish app finds nothing in its bucket. See docs/concepts/file-reference.md.
        #
        # await self.upload(UploadInput(local_path=out.output_path, tier=StorageTier.RETAINED))
        return {EntrypointName}Output(record_count=out.count)

    @task(timeout_seconds=3600, auto_heartbeat_seconds=30)
    async def {fetch_task_name}(self, input: {FetchTask}Input) -> {FetchTask}Output:
        # Actual work goes here
        ...
```

**Important:**
- Import `entrypoint` from `application_sdk.app`.
- `@entrypoint` is mutually exclusive with `run()` — if the class has `run()`, convert it to `@entrypoint` first (or keep `run()` as the default entry point alongside the new `@entrypoint`).
- Each `@entrypoint` becomes a separate Temporal workflow named `{app-name}:{entrypoint-kebab-name}` where the kebab name is the method name with underscores replaced by hyphens (e.g. `mine_queries` → `mine-queries`).

### 4. Trigger the new entrypoint

Via curl:
```bash
curl -X POST http://localhost:8000/workflows/v1/start \
  -H "Content-Type: application/json" \
  -d '{
    "credential_guid": "...",
    "connection": { "connection_qualified_name": "default/app/123" },
    "entrypoint": "{entrypoint-kebab-name}"
  }'
```

### 5. Update manifest.json (if PKL-driven)

If the app uses PKL contracts, add a new DAG node to `contract/app.pkl` for the new entrypoint and re-run the contract skill:
```
/contract update
```

Or manually add to `app/generated/manifest.json`:
```json
{
  "{entrypoint-kebab-name}": {
    "activity_name": "{entrypoint-kebab-name}",
    "activity_display_name": "{Description}",
    "app_name": "{app-name}",
    "inputs": {
      "workflow_type": "{entrypoint-kebab-name}",
      "task_queue": "atlan-{app-name}-{deployment-name}",
      "args": {
        "credential_guid": "{{credential}}",
        "connection_qualified_name": "{{connection}}"
      }
    }
  }
}
```

**Note on `task_queue`:** The value `atlan-{app-name}-{deployment-name}` is only used when both `ATLAN_APPLICATION_NAME` and `ATLAN_DEPLOYMENT_NAME` env vars are set. In local dev (where neither is set), the SDK falls back to `{ClassName}-queue`. Update `task_queue` to match how your local Temporal worker is configured (check the output of `uv run python run_dev.py` for the actual queue name).

## Verification

1. `uv run python run_dev.py` — smoke test the new entrypoint.
2. In Temporal UI, verify `{app-name}:{entrypoint-kebab-name}` appears as the workflow type (e.g. `my-app:mine-queries` if the method is `mine_queries`).
3. Check the response from `GET /workflows/v1/manifest` includes the new node.
