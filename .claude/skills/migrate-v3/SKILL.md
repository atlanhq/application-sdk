---
name: migrate-v3
description: Migrate a connector repo from application-sdk v2 to v3 — runs the import rewriter, performs AI-assisted structural refactoring, and validates the result with the migration checker.
argument-hint: "<path-to-connector-repo>"
---

# /migrate-v3

Performs a complete v2 → v3 migration of an application-sdk connector.

**Must be run from the application-sdk repo root** (so the migration tooling and docs are reachable).

## Usage

```
/migrate-v3 ../my-connector/src
/migrate-v3 /absolute/path/to/connector/
```

---

## Phase 0 — Setup and validation

1. Parse `$ARGUMENTS` to get the target path. If no argument is given, stop and ask the user for one.
2. Confirm the target path exists. If it does not, stop and report the error.
3. Confirm you are running from within the application-sdk repo by checking that `tools/migrate_v3/rewrite_imports.py` exists. If it does not, stop and tell the user to run this skill from the application-sdk repo root.
4. **Check the connector's SDK dependency.** Read the connector's `pyproject.toml` and look for `atlan-application-sdk` in the dependencies. Until v3 is released on PyPI the connector must use the git source:
   ```toml
   [tool.uv.sources]
   atlan-application-sdk = { git = "https://github.com/atlanhq/application-sdk", branch = "refactor-v3" }
   ```
   If it still points to a v2 PyPI release (e.g. `atlan-application-sdk>=2.x`), add the `[tool.uv.sources]` block above and run `uv sync` in the connector repo before continuing. If it already has this source override or references a v3+ PyPI release, proceed.

4b. **Check temporalio version.** The v3 SDK requires `temporalio` with `VersioningBehavior`. Run in the connector repo root (where `pyproject.toml` is):
   ```bash
   cd <connector-repo-root> && uv run python -c "from temporalio.common import VersioningBehavior"
   ```
   If this fails with `ImportError`, upgrade temporalio before continuing:
   ```bash
   cd <connector-repo-root> && uv add temporalio --upgrade && uv sync
   ```

5. Read `tools/migrate_v3/MIGRATION_PROMPT.md` in full. This is the authoritative reference for all structural changes you will make. Do not proceed to Phase 3 without having read it.

5b. **Read a reference implementation.** Before touching code, read a working v3 connector of the same type. This prevents hours of debugging by showing the target state upfront.

   - **SQL metadata extractor** → Read two references:
     1. `apps/mssql/app/mssql_extractor.py` in the `connectors-sql` repo — full multidb pattern, custom retry logic, ODBC connection handling.
     2. `app/saphana_extractor.py` in the `atlan-saphana-app` repo — standalone repo pattern, custom SQL queries, calculation view processing, non-standard DBAPI driver handling.
     Pay attention to: `_app_registered`, `name: ClassVar[str]`, custom Input types, `_create_client` 3-source credential fallback, `run()` orchestration, `transform_data` wiring, `SafeParquetFileWriter`.
   - **SQL query extractor** → Read the SDK's `application_sdk/templates/sql_query_extractor.py` and any existing query extractor in `connectors-sql`.
   - **REST/HTTP connector** → Read an existing v3 REST connector (e.g. dbt app at `atlan-dbt-app/app/`).
   - **Custom app** → Read the dbt app or any v3 app that matches the pattern.

   > **Why this matters:** In the SAP HANA migration, comparing against MSSQL (Phase 8) and dbt (Phase 11) each revealed missing patterns. Doing this comparison BEFORE the first workflow run saves days of debugging.

6. Run an initial checker pass to establish the baseline — **do not fix anything yet**:

```bash
uv run python -m tools.migrate_v3.check_migration --no-color <target-path>
```

Print a short summary: how many FAILs and WARNs were found. If zero FAILs, tell the user the connector may already be migrated and stop.

---

## Phase 1 — Mechanical import rewrites

Run the import rewriter across the **entire** target directory tree, including test files. This step is purely mechanical — import paths are rewritten losslessly; no logic is touched.

```bash
uv run python -m tools.migrate_v3.rewrite_imports <target-path>
```

Log every file that was changed. After the rewriter completes, tell the user which files were rewritten and how many import rewrites were applied.

**Test files are NOT exempt from this phase.** Deprecated import paths in tests must be updated just like production code — they are purely mechanical path changes. The constraint that applies to test files is that you must NEVER modify test logic, assertions, fixtures, or test data in any phase.

---

## Phase 1b — Automated structural codemods

Before any AI-assisted structural work, run the codemod pipeline. This eliminates the mechanical transforms (decorator removal, signature rewrites, activity call rewrites, activities plumbing cleanup, entry point rewrite) deterministically, so the AI only needs to handle what remains.

```bash
uv run python -m tools.migrate_v3.run_codemods <target-path>
```

Review the output: files changed, any `SKIPPED` entries (dynamic dispatch or complex entry points that need manual attention), and errors.

Then re-run the checker to see what FAILs remain — those are the AI's work scope for Phase 2b:

```bash
uv run python -m tools.migrate_v3.check_migration --no-color <target-path>
```

Log a short summary of what the codemods changed and what FAILs remain.

**Extract structured context for Phase 2b** — before writing any AI-assisted structural changes, run the context extractor to get a compact summary of what needs to be done. Use this summary when prompting yourself for Phase 2b instead of re-reading raw source files:

```bash
uv run python -m tools.migrate_v3.extract_context <target-path>
```

The output lists: connector type and confidence, difficulty estimate, classes with their roles and method inventories, infrastructure usage patterns, and any warnings about complex entry points or dynamic dispatch. Include this summary at the start of your Phase 2b analysis.

---

## Phase 2 — Structural migration

Read the checker output from Phase 0 and the structure of the connector code to determine what structural work is needed.

### 2a — Identify connector type

Examine the source files in the target path (exclude test files from this analysis):

- Look for classes inheriting from `BaseSQLMetadataExtractionWorkflow` / `BaseSQLMetadataExtractionActivities` → SQL metadata extractor (§2a of MIGRATION_PROMPT.md)
- Look for classes inheriting from `SQLQueryExtractionWorkflow` / `SQLQueryExtractionActivities` → SQL query extractor (§2b)
- Look for classes inheriting from `IncrementalSQLMetadataExtractionWorkflow` → Incremental SQL extractor (§2c)
- Look for HTTP/REST client usage (`httpx`, `aiohttp`, `requests`, or custom `BaseClient` subclasses) with no SQL queries → REST/HTTP metadata extractor (§2d)
- Look for any other `WorkflowInterface` / `ActivitiesInterface` subclasses that don't fit above → Custom App (§3)
- In all cases: identify the handler class (§4) and the entry point (§5)

> **Tip — auto-detect the connector type:**
> ```bash
> uv run python -m tools.migrate_v3.check_migration --classify <target-path>
> ```
> This runs the F3 fingerprinter before the check pass and prints the detected
> connector type with confidence score and evidence.

### 2b — Apply structural changes

> **Incremental validation**: Run `check_migration` after each sub-step below.
> The checker output shows specific remaining FAILs — use it to guide the next step.

Follow the exact checklists in `tools/migrate_v3/MIGRATION_PROMPT.md` for the connector type(s) identified above.

**Hard constraint — tests are completely out of bounds for structural changes:**

- You MUST NOT modify test method bodies, assertions, fixtures, mock setup, or test data in any file under any directory whose name contains `test` or starts with `test_`.
- You MUST NOT add, remove, or rewrite test cases.
- You MUST NOT change the logic of any existing test.
- The only change permitted in test files is the mechanical import rewrite already performed in Phase 1. If a test file needs structural changes to compile (e.g. it directly instantiates a v2 class that no longer exists), add a `# TODO(v3-migration): update test to use v3 API` comment and leave the test body unchanged. The user will update tests manually after verifying the migration is correct.

**Hard constraint — handler method signatures:**

- Handler methods (`test_auth`, `preflight_check`, `fetch_metadata`) MUST use typed contract parameters. Do NOT use `*args` or `**kwargs`.
- Correct: `async def test_auth(self, input: AuthInput) -> AuthOutput:`
- Forbidden: `async def test_auth(self, *args, **kwargs):`
- The checker will FAIL if `*args`/`**kwargs` appear in a Handler subclass method.

**Constraint — `allow_unbounded_fields=True` must be used sparingly:**

- Needed on top-level `run()` input models that receive arbitrary dicts from AE/Heracles (e.g. `connection: dict[str, Any]`, `metadata: dict[str, Any]`).
- Must NOT be used on inter-task Input/Output contracts — use `Annotated[list[T], MaxItems(N)]` or `FileReference` instead.
- The checker will WARN if `allow_unbounded_fields=True` appears on task-level contracts.

### v3 Mandatory Patterns — All Connectors

> **These patterns were discovered during real v3 migrations (MSSQL, Postgres, SAP HANA, dbt). Every one was a production failure. They apply to ALL connector types — SQL, REST, and custom.**
>
> Apply each pattern during the relevant step below. After applying, run `check_migration` to verify.

#### Pattern 1: App Registration + Name

```python
from typing import ClassVar

class MyExtractor(SqlMetadataExtractor):
    name: ClassVar[str] = "my-connector-name"       # matches pyproject.toml
    _app_registered: ClassVar[bool] = False          # MANDATORY — force re-registration
```

**Without `_app_registered = False`:** The SDK's `__init_subclass__` uses `getattr(cls, "_app_registered", False)` which traverses the MRO, finds the parent's `True`, and skips registration entirely. Temporal runs the base `SqlMetadataExtractor.run()` instead of yours. You get `NotImplementedError: SqlMetadataExtractor must implement fetch_tables()` with no useful error pointing to the real cause.

**Without `name`:** The workflow type registered with Temporal won't match what marketplace-packages expects. On tenant, workflow starts fail with "Workflow class X is not registered on this worker".

#### Pattern 2: Custom Input Types with Explicit Credentials

```python
from pydantic import Field
from application_sdk.templates.contracts.sql_metadata import (
    ExtractionInput, ExtractionTaskInput,
)

class MyExtractionInput(ExtractionInput, allow_unbounded_fields=True):
    """Top-level run() input — carries inline credentials through serialization."""
    credentials: dict[str, Any] = Field(default_factory=dict)

class MyTaskInput(ExtractionTaskInput, allow_unbounded_fields=True):
    """Per-task input with credentials + transform fields."""
    credentials: dict[str, Any] = Field(default_factory=dict)
    typename: str = ""                                    # for transform_data
    file_names: list[str] = Field(default_factory=list)   # for transform_data
    chunk_start: int = 0                                  # for transform_data
```

**Why `allow_unbounded_fields=True` is NOT enough:** It lets extra fields through Pydantic serialization, but does NOT make them accessible as attributes. `input.typename` raises `AttributeError` unless declared on the class.

**Why credentials must be explicit:** `ExtractionInput` uses Pydantic `extra='ignore'` — inline credentials from `/start` are silently dropped. Temporal creates fresh instances per activity, so instance variables don't carry.

**Naming:** Use `credentials` (plural) with `Field(default_factory=dict)` — matches MSSQL/SAP HANA pattern and Pydantic best practice for mutable defaults.

#### Pattern 3: Multi-Source `_create_client`

```python
async def _create_client(self, input: Any) -> MyClient:
    client = MyClient()
    if input.credential_ref:
        creds = await self.context.resolve_credential_raw(input.credential_ref)
        await client.load(creds)
    elif input.credential_guid:
        from application_sdk.infrastructure.secrets import SecretStore
        creds = await SecretStore.get_credentials(input.credential_guid)
        await client.load(creds)
    elif hasattr(input, "credentials") and input.credentials:
        await client.load(input.credentials)
    else:
        raise ValueError("No credential source provided (credential_ref, credential_guid, or credentials)")
    return client
```

The three sources, in priority order:
1. `credential_ref` — v3 production (CredentialRef object)
2. `credential_guid` — legacy secret store lookup (use `SecretStore.get_credentials` which merges state + secret stores)
3. `credentials` — inline dict from `/start` (local dev)

**Critical for split credentials:** Some connectors (SAP HANA, JDBC connectors) store host/port in the state store and username/password in the secret store. `self.context.get_secret(guid)` only returns the secret part — no host. `SecretStore.get_credentials(guid)` merges both stores. Use `get_credentials` for the `credential_guid` path. The dbt pattern (`get_secret`) only works for connectors with unified credentials.

**WARNING — `resolve_credential_raw()` has a known SDK bug:** `_resolve_legacy_raw` passes `{"credential_guid": guid}` (a dict) to `SecretStore.get_credentials()` which expects a string. This means the `credential_ref` path can fail silently. Until this is fixed in the SDK, use `credential_guid` → `SecretStore.get_credentials(string)` as the primary production path, and `credential_ref` as a future/fallback path only.

**Nested tenant credential format:** On tenant, credentials from the secret store may be nested: `{"authType": "basic", "host": "...", "basic": {"username": "...", "password": "..."}}`. Your `client.load()` must detect and normalize this — check if `username` is at top level; if not, merge `credentials[auth_type]` to top level.

#### Pattern 4: Client Cleanup with try/finally

**EVERY `_create_client` call MUST have a matching `await sql_client.close()` in a `finally` block.** No exceptions.

```python
@task(timeout_seconds=1800, heartbeat_timeout_seconds=300)
async def fetch_schemas(self, input: MyTaskInput) -> FetchSchemasOutput:
    sql_client = await self._create_client(input)
    try:
        # ... all extraction logic ...
        return FetchSchemasOutput(...)
    finally:
        await sql_client.close()
```

**Without cleanup:** Each activity creates a SQLAlchemy engine with a connection pool. 5 parallel activities = 5 leaked pools per workflow. On continuous runs, this exhausts `max_connections` on the database server.

This applies to ALL `@task` methods AND all handler methods (`test_auth`, `preflight_check`, `fetch_metadata`).

#### Pattern 5: Output Path Computation in run()

```python
async def run(self, input: MyExtractionInput) -> ExtractionOutput:
    from application_sdk.constants import APPLICATION_NAME, TEMPORARY_PATH
    import temporalio.workflow

    wf_info = temporalio.workflow.info()
    wf_id = wf_info.workflow_id
    run_id = wf_info.run_id

    output_path = input.output_path
    if not output_path:
        output_path = os.path.join(
            TEMPORARY_PATH,
            f"artifacts/apps/{APPLICATION_NAME}/workflows/{wf_id or 'local'}/{run_id}",
        )
    output_prefix = input.output_prefix or TEMPORARY_PATH
```

**MUST include both `wf_id` AND `run_id`** — without `run_id`, multiple runs of the same workflow overwrite each other's output on S3.

Do NOT call `build_output_path()` from `run()` — it requires Temporal activity context (`activity.info().workflow_id`) which doesn't exist in the workflow context.

Do NOT use `self.context.workflow_id` — `AppContext` has no `workflow_id` attribute. Use `temporalio.workflow.info().workflow_id`.

#### Pattern 6: Pass Credentials in task_kwargs

```python
task_kwargs: dict[str, Any] = dict(
    workflow_id=wf_id,
    connection=input.connection,
    credential_guid=input.credential_guid,
    credential_ref=cred_ref,
    output_prefix=output_prefix,
    output_path=output_path,
    exclude_filter=input.exclude_filter,
    include_filter=input.include_filter,
    temp_table_regex=input.temp_table_regex,
    source_tag_prefix=input.source_tag_prefix,
    credentials=input.credentials,         # <-- MUST include this
)
```

Without `credentials` in `task_kwargs`, Temporal creates fresh `MyTaskInput` instances per activity with `credentials={}`. All three credential sources will be empty → `ValueError: No credential source provided`.

Also convert legacy `credential_guid` to `credential_ref` in `run()`:
```python
from application_sdk.templates.contracts.sql_metadata import legacy_credential_ref
cred_ref = input.credential_ref or legacy_credential_ref(input.credential_guid)
```

#### Pattern 7: TaskStatistics is a dataclass

`ParquetFileWriter.close()` returns `TaskStatistics` — a `@dataclass`, NOT a Pydantic model.

```python
# WRONG — crashes with AttributeError
stats.model_copy(update={"typename": typename})

# CORRECT
from dataclasses import replace
replace(stats, typename=typename)
```

---

### SQL Connector Additional Patterns

> The patterns above (1-7, 10-12, 14, 16) apply to **all** v3 connectors. The patterns below are specific to **SQL metadata extractors** that use `SqlMetadataExtractor`, `QueryBasedTransformer`, and parquet-based extraction pipelines.

#### Pattern 8: Implement transform_data Explicitly (SQL)

The base template has `transform_data` as a `NotImplementedError` stub. In v2 it ran automatically. In v3 you must implement and call it explicitly.

**CRITICAL:** Do NOT use `ParquetFileReader` with daft's multi-file read (`daft.read_parquet(all_files)`). When early parquet chunks have all-null columns (type `null`) and later chunks have `string`-typed columns, daft silently drops all data. Read files ONE AT A TIME.

```python
@task(timeout_seconds=1800, heartbeat_timeout_seconds=300)
async def transform_data(self, input: MyTaskInput) -> TransformOutput:
    import daft
    from application_sdk.io.json import JsonFileWriter
    from application_sdk.io.parquet import PARQUET_FILE_EXTENSION, download_files
    from application_sdk.io.utils import is_empty_dataframe
    from app.transformers.query import MyTransformer

    typename = input.typename
    raw_dir = os.path.join(input.output_path, "raw", typename)

    parquet_files = await download_files(raw_dir, PARQUET_FILE_EXTENSION, None)
    transformed_output = JsonFileWriter(
        path=os.path.join(input.output_path, "transformed"),
        typename=typename,
        chunk_start=input.chunk_start,
        dataframe_type=DataframeType.daft,
    )

    connection = input.connection
    transformer = MyTransformer(connector_name="my-connector", tenant_id="default")

    # ConnectionRef uses NESTED attributes — see Pattern 12
    connection_name = connection.attributes.name if connection else None
    connection_qn = connection.attributes.qualified_name if connection else None

    transform_kwargs: dict[str, Any] = {
        "workflow_id": input.workflow_id,
        "workflow_run_id": input.workflow_id or "",
        "connection_name": connection_name,
        "connection_qualified_name": connection_qn,
        "output_path": input.output_path,
        "output_prefix": input.output_prefix,
    }

    for pq_file in parquet_files:
        dataframe = daft.read_parquet(pq_file)
        if not is_empty_dataframe(dataframe):
            transformed = transformer.transform_metadata(
                typename=typename,
                dataframe=dataframe,
                **transform_kwargs,
            )
            if transformed is not None:
                await transformed_output.write(transformed)

    stats = await transformed_output.close()
    return TransformOutput(
        typename=typename,
        total_record_count=stats.total_record_count,
        chunk_count=stats.chunk_count,
    )
```

**Common errors:**
- `is_empty_dataframe` is in `application_sdk.io.utils`, NOT `application_sdk.common.utils`
- `transform_metadata()` requires `typename` and `workflow_run_id` as positional args — not just kwargs
- `connection_qualified_name` must be a non-empty string — if None, transformed JSONL has wrong qualifiedNames on all lineage references

#### Pattern 8b: SafeParquetFileWriter for Extraction (SQL)

When pandas writes an all-null column to parquet, pyarrow infers the type as `null` instead of `string`. Later, when daft reads multiple parquet files, it uses `null` for ALL rows — silently dropping data. Create `app/io.py`:

```python
from application_sdk.io.parquet import ParquetFileWriter
import pyarrow as pa
import pyarrow.parquet as pq

class SafeParquetFileWriter(ParquetFileWriter):
    async def _write_chunk(self, chunk, file_name):
        table = pa.Table.from_pandas(chunk, preserve_index=False)
        null_cols = [f.name for f in table.schema if pa.types.is_null(f.type)]
        if null_cols:
            new_schema = pa.schema([
                pa.field(f.name, pa.large_string()) if pa.types.is_null(f.type) else f
                for f in table.schema
            ])
            table = table.cast(new_schema)
        pq.write_table(table, file_name, compression="snappy")
```

Use `SafeParquetFileWriter` everywhere instead of `ParquetFileWriter` in extraction tasks.

#### Cleanup: Remove dead resolve_credentials task

v2 connectors often have a `resolve_credentials` activity/task that looks up credentials from the secret store. In v3, credential resolution is handled by `_create_client()` inside each task. If the v2 code has a `resolve_credentials` method that's now a no-op (returns empty output), delete it entirely during migration. Don't leave dead tasks registered with Temporal.

#### Pattern 9: Wire Transform into run() (SQL)

After the fetch `asyncio.gather`, add transform:

```python
# Fetch all entity types in parallel
(db_result, schema_result, ...) = await asyncio.gather(
    self.fetch_databases(MyTaskInput(**task_kwargs)),
    self.fetch_schemas(MyTaskInput(**task_kwargs)),
    ...
)

# Transform all entity types in parallel
entity_types = ["database", "schema", "table", "column", "procedure"]
await asyncio.gather(
    *(self.transform_data(MyTaskInput(**{**task_kwargs, "typename": t}))
      for t in entity_types)
)
```

#### Pattern 10: Handler Cleanup

Every handler method must use `try/finally`:

```python
async def test_auth(self, input: AuthInput) -> AuthOutput:
    client: MyClient | None = None
    try:
        client = await self._build_client(input)
        return AuthOutput(status=AuthStatus.SUCCESS)
    except Exception as e:
        return AuthOutput(status=AuthStatus.FAILED, message=str(e))
    finally:
        if client:
            await client.close()
```

#### Pattern 11: Set heartbeat_timeout_seconds on @task

**ALWAYS set `heartbeat_timeout_seconds=300` on every `@task` decorator.** The v3 `@task` defaults to 60s, which is too short for SQL queries against remote databases. The env var `ATLAN_HEARTBEAT_TIMEOUT_SECONDS` is ignored by v3 `@task` decorators.

```python
# WRONG — uses default 60s, will timeout on slow queries
@task(timeout_seconds=1800)

# CORRECT — 300s matches SDK's SQL extraction workflows
@task(timeout_seconds=1800, heartbeat_timeout_seconds=300)
```

#### Pattern 12: ConnectionRef Uses Nested Attributes

```python
# WRONG — AttributeError at runtime
connection_name = connection.connection_name
connection_qn = connection.connection_qualified_name

# CORRECT — SDK ConnectionRef model nests under .attributes
connection_name = connection.attributes.name
connection_qn = connection.attributes.qualified_name
```

V2 connectors used a flat dict. V3 uses the SDK's `ConnectionRef` model which mirrors the Atlas wire shape: `{ typeName: "Connection", attributes: { name, qualifiedName } }`. Pyright won't catch this because the old attribute names don't exist and resolve as `Any`.

#### Pattern 13: URL-Encode ALL Connection String Components (SQL)

```python
from urllib.parse import quote

# WRONG — only encodes username/password
f"postgresql+psycopg://{quote(user)}:{quote(pass)}@{host}:{port}/{database}"

# CORRECT — encode all 5 components
f"driver://{quote(user, safe='')}:{quote(pass, safe='')}@{quote(host, safe='')}:{quote(str(port), safe='')}/{quote(database, safe='')}"
```

Also validate `sslmode` against an allowlist — don't interpolate it raw:
```python
valid_sslmodes = {"disable", "allow", "prefer", "require", "verify-ca", "verify-full"}
if sslmode and sslmode not in valid_sslmodes:
    raise ValueError(f"Invalid sslmode: {sslmode!r}")
```

#### Pattern 14: Auth Validation Before try/except

```python
# WRONG — ValueError caught by generic handler, re-raised as "Connection failed"
try:
    if auth_type != AuthType.BASIC.value:
        raise ValueError(f"Unsupported auth type: {auth_type}")
    ...
except Exception as e:
    raise ValueError(f"Connection failed to {host}") from e

# CORRECT — validation errors escape cleanly
if auth_type != AuthType.BASIC.value:
    raise ValueError(f"Unsupported auth type: {auth_type}")
try:
    ...
except Exception as e:
    raise ValueError(f"Connection failed to {host}") from e
```

#### Pattern 15: YAML Templates Must Match Assets List 1:1 (SQL)

The transformer's `assets` list and YAML files in `sql_query_templates/` must correspond exactly:

```python
# In transformers/query/__init__.py
assets = ["TABLE", "COLUMN", "DATABASE", "SCHEMA", "PROCEDURE"]
```

Requires: `table.yaml`, `column.yaml`, `database.yaml`, `schema.yaml`, `procedure.yaml`.

**Common mistake:** Copying v2 assets list which uses `"EXTRAS-PROCEDURE"` and `"FUNCTION"`. V3 expects `"PROCEDURE"` — no prefix, no standalone function type. There is NO startup validation — the mismatch is caught only at transform time: `"No SQL transformation registered for PROCEDURE"`.

#### Pattern 16: Recreate Writer Inside Retry Loop

```python
# WRONG — partial writes persist, causing duplicate rows
parquet_output = SafeParquetFileWriter(...)
while attempt < max_retries and not success:
    attempt += 1
    try: ...

# CORRECT — fresh writer per attempt
while attempt < max_retries and not success:
    attempt += 1
    parquet_output = SafeParquetFileWriter(...)  # fresh on each attempt
    try: ...
```

#### Pattern 17: Frontend Form Validation

`required: true` in JSON Schema means "key must be present" — NOT "value must be non-empty". Always add:
- `minLength: 1` on required string fields (host, username, password, database)
- `minimum: 1, maximum: 65535` on port
- `pattern: "^[^\\s/:]+$"` on host (no spaces, slashes, protocol prefixes)
- `maxLength` on free-text inputs (especially regex fields — prevents ReDoS)

---

Apply changes in this order:

1. **App class** — merge Workflow + Activities into the appropriate template subclass with `@task` methods. Preserve all SQL query strings and business logic verbatim. **Apply universal Patterns 1-7, 11, 12, 16 during this step. For SQL connectors, also apply SQL Patterns 8, 8b, 9.**

   **Non-standard DBAPI drivers:** If the connector uses a driver that returns plain PEP 249 tuples from `cursor.description` (e.g. SAP HANA hdbcli returns `(name, type_code, ...)` instead of named tuples with `.name`), the SDK's `BaseSQLClient.run_query()` will crash with `AttributeError: 'tuple' object has no attribute 'name'`. Override `run_query` in the client with a helper that handles both formats:
   ```python
   def _col_name(self, desc_item) -> str:
       return desc_item.name if hasattr(desc_item, "name") else desc_item[0]
   ```

   > After completing this step, run:
   > ```bash
   > uv run python -m tools.migrate_v3.check_migration --no-color <target-path>
   > ```
   > Review any new/resolved FAILs (especially `no-v2-decorators`, `no-execute-activity-method`) before proceeding.

2. **Handler** — update base class, method signatures (typed contracts, no `**kwargs`), remove `load()`. **Apply universal Patterns 10 and 14. For SQL connectors, also apply Pattern 13.**

   **`fetch_metadata` must return `MetadataObject` instances:** The handler's `fetch_metadata` must return `MetadataOutput(objects=[MetadataObject(name=..., ...)])`. Returning raw SQL dicts (`{TABLE_CATALOG, TABLE_SCHEMA}`) fails Pydantic validation — the `except Exception` block in the SDK swallows the error silently and returns 0 objects. This is extremely hard to debug because there's no error log.

   **Preflight checks must populate `checks` list:** Return `PreflightOutput(status="ready", checks=[PreflightCheck(name="...", passed=True, message="...")])`. Do NOT return empty `checks: []` — the playground renders each empty check as failed. If using custom `checks` in the pkl contract's `SageV2` widget, the `PreflightCheck.name` must exactly match the contract's `Dynamic { name = "..." }`. Safe default: omit custom checks from the contract and let the playground render from the API response generically.

   > After completing this step, run:
   > ```bash
   > uv run python -m tools.migrate_v3.check_migration --no-color <target-path>
   > ```
   > Review any new/resolved FAILs (especially `handler-typed-signatures`) before proceeding.

3. **Entry point** — replace `BaseXxxApplication` instantiation with `run_dev_combined()` or CLI reference.

   > After completing this step, run:
   > ```bash
   > uv run python -m tools.migrate_v3.check_migration --no-color <target-path>
   > ```
   > Review any new/resolved FAILs (especially `no-base-application`) before proceeding.

4. **Infrastructure calls** — replace `SecretStore`/`StateStore`/`ObjectStore` calls with `self.context.*` per §6 of MIGRATION_PROMPT.md.

   > After completing this step, run:
   > ```bash
   > uv run python -m tools.migrate_v3.check_migration --no-color <target-path>
   > ```
   > Review any new/resolved FAILs (especially `no-dapr-client`, `use-app-state`) before proceeding.

Work through one section at a time. After completing each section, check your changes are self-consistent before moving on.

### 2c — Directory consolidation

After completing the structural migration in 2b, consolidate the v2 directory layout. v2 connectors split logic across `app/activities/` and `app/workflows/`; v3 uses a single flat file.

1. Identify the main App class file (typically `app/activities/<name>.py`).
2. Move it to `app/<app_name>.py` (derive the filename from the App class or connector name, snake_cased).
3. If `app/workflows/<name>.py` exists and only re-exports from activities (e.g. `from app.activities.<name> import MyConnector`), delete it.
4. Delete the now-empty `app/activities/` and `app/workflows/` directories.
5. Update all **production-code** imports that referenced the old paths.
6. For **test-file** imports pointing to the old paths:
   a. Construct a JSON mapping of old module paths → new module paths from steps 1–4. For example, if `app/activities/metadata_extraction.py` moved to `app/metadata_extraction.py`, the mapping is `{"app.activities.metadata_extraction": "app.metadata_extraction"}`.
   b. Run the internal import rewriter on the test directory:
      ```bash
      uv run python -m tools.migrate_v3.rewrite_imports \
        --internal-map '{"app.activities.<name>": "app.<name>"}' \
        <target-path>/tests/
      ```
   c. For any **symbol names** that also changed (e.g. `AnaplanMetadataExtractionActivities` → `AnaplanApp`), manually update the import line's symbol name in each affected test file AND add a comment at the top of that file: `# TODO(v3-migration): update references from OldClass to NewClass in test bodies`. Do NOT modify test bodies, assertions, fixtures, or mocks.
7. Re-run the checker to confirm the `no-v2-directory-structure` advisory is gone.

If the connector does not have an `activities/` or `workflows/` directory, skip this step.

---

## Phase 2d — Post-processing cleanup

After completing the structural changes in 2b and directory consolidation in 2c, run these cleanup steps to normalize imports and formatting before the validation loop:

```bash
# Remove unused imports and sort import order
uv run ruff check --fix --select I,F401 <target-path>

# Normalize formatting
uv run ruff format <target-path>

# Check migration status
uv run python -m tools.migrate_v3.check_migration --no-color <target-path>
```

All FAIL checks should pass at this point. If any remain, address them before moving to Phase 3.

---

## Phase 3 — Validation loop

Run the checker after completing Phase 2:

```bash
uv run python -m tools.migrate_v3.check_migration --no-color <target-path>
```

**If FAILs remain:**
- Read each failing item and the relevant source file.
- Fix the specific issue according to MIGRATION_PROMPT.md.
- Re-run the checker.
- Repeat until zero FAILs. Do not move to Phase 4 until the checker exits with code 0.

**If only WARNs remain:**
- Read each WARN item. If it is fixable without modifying test logic, fix it.
- If a WARN requires modifying test logic, skip it and add it to the manual follow-up list.

---

## Phase 4 — Test run

Run the connector's test suite **without modifying any test files**:

```bash
cd <target-path> && uv run pytest --tb=short -q 2>&1 | head -80
```

If `uv` is not available in the connector repo, try `python -m pytest --tb=short -q` instead.

Do **not** modify any test to make it pass. If tests fail:
- Read the failing test and the code it exercises.
- If the failure is due to a production code issue introduced during migration (e.g. wrong method signature, missing attribute), fix the production code.
- If the failure requires understanding test intent or rewriting test logic, do NOT fix it. Add it to the manual follow-up list.

### Phase 4b — E2E test generation

After the test suite run, check whether the connector has e2e tests using the v2 `BaseTest` / `TestInterface` pattern:

1. Search for files under `tests/e2e/` (or `tests/integration/`) that import `BaseTest` or `TestInterface`.
2. If found, **read the original v2 e2e test file completely**. List every test method and what it asserts before writing a single line of the new file.
3. Generate a **new** equivalent e2e test file using the v3 `application_sdk.testing.e2e` API (§9 of MIGRATION_PROMPT.md):
   - For **each** test method in the original, generate a corresponding `async def test_xxx(deployed_app)` function. The generated file MUST have at least as many test functions as the original has test methods.
   - Extract actual payload values from the original (hardcoded dicts, `default_payload()` bodies, connection IDs) — do **not** substitute placeholder values like `"test-connection"` if the original has real values.
   - If an assertion checks response fields whose format changed (e.g. `result['authenticationCheck']`), keep the assertion but add `# TODO(v3-migration): response format changed — update field names`.
   - Use the `AppConfig` fixture with real values derived from the connector's `pyproject.toml` (`name`, `tool.poetry.name`, or Helm chart values) — not generic placeholders.
4. Place the new file alongside the original, named `tests/e2e/test_<connector_name>_v3.py`.
5. Add `# TODO(v3-migration): human must validate this test is equivalent to the original` at the top of the new file.
6. Do NOT delete or modify the original test file.
7. Add the new test file to the manual follow-up list so the user knows to validate it.

If the connector has no v2-style e2e tests, skip this step.

---

## Phase 5 — Contract setup

> **This phase delegates to the `/contract` skill** (`connector-os/skills/contract/SKILL.md`) which uses the `app-contract-toolkit` (`atlanhq/app-contract-toolkit`) as its source of truth. The toolkit repo has the authoritative docs, examples, and pkl modules. The skill orchestrates the workflow. Do NOT duplicate their logic here.

### 5a — Run the /contract skill

Run the `/contract` skill which handles the full contract lifecycle — create, migrate, or update:
```
/contract <connector-name>
```

The skill reads the `app-contract-toolkit` repo (must be cloned as a sibling directory) for pkl modules, examples (Teradata, Redshift, Trino), and generation commands. It produces: `contract/app.pkl`, and generated artifacts (`{name}.json`, `atlan-connectors-{name}.json`, `manifest.json`, `_input.py`) copied into `app/generated/`.

If the `/contract` skill is not available, follow the `app-contract-toolkit` README directly:
1. Clone `atlanhq/app-contract-toolkit` as a sibling directory
2. Create `contract/app.pkl` following the toolkit's examples
3. Run `pkl project resolve && pkl eval -m contract/generated contract/app.pkl`
4. Copy artifacts to `app/generated/`

### 5b — Post-contract verification (migration-specific)

After the contract artifacts are generated, verify these migration-specific items:

1. **`app/generated/` must be a REAL directory** (not a symlink — symlinks break in Docker because the symlink target doesn't exist inside the container). The `/contract` skill should handle this, but verify.

2. **Pre-commit exclusions** for generated files in `.pre-commit-config.yaml`:
   ```yaml
   - id: ruff
     exclude: app/generated/
   - id: ruff-format
     exclude: app/generated/
   - id: pyright
     exclude: app/generated/
   ```

3. **Remove legacy directories:**
   - `app/templates/` with old configmap JSONs → delete (now served from `app/generated/`)
   - `frontend/` with `static/` and `templates/` → dead code in v3. Playground is installed via `npx --yes @atlanhq/app-playground@latest install-to app/generated/frontend/static`
   - `get_configmap()` handler overrides → remove (SDK serves configmaps from `app/generated/` automatically)

4. **`ATLAN_CONTRACT_GENERATED_DIR=/app/app/generated`** must be set in the Dockerfile.

**Gotcha — contract workflowType vs marketplace-packages:** The `workflowType` in `contract/app.pkl` generates `manifest.json` for the **new Automation Engine flow**. If the tenant still uses the **Argo/interim flow** (marketplace-packages templates), the manifest's `workflowType` is irrelevant — the Argo template's `workflow-type` parameter is what matters. Don't waste time aligning contract workflowType with the Argo template; focus on `marketplace-packages` alignment in Phase 8b instead.

**Gotcha — pkl SSL errors:** Corporate proxies (Netskope) intercept HTTPS and re-sign with their own CA. pkl's embedded cert store doesn't trust it. Fix: build a CA bundle with `openssl s_client` to capture the live proxy chain, then set `SSL_CERT_FILE` before running pkl.

---

## Phase 6 — Summary report

Print a structured summary:

```
## v3 Migration Summary

### Target
<path>

### Phase 1 — Import rewrites
- Files modified: N
- Imports rewritten: N
- Files with structural TODO comments: N

### Phase 2 — Structural changes
<bullet per file changed: what was changed>

### Phase 2c — Directory consolidation
- Activities/workflows dirs found: yes/no
- Files moved: <list>
- Directories deleted: <list>
- Import references updated: N files

### Phase 3 — Checker result
- FAIL items resolved: N
- WARN items resolved: N
- WARN items remaining (manual): <list>

### Phase 4 — Test results
- Tests passing: N/N
- Tests failing (manual follow-up): <list of test names and reason>

### Phase 4b — E2E test generation
- v2 BaseTest files found: yes/no
- New v3 e2e test file generated: <path or "N/A">
- Original test method count: N
- Generated test function count: N
- Human validation required: yes/no

### Phase 5 — Contract setup
- pkl contract found: yes/no
- Artifacts generated: <list of files>
- app/generated/ created: yes/no (real directory, not symlink)
- Pre-commit exclusions added: yes/no
- Legacy directories removed: <list or "none">

### API Contract Changes (inform frontend consumers)
<list any response-format-change WARNs from the checker — these indicate v3 handler
methods that return a different response shape than v2, which may break frontends>
- fetch_metadata: returns MetadataOutput (flat list) — was hierarchical [{value, title, children}]
- preflight_check: returns PreflightOutput — was {authenticationCheck, hostCheck, permissionsCheck}

### Manual follow-up required
<bulleted list of anything the AI skipped due to the test constraint or ambiguity>
```

Remind the user:
- Run `uv run pre-commit run --all-files` in the connector repo before committing.
- Review all `# TODO(v3-migration)` comments — each one marks a location that needs human verification.
- The typed `Input`/`Output` models for custom `@task` methods should be defined (see §7 of MIGRATION_PROMPT.md) — these were not auto-generated.
- If an e2e test was generated in Phase 4b, validate that it is logically equivalent to the original before deleting the old file.
- **Keep a build journey document** — record every error, root cause, and fix as you encounter them. This is the most valuable artifact for improving the migration skill and helping the next person. See `atlan-saphana-app/docs/BUILD_JOURNEY.md` as an example.
- **Test on a tenant early** — local dev hides credential resolution, Temporal connectivity, Dockerfile base image, and marketplace-packages integration issues. Every Phase 8 issue is invisible locally.

---

## Phase 7 — Live verification

This phase starts the app locally and verifies handler endpoints and workflow execution against a v2 baseline. **Ask the user for confirmation before each major step.**

### 7a — Establish v2 baseline

Before starting the v3 app, check for an existing v2 workflow run:

1. Look for output in `./local/dapr/objectstore/artifacts/apps/<app-name>/workflows/`:
   ```bash
   find ./local/dapr/objectstore/artifacts -name "*.json" -path "*/transformed/*" | head -20
   ```
2. If output exists, ask the user:
   > "Found existing workflow output at `<path>`. Was this from a v2 SDK run with full parity? Should I use it as the baseline for comparison?"
3. If no output exists, ask:
   > "No existing workflow output found. Have you run the v2 version of this connector locally? A v2 baseline is needed to verify parity after migration."
4. If the user confirms a baseline exists, count entities per type:
   ```bash
   for f in ./local/dapr/objectstore/artifacts/apps/<app-name>/workflows/<latest-run>/transformed/*/*.json; do
     echo "$(basename $(dirname $f)): $(wc -l < "$f") entities"
   done
   ```
   Record these counts — they are the parity target.

### 7b — Run tests

Run the full test suite before attempting a live run:

```bash
cd <target-path> && uv run pytest tests/unit/ --tb=short -q
cd <target-path> && uv run pytest tests/e2e/ --tb=short -q
```

If any tests fail, fix production code issues before proceeding. Do not continue to 6c with failing tests.

### 7c — Start the app

Ask the user:
> "Tests pass. Ready to start the app with `atlan app run`. This requires credentials in `.env`. Should I proceed?"

If confirmed:

```bash
cd <target-path> && atlan app run -p .
```

Wait for the app to be ready (look for `Uvicorn running on http://127.0.0.1:8000`).

### 7d — Test handler endpoints

Read credentials from `.env` (look for `API_KEY_ID`, `API_SECRET`, and any workspace/extra fields). Then test all three handler endpoints:

**Auth test:**
```bash
curl -s -X POST http://localhost:8000/workflows/v1/auth \
  -H "Content-Type: application/json" \
  -d '{
    "credentials": {
      "host": "<host>",
      "username": "<API_KEY_ID>",
      "password": "<API_SECRET>",
      "extra": {"workspace": "<workspace>"}
    }
  }'
```
Expected: `{"success": true, "data": {"status": "success"}}`

**Preflight check:**
```bash
curl -s -X POST http://localhost:8000/workflows/v1/check \
  -H "Content-Type: application/json" \
  -d '{
    "credentials": { ... },
    "metadata": {}
  }'
```
Expected: `{"success": true, "data": {"status": "ready", "checks": [...]}}`

**Metadata fetch:**
```bash
curl -s -X POST http://localhost:8000/workflows/v1/metadata \
  -H "Content-Type: application/json" \
  -d '{
    "credentials": { ... },
    "metadata": {}
  }'
```
Expected: `{"success": true, "data": {"objects": [...], "total_count": N}}`

**Config endpoints:**
```bash
# List configmaps
curl -s http://localhost:8000/workflows/v1/configmaps

# Get specific configmap
curl -s http://localhost:8000/workflows/v1/configmap/<id>

# Get manifest
curl -s http://localhost:8000/workflows/v1/manifest
```

If any endpoint returns 500, check the app terminal for the traceback. Common issues:
- Handler not discovered → import the handler class in the App module
- Credential format error → ensure `_normalize_credentials` is in the SDK version being used
- Missing configmap files → verify `app/generated/` has the JSON files from `poe generate`

### 7e — Start a workflow run

Ask the user:
> "Handler endpoints verified. Ready to trigger a workflow run. This will call external APIs and write output to `./local/dapr/objectstore/`. Should I proceed?"

If confirmed:
```bash
curl -s -X POST http://localhost:8000/workflows/v1/start \
  -H "Content-Type: application/json" \
  -d '{
    "credentials": { ... },
    "metadata": { <connector-specific metadata> },
    "connection": {"connection": "dev"}
  }'
```

Capture `workflow_id` and `run_id` from the response. Monitor status:
```bash
curl -s http://localhost:8000/workflows/v1/status/<workflow_id>/<run_id>
```

Poll until status is `COMPLETED` or `FAILED`. Also monitor Temporal UI at `http://localhost:8233`.

### 7f — Parity comparison

Once the workflow completes, locate the v3 output. The path follows this structure:
```
./local/dapr/objectstore/artifacts/apps/<app-name>/workflows/<workflow_id>/<run_id>/transformed/
```

Use the `workflow_id` and `run_id` from the start response to find it:
```bash
V3_OUTPUT="./local/dapr/objectstore/artifacts/apps/<app-name>/workflows/<workflow_id>/<run_id>/transformed"
```

Count entities per type in the v3 output:
```bash
for dir in "$V3_OUTPUT"/*/; do
  typename=$(basename "$dir")
  count=0
  for f in "$dir"*.json; do
    [ -f "$f" ] && count=$((count + $(wc -l < "$f")))
  done
  echo "$typename: $count"
done
```

If a v2 baseline exists (from step 6a), compare:

```
Entity type      | v2 count | v3 count | Match
-----------------|----------|----------|------
workspace        |        1 |        1 | ✓
collection       |       67 |       67 | ✓
report           |       68 |       30 | ✗ (-38)
```

### 7g — Fix-and-retry loop

If parity fails (counts differ, workflow errors, or endpoint failures):

1. **Diagnose.** Read the app terminal logs and Temporal UI (`http://localhost:8233`) for errors.

   **Error → Fix lookup table** (from MSSQL, Postgres, SAP HANA migrations):

   | Error Message | Root Cause | Fix |
   |---|---|---|
   | `SqlMetadataExtractor must implement fetch_*()` | `_app_registered` not set on subclass | Add `_app_registered: ClassVar[bool] = False` (Pattern 1) |
   | `Secret '' not found` or `No credential source provided` | Credentials not in task input | Custom input types + pass `credentials` in task_kwargs (Patterns 2, 6) |
   | `Output path must be specified` | v3 doesn't auto-compute | Compute default in `run()` with `wf_id` + `run_id` (Pattern 5) |
   | `'TaskStatistics' has no attribute 'model_copy'` | It's a dataclass, not Pydantic | Use `dataclasses.replace()` (Pattern 7) |
   | `is_empty_dataframe` import error | Wrong module path | Import from `application_sdk.io.utils` not `common.utils` |
   | `transform_metadata()` missing positional args | `typename` and `workflow_run_id` required | Pass as positional args (Pattern 8) |
   | No `transformed/` directory after success | Transform not wired into run() | Implement `transform_data` and call in `run()` (Patterns 8, 9) |
   | DB connections exhausted over time | No cleanup in finally block | Add `try/finally: await sql_client.close()` (Pattern 4) |
   | Descriptions/definitions `None` despite raw data | null-type parquet schema merge | Use `SafeParquetFileWriter` + per-file reads (Pattern 8b) |
   | `'ConnectionRef' has no attribute 'connection_name'` | Nested attributes model | Use `.attributes.name` / `.attributes.qualified_name` (Pattern 12) |
   | `heartbeat timeout` after 60s | Default too short, env var ignored | Set `heartbeat_timeout_seconds=300` (Pattern 11) |
   | `'AppContext' has no attribute 'workflow_id'` | Wrong API for workflow context | Use `temporalio.workflow.info().workflow_id` |
   | `KeyError: 'username'` with nested credentials | Tenant cred format is nested | Normalize nested format in `client.load()` (Pattern 3 note) |
   | `connection_qualified_name` is None | ConnectionRef nested attributes | Use `_resolve_connection_info()` fallback chain (Pattern 12) |
   | `'tuple' has no attribute 'name'` in run_query | Non-standard DBAPI cursor.description | Override `run_query` with `_col_name()` helper |
   | `No SQL transformation registered for X` | Asset name mismatch with YAML | Check assets list matches YAML files 1:1 (Pattern 15) |
   | Duplicate rows after transient failure | Stateful writer reused in retry loop | Recreate `SafeParquetFileWriter` per attempt (Pattern 16) |
   | `ModuleNotFoundError: No module named 'application_sdk.secrets'` | Importing v2 module path inside workflow sandbox | Use `_create_client` pattern; do NOT import v2 module paths in workflow code |
   | Metadata endpoint returns 0 objects (silently) | Raw dicts instead of MetadataObject | Return `MetadataObject(name=...)` instances |

   Common causes:
   - HTTP errors during extraction → check client retry logic, rate limiting, auth
   - Missing entities → check filtering logic, pagination, or API response parsing
   - Transform errors → check Parquet round-trip issues (nested dicts become JSON strings)
   - Empty output → check `output_path` computation and `output_prefix` defaults

2. **Fix.** Apply the fix to production code only. Do not modify tests.

3. **Re-run from 6b.** After fixing:
   - Re-run tests (6b) to confirm the fix doesn't break anything
   - Restart the app (6c) to pick up code changes
   - Re-test handler endpoints (6d) to confirm they still work
   - Trigger a new workflow run (6e)
   - Re-compare parity (6f)

4. **Repeat** until all entity counts match the v2 baseline and all endpoints return success.

Do not proceed to the summary until:
- All handler endpoints return success
- Workflow completes without errors
- Entity counts match the v2 baseline (or the user explicitly accepts the difference with an explanation)

### 7h — Print verification summary

Only print this after parity is achieved or the user accepts the result:

```
## Live Verification Summary

### Handler Endpoints
- POST /workflows/v1/auth:      ✓ success
- POST /workflows/v1/check:     ✓ ready (N checks passed)
- POST /workflows/v1/metadata:  ✓ N objects returned
- GET  /workflows/v1/configmaps: ✓ N configmaps
- GET  /workflows/v1/manifest:   ✓ served

### Workflow Execution
- Workflow ID: <id>
- Run ID: <id>
- Status: COMPLETED
- Duration: Ns
- Retry attempts: N (if fix-and-retry loop was needed)

### Parity
<entity count comparison table>
- Parity: ACHIEVED
- Fixes applied during retry loop: <list of fixes, or "none">
```

---

## Phase 8 — Tenant deployment readiness

Before deploying to a tenant, verify these items. Every issue in this checklist was discovered during a real tenant deployment (SAP HANA on `atlanp8p01`) and is invisible during local development.

### 8a — Dockerfile verification

Read the connector's `Dockerfile` and verify:

- [ ] Base image is `registry.atlan.com/public/app-runtime-base:refactor-v3-latest` — NOT `ghcr.io/atlanhq/application-sdk-main:2.x`
- [ ] `ATLAN_APP_MODULE` is set correctly (e.g. `app.my_extractor:MyExtractor`). Comma-separated for multi-app.
- [ ] `ATLAN_CONTRACT_GENERATED_DIR=/app/app/generated`
- [ ] No `CMD` — the v3 base image handles mode via `APPLICATION_MODE` env var set by Helm
- [ ] No `entrypoint.sh`, `supervisord.conf`, or `otel-config.yaml` — v3 base image handles all of these
- [ ] `COPY app/ app/` — only app code, NOT the entire repo
- [ ] `git` is installed (`RUN apk add --no-cache git`) for uv to fetch git-sourced dependencies

**Why base image matters:** The v2 base image's entrypoint runs `main.py` → `run_dev_combined` → defaults to `localhost:7233`. On tenant, Temporal runs at `temporal-cluster-internal-frontend-headless.temporal.svc.cluster.local:7236` (set via `ATLAN_WORKFLOW_HOST`), but the v2 entrypoint doesn't read it. The pod will crash-loop connecting to localhost.

### 8b — marketplace-packages alignment

If the connector uses an Argo WorkflowTemplate (in `marketplace-packages`) to trigger workflows:

- [ ] `workflow-type` in the Argo template matches the v3 `name` ClassVar (kebab-case), NOT the old v2 class name (e.g. `saphana-metadata-extractor`, not `SAPHanaMetadataExtractionWorkflow`)
- [ ] `workflow-arguments` JSON includes ALL required fields: `credential_guid`, `connection`, `exclude_filter`, `include_filter`, `workflow_id`

**Why this is critical:** `workflow-arguments` is the ENTIRE Temporal workflow input. If a field isn't in that JSON, the workflow doesn't get it. There's no magic injection of credentials, connection, or filters. Missing `credential_guid` → `No credential source provided`. Missing `connection` → transform outputs have wrong qualifiedNames.

### 8c — Credential format verification

Identify how this connector's credentials are stored:

- **Unified** (host + username + password all in secret store) → `self.context.get_secret(guid)` works (dbt pattern)
- **Split** (host/port in state store, username/password in secret store) → MUST use `SecretStore.get_credentials(guid)` which merges both stores

Also verify `client.load()` handles **nested tenant credential format**: `{"authType": "basic", "host": "...", "basic": {"username": "...", "password": "..."}}`. Detect and normalize: if `username` not at top level, merge `credentials[auth_type]` to top level.

### 8d — Output path verification

- [ ] Output path includes both `workflow_id` AND `run_id`: `artifacts/apps/{app}/workflows/{wf_id}/{run_id}`
- [ ] `transformed_data_prefix` is populated in the output contract (AE reads this via JSONPath)
- [ ] `connection_qualified_name` is populated in the output contract

### 8e — Print tenant deployment checklist

```
## Tenant Deployment Checklist
- [ ] Dockerfile uses v3 base image (registry.atlan.com/public/app-runtime-base:refactor-v3-latest)
- [ ] ATLAN_APP_MODULE set correctly
- [ ] ATLAN_CONTRACT_GENERATED_DIR=/app/app/generated
- [ ] marketplace-packages workflow-type matches name ClassVar
- [ ] marketplace-packages workflow-arguments includes all required fields
- [ ] Credential format handles nested tenant format
- [ ] Output path includes run_id
- [ ] pre-commit passes on all files
- [ ] Build and push Docker image to registry
- [ ] Deploy via FluxCD / HelmRelease / ArgoCD
```

---

## Known Gotchas — learned from real migrations

These are issues discovered during production migrations that are not covered by the automated checker or migration tooling. Read these before starting.

### Handler discovery

v3 discovers the `Handler` subclass by looking in the same module as the `App` class. If your handler is in a different module (e.g. `app/handlers/handler.py` while the App is in `app/app.py`), the SDK will use `DefaultHandler` silently.

**Fix (default approach):** Import the handler in your primary App module:
```python
# app/my_app.py
from app.handlers import MyHandler  # noqa: F401 — registers handler
```
This is the recommended approach. Alternatively, set `ATLAN_HANDLER_MODULE=app.handlers:MyHandler` in the Dockerfile.

### Handler credentials — v2 nested dict vs v3 list format

v3 handler endpoints receive credentials as `list[HandlerCredential]` (`[{key, value}]` pairs). Heracles and existing frontends send v2 nested dicts (`{host, username, password, extra: {workspace}}`). The SDK normalizes v2 format to v3 automatically in `service.py`, but your handler's `_build_client` helper must reconstruct the nested dict from `input.credentials`:

```python
async def _build_client(credentials: list[HandlerCredential]) -> MyClient:
    cred_dict = {}
    extra = {}
    for cred in credentials:
        if cred.key.startswith("extra."):
            extra[cred.key[len("extra."):]] = cred.value
        else:
            cred_dict[cred.key] = cred.value
    if extra:
        cred_dict["extra"] = extra
    client = MyClient()
    await client.load(credentials=cred_dict)
    return client
```

### Payload safety and `allow_unbounded_fields`

v3 enforces strict types on `Input`/`Output` contracts. `dict[str, Any]` fields will raise `PayloadSafetyError` at import time. For top-level input models that receive arbitrary config from AE/Heracles (connection dicts, metadata dicts), use:

```python
class MyExtractionInput(Input, allow_unbounded_fields=True):
    credential_guid: str = ""
    credentials: dict[str, Any] = {}
    connection: dict[str, Any] = {}
    metadata: dict[str, Any] = {}
```

For inter-task contracts, prefer typed fields with `MaxItems` or `FileReference`.

### Dockerfile must use v3 base image and pattern

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

### output_path and output_prefix must be computed

v3 does not auto-compute `output_path` like v2 did. If `input.output_path` is empty (which it is for local dev), compute it in your extract task:

```python
from application_sdk.constants import TEMPORARY_PATH
from application_sdk.common.utils import build_output_path

if not input.output_path:
    output_path = os.path.join(TEMPORARY_PATH, build_output_path())
```

Similarly, `output_prefix` must default to `TEMPORARY_PATH` for the `transformed_data_prefix` stripping logic to work correctly in the output contract.

### pre-commit must exclude app/generated/

The contract toolkit generates `_input.py` and other files in `app/generated/`. These contain auto-generated code that will fail ruff and pyright checks. Exclude the directory in `.pre-commit-config.yaml`:

```yaml
- id: ruff
  exclude: app/generated/
- id: ruff-format
  exclude: app/generated/
- id: pyright
  exclude: app/generated/
```

### Local dev — verify Dapr is actually being used

The v3 SDK checks `DAPR_HTTP_PORT` (not `ATLAN_DAPR_HTTP_PORT`) to detect the Dapr sidecar. If running via `atlan app run`, the CLI sets this automatically. If running manually (`uv run python main.py`), Dapr will be skipped and the SDK falls back to InMemory + LocalStore silently. This means misconfigured Dapr components won't be caught until production.

**Fix:** When running manually, export `DAPR_HTTP_PORT=3500` before starting the app, or use `atlan app run` which handles this.

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

### Credential resolution: split vs unified stores

`self.context.get_secret(guid)` only returns the secret store part (username, password). For connectors where host/port live in the **state store** (SAP HANA, some JDBC connectors), this returns an incomplete credential dict — no host. Use `SecretStore.get_credentials(guid)` which merges both state and secret stores. The dbt pattern (`get_secret`) only works for connectors with unified credentials (host + password all in the secret store).

```python
# For unified credentials (dbt pattern):
creds_json = await self.context.get_secret(credential_guid)
creds = json.loads(creds_json) if isinstance(creds_json, str) else creds_json

# For split credentials (SAP HANA, JDBC pattern):
from application_sdk.infrastructure.secrets import SecretStore
creds = await SecretStore.get_credentials(credential_guid)
```

### Nested tenant credential format

On tenant, credentials from the secret store may be nested: `{"authType": "basic", "host": "...", "basic": {"username": "...", "password": "..."}}`. Your `client.load()` must detect and normalize:
```python
if "username" not in creds and creds.get("authType"):
    auth_type = creds["authType"]
    if auth_type in creds and isinstance(creds[auth_type], dict):
        creds = {**creds, **creds[auth_type]}
```

### MetadataOutput expects MetadataObject instances

`fetch_metadata` handler must return `MetadataOutput(objects=[MetadataObject(name=..., ...)])`. Returning raw SQL dicts (`{TABLE_CATALOG, TABLE_SCHEMA}`) fails Pydantic validation — the `except Exception` block in the SDK swallows the error and returns 0 objects with no error log. Extremely hard to debug.

### Preflight check rendering in playground

Don't define custom `checks` in `Config.SageV2` contract unless the handler returns `PreflightCheck` objects with names that exactly match the contract's `Dynamic { name = "..." }`. Safe default: omit `checks` entirely and let the playground render from the API response generically. Always compare against a working example in `app-contract-toolkit/examples/`.

### App Playground not at localhost:8000

v3 handler service is API-only — no static file serving. Navigating to `http://localhost:8000` returns `{"detail":"Not Found"}`. The playground is installed via `npx --yes @atlanhq/app-playground@latest install-to app/generated/frontend/static` and served by the SDK from `app/generated/frontend/static`.

### workflow-arguments in marketplace-packages is the entire Temporal input

If a field isn't in the `workflow-arguments` JSON in the Argo template, the workflow doesn't get it. This is the bridge between the Argo world and the Temporal world. No magic injection. Missing `credential_guid` → `No credential source provided`. Missing `connection` → transform outputs have wrong qualifiedNames.

### Non-standard DBAPI cursor.description (SQL connectors)

Some drivers (SAP HANA hdbcli) return plain PEP 249 tuples `(name, type_code, ...)` from `cursor.description` instead of named tuples with `.name`. The SDK's `BaseSQLClient.run_query()` crashes with `AttributeError: 'tuple' object has no attribute 'name'`. Override `run_query` with a helper that handles both formats.

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
