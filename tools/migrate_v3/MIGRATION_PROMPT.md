# v3 Migration Prompt — AI Agent Instructions

> **Scope**: This document guides an AI coding agent through the *structural*
> parts of the v2 → v3 migration (Categories B and C).
>
> **Pre-condition — SDK dependency**: Until v3 is published to PyPI, the
> connector must depend on `atlan-application-sdk` from the `refactor-v3`
> branch.  If the connector's `pyproject.toml` still references a v2 PyPI
> release, update it before doing anything else:
>
> ```toml
> [tool.uv.sources]
> atlan-application-sdk = { git = "https://github.com/atlanhq/application-sdk", branch = "refactor-v3" }
> ```
>
> **Pre-condition — import rewriter**: Run the import rewriter first.  All
> `# TODO(v3-migration)` comments in the codebase mark exactly where
> structural work is needed.
>
> **Post-condition**: Run `check_migration` and confirm zero FAIL items.

---

## 0. How to use this prompt

1. Copy the connector source into your context.
2. Follow the decision tree in §1 to identify the connector type.
3. Apply the matching step-by-step checklist (§2–§5).
4. Verify with `python -m tools.migrate_v3.check_migration <src_dir>`.

Do **not** guess at business logic.  If a method body does something
non-obvious, preserve it verbatim inside the new `@task` method — the goal
is structural migration, not refactoring.

---

## 1. Decision Tree

```
Does the connector extract SQL database metadata?
├── YES → Does it also do incremental extraction (diff-based)?
│         ├── YES → §2c  IncrementalSqlMetadataExtractor
│         └── NO  → §2a  SqlMetadataExtractor
├── Does the connector extract SQL query logs?
│   └── YES → §2b  SqlQueryExtractor
└── Custom connector (non-SQL)?
    └── YES → §3   Custom App
```

Every connector also has a **Handler** (§4) and an **entry point** (§5).
Apply those sections regardless of which workflow path you chose.

---

## 2a. SQL Metadata Extractor

### Before (v2)

```python
from application_sdk.workflows.metadata_extraction.sql import BaseSQLMetadataExtractionWorkflow
from application_sdk.activities.metadata_extraction.sql import BaseSQLMetadataExtractionActivities

@workflow.defn
class MyWorkflow(BaseSQLMetadataExtractionWorkflow):
    activities_cls = MyActivities

class MyActivities(BaseSQLMetadataExtractionActivities):
    fetch_database_sql = "SELECT ..."
    fetch_schema_sql   = "SELECT ..."
    fetch_table_sql    = "SELECT ..."
    fetch_column_sql   = "SELECT ..."

    async def fetch_databases(self, workflow_args: Dict[str, Any]) -> ActivityStatistics:
        # custom logic
        return ActivityStatistics(chunk_count=1, total_record_count=n)
```

### After (v3)

```python
from application_sdk.templates import SqlMetadataExtractor
from application_sdk.templates.contracts.sql_metadata import (
    FetchDatabasesInput, FetchDatabasesOutput,
    FetchSchemasInput, FetchSchemasOutput,
    FetchTablesInput, FetchTablesOutput,
    FetchColumnsInput, FetchColumnsOutput,
    ExtractionInput, ExtractionOutput,
)
from application_sdk.app import task
from application_sdk.common.models import TaskStatistics

class MyExtractor(SqlMetadataExtractor):
    fetch_database_sql = "SELECT ..."
    fetch_schema_sql   = "SELECT ..."
    fetch_table_sql    = "SELECT ..."
    fetch_column_sql   = "SELECT ..."

    @task(timeout_seconds=1800)
    async def fetch_databases(self, input: FetchDatabasesInput) -> FetchDatabasesOutput:
        # custom logic  ← paste original method body here; rename workflow_args → input
        return FetchDatabasesOutput(chunk_count=1, total_record_count=n)
```

### Checklist

- [ ] Delete the `@workflow.defn` class and `activities_cls = …` line.
- [ ] Merge both classes into one `MyExtractor(SqlMetadataExtractor)`.
- [ ] Move all SQL class-level attributes (`fetch_database_sql`, etc.) to the merged class.
- [ ] For each overridden activity method:
  - Rename the parameter from `workflow_args: Dict[str, Any]` to
    `input: FetchXxxInput`.
  - Replace `ActivityStatistics(...)` with `FetchXxxOutput(...)`.
  - Add `@task(timeout_seconds=…)`.
- [ ] Delete methods you did **not** override (the base class provides defaults).
- [ ] Replace `from application_sdk.activities.common.models import ActivityStatistics`
      with `from application_sdk.common.models import TaskStatistics` if used
      directly (usually it is now inside the typed Output).

---

## 2b. SQL Query Extractor

### Before (v2)

```python
from application_sdk.workflows.query_extraction.sql import SQLQueryExtractionWorkflow
from application_sdk.activities.query_extraction.sql import SQLQueryExtractionActivities

@workflow.defn
class MyQueryWorkflow(SQLQueryExtractionWorkflow):
    activities_cls = MyQueryActivities

class MyQueryActivities(SQLQueryExtractionActivities):
    async def get_query_batches(self, workflow_args: Dict[str, Any]):
        ...
    async def fetch_queries(self, workflow_args: Dict[str, Any]):
        ...
```

### After (v3)

```python
from application_sdk.templates import SqlQueryExtractor
from application_sdk.templates.contracts.sql_query import (
    QueryBatchInput, QueryBatchOutput,
    QueryFetchInput, QueryFetchOutput,
)
from application_sdk.app import task

class MyQueryExtractor(SqlQueryExtractor):
    @task(timeout_seconds=600)
    async def get_query_batches(self, input: QueryBatchInput) -> QueryBatchOutput:
        ...

    @task(timeout_seconds=3600)
    async def fetch_queries(self, input: QueryFetchInput) -> QueryFetchOutput:
        ...
```

### Checklist

- [ ] Delete the `@workflow.defn` class.
- [ ] Merge both classes into `MyQueryExtractor(SqlQueryExtractor)`.
- [ ] Rename `workflow_args: Dict[str, Any]` → `input: QueryBatchInput` / `input: QueryFetchInput`.
- [ ] Add `@task(timeout_seconds=…)` to each overridden method.

---

## 2c. Incremental SQL Metadata Extractor

### Before (v2)

```python
from application_sdk.workflows.metadata_extraction.incremental_sql import (
    IncrementalSQLMetadataExtractionWorkflow,
)
from application_sdk.activities.metadata_extraction.incremental import (
    BaseSQLIncrementalMetadataExtractionActivities,
)

@workflow.defn
class MyIncrementalWorkflow(IncrementalSQLMetadataExtractionWorkflow):
    activities_cls = MyIncrementalActivities

class MyIncrementalActivities(BaseSQLIncrementalMetadataExtractionActivities):
    async def fetch_columns(self, workflow_args: Dict[str, Any]) -> ActivityStatistics:
        ...
```

### After (v3)

```python
from application_sdk.templates import IncrementalSqlMetadataExtractor
from application_sdk.templates.contracts.incremental_sql import (
    IncrementalExtractionInput, IncrementalExtractionOutput,
    FetchColumnsInput, FetchColumnsOutput,
)
from application_sdk.app import task

class MyIncrementalExtractor(IncrementalSqlMetadataExtractor):
    @task(timeout_seconds=1800)
    async def fetch_columns(self, input: FetchColumnsInput) -> FetchColumnsOutput:
        ...
```

### Checklist

- [ ] Same pattern as §2a — delete the workflow class, merge, rename params, add `@task`.
- [ ] The 4-phase incremental sequence (marker → fetch → diff → finalize) is
      provided automatically by the base class `run()`.  Only override `run()`
      if you need a custom sequence.

---

## 3. Custom App (non-SQL connector)

For connectors that do not fit the SQL templates, subclass `App` directly.

```python
from application_sdk.app import App, task
from application_sdk.contracts.base import Input, Output
from typing import Annotated
from application_sdk.contracts.types import MaxItems

class MyInput(Input):
    connection_id: str
    items: Annotated[list[str], MaxItems(1000)]

class MyOutput(Output):
    processed: int

class MyConnector(App):
    @task(timeout_seconds=3600, heartbeat_timeout_seconds=60, auto_heartbeat_seconds=10)
    async def extract(self, input: MyInput) -> MyOutput:
        # move the body of your old activity method here
        ...
        return MyOutput(processed=n)

    async def run(self, input: MyInput) -> MyOutput:
        return await self.extract(input)
```

### Checklist

- [ ] For each `@activity.defn` method in the old activities class:
  - Define a typed `Input` / `Output` model pair (see §8 of the migration guide).
  - Move the method body into a `@task` method on the new `App` subclass.
  - Rename `workflow_args: Dict[str, Any]` → `input: MyInput`.
- [ ] For the old `@workflow.run` method, implement `run()` on the `App` subclass.
      Replace `await workflow.execute_activity_method(...)` with direct
      `await self.my_task(input)` calls.
- [ ] Remove `@workflow.defn`, `@activity.defn`, `@auto_heartbeater` decorators.
- [ ] Remove `get_activities()` static method (no longer needed).

---

## 4. Handler

### Before (v2)

```python
from application_sdk.handlers import HandlerInterface

class MyHandler(HandlerInterface):
    async def load(self, *args, **kwargs) -> None: ...
    async def test_auth(self, *args, **kwargs) -> bool: ...
    async def preflight_check(self, *args, **kwargs): ...
    async def fetch_metadata(self, *args, **kwargs): ...
```

### After (v3)

```python
from application_sdk.handler import Handler
from application_sdk.handler.contracts import (
    AuthInput, AuthOutput, AuthStatus,
    PreflightInput, PreflightOutput, PreflightStatus,
    MetadataInput, MetadataOutput,
)

class MyHandler(Handler):
    async def test_auth(self, input: AuthInput) -> AuthOutput:
        # move body of old test_auth here; use self.context.get_secret() instead
        # of direct SecretStore calls
        return AuthOutput(status=AuthStatus.SUCCESS)

    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        return PreflightOutput(status=PreflightStatus.READY)

    async def fetch_metadata(self, input: MetadataInput) -> MetadataOutput:
        return MetadataOutput(fields=[])
```

### Checklist

- [ ] Change base class `HandlerInterface` → `Handler`.
- [ ] Delete `load()` — context is injected automatically via `self.context`.
- [ ] Update `test_auth` signature: add `input: AuthInput`, return `AuthOutput`.
- [ ] Update `preflight_check` signature: add `input: PreflightInput`, return `PreflightOutput`.
- [ ] Update `fetch_metadata` signature: add `input: MetadataInput`, return `MetadataOutput`.
- [ ] Replace `SecretStore.get_credentials(...)` with `await self.context.get_secret(name)`.

---

## 5. Entry Point

### Before (v2)

```python
from application_sdk.application.metadata_extraction.sql import BaseSQLMetadataExtractionApplication

app = BaseSQLMetadataExtractionApplication(
    name="my-connector",
    client_class=MyClient,
    handler_class=MyHandler,
)
await app.start()
```

### After (v3) — programmatic (local dev)

```python
from application_sdk.main import run_dev_combined
import asyncio

asyncio.run(run_dev_combined(MyExtractor, handler_class=MyHandler))
```

### After (v3) — CLI / Dockerfile CMD

**`ATLAN_APP_MODULE` is mandatory.** The entrypoint hard-fails if it is not set.
Set it as a Dockerfile `ENV` so the value is locked to the image:

```dockerfile
ENV ATLAN_APP_MODULE=app.app:MyExtractor
CMD ["application-sdk", "--mode", "combined"]
```

Alternatively, pass it inline via `--app` (takes precedence over the env var):

```dockerfile
CMD ["application-sdk", "--mode", "combined", "--app", "app.app:MyExtractor"]
```

### Checklist

- [ ] Replace the `BaseXxxApplication(...)` instantiation with `run_dev_combined(...)`.
- [ ] Add `ENV ATLAN_APP_MODULE=<module.path:ClassName>` to the app's `Dockerfile`.
      **This is required** — the process will not start without it.
- [ ] Set `CMD ["application-sdk", "--mode", "combined"]` (or `worker`/`handler`) in
      the `Dockerfile`.
- [ ] If the connector used `app.setup_workflow(...)` / `app.start_workflow(...)` for
      integration tests, replace with direct `await connector.run(input)` calls
      (the framework handles Temporal wiring automatically).

---

## 6. Infrastructure / Services

| Old call | New call |
|----------|----------|
| `SecretStore.get_credentials({"credential_guid": guid})` | `await self.context.get_secret(name)` (in `@task`) |
| `StateStore.get_state(key)` | `await self.context.load_state(key)` |
| `StateStore.save_state(key, val)` | `await self.context.save_state(key, val)` |
| `ObjectStore.upload_file(src, key)` | `await self.context.upload_bytes(key, data)` or `upload_file(key, local_path=src)` |
| `ObjectStore.get_content(key)` | `await self.context.download_bytes(key)` |
| `ObjectStore.list_files(prefix)` | `await list_keys(prefix)` from `application_sdk.storage` |
| `ObjectStore.delete_file(key)` | `await delete(key)` from `application_sdk.storage` |

### Checklist

- [ ] Remove all `DaprClient` instantiations — the framework creates and injects them.
- [ ] Replace direct `SecretStore` / `StateStore` / `ObjectStore` calls with
      `self.context.*` inside `@task` methods.
- [ ] For scripts or utilities outside an `App`, use `application_sdk.storage` functions
      directly.

---

## 7. Logging

All connector code must use the SDK's logger — **never** import loguru or stdlib
`logging` directly.  Direct imports bypass `AtlanLoggerAdapter`, which injects
Temporal/tenant context, enforces `%`-style formatting, and routes to the
observability back-end.

### Setup (every module that logs)

```python
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)
```

### Forbidden patterns

```python
# BAD — bypasses AtlanLoggerAdapter entirely
from loguru import logger
import loguru

# BAD — bypasses AtlanLoggerAdapter
import logging
logger = logging.getLogger(__name__)
```

### Log call style

Use `%`-style placeholders in the message body.  Do **not** pass structured
data as keyword arguments (they are not indexed in the Grafana/ClickHouse
back-end and add noise without signal).

```python
# CORRECT
logger.info("Fetched %d rows from schema %s", row_count, schema_name)
logger.warning("Skipping table %s — permission denied", table_name)
logger.error("Connection failed after %d retries", retries, exc_info=True)

# WRONG — kwargs are silently swallowed, not logged or indexed
logger.info("Fetched rows", row_count=row_count, schema=schema_name)
```

### Checklist

- [ ] Replace every `from loguru import logger` / `import loguru` with
      `from application_sdk.observability.logger_adaptor import get_logger` +
      `logger = get_logger(__name__)`.
- [ ] Replace every `logging.getLogger(...)` with the same.
- [ ] Convert any `f"..."` log messages to `%`-style (`"... %s", value`).
- [ ] Remove structured kwargs from log calls — embed values in the message body.
- [ ] `exc_info=True` is the only keyword argument that should remain.

---

## 8. Typed Contracts — quick reference

```python
from application_sdk.contracts.base import Input, Output
from application_sdk.contracts.types import MaxItems, FileReference
from typing import Annotated

class MyInput(Input):
    workflow_id: str
    connection: str
    # Unbounded list → must be bounded or use FileReference
    items: Annotated[list[str], MaxItems(1000)]

class MyOutput(Output):
    processed: int
    result_ref: FileReference  # for large data (> ~2 MB)
```

**Forbidden** (raises `PayloadSafetyError` at class definition):
- `Any`, `bytes`, `bytearray`
- Unbounded `list[T]` or `dict[K, V]`

**Escape hatch — connector code must NOT use this.** `allow_unbounded_fields=True` is
reserved for SDK-internal types only. The checker will FAIL if it appears in connector
contracts. If you have an unbounded list, use `Annotated[list[T], MaxItems(N)]` or
`FileReference` instead.

---

## 9. Final verification

```bash
# 1. Import rewriter should already have run. Re-run to confirm zero remaining rewrites:
python -m tools.migrate_v3.rewrite_imports --dry-run src/

# 2. Check for remaining structural issues:
python -m tools.migrate_v3.check_migration src/

# 3. Run the connector's own test suite:
uv run pytest tests/

# 4. Smoke-test locally (requires Dapr + Temporal running):
uv run poe start-deps
python -m my_connector.main
```

All `check_migration` FAIL items must be resolved before the migration is
considered complete.  WARN items are advisory but should be addressed before
production deployment.

---

## 10. E2E test migration reference

If the connector has e2e tests that use a `BaseTest` / `TestInterface` pattern from
v2, replace them with the v3 `application_sdk.testing.e2e` API.

### Before (v2)

```python
from application_sdk.test_utils.base_test import BaseTest

class TestMyConnector(BaseTest):
    connector_name = "my-connector"

    async def test_metadata_extraction(self):
        result = await self.run_workflow("metadata_extraction", self.default_payload())
        assert result["status"] == "completed"
```

### After (v3)

```python
# TODO(v3-migration): human must validate this test is equivalent to the original
import pytest
from application_sdk.testing.e2e import AppConfig, AppDeployer, run_workflow, wait_for_workflow

@pytest.fixture(scope="session")
def app_config() -> AppConfig:
    return AppConfig(
        app_name="my-connector",
        app_module="my_connector.main:MyExtractor",
        namespace="default",
        image="ghcr.io/atlanhq/my-connector:latest",
    )

@pytest.fixture(scope="session")
async def deployed_app(app_config: AppConfig):
    deployer = AppDeployer(app_config)
    await deployer.deploy()
    yield deployer
    await deployer.undeploy()

async def test_metadata_extraction(deployed_app: AppDeployer) -> None:
    cfg = deployed_app.config
    workflow_id = await run_workflow(
        namespace=cfg.namespace,
        service=cfg.app_name,
        port=cfg.handler_port,
        workflow_name="metadata_extraction",
        payload={"connection_id": "test-connection"},
    )
    result = await wait_for_workflow(
        namespace=cfg.namespace,
        service=cfg.app_name,
        port=cfg.handler_port,
        workflow_id=workflow_id,
        timeout=300.0,
    )
    assert result["status"] == "completed"
```

### Key changes

| v2 | v3 |
|----|----|
| `BaseTest` class | `AppConfig` + `AppDeployer` fixtures |
| `self.run_workflow(name, payload)` | `run_workflow(namespace, service, port, name, payload)` |
| Built-in wait/poll | `wait_for_workflow(namespace, service, port, workflow_id, timeout)` |
| `self.default_payload()` | Explicit `payload` dict — extract values from the v2 method body |
| `self.check_health()` / health-check step | `kube_http_call(namespace, service, port, "/healthz")` or skip if not applicable |
| Ordered `test_*` methods in a class | Independent top-level `async def test_xxx(deployed_app)` functions |
| `assert result['authenticationCheck']` | `assert result.status == AuthStatus.SUCCESS` (update field names) |
| `assert result['hostCheck']` | `assert result.status == PreflightStatus.READY` (update field names) |
| `result['metadata']` hierarchical list | `result.fields` flat list — response shape changed |
| `connector_name = "foo"` class attribute | `AppConfig(app_name="foo", ...)` fixture parameter |

### Generation rules

1. **Count before you generate.** Count every test method in the original. The new file must have at least that many test functions.
2. **Copy real payload values.** If `default_payload()` returns `{"connection_id": "abc-123", "tenant_id": "xyz"}`, use those exact values — not `"test-connection"`.
3. **Map assertions, not just structure.** For each `assert` in the original, write an equivalent `assert` in the new test. If the response shape changed, keep the assert but add `# TODO(v3-migration): response format changed — update field names`.
4. **One fixture, many tests.** All test functions share the `deployed_app` session-scoped fixture. Do not deploy/undeploy per-test.
5. **Preserve test names.** Derive the new function name directly from the original method name (strip the `test_` prefix rule of the class if needed, but keep the semantic name).

Place the new test at `tests/e2e/test_<connector_name>_v3.py` alongside the
original. Do NOT delete the original test file — a human must verify equivalence.

---

## 11. Directory consolidation

v2 connectors typically use a split directory layout:

```
app/
  activities/
    my_connector.py    ← main App logic
  workflows/
    my_workflow.py     ← often just re-exports activities
```

v3 consolidates this into a single file:

```
app/
  my_connector.py      ← merged App class (moved from activities/)
```

### Steps

1. Identify the main App class file (usually `app/activities/<name>.py`).
2. Move it to `app/<app_name>.py` (derive the name from the App class or
   connector name, snake_cased).
3. If `app/workflows/<name>.py` only re-exports symbols from activities
   (e.g. `from app.activities.my_connector import MyConnector`), delete it.
4. Delete the now-empty `app/activities/` and `app/workflows/` directories.
5. Update all production-code imports that referenced the old paths.
6. In test files, add a `# TODO(v3-migration): update import to app.<app_name>`
   comment but leave the import line unchanged (test files are out of bounds for
   structural changes).
7. Re-run `check_migration` to confirm `no-v2-directory-structure` warning is gone.
