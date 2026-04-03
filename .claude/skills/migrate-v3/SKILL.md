---
name: migrate-v3
description: Migrate a connector repo from application-sdk v2 to v3 — runs automated tooling, performs AI-assisted structural refactoring, validates with tests and live workflow execution.
argument-hint: "<path-to-connector-repo> [Use local sdk reference.]"
---

# /migrate-v3

Performs a complete v2 → v3 migration of an application-sdk connector.

**Must be run from the application-sdk repo root.**

```
/migrate-v3 ../my-connector
/migrate-v3 ../my-connector Use local sdk reference.
```

---

## Architecture overview

The migration has 4 stages:

```
Stage 0: Research (understand BOTH codebases before touching anything)
  Read v3 SDK templates → Read connector code → Read golden dataset → Plan

Stage 1: Mechanical (fully automated, no judgment)
  rewrite_imports → run_codemods → check_migration

Stage 2: Structural (AI-assisted, guided by research + checker)
  Merge classes → Implement handler → Update entry point → Fix credentials

Stage 3: Validation (automated tests + live verification)
  Unit tests → Live app → Handler endpoints → Workflow run → Parity check
```

---

## Phase R — Research (do this BEFORE any code changes)

Launch up to 3 Explore agents in parallel to understand both codebases. Do NOT proceed to Phase 0 until all agents complete.

### Agent 1: Understand the v3 SDK target

Read these files in the application-sdk repo:

1. **`application_sdk/templates/sql_metadata_extractor.py`** — The base template. What methods exist? What do they take/return? Which are abstract?
2. **`application_sdk/templates/contracts/sql_metadata.py`** — All typed Input/Output contracts. What fields do `ExtractionInput`, `FetchDatabasesInput`, etc. have?
3. **`application_sdk/handler/base.py`** — The `Handler` ABC. What methods must be implemented?
4. **`application_sdk/handler/contracts.py`** — `AuthInput/Output`, `PreflightInput/Output`, `MetadataInput/Output` — what fields?
5. **`application_sdk/handler/service.py` lines 540-620** — How does `/start` create the input? How do credentials flow?
6. **`application_sdk/main.py`** — Find `run_dev_combined()` signature. What params does it accept?
7. **`application_sdk/app/base.py`** — Find `__init_subclass__`. How does app registration work? What is `_app_registered`?
8. **`application_sdk/common/sql_utils.py`** — What standalone functions exist? `execute_multidb_flow`, `execute_single_db`, `prepare_database_query`?

Report: template method signatures, contract field lists, handler interface, credential flow, registration mechanism.

### Agent 2: Understand the connector being migrated

Read ALL Python files in the connector's `app/` directory and `main.py`:

1. What classes exist? What do they inherit from?
2. What SQL queries are defined (class attributes or `app/sql/` files)?
3. What does the handler do? Does it override any methods?
4. What does `main.py` wire up?
5. What's in `app/generated/_input.py` (contract-generated input)? What fields does it have?
6. What YAML templates exist in `app/transformers/`?
7. What custom logic exists beyond the SDK base (multidb toggle, enrichment, filter handling)?

Report: class hierarchy, method inventory, SQL queries, handler behavior, custom logic, contract input fields.

### Agent 3: Understand the baseline (golden dataset + v2 output)

1. Check `golden-dataset/extract/` — What entity types? What fields per entity? Any enrichment fields (PARTITIONS, EXTRA_INFO)?
2. Check `golden-dataset/expected-output/` — What attributes and customAttributes are expected?
3. Check `./local/dapr/objectstore/artifacts/` — Any existing v2 workflow runs? Count entities per type.
4. Check `.env` — What credentials are available for live testing?
5. Check `tests/unit/` — What do existing tests test? What v2 APIs do they reference?
6. Check `tests/e2e/` — What e2e test patterns exist?

Report: entity counts, field lists, v2 APIs used in tests, credentials available, baseline for parity.

### After all agents complete

Synthesize the research into a migration plan:

1. **Connector type**: SQL metadata / SQL query / Incremental / REST / Custom
2. **Template to use**: `SqlMetadataExtractor` / `SqlQueryExtractor` / etc.
3. **Contract input mismatch**: What fields does `app/generated/_input.py` have that `ExtractionInput` doesn't? (e.g. `output_dir` vs `output_path`, `trino_credential` vs `credential_ref`)
4. **Handler work**: What did `BaseSQLHandler` provide that must be reimplemented?
5. **Custom logic to preserve**: Multidb toggle, enrichment queries, filter handling
6. **Test rewrite scope**: Which tests reference v2 APIs that won't exist?
7. **Feature gaps to close**: What enrichment/attributes does the golden dataset show that the v2 app produced?

Print this plan and proceed.

---

## Phase 0 — Setup

1. Parse arguments. If `Use local sdk reference.` is present, use `path = "../application-sdk", editable = true` in `[tool.uv.sources]`. Otherwise use `git = "...", branch = "refactor-v3"`.
2. Confirm target path exists and has `pyproject.toml`.
3. Confirm `tools/migrate_v3/rewrite_imports.py` exists (we're in the SDK repo).
4. Update the connector's `pyproject.toml` SDK dependency and run `uv sync`.
5. Check temporalio version — v3 requires `VersioningBehavior`:
```bash
cd <connector-repo> && uv run python -c "from temporalio.common import VersioningBehavior"
```
If it fails, run `uv add temporalio --upgrade && uv sync`.
6. Read `tools/migrate_v3/MIGRATION_PROMPT.md` — this is the structural migration reference.
6. Run the initial checker:

```bash
uv run python -m tools.migrate_v3.check_migration --classify --no-color <target-path>/app <target-path>/main.py
```

If zero FAILs, the connector may already be migrated. Stop and tell the user.

---

## Phase 1 — Automated transforms

### 1a — Import rewrites

```bash
uv run python -m tools.migrate_v3.rewrite_imports <target-path>
```

This rewrites all deprecated import paths (production + test files). Purely mechanical.

### 1b — Structural codemods

```bash
uv run python -m tools.migrate_v3.run_codemods <target-path>
```

Removes `@activity.defn`/`@workflow.defn`/`@auto_heartbeater`, adds `@task`, rewrites signatures, rewrites activity calls, cleans up plumbing.

### 1c — Extract context

```bash
uv run python -m tools.migrate_v3.extract_context <target-path>
```

Produces: connector type, difficulty score, class inventory, infrastructure patterns. Use this to guide Phase 2.

### 1d — Check remaining work

```bash
uv run python -m tools.migrate_v3.check_migration --no-color <target-path>/app <target-path>/main.py
```

The remaining FAILs are the AI's scope for Phase 2.

---

## Phase 2 — Structural migration

Work through these in order. **Run `check_migration` after EACH step** — the checker output shows exactly what FAILs remain and guides the next step.

### 2a — Identify connector type

The context extractor already classified this. Confirm by reading the code:

| Pattern | Type | Template |
|---------|------|----------|
| `BaseSQLMetadataExtractionWorkflow` / `BaseSQLMetadataExtractionActivities` | SQL metadata | `SqlMetadataExtractor` |
| `SQLQueryExtractionWorkflow` / `SQLQueryExtractionActivities` | SQL query | `SqlQueryExtractor` |
| `IncrementalSQLMetadataExtractionWorkflow` | Incremental SQL | `IncrementalSqlMetadataExtractor` |
| HTTP/REST client, no SQL | REST/HTTP | `BaseMetadataExtractor` |
| Other | Custom | `App` |

### 2b — Create the App class

Create `app/<connector_name>.py` with the merged App class. Follow §2a-2d of MIGRATION_PROMPT.md.

**Critical patterns learned from production migrations:**

#### App registration

The base template (e.g. `SqlMetadataExtractor`) sets `_app_registered = True` during `__init_subclass__`. Your subclass inherits this and **skips registration**. Temporal will create base class instances whose tasks all raise `NotImplementedError`.

**Fix:** Always set these on your App class:
```python
class MyApp(SqlMetadataExtractor):
    name: ClassVar[str] = "my-connector-name"  # matches pyproject.toml name
    _app_registered: ClassVar[bool] = False     # force re-registration
```

#### Credential flow (SDK design gap — BLDX-832)

The SDK's `ExtractionInput` and `FetchXxxInput` use Pydantic `extra='ignore'` — inline credentials from `/start` are silently dropped. Additionally, Temporal creates fresh App instances for each `@task` activity, so instance variables set in `run()` don't carry to tasks.

**Workaround:** Create custom input types that carry credentials:
```python
class MyExtractionInput(ExtractionInput, allow_unbounded_fields=True):
    credentials: dict[str, Any] = Field(default_factory=dict)

class MyTaskInput(ExtractionTaskInput, allow_unbounded_fields=True):
    credentials: dict[str, Any] = Field(default_factory=dict)
```

Use `MyTaskInput` for all `@task` method signatures. This requires `# type: ignore[override]` on methods that override the base template — this is expected until BLDX-832 is resolved.

#### Client creation pattern

```python
async def _create_client(self, input: Any) -> MyClient:
    client = MyClient()
    if input.credential_ref:
        creds = await self.context.resolve_credential_raw(input.credential_ref)
        await client.load(creds)
    elif input.credential_guid:
        from application_sdk.infrastructure.secrets import SecretStore
        creds = await SecretStore.get_credentials(input.credential_guid)  # type: ignore[attr-defined]
        await client.load(creds)
    elif hasattr(input, "credentials") and input.credentials:
        await client.load(input.credentials)
    else:
        raise ValueError("No credential source provided")
    return client
```

#### SQL client per-instance config

If the client has a class-level `DB_CONFIG`, create a **fresh instance** in `load()` to avoid shared mutable state across concurrent tasks:

```python
async def load(self, credentials):
    self.DB_CONFIG = DatabaseConfig(
        template=self.DB_CONFIG.template,
        required=list(self.DB_CONFIG.required),
        defaults=dict(self.DB_CONFIG.defaults) if self.DB_CONFIG.defaults else {},
        connect_args={},
    )
    credentials = dict(credentials)  # copy to avoid mutating caller's dict
    ...
```

#### Output path computation

v3 does not auto-compute `output_path`. In `run()`:
```python
output_path = input.output_path  # or input.output_dir for contract-generated inputs
if not output_path:
    from application_sdk.constants import APPLICATION_NAME, TEMPORARY_PATH
    output_path = os.path.join(
        TEMPORARY_PATH,
        f"artifacts/apps/{APPLICATION_NAME}/workflows/{workflow_id or 'local'}",
    )
```

Do NOT call `build_output_path()` from `run()` — it requires Temporal activity context, and `run()` is a workflow.

#### Handler import for discovery

v3 discovers the Handler by inspecting the App's module. If the handler is in a separate file, import it in the App module:
```python
from app.handlers.my_handler import MyHandler  # noqa: F401
```

### 2c — Implement the Handler

v3 `Handler` is abstract — you must implement `test_auth`, `preflight_check`, `fetch_metadata` from scratch. The v2 `BaseSQLHandler` provided these automatically; in v3 they don't exist.

The SDK normalizes v2 nested dict credentials to v3 `list[HandlerCredential]` format automatically in `service.py`. Your handler receives the v3 format. To build a SQL client from it:

```python
async def _build_client(self, input: AuthInput | PreflightInput | MetadataInput) -> MyClient:
    creds: dict[str, Any] = {}
    extra: dict[str, Any] = {}
    for cred in input.credentials:
        if cred.key.startswith("extra."):
            extra[cred.key[len("extra."):]] = cred.value
        else:
            creds[cred.key] = cred.value
    if extra:
        creds["extra"] = extra
    client = MyClient()
    await client.load(credentials=creds)
    return client
```

**Always use try/finally for client cleanup** in every handler method:
```python
async def test_auth(self, input: AuthInput) -> AuthOutput:
    client: MyClient | None = None
    try:
        client = await self._build_client(input)
        await client.get_results("SELECT 1")
        return AuthOutput(status=AuthStatus.SUCCESS)
    except Exception as e:
        return AuthOutput(status=AuthStatus.FAILED, message=str(e))
    finally:
        if client:
            await client.close()
```

### 2d — Update entry point

```python
# main.py
import asyncio
from application_sdk.main import run_dev_combined
from app.my_app import MyApp

async def main():
    await run_dev_combined(MyApp)

if __name__ == "__main__":
    asyncio.run(main())
```

**Note:** `run_dev_combined` does NOT accept `handler_class`. The handler is discovered automatically via module inspection.

### 2e — Update Dockerfile

The Dockerfile MUST follow this exact pattern. Do NOT deviate from the base image, layer order, or env vars:

```dockerfile
# syntax=docker/dockerfile:1
FROM registry.atlan.com/public/app-runtime-base:refactor-v3-latest

# git is required for uv to fetch git-sourced dependencies (atlan-application-sdk)
USER root
RUN apk add --no-cache git
USER appuser

WORKDIR /app

# Copy lock files first for dependency caching
COPY --chown=appuser:appuser pyproject.toml uv.lock ./

# Install dependencies (excluding the project itself) into a new venv
RUN --mount=type=cache,target=/home/appuser/.cache/uv,uid=1000,gid=1000 \
    uv venv .venv && \
    uv sync --locked --no-install-project --no-dev

# Copy application code only
COPY --chown=appuser:appuser app/ app/

# Comma-separated: first is primary (HTTP handler), rest register on worker
ENV ATLAN_APP_MODULE=app.my_app:MyApp
ENV ATLAN_CONTRACT_GENERATED_DIR=/app/app/generated
ENV APPLICATION_SDK_ENABLE_EVENT_INTERCEPTOR=false
```

Key rules:
- Base image: `registry.atlan.com/public/app-runtime-base:refactor-v3-latest` — NOT `ghcr.io/atlanhq/application-sdk-main:2.x`
- `COPY app/ app/` — only app code, NOT the entire repo
- `ATLAN_APP_MODULE` — hardcoded, comma-separated for multi-app
- No `CMD` — the base image handles mode via `APPLICATION_MODE` env var set by Helm
- No `entrypoint.sh`, `supervisord.conf`, `otel-config.yaml` — v3 base image handles all of these

### 2f — Directory consolidation

Delete `app/activities/` and `app/workflows/` directories. Update all imports. For test files, use the import rewriter:

```bash
uv run python -m tools.migrate_v3.rewrite_imports \
  --internal-map '{"app.activities.metadata_extraction.old_name": "app.new_name"}' \
  <target-path>/tests/
```

### 2g — Post-processing

```bash
uv run ruff check --fix --select I,F401 <target-path>
uv run ruff format <target-path>
uv run python -m tools.migrate_v3.check_migration --no-color <target-path>/app <target-path>/main.py
```

All FAILs should be resolved. WARNs are advisory.

---

## Phase 3 — Tests

### 3a — Rewrite unit tests

v2 tests test v2 APIs (`sql_client_class`, `handler_class`, `multidb`, `get_workflow_args`) that no longer exist. **Rewrite tests to test the v3 API directly:**

- App config: SQL queries loaded, class hierarchy, name
- Input contracts: credentials field, default values
- Helper methods: `_create_client` raises on no creds, exclude filter logic
- Handler: inherits `Handler`, has required methods

### 3b — Run tests

```bash
uv run pytest tests/unit/ -v --tb=short
```

Fix production code for any failures. Do not leave broken tests.

### 3c — Suppress SSL warnings

If the connector disables SSL verification, suppress urllib3 warnings in the client:
```python
if disable_ssl:
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
```

---

## Phase 4 — Live verification

### 4a — Start the app

```bash
atlan app run -p <target-path>
```

Wait for `Uvicorn running on http://127.0.0.1:8000`. This handles Temporal + Dapr automatically.

### 4b — Test all handler endpoints

Read `.env` for credentials. Test in this order:

1. `POST /workflows/v1/auth` — should return `status: success`
2. `POST /workflows/v1/check` — should return `status: ready`
3. `POST /workflows/v1/metadata` — should return objects
4. `GET /workflows/v1/configmaps` — should list configmaps
5. `GET /workflows/v1/manifest` — should return DAG

If any fail, fix and restart before proceeding.

### 4c — Run a workflow

```bash
curl -s -X POST http://localhost:8000/workflows/v1/start \
  -H "Content-Type: application/json" \
  -d '{"credentials":{...},"connection":{"connection_qualified_name":"..."}}'
```

Poll status until `COMPLETED` or `FAILED`.

### 4d — Verify parity

Compare entity counts against the latest v2 run:

```bash
# Count entities in v3 output
find ./local/dapr/objectstore/artifacts -name "*.parquet" -path "*/raw/*" | while read f; do
  entity=$(echo "$f" | grep -o 'raw/[^/]*' | cut -d/ -f2)
  echo "$entity"
done | sort | uniq -c | sort -rn
```

### 4e — Test filters

Run with exclude filter to verify filtering works:
```json
{"exclude_filter": "{\"catalog_name\": [\"*\"]}", ...}
```

Should produce 0 entities for multidb tasks.

---

## Phase 5 — Feature parity (if applicable)

After core extraction works, check for connector-specific features the v2 version had:

### Enrichment (view definitions, partition metadata, etc.)

For SQL connectors, check if the legacy extractor had per-row dynamic queries (SHOW CREATE VIEW, $partitions, etc.). If so:

1. **Use bulk queries** — `information_schema.views` per catalog, not SHOW CREATE VIEW per view
2. **Detect connector type** — `SELECT catalog_name, connector_name FROM system.metadata.catalogs`
3. **Skip non-applicable catalogs** — PostgreSQL doesn't support `$partitions`, only hive/iceberg/delta do
4. **Use bounded concurrency** — `asyncio.Semaphore(5)` for per-row queries that can't be batched

### Preflight validation

If the legacy had sage/preflight checks beyond SELECT 1 (like validating include-filter targets exist), implement those in the handler's `preflight_check`.

### Custom attributes

Check YAML templates for unmapped columns — data already in the SQL but not mapped to entity attributes.

---

## Known gotchas

### Checker scans .venv

Pass specific paths to the checker, not the repo root:
```bash
uv run python -m tools.migrate_v3.check_migration --no-color <target-path>/app <target-path>/main.py
```

### `ExtractionOutput` missing production fields

The v3 `ExtractionOutput` has no `transformed_data_prefix` or `connection_qualified_name`. The publish app reads these via AE JSONPath. This is an SDK gap (BLDX-832). For now, the connector works for extraction but publish integration needs the SDK fix.

### Contract-generated input vs SDK input

`app/generated/_input.py` (from PKL contract) uses different field names than `ExtractionInput`:
- `output_dir` vs `output_path`
- `trino_credential` (CredentialRef) vs `credential_ref`
- `include_filter` (dict) vs `include_filter` (str)

The `run()` method must bridge these when using the contract-generated input as the run() type.

### `atlan app run` vs `uv run python main.py`

Always prefer `atlan app run -p .` — it sets `DAPR_HTTP_PORT` and starts Temporal automatically. Running `main.py` directly skips Dapr and the SDK falls back to InMemory silently.

### pre-commit must exclude app/generated/

```yaml
exclude: app/generated/
```

### Logging

Replace all `from loguru import logger` and `import logging; logger = logging.getLogger(...)` with the SDK's logger:
```python
from application_sdk.observability.logger_adaptor import get_logger
logger = get_logger(__name__)
```
Use `%`-style formatting: `logger.info("Fetched %d rows", count)` — not f-strings or kwargs.

### Filter handling

The SDK's `get_database_names()` only reads `include-filter`, not `exclude-filter`. Handle exclude filtering in the app by discovering all databases then removing excluded ones.

### run() return type must match AE JSONPath queries

The output contract from `run()` is what AE reads via JSONPath to find transformed data. The field names must match exactly what AE expects:

```python
class MyExtractionOutput(Output):
    transformed_data_prefix: str = ""          # AE reads this to find JSONL
    connection_qualified_name: str = ""
```

If these fields are empty or named differently, AE won't find the output and the publish step fails silently.

### Multi-app connectors (multiple v2 workflows → multiple v3 Apps)

When the v2 connector has multiple workflows (e.g. metadata extraction + lineage extraction), each becomes a separate `App` subclass in v3. All apps share the same Temporal worker and task queue.

**Rules:**
- ONE primary app — serves the HTTP handler (`/auth`, `/check`, `/start`, `/metadata`)
- N secondary apps — registered on the worker, triggered only via Temporal by workflow name
- ONE handler total — imported in the primary app module. Secondary apps have no handler.

**Dockerfile pattern:**
```dockerfile
# Comma-separated: first is primary (HTTP), rest register on worker
ENV ATLAN_APP_MODULE=app.primary_app:PrimaryApp,app.secondary_app:SecondaryApp
```

**Handler registration** — import in the primary app module (recommended approach):
```python
# app/primary_app.py
from app.handlers import MyHandler  # noqa: F401 — registers handler
```

The SDK parses `ATLAN_APP_MODULE`:
1. First entry → loaded as primary app, serves HTTP via handler
2. Remaining entries → loaded and registered on the worker (workflows + tasks)
3. Only explicitly declared apps' tasks are registered — template base classes imported transitively are excluded

**File structure:**
```
app/
  primary_app.py         — PrimaryApp(App) with @task methods
  secondary_app.py       — SecondaryApp(App) with @task methods
  handlers/__init__.py   — MyHandler(Handler) — shared by primary app's HTTP endpoints
  contracts.py           — Input/Output for all apps
  clients/__init__.py    — Shared API client
```

**Do NOT:**
- Import secondary apps inside the primary app module — use `ATLAN_APP_MODULE` comma syntax instead
- Define handlers on secondary apps — only the primary app serves HTTP
- Give each app its own handler — one handler per connector

**Marketplace template:**
Each app has a distinct `workflow-type` (kebab-case of the App class name). The Argo template triggers them by name:
```yaml
# Primary app
- name: workflow-type
  value: "primary-app"

# Secondary app
- name: workflow-type
  value: "secondary-app"
```

Both run on the same task queue (derived from `ATLAN_APPLICATION_NAME` + `ATLAN_DEPLOYMENT_NAME`).

### self.context vs self.task_context

Use `self.context` (returns `AppContext`) inside `@task` methods. It has `get_secret()`, `storage`, `run_id`, etc. `self.task_context` returns `TaskExecutionContext` which does NOT have `get_secret`. Using `task_context.get_secret()` will crash with `AttributeError`.

### app_state only inside @task methods

`self.get_app_state()` and `self.set_app_state()` can only be called inside `@task` methods. Calling from `run()` crashes with "Cannot access app state outside of task context". Pass data to tasks via their typed Input contracts instead.

### build_output_path() cannot be called from run()

`build_output_path()` uses `activity.info().workflow_id` internally which is not available in the workflow context (`run()`). Use `self.run_id` directly:
```python
output_path = str(Path(tempfile.gettempdir()) / "my-app" / self.run_id)
```

### Write to local disk, not ParquetFileWriter/JsonFileWriter

`ParquetFileWriter` and `JsonFileWriter` internally call `ObjectStore` → `DaprClient()` → Dapr health check (60s timeout). This fails locally and adds unnecessary coupling. Write directly to local disk:
```python
# Parquet
pandas_df.to_parquet(str(output_file))

# JSONL
with output_file.open("wb") as f:
    f.write(json.dumps(entity).encode() + b"\n")
```

### workflows extra is required

`orjson`, `temporalio`, and `dapr` are in the `[workflows]` optional extra, not core dependencies. Without it the container fails with `ModuleNotFoundError: No module named 'orjson'`.
```toml
"atlan-application-sdk[daft,iam-auth,pandas,workflows]"
```

### InMemorySecretStore for local dev

For local testing with credentials, use `run_dev_combined()` with `InMemorySecretStore`:
```python
from application_sdk.infrastructure.secrets import InMemorySecretStore

secrets = {"my-cred": json.dumps({"host": "...", "password": "..."})}
credential_stores = {"default": InMemorySecretStore(secrets)}

await run_dev_combined(MyApp, credential_stores=credential_stores)
```

### Credential resolution only via credential_guid

No `credentials` dict fallback. Always resolve from secret store inside `@task` methods:
```python
creds_json = await self.context.get_secret(credential_guid)
creds = json.loads(creds_json) if isinstance(creds_json, str) else creds_json
```

### QueryBasedTransformer needs workflow_id and workflow_run_id

`transform_metadata()` requires `workflow_id` and `workflow_run_id` as kwargs. Use `self.context.run_id` for both:
```python
transform_kwargs = {
    "workflow_id": self.context.run_id,
    "workflow_run_id": self.context.run_id,
    ...
}
```

### Transformer output format

`QueryBasedTransformer.transform_metadata()` returns a Daft DataFrame with columns `typeName`, `status`, `attributes` — NOT `entity`. Write JSONL by iterating these:
```python
result_dict = transformed.to_pydict()
for i in range(len(result_dict["typeName"])):
    entity = {
        "typeName": result_dict["typeName"][i],
        "status": result_dict["status"][i],
        "attributes": result_dict["attributes"][i],
    }
    f.write(json.dumps(entity).encode() + b"\n")
```

### Output paths computed internally

Do not pass `output_path` or `output_prefix` via the run() Input contract. Compute them inside `run()` and pass to tasks:
```python
output_path = str(Path(tempfile.gettempdir()) / "my-app" / self.run_id)
output_prefix = str(Path(tempfile.gettempdir()))
```
The interim-apps framework handles S3 mapping in production.

### auto_heartbeat_seconds triggers Dapr health check

The auto-heartbeater creates a `DaprClient` which blocks on Dapr health check (60s timeout). For tasks that don't need fine-grained heartbeating, omit `auto_heartbeat_seconds`:
```python
@task(timeout_seconds=3600)  # no auto_heartbeat_seconds
async def my_long_task(self, input: MyInput) -> MyOutput:
    ...
```

### Template base classes auto-register and can shadow task overrides

When you `from application_sdk.templates import SqlMetadataExtractor`, Python imports all templates from `__init__.py`. Each has a concrete `run()`, so `__init_subclass__` registers them all in `AppRegistry`. If the worker uses `app_names=None`, base class tasks like `fetch_databases` register first and shadow your connector's overrides.

The SDK handles this by only registering tasks for apps explicitly declared in `ATLAN_APP_MODULE`. Template base classes register in `AppRegistry` (harmless) but their tasks are excluded from the worker. You don't need to do anything — just make sure all your apps are listed in `ATLAN_APP_MODULE`.

### os.environ is blocked inside Temporal workflow sandbox

`os.environ.get()` cannot be called from `run()` — Temporal's sandbox blocks it as non-deterministic. Read env vars at module level:
```python
# Module level — runs at import time, before sandbox
_MY_CONFIG = os.environ.get("MY_CONFIG", "default")

class MyApp(App):
    async def run(self, input: MyInput) -> MyOutput:
        # Use _MY_CONFIG here, not os.environ.get()
        ...
```
