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

```dockerfile
ENV ATLAN_APP_MODULE=app.my_app:MyApp
CMD []  # Let APPLICATION_MODE env var control mode
```

Do NOT hardcode `CMD ["--mode", "combined"]` — it breaks production Helm-controlled mode switching.

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
