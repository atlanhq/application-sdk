# Proposal: One-Prompt Connectors

## Problem

The [AI-native SDK plan](ai-native-sdk-plan.md) targets manifest-driven apps where a Postgres extractor is ~20 lines of YAML. That plan works well for simple connectors — single auth method, 4-6 entity types, one extraction path. But production connectors are a different beast.

Atlan supports **93 connectors** across **15 categories**. They fall into 6 extraction patterns:

| Pattern | How it extracts | Examples | Count |
|---------|----------------|----------|-------|
| **SQL Catalog Query** | SQL driver → system views | Snowflake, BigQuery, Redshift, PostgreSQL, Oracle, Databricks, MySQL, Teradata, Hive, Athena, Trino, ClickHouse, ... | ~22 |
| **REST/GraphQL API** | Vendor API → paginate → traverse hierarchy | Tableau, Looker, Power BI, Salesforce, Mode, Sigma, Metabase, Monte Carlo, Qlik Sense, ThoughtSpot, dbt Cloud, Dagster, ... | ~38 |
| **Hybrid (API + SQL)** | Platform API for config, SQL on warehouse for data | Fivetran, Matillion, Hightouch, AWS Glue, Azure Data Factory, Informatica, ... | ~9 |
| **Wire Protocol** | Native client (not SQL, not REST) | Kafka (x5 variants), MongoDB (x2), Cosmos DB, DynamoDB, DataStax | ~12 |
| **File/Artifact Ingestion** | Read manifests from object storage | dbt Core, S3, GCS | ~3 |
| **OpenLineage Receiver** | Passively ingest lineage events | Airflow OL, Spark OL, Astronomer OL, MWAA OL, GCC OL | ~6 |

The **SQL** and **REST API** patterns together cover **60 of 93 connectors (65%)**. They share the same structural problems:

- **Multiple auth methods** — Snowflake: keypair/password/OAuth. Tableau: basic/PAT/JWT. Salesforce: JWT bearer/client credentials/username-password. Every connector hand-writes the same branching logic.
- **Many entity types** — Snowflake: 16 (databases through AI models). Tableau: 8+ (projects, workbooks, dashboards, sheets, data sources, fields, calculated fields, metrics). The SDK templates only orchestrate 4-6.
- **Manual transformer wiring** — each new entity type needs a hand-written Atlas mapping class, following the same mechanical pattern.

Today, bridging these gaps requires 2,000-5,000 lines of hand-written Python per connector. This proposal addresses four structural gaps that block the AI-native plan from reaching production connectors:

1. **No auth strategy pattern** — the typed credential system and the client classes don't talk to each other
2. **Fixed entity slots** — the template's `run()` is hardcoded to 4 parallel fetches
3. **Manual transformer wiring** — each new entity type needs a hand-written Atlas mapping class
4. **No REST API client abstraction** — 38 REST API connectors hand-write pagination, rate limiting, and auth header injection from scratch

---

## Relationship to Existing Plan

The AI-native plan defines 5 phases: Type Catalog -> CLI Scaffolding -> Manifest System -> LLM Generation -> MCP Dev Loop. This proposal fills gaps in **Phase 3** (Manifest System) that are prerequisites for Phase 4 (LLM Generation) to work on complex connectors.

Specifically:

| AI-native plan component | What it assumes | What's actually missing |
|--------------------------|----------------|----------------------|
| Handler Strategies (Phase 3A) | Auth is a handler concern | Auth also drives connection-string construction in the SQL client — handler and client need the same strategy |
| Manifest `queries:` block (Phase 3B) | Fixed set of query keys (`fetch_databases`, etc.) | Snowflake needs `fetch_stages`, `fetch_streams`, `fetch_pipes`, etc. — the manifest schema must support arbitrary entity types |
| Manifest `client:` block (Phase 3B) | One `template:` string per client | Snowflake needs 3 different connection-string constructions depending on credential type |

This proposal provides the runtime primitives that make the manifest system expressive enough for production connectors — both SQL and REST API.

| AI-native plan gap | Fixed by |
|-------------------|----------|
| Manifest can't express multi-auth | Change 1: Auth Strategy Pattern |
| Manifest can't express arbitrary entities | Change 2: Entity Registry (protocol-agnostic) |
| No manifest target for REST API connectors | Change 4: BaseApiClient + ApiMetadataExtractor template |
| Manual Atlas mapping per entity | Change 3: Transformer Mapping Registry |
| LLM doesn't know what auth/entities/mappings a connector needs | Change 5: Context Sources (SDK type catalog + Atlas typedefs + connector brief) |

---

## Change 1: Auth Strategy Pattern

### The Gap

Two systems exist that don't connect:

**System A — Typed credentials** (`credentials/types.py`): `BasicCredential`, `CertificateCredential`, `OAuthClientCredential`, etc. Clean Pydantic models with validation. Resolved via `CredentialResolver`.

**System B — SQL client auth** (`clients/sql.py:227`):
```python
def get_auth_token(self) -> str:
    authType = self.credentials.get("authType", "basic")  # raw dict, not typed
    match authType:
        case "iam_user": token = self.get_iam_user_token()
        case "iam_role": token = self.get_iam_role_token()
        case "basic":    token = self.credentials.get("password")
```

The SQL client bypasses the typed credential system entirely. It reads from a raw dict and branches with hand-written match statements. Every multi-auth connector duplicates this pattern with source-specific quirks (PEM decryption for keypair, token refresh for OAuth, STS assume-role for IAM).

### The Design

Introduce `AuthStrategy` — a protocol that maps a typed `Credential` to the parameters a client needs to connect. The protocol is intentionally client-agnostic: SQL clients use `build_url_params` + `build_connect_args`; REST API clients use `build_headers`.

**New file: `application_sdk/clients/auth.py`**

```python
from __future__ import annotations
from typing import Any, Protocol, runtime_checkable
from application_sdk.credentials.types import Credential

@runtime_checkable
class AuthStrategy(Protocol):
    """Maps a typed Credential to client connection parameters.

    SQL clients use build_url_params + build_connect_args.
    REST API clients use build_headers.
    Both can use build_url_query_params.
    """

    credential_type: type[Credential]

    def build_url_params(self, credential: Credential) -> dict[str, str]:
        """Return params to substitute into a URL template.

        SQL example: {"password": "s3cret"}.
        REST example: {} (REST clients rarely template credentials into URLs).
        """
        ...

    def build_connect_args(self, credential: Credential) -> dict[str, Any]:
        """Return driver-level connect_args for create_engine().

        Example: {"private_key": <decrypted_key_bytes>} for Snowflake keypair.
        REST clients return {} — auth goes in headers, not connect_args.
        """
        ...

    def build_url_query_params(self, credential: Credential) -> dict[str, str]:
        """Return extra query parameters appended to the connection/request URL.

        Example: {"authenticator": "oauth"} for Snowflake OAuth.
        """
        ...

    def build_headers(self, credential: Credential) -> dict[str, str]:
        """Return HTTP headers for REST API authentication.

        Example: {"Authorization": "Bearer eyJ..."} for OAuth.
        Example: {"X-API-Key": "abc123"} for API key auth.
        SQL clients ignore this — auth goes in the connection string.
        """
        ...
```

**Built-in strategies: `application_sdk/clients/auth_strategies/`**

| File | Strategy | What it does |
|------|----------|-------------|
| `basic.py` | `BasicAuthStrategy` | SQL: URL-encodes password into template. REST: returns `Authorization: Basic <b64>` header |
| `keypair.py` | `KeypairAuthStrategy` | Decrypts PEM from `CertificateCredential`, returns `connect_args={"private_key": ...}` |
| `oauth.py` | `OAuthAuthStrategy` | Checks `needs_refresh()`, performs token refresh if needed. SQL: returns `token` + query param. REST: returns `Authorization: Bearer <token>` header |
| `api_key.py` | `ApiKeyAuthStrategy` | Returns `{header_name: prefix + api_key}` header (Looker API3, Confluent, etc.) |
| `bearer.py` | `BearerTokenAuthStrategy` | Returns `Authorization: Bearer <token>` header, checks `is_expired()` |
| `iam.py` | `IamUserAuthStrategy`, `IamRoleAuthStrategy` | Generates RDS/Redshift auth tokens via boto3 (moves logic from `common/aws_utils.py`) |

**Change to `BaseSQLClient`:**

```python
class BaseSQLClient:
    DB_CONFIG: ClassVar[DatabaseConfig]
    AUTH_STRATEGIES: ClassVar[dict[type[Credential], AuthStrategy]] = {
        BasicCredential: BasicAuthStrategy(),
    }

    async def load(self, credential: Credential) -> None:
        strategy = self.AUTH_STRATEGIES.get(type(credential))
        if strategy is None:
            raise ClientError(f"No auth strategy for {type(credential).__name__}")

        url_params = strategy.build_url_params(credential)
        connect_args = strategy.build_connect_args(credential)
        query_params = strategy.build_url_query_params(credential)

        conn_str = self.DB_CONFIG.template.format(**url_params)
        conn_str = self._append_query_params(conn_str, query_params)

        self.engine = create_engine(conn_str, connect_args=connect_args)
```

### What a Snowflake Client Looks Like After

```python
class SnowflakeClient(BaseSQLClient):
    DB_CONFIG = DatabaseConfig(
        template="snowflake://{username}@{account}/{database}",
        required=["username", "account"],
    )

    AUTH_STRATEGIES = {
        BasicCredential: BasicAuthStrategy(),
        CertificateCredential: KeypairAuthStrategy(),
        OAuthClientCredential: OAuthAuthStrategy(
            url_query_params={"authenticator": "oauth"},
        ),
    }
```

Three auth methods. Zero branching logic. The connector declares what it supports; the SDK handles the plumbing.

### What a Snowflake Client Looks Like Today (for comparison)

```python
class SnowflakeClient(BaseSQLClient):
    DB_CONFIG = DatabaseConfig(...)

    async def load(self, credentials: dict) -> None:
        auth_type = credentials.get("authType", "basic")

        if auth_type == "basic":
            password = credentials["password"]
            url = f"snowflake://{user}:{quote_plus(password)}@{account}/{db}"
            connect_args = {}

        elif auth_type == "keypair":
            from cryptography.hazmat.primitives import serialization
            key_data = credentials["private_key"].encode()
            passphrase = credentials["passphrase"].encode()
            private_key = serialization.load_pem_private_key(key_data, passphrase)
            pkb = private_key.private_bytes(
                serialization.Encoding.DER,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
            url = f"snowflake://{user}@{account}/{db}"
            connect_args = {"private_key": pkb}

        elif auth_type == "oauth":
            token = credentials.get("access_token", "")
            if not token or is_expired(credentials.get("expires_at")):
                token = await refresh_oauth_token(credentials)
            url = f"snowflake://{user}@{account}/{db}?authenticator=oauth&token={token}"
            connect_args = {}

        else:
            raise ValueError(f"Unknown auth type: {auth_type}")

        url += f"?warehouse={credentials['warehouse']}&role={credentials['role']}"
        self.engine = create_engine(url, connect_args=connect_args)
```

~40 lines of imperative branching vs 6 lines of declarative config.

### Backward Compatibility

- `BaseSQLClient.get_auth_token()` and `get_sqlalchemy_connection_string()` remain as-is with a deprecation warning
- If `AUTH_STRATEGIES` is not set (empty dict), the client falls back to the existing `get_auth_token()` path
- Existing connectors using raw dict credentials continue to work without changes
- New connectors can adopt `AUTH_STRATEGIES` incrementally

### LLM Generation Integration

The auth strategy pattern maps directly to the connector brief's `auth` list. Given a brief that says `auth: [basic, keypair, oauth]`, the LLM queries the SDK type catalog to learn:

- `basic` → `BasicAuthStrategy` maps `BasicCredential`
- `keypair` → `KeypairAuthStrategy` maps `CertificateCredential`
- `oauth` → `OAuthAuthStrategy` maps `OAuthClientCredential`

And generates the correct `AUTH_STRATEGIES` dict in the Python client class. The type catalog is the LLM's reference for which strategies exist and how they wire up.

### Files to Create/Modify

| File | Action | Risk |
|------|--------|------|
| `application_sdk/clients/auth.py` | Create — `AuthStrategy` protocol | None |
| `application_sdk/clients/auth_strategies/__init__.py` | Create — re-exports | None |
| `application_sdk/clients/auth_strategies/basic.py` | Create — `BasicAuthStrategy` | None |
| `application_sdk/clients/auth_strategies/keypair.py` | Create — `KeypairAuthStrategy` | None |
| `application_sdk/clients/auth_strategies/oauth.py` | Create — `OAuthAuthStrategy` | None |
| `application_sdk/clients/auth_strategies/iam.py` | Create — IAM strategies (extract from `common/aws_utils.py`) | Low |
| `application_sdk/clients/sql.py` | Modify — add `AUTH_STRATEGIES` class var, new `load()` path, deprecate `get_auth_token()` | Medium — existing connectors use the old path |

---

## Change 2: Entity Registry

### The Gap

`SqlMetadataExtractor.run()` is hardcoded to 4 parallel fetches:

```python
async def run(self, input):
    db, schema, table, column = await asyncio.gather(
        self.fetch_databases(...),
        self.fetch_schemas(...),
        self.fetch_tables(...),
        self.fetch_columns(...),
    )
```

The template defines 6 task slots total (+ procedures, views), but `run()` only calls 4. Snowflake needs 16 entity types. To add stages, streams, pipes, etc., a connector must:

1. Define new `@task` methods (not in the template)
2. Define new Input/Output contracts (not in the template)
3. Override `run()` entirely to include the new tasks
4. Lose the template's orchestration benefits (error handling, logging, output aggregation)

This means every connector with >6 entity types rewrites `run()` from scratch, duplicating orchestration logic.

### The Design

Replace hardcoded task slots with a declarative entity registry. The template discovers entities at class definition time and orchestrates all of them automatically.

**New file: `application_sdk/templates/entity.py`**

```python
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

@dataclass(frozen=True)
class EntityDef:
    """Declares an extractable entity type.

    Protocol-agnostic: works for SQL connectors (set `sql`),
    REST API connectors (set `endpoint`), or custom logic
    (leave both empty, implement fetch_{name}() method).
    """

    name: str
    """Entity name used in output keys, logging, and file paths (e.g. 'stages')."""

    # --- Fetch strategy (exactly one should be set, or none for custom method) ---

    sql: str = ""
    """SQL query template for SQL connectors. Supports {normalized_include_regex},
    {normalized_exclude_regex}, {temp_table_regex_sql}, {database_name} placeholders."""

    endpoint: str = ""
    """REST API endpoint path for API connectors (e.g. '/api/3.1/workbooks').
    Relative to BaseApiClient.BASE_URL."""

    pagination: str = "none"
    """Pagination strategy for API endpoints: 'none', 'cursor', 'offset', 'page_token'.
    Ignored for SQL entities."""

    response_items_key: str = ""
    """JSON key containing the array of items in API response (e.g. 'workbooks.workbook').
    Empty = response is the array. Ignored for SQL entities."""

    # --- Orchestration ---

    phase: int = 1
    """Execution phase (1 = parallel with core entities, 2 = after phase 1, etc.).
    Entities in the same phase run concurrently."""

    depends_on: tuple[str, ...] = ()
    """Entity names that must complete before this one starts.
    Empty = runs in the first parallel batch."""

    enabled: bool = True
    """Set to False to skip this entity (e.g. exclude_views toggle)."""

    timeout_seconds: int = 1800
    """Task timeout for this entity's extraction."""

    # --- Output ---

    input_type: type | None = None
    """Custom Input contract. Defaults to ExtractionTaskInput if None."""

    output_type: type | None = None
    """Custom Output contract. Defaults to a generic FetchEntityOutput if None."""

    result_key: str = ""
    """Key in ExtractionOutput for the count. Defaults to '{name}_extracted'."""
```

**Change to `SqlMetadataExtractor`:**

```python
class SqlMetadataExtractor(App):
    """Base class for SQL metadata extraction apps.

    Override `entities` to declare what to extract. The `run()` method
    automatically orchestrates all registered entities by phase.
    """

    entities: ClassVar[list[EntityDef]] = [
        EntityDef(name="databases", sql="", phase=1),
        EntityDef(name="schemas", sql="", phase=1),
        EntityDef(name="tables", sql="", phase=1),
        EntityDef(name="columns", sql="", phase=1),
    ]

    # --- backward-compat: existing SQL class attributes still work ---
    fetch_database_sql: ClassVar[str] = ""
    fetch_schema_sql: ClassVar[str] = ""
    fetch_table_sql: ClassVar[str] = ""
    fetch_column_sql: ClassVar[str] = ""

    def _resolve_entities(self) -> list[EntityDef]:
        """Merge class-attribute SQL with entity definitions.

        If a subclass sets fetch_database_sql = "SELECT ...", that SQL
        is applied to the 'databases' EntityDef. This preserves backward
        compatibility with the existing class-attribute pattern.
        """
        legacy_sql_map = {
            "databases": self.fetch_database_sql,
            "schemas": self.fetch_schema_sql,
            "tables": self.fetch_table_sql,
            "columns": self.fetch_column_sql,
        }
        resolved = []
        for entity in self.entities:
            sql = legacy_sql_map.get(entity.name, "") or entity.sql
            if sql:
                resolved.append(EntityDef(**{**entity.__dict__, "sql": sql}))
            else:
                resolved.append(entity)
        return resolved

    async def run(self, input: ExtractionInput) -> ExtractionOutput:
        entities = self._resolve_entities()
        entities = [e for e in entities if e.enabled]

        # Group by phase
        phases: dict[int, list[EntityDef]] = {}
        for e in entities:
            phases.setdefault(e.phase, []).append(e)

        results: dict[str, int] = {}
        for phase_num in sorted(phases):
            phase_results = await asyncio.gather(*[
                self._fetch_entity(e, input) for e in phases[phase_num]
            ])
            for entity, result in zip(phases[phase_num], phase_results):
                key = entity.result_key or f"{entity.name}_extracted"
                results[key] = result.total_record_count

        return ExtractionOutput(
            workflow_id=input.workflow_id,
            success=True,
            **results,
        )

    async def _fetch_entity(self, entity: EntityDef, input: ExtractionInput):
        """Dispatch to a named @task method if it exists, otherwise use generic SQL fetch."""
        method_name = f"fetch_{entity.name}"
        if hasattr(self, method_name):
            method = getattr(self, method_name)
            task_input = self._build_task_input(entity, input)
            return await method(task_input)
        elif entity.sql:
            return await self._generic_sql_fetch(entity, input)
        else:
            raise NotImplementedError(
                f"{type(self).__name__} has no fetch_{entity.name}() method "
                f"and no SQL defined for entity '{entity.name}'."
            )
```

### What a Snowflake Extractor Looks Like After

```python
class SnowflakeExtractor(SqlMetadataExtractor):
    entities = [
        # Phase 1: core entities (parallel)
        EntityDef(name="databases", sql="SELECT ... FROM SNOWFLAKE.ACCOUNT_USAGE.DATABASES ..."),
        EntityDef(name="schemas",   sql="SELECT ... FROM SNOWFLAKE.ACCOUNT_USAGE.SCHEMATA ..."),
        EntityDef(name="tables",    sql="SELECT ... FROM SNOWFLAKE.ACCOUNT_USAGE.TABLES ..."),
        EntityDef(name="columns",   sql="SELECT ... FROM SNOWFLAKE.ACCOUNT_USAGE.COLUMNS ..."),
        EntityDef(name="views",     sql="SELECT ... FROM SNOWFLAKE.ACCOUNT_USAGE.VIEWS ..."),
        EntityDef(name="stages",    sql="SELECT ... FROM SNOWFLAKE.ACCOUNT_USAGE.STAGES ..."),
        EntityDef(name="streams",   sql="SELECT ... FROM SNOWFLAKE.ACCOUNT_USAGE.STREAMS ..."),
        EntityDef(name="pipes",     sql="SELECT ... FROM SNOWFLAKE.ACCOUNT_USAGE.PIPES ..."),
        EntityDef(name="functions", sql="SELECT ... FROM SNOWFLAKE.ACCOUNT_USAGE.FUNCTIONS ..."),
        EntityDef(name="procedures",sql="SELECT ... FROM SNOWFLAKE.ACCOUNT_USAGE.PROCEDURES ..."),

        # Phase 2: entities that need phase 1 results (e.g. dynamic tables need table list)
        EntityDef(name="dynamic_tables", sql="SELECT ... FROM ...", phase=2),
        EntityDef(name="external_tables", sql="SELECT ... FROM ...", phase=2),

        # Conditional: toggled off by default, enabled via input config
        EntityDef(name="ai_models", sql="SELECT ... FROM ...", phase=2, enabled=False),
    ]

    # Override only the entities that need custom logic beyond SQL
    @task(timeout_seconds=3600)
    async def fetch_columns(self, input: FetchColumnsInput) -> FetchColumnsOutput:
        """Columns need batched extraction for large schemas."""
        # custom batching logic
        ...
```

No `run()` override. Adding a new entity type is one line in the `entities` list. The template handles orchestration, phasing, error wrapping, and output aggregation.

### What a Snowflake Extractor Looks Like Today (for comparison)

```python
class SnowflakeExtractor(SqlMetadataExtractor):
    # Must override run() entirely to add stages, streams, pipes, etc.
    async def run(self, input: ExtractionInput) -> ExtractionOutput:
        # Duplicate credential resolution from parent
        cred_ref = input.credential_ref or legacy_credential_ref(input.credential_guid)

        # Phase 1: core + extended entities
        db, schema, table, column, view, stage, stream, pipe, func, proc = (
            await asyncio.gather(
                self.fetch_databases(FetchDatabasesInput(...)),
                self.fetch_schemas(FetchSchemasInput(...)),
                self.fetch_tables(FetchTablesInput(...)),
                self.fetch_columns(FetchColumnsInput(...)),
                self.fetch_views(FetchViewsInput(...)),          # custom
                self.fetch_stages(FetchStagesInput(...)),        # custom
                self.fetch_streams(FetchStreamsInput(...)),       # custom
                self.fetch_pipes(FetchPipesInput(...)),          # custom
                self.fetch_functions(FetchFunctionsInput(...)),  # custom
                self.fetch_procedures(FetchProceduresInput(...)),# custom
            )
        )

        # Phase 2: dependent entities
        dynamic, external = await asyncio.gather(
            self.fetch_dynamic_tables(FetchDynamicTablesInput(...)),
            self.fetch_external_tables(FetchExternalTablesInput(...)),
        )

        # Must manually aggregate all counts — easy to miss one
        return ExtractionOutput(
            databases_extracted=db.total_record_count,
            schemas_extracted=schema.total_record_count,
            tables_extracted=table.total_record_count,
            # ... 10 more fields
        )

    # Must define each fetch method + its Input/Output contracts
    @task(timeout_seconds=1800)
    async def fetch_stages(self, input: FetchStagesInput) -> FetchStagesOutput: ...

    @task(timeout_seconds=1800)
    async def fetch_streams(self, input: FetchStreamsInput) -> FetchStreamsOutput: ...

    # ... 8 more @task methods, each with its own Input/Output pair
```

### Backward Compatibility

- Existing connectors that set `fetch_database_sql` etc. continue working via `_resolve_entities()`
- Existing connectors that override `run()` continue working — their override takes precedence
- Existing connectors that override individual `fetch_*` methods continue working — `_fetch_entity()` dispatches to them
- `ExtractionOutput` gains `**kwargs` support for dynamic entity count fields alongside the existing named fields

### LLM Generation Integration

Given a connector brief that lists `entities: [databases, schemas, tables, columns, stages]`, the LLM:

1. Queries the **SDK type catalog** to learn the `EntityDef` fields (`name`, `sql`, `phase`, `depends_on`, etc.)
2. Queries the **target system docs** to find the correct SQL queries (e.g. `SELECT * FROM SNOWFLAKE.ACCOUNT_USAGE.STAGES`)
3. Generates a static Python `entities` list with fully populated `EntityDef` instances

The LLM figures out SQL queries, phasing, and dependencies — the connector brief just names the entities.

### Files to Create/Modify

| File | Action | Risk |
|------|--------|------|
| `application_sdk/templates/entity.py` | Create — `EntityDef` dataclass | None |
| `application_sdk/templates/sql_metadata_extractor.py` | Modify — add `entities` class var, phased `run()`, `_fetch_entity()` dispatch, `_resolve_entities()` for legacy SQL compat | Medium — must not break existing subclasses |
| `application_sdk/templates/contracts/sql_metadata.py` | Modify — add `FetchEntityOutput` generic output, extend `ExtractionOutput` with `**kwargs` | Low |
| `application_sdk/templates/incremental_sql_metadata_extractor.py` | Modify — adopt entity registry for its 5-phase orchestration | Medium — complex existing logic |

---

## Change 3: Transformer Mapping Registry

### The Gap

The Atlas SQL transformer (`transformers/atlas/sql.py`) hardcodes 6 entity classes:

```
Database, Schema, Table, Column, Procedure, Function
```

Each class has a `get_attributes()` method that maps raw SQL result rows to Atlas entity attributes (qualified name, parent references, type-specific fields). For Snowflake's 10 additional entity types (Stage, Stream, Pipe, Dynamic Table, etc.), a connector must hand-write a transformer class with:

- Qualified name construction logic
- Parent-child relationship wiring
- Type-specific attribute mapping
- Atlas entity class name (the typedef)

This is 30-60 lines per entity type, following a mechanical pattern.

### The Design

Make entity-to-Atlas mapping declarative via `EntityMapping` — a data structure that captures how to transform a SQL result row into an Atlas entity.

**New file: `application_sdk/transformers/mapping.py`**

```python
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

@dataclass(frozen=True)
class EntityMapping:
    """Declares how to transform extracted rows into Atlas entities."""

    entity_name: str
    """Matches the EntityDef.name (e.g. 'stages')."""

    atlas_type: str
    """Atlas typedef name (e.g. 'SnowflakeStage')."""

    qualified_name_template: str
    """Template for building the Atlas qualifiedName.
    Supports {connection_qn}, {database}, {schema}, and any column name
    from the SQL result row in lowercase.

    Example: '{connection_qn}/{database}/{schema}/{stage_name}'
    """

    name_column: str
    """Column from the SQL result that provides the entity's display name."""

    parent_entity: str = ""
    """Parent entity name for hierarchy wiring (e.g. 'schemas' for tables).
    Empty = top-level entity under the connection."""

    parent_qualified_name_template: str = ""
    """Template for the parent's qualifiedName. Required if parent_entity is set."""

    attribute_map: dict[str, str] = field(default_factory=dict)
    """Maps Atlas attribute names to SQL column names.
    Example: {"stageType": "STAGE_TYPE", "stageUrl": "STAGE_URL"}
    Simple 1:1 column mappings. For computed attributes, use attribute_transforms.
    """

    attribute_transforms: dict[str, Callable[[dict[str, Any]], Any]] = field(
        default_factory=dict
    )
    """Maps Atlas attribute names to transform functions.
    Each function receives the full SQL row dict and returns the attribute value.
    Example: {"sizeBytes": lambda row: int(row.get("BYTES", 0))}
    """

    custom_attributes_map: dict[str, str] = field(default_factory=dict)
    """Same as attribute_map but for custom metadata attributes."""
```

**New file: `application_sdk/transformers/registry.py`**

```python
class TransformerRegistry:
    """Registry of entity mappings used by the generic transformer."""

    _mappings: dict[str, EntityMapping]

    def register(self, mapping: EntityMapping) -> None: ...
    def get(self, entity_name: str) -> EntityMapping | None: ...
    def transform_row(self, entity_name: str, row: dict, connection_qn: str, ...) -> dict: ...
```

**Built-in mappings for the 6 existing types** — the current `Database`, `Schema`, `Table`, `Column`, `Procedure`, `Function` classes in `transformers/atlas/sql.py` get converted to `EntityMapping` instances. The classes remain for backward compatibility but delegate to mappings internally.

### What Snowflake Entity Mappings Look Like

```python
class SnowflakeExtractor(SqlMetadataExtractor):
    entities = [
        EntityDef(name="databases", sql="..."),
        EntityDef(name="stages", sql="SELECT * FROM SNOWFLAKE.ACCOUNT_USAGE.STAGES"),
        EntityDef(name="streams", sql="SELECT * FROM SNOWFLAKE.ACCOUNT_USAGE.STREAMS"),
        EntityDef(name="pipes", sql="SELECT * FROM SNOWFLAKE.ACCOUNT_USAGE.PIPES"),
        # ...
    ]

    entity_mappings = [
        # Core types use built-in mappings (inherited from SqlMetadataExtractor)
        # Only declare mappings for Snowflake-specific types:

        EntityMapping(
            entity_name="stages",
            atlas_type="SnowflakeStage",
            qualified_name_template="{connection_qn}/{database_name}/{schema_name}/{stage_name}",
            name_column="STAGE_NAME",
            parent_entity="schemas",
            parent_qualified_name_template="{connection_qn}/{database_name}/{schema_name}",
            attribute_map={
                "stageType": "STAGE_TYPE",
                "stageUrl": "STAGE_URL",
                "stageRegion": "STAGE_REGION",
                "stageOwner": "STAGE_OWNER",
                "storageIntegration": "STORAGE_INTEGRATION",
            },
        ),
        EntityMapping(
            entity_name="streams",
            atlas_type="SnowflakeStream",
            qualified_name_template="{connection_qn}/{database_name}/{schema_name}/{stream_name}",
            name_column="STREAM_NAME",
            parent_entity="schemas",
            parent_qualified_name_template="{connection_qn}/{database_name}/{schema_name}",
            attribute_map={
                "sourceType": "SOURCE_TYPE",
                "streamMode": "MODE",
                "isStale": "STALE",
                "staleAfter": "STALE_AFTER",
            },
        ),
        EntityMapping(
            entity_name="pipes",
            atlas_type="SnowflakePipe",
            qualified_name_template="{connection_qn}/{database_name}/{schema_name}/{pipe_name}",
            name_column="PIPE_NAME",
            parent_entity="schemas",
            parent_qualified_name_template="{connection_qn}/{database_name}/{schema_name}",
            attribute_map={
                "definition": "DEFINITION",
                "isAutoIngestEnabled": "IS_AUTOINGEST_ENABLED",
                "notificationChannel": "NOTIFICATION_CHANNEL_NAME",
            },
        ),
    ]
```

Each mapping is a data structure — no Python classes, no method overrides. An LLM can generate these from knowledge of the source system's catalog views and Atlan's Atlas typedefs.

### LLM Generation Integration

Given a connector brief that lists entity names, the LLM queries the **Atlas typedef index** to learn:

- `SnowflakeStage` has attributes `stageType`, `stageUrl`, `stageRegion`, parent is `SnowflakeSchema`
- `SnowflakeStream` has attributes `sourceType`, `streamMode`, `isStale`, parent is `SnowflakeSchema`
- Qualified name pattern: `{connection_qn}/{database_name}/{schema_name}/{stage_name}`

Then queries the **target system docs** to learn the SQL column names (e.g. `STAGE_TYPE`, `STAGE_URL`) and generates the correct `attribute_map` entries.

The LLM emits static `EntityMapping` instances in the generated Python module. No YAML is parsed at runtime. The Atlas typedef index is what prevents the LLM from hallucinating entity types or attributes that don't exist.

### Files to Create/Modify

| File | Action | Risk |
|------|--------|------|
| `application_sdk/transformers/mapping.py` | Create — `EntityMapping` dataclass | None |
| `application_sdk/transformers/registry.py` | Create — `TransformerRegistry` | None |
| `application_sdk/transformers/atlas/sql.py` | Modify — convert existing 6 classes to `EntityMapping` instances, keep classes as thin wrappers for backward compat | Medium |
| `application_sdk/templates/sql_metadata_extractor.py` | Modify — `_fetch_entity()` uses `TransformerRegistry` for transformation | Low — only affects the new generic path |

---

## Change 4: REST API Client & Template

### The Gap

38 of 93 connectors (41%) extract metadata via REST/GraphQL APIs — BI tools (Tableau, Looker, Power BI, Mode, Sigma, Metabase, ThoughtSpot, ...), SaaS platforms (Salesforce, NetSuite, ...), data quality tools (Monte Carlo, Anomalo, ...), and more. They are the **largest connector category**.

The SDK has `BaseSQLClient` for SQL connectors and `BaseMetadataExtractor` as a thin orchestration wrapper. But there is no REST API client equivalent. Every API connector hand-writes:

1. **Base URL construction** — `f"https://{instance}.tableau.com/api/3.1"`
2. **Auth header injection** — different per credential type (PAT, OAuth, API key)
3. **Pagination loops** — cursor-based (Tableau), offset (Looker), page-token (Salesforce), each with different field names
4. **Rate limit handling** — 429 backoff with Retry-After parsing
5. **Response traversal** — nested JSON paths to the actual record array (`response["workbooks"]["workbook"]`)
6. **Hierarchical entity traversal** — Projects → Workbooks → Dashboards → Sheets (parent IDs flow from phase 1 to phase 2)

This is 200-500 lines of boilerplate per connector, following a mechanical pattern that differs only in URL templates, pagination style, and response shapes.

### The Design

#### Part A: `BaseApiClient`

A counterpart to `BaseSQLClient` for REST API connectors. Consumes `AuthStrategy` for auth, provides paginated fetch, rate-limit backoff, and response parsing.

**New file: `application_sdk/clients/api.py`**

```python
from __future__ import annotations
from typing import Any, AsyncIterator, ClassVar

from application_sdk.clients.auth import AuthStrategy
from application_sdk.clients.base import BaseClient
from application_sdk.credentials.types import Credential

class BaseApiClient(BaseClient):
    """Base HTTP client for REST API connectors.

    Subclass and configure BASE_URL, AUTH_STRATEGIES, and optionally
    DEFAULT_HEADERS, RATE_LIMIT_MAX_RETRIES, TIMEOUT_SECONDS.
    """

    BASE_URL: ClassVar[str] = ""
    """URL template. Supports credential fields: 'https://{instance}.example.com/api/v1'"""

    AUTH_STRATEGIES: ClassVar[dict[type[Credential], AuthStrategy]] = {}

    DEFAULT_HEADERS: ClassVar[dict[str, str]] = {
        "Accept": "application/json",
    }

    RATE_LIMIT_MAX_RETRIES: ClassVar[int] = 5
    TIMEOUT_SECONDS: ClassVar[int] = 30

    async def load(self, credential: Credential, **url_params: str) -> None:
        """Initialize client with resolved credential.

        Selects the auth strategy, builds headers, resolves BASE_URL template.
        """
        strategy = self.AUTH_STRATEGIES.get(type(credential))
        if strategy is None:
            raise ClientError(f"No auth strategy for {type(credential).__name__}")

        self._headers = {
            **self.DEFAULT_HEADERS,
            **strategy.build_headers(credential),
        }
        self._base_url = self.BASE_URL.format(**url_params)

    async def fetch(self, endpoint: str, params: dict[str, Any] | None = None) -> dict:
        """Single GET request with auth headers and rate-limit retry."""
        ...

    async def fetch_all(
        self,
        endpoint: str,
        pagination: str = "none",
        items_key: str = "",
        params: dict[str, Any] | None = None,
    ) -> AsyncIterator[dict]:
        """Paginated GET that yields individual records.

        pagination: 'none' | 'cursor' | 'offset' | 'page_token'
        items_key: dot-delimited path to items array in response
                   (e.g. 'workbooks.workbook'). Empty = response is the array.
        """
        ...

    async def post(self, endpoint: str, body: dict[str, Any]) -> dict:
        """POST request with auth headers and rate-limit retry."""
        ...
```

**What a Tableau client looks like:**

```python
class TableauClient(BaseApiClient):
    BASE_URL = "https://{server}/api/{api_version}"

    AUTH_STRATEGIES = {
        BasicCredential: TableauBasicAuthStrategy(),       # username/password → session token
        ApiKeyCredential: ApiKeyAuthStrategy(),             # PAT → X-Tableau-Auth header
        BearerTokenCredential: BearerTokenAuthStrategy(),   # JWT → Authorization: Bearer
    }
```

**What a Looker client looks like:**

```python
class LookerClient(BaseApiClient):
    BASE_URL = "https://{instance}.looker.com/api/4.0"

    AUTH_STRATEGIES = {
        ApiKeyCredential: LookerApi3AuthStrategy(),  # client_id + client_secret → session token
    }
```

#### Part B: `ApiMetadataExtractor` Template

A counterpart to `SqlMetadataExtractor` for REST API connectors. Uses the same `EntityDef` + `EntityMapping` primitives but dispatches to `BaseApiClient.fetch_all()` instead of SQL queries.

**New file: `application_sdk/templates/api_metadata_extractor.py`**

```python
class ApiMetadataExtractor(App):
    """Base class for REST API metadata extraction apps.

    Same entity registry pattern as SqlMetadataExtractor,
    but dispatches to API endpoints instead of SQL queries.
    """

    client_class: ClassVar[type[BaseApiClient]]

    entities: ClassVar[list[EntityDef]] = []
    entity_mappings: ClassVar[list[EntityMapping]] = []

    async def run(self, input: ExtractionInput) -> ExtractionOutput:
        """Orchestrate extraction: resolve credential → initialize client → fetch entities by phase."""
        credential = await self._resolve_credential(input)
        client = self.client_class()
        await client.load(credential, **input.connection_params)

        entities = [e for e in self.entities if e.enabled]
        phases: dict[int, list[EntityDef]] = {}
        for e in entities:
            phases.setdefault(e.phase, []).append(e)

        results: dict[str, int] = {}
        for phase_num in sorted(phases):
            phase_results = await asyncio.gather(*[
                self._fetch_entity(e, input, client) for e in phases[phase_num]
            ])
            for entity, result in zip(phases[phase_num], phase_results):
                key = entity.result_key or f"{entity.name}_extracted"
                results[key] = result.total_record_count

        return ExtractionOutput(workflow_id=input.workflow_id, success=True, **results)

    async def _fetch_entity(self, entity: EntityDef, input, client):
        """Dispatch: custom method > API endpoint > error."""
        method_name = f"fetch_{entity.name}"
        if hasattr(self, method_name):
            return await getattr(self, method_name)(self._build_task_input(entity, input))
        elif entity.endpoint:
            return await self._generic_api_fetch(entity, input, client)
        else:
            raise NotImplementedError(f"No fetch_{entity.name}() and no endpoint for '{entity.name}'")

    async def _generic_api_fetch(self, entity: EntityDef, input, client):
        """Fetch all pages from an API endpoint, write to output files."""
        records = []
        async for record in client.fetch_all(
            entity.endpoint,
            pagination=entity.pagination,
            items_key=entity.response_items_key,
        ):
            records.append(record)
        # Write to JSONL, return count
        ...
```

#### What a Tableau Extractor Looks Like

```python
class TableauExtractor(ApiMetadataExtractor):
    client_class = TableauClient

    entities = [
        # Phase 1: top-level entities (parallel)
        EntityDef(name="projects",     endpoint="/sites/{site_id}/projects",     pagination="offset", response_items_key="projects.project"),
        EntityDef(name="workbooks",    endpoint="/sites/{site_id}/workbooks",    pagination="offset", response_items_key="workbooks.workbook"),
        EntityDef(name="datasources",  endpoint="/sites/{site_id}/datasources",  pagination="offset", response_items_key="datasources.datasource"),

        # Phase 2: child entities (need parent IDs from phase 1)
        EntityDef(name="views",        endpoint="/sites/{site_id}/views",        pagination="offset", response_items_key="views.view",       phase=2),
        EntityDef(name="fields",       endpoint="",                              phase=2),  # custom method — uses Metadata API GraphQL
    ]

    entity_mappings = [
        EntityMapping(entity_name="projects",    atlas_type="TableauProject",    name_column="name", ...),
        EntityMapping(entity_name="workbooks",   atlas_type="TableauWorkbook",   name_column="name", parent_entity="projects", ...),
        EntityMapping(entity_name="datasources", atlas_type="TableauDatasource", name_column="name", ...),
        EntityMapping(entity_name="views",       atlas_type="TableauDashboard",  name_column="name", parent_entity="workbooks", ...),
    ]

    # Override only the one entity that needs GraphQL instead of REST
    @task(timeout_seconds=3600)
    async def fetch_fields(self, input: FetchFieldsInput) -> FetchFieldsOutput:
        """Field extraction uses Tableau Metadata API (GraphQL), not REST."""
        ...
```

#### What a Salesforce Extractor Looks Like

```python
class SalesforceExtractor(ApiMetadataExtractor):
    client_class = SalesforceClient  # BASE_URL = "https://{instance}.salesforce.com/services/data/v59.0"

    entities = [
        EntityDef(name="objects",       endpoint="/sobjects",              pagination="none",   response_items_key="sobjects"),
        EntityDef(name="fields",        endpoint="/sobjects/{object}/describe", pagination="none", response_items_key="fields", phase=2),
        EntityDef(name="record_types",  endpoint="/sobjects/{object}/describe", pagination="none", response_items_key="recordTypeInfos", phase=2),
    ]
```

### Scope & Applicability

This template covers the **pure REST API** connectors — the ~38 that call a vendor API, paginate, and traverse a resource hierarchy. It does NOT cover:

- **Hybrid connectors** (Fivetran, Matillion) that query a warehouse — those use `SqlMetadataExtractor`
- **Wire protocol connectors** (Kafka, MongoDB) that need native client libraries
- **File ingestion** (dbt Core, S3) — different pattern entirely
- **OpenLineage receivers** — passive event ingestion, not active crawling

### Files to Create/Modify

| File | Action | Risk |
|------|--------|------|
| `application_sdk/clients/api.py` | Create — `BaseApiClient` with paginated fetch, rate limiting, auth strategy | None |
| `application_sdk/templates/api_metadata_extractor.py` | Create — `ApiMetadataExtractor` template using entity registry | None |
| `application_sdk/templates/contracts/api_metadata.py` | Create — `ApiExtractionInput/Output` contracts | None |
| `application_sdk/templates/__init__.py` | Modify — export `ApiMetadataExtractor` | None |

---

## Change 5: Context Sources

### The Gap

Changes 1-4 give the SDK clean, declarative primitives (`AuthStrategy`, `EntityDef`, `EntityMapping`, `BaseApiClient`). An LLM can target these primitives directly — no codegen CLI needed. But the LLM still needs to know **what to generate**: which auth methods does Snowflake support? What entities should it extract? What Atlas typedefs exist? What SQL queries hit the right system views?

Without structured context, the LLM either hallucinates (invents entity types that don't exist in Atlas) or under-generates (misses entity types the connector should support).

### The Design

Three structured context sources that the LLM queries during code generation. Together they answer every question the LLM needs to produce a complete connector.

#### Source 1: SDK Type Catalog

Auto-generated from the SDK source code. Tells the LLM what primitives are available and how to use them.

```
# Auto-exported from application_sdk (generated by `uv run poe export-type-catalog`)

## Auth Strategies
- BasicAuthStrategy        → BasicCredential       → SQL: URL-encodes password. REST: Authorization: Basic header
- KeypairAuthStrategy      → CertificateCredential → Decrypts PEM, returns connect_args={"private_key": ...}
- OAuthAuthStrategy        → OAuthClientCredential  → Token refresh + query param. REST: Bearer header
- ApiKeyAuthStrategy       → ApiKeyCredential       → Returns {header_name: prefix + api_key} header
- BearerTokenAuthStrategy  → BearerTokenCredential  → Returns Authorization: Bearer header
- IamUserAuthStrategy      → IamCredential          → Generates RDS/Redshift auth tokens via boto3
- IamRoleAuthStrategy      → IamCredential          → STS assume-role → auth token

## EntityDef Fields
- name: str           — entity name (e.g. 'stages')
- sql: str            — SQL query template (for SQL connectors)
- endpoint: str       — REST API path (for API connectors)
- pagination: str     — 'none' | 'cursor' | 'offset' | 'page_token'
- response_items_key: str — dot-path to items array in API response
- phase: int          — execution phase (1 = parallel, 2 = after phase 1, etc.)
- depends_on: tuple   — entity names that must complete first
- enabled: bool       — toggle entity on/off
- timeout_seconds: int

## EntityMapping Fields
- entity_name: str                    — matches EntityDef.name
- atlas_type: str                     — Atlas typedef name
- qualified_name_template: str        — supports {connection_qn}, {database}, {schema}, column names
- name_column: str                    — column for display name
- parent_entity: str                  — parent entity name for hierarchy
- parent_qualified_name_template: str
- attribute_map: dict[str, str]       — Atlas attr → SQL column
- attribute_transforms: dict          — Atlas attr → transform function

## Base Classes
- BaseSQLClient(DB_CONFIG, AUTH_STRATEGIES)     → subclass for SQL connectors
- BaseApiClient(BASE_URL, AUTH_STRATEGIES)      → subclass for REST API connectors
- SqlMetadataExtractor(entities, entity_mappings, client_class)
- ApiMetadataExtractor(entities, entity_mappings, client_class)
```

This is generated by a poe task (`uv run poe export-type-catalog`) that introspects the SDK and emits a structured document. Updated whenever the SDK changes.

#### Source 2: Atlas Typedef Index

Extracted from the `models` repo. Tells the LLM what entity types exist for each connector, their attributes, and parent-child relationships.

```
# Atlas typedefs for: Snowflake

## Entity Types
- SnowflakeDatabase
    qualifiedName: "{connection_qn}/{database_name}"
    parent: Connection
    attributes: [databaseName, databaseOwner, isTransient, retentionTime]

- SnowflakeSchema
    qualifiedName: "{connection_qn}/{database_name}/{schema_name}"
    parent: SnowflakeDatabase
    attributes: [schemaName, schemaOwner, isTransient, isManagedAccess]

- SnowflakeStage
    qualifiedName: "{connection_qn}/{database_name}/{schema_name}/{stage_name}"
    parent: SnowflakeSchema
    attributes: [stageType, stageUrl, stageRegion, stageOwner, storageIntegration]

- SnowflakeStream
    qualifiedName: "{connection_qn}/{database_name}/{schema_name}/{stream_name}"
    parent: SnowflakeSchema
    attributes: [sourceType, streamMode, isStale, staleAfter]

- SnowflakePipe
    qualifiedName: "{connection_qn}/{database_name}/{schema_name}/{pipe_name}"
    parent: SnowflakeSchema
    attributes: [definition, isAutoIngestEnabled, notificationChannel]

# ... all entity types for this connector
```

This can be generated by a script that reads the typedef JSON from the `models` repo, or served as an MCP resource.

#### Source 3: Connector Brief

A minimal, human-authored spec (~10 lines) that tells the LLM **what** to build without specifying **how**. This is the only input a human needs to provide.

```yaml
# connector-brief.yaml
target: snowflake
type: sql
connection: "snowflake://{username}@{account}/{database}"
auth:
  - basic
  - keypair
  - oauth
entities:
  - databases
  - schemas
  - tables
  - columns
  - views
  - stages
  - streams
  - pipes
  - functions
  - procedures
```

No SQL queries. No attribute maps. No qualified name templates. The LLM resolves those from the Atlas typedef index and the target system's documentation.

For REST API connectors:

```yaml
# connector-brief.yaml
target: tableau
type: api
base_url: "https://{server}/api/{api_version}"
auth:
  - basic       # username/password → session token
  - api_key     # PAT
  - bearer      # JWT
entities:
  - projects
  - workbooks
  - datasources
  - views
  - fields
```

### The LLM Generation Flow

```
┌─────────────────────────────────────────────────────┐
│  Connector Brief (human-authored, ~10 lines)        │
│  "Snowflake, SQL, basic+keypair+oauth, 10 entities" │
└──────────────────────┬──────────────────────────────┘
                       ↓
┌─────────────────────────────────────────────────────┐
│  LLM queries 3 context sources:                     │
│                                                     │
│  ① SDK Type Catalog                                 │
│     → "BasicAuthStrategy maps BasicCredential to    │
│        URL-encoded password..."                     │
│     → "EntityDef has sql, phase, depends_on..."     │
│                                                     │
│  ② Atlas Typedef Index (from models repo)           │
│     → "SnowflakeStage has attrs: stageType,         │
│        stageUrl, stageRegion, parent=Schema,         │
│        qn={connection_qn}/{db}/{schema}/{stage}"    │
│                                                     │
│  ③ Target System Docs (via MCP / web search)        │
│     → "SNOWFLAKE.ACCOUNT_USAGE.STAGES has columns:  │
│        STAGE_NAME, STAGE_TYPE, STAGE_URL, ..."      │
└──────────────────────┬──────────────────────────────┘
                       ↓
┌─────────────────────────────────────────────────────┐
│  LLM generates static Python                        │
│                                                     │
│  snowflake_client.py    → BaseSQLClient subclass    │
│                           with AUTH_STRATEGIES       │
│  snowflake_extractor.py → SqlMetadataExtractor      │
│                           with entities + mappings   │
│  snowflake_handler.py   → Handler subclass          │
└──────────────────────┬──────────────────────────────┘
                       ↓
              committed → reviewed → tested → Docker
```

The LLM does the work that was previously done by humans writing YAML manifests — looking up SQL queries, mapping column names to Atlas attributes, wiring parent-child relationships. The connector brief just tells it which connector to build.

### Why This Is Better Than a Codegen CLI

| Concern | Codegen CLI | LLM with context sources |
|---------|------------|-------------------------|
| Input required | Full YAML manifest (~60 lines with SQL, attribute maps) | Connector brief (~10 lines, just entity names + auth) |
| Who writes the SQL? | Human (in the YAML) | LLM (from target system docs) |
| Who maps attributes? | Human (in the YAML) | LLM (from Atlas typedef index) |
| Edge cases | Rigid template, must escape to hand-edit | LLM adds custom `fetch_*` methods where needed |
| SDK updates | Must update codegen templates | LLM reads current type catalog |
| New auth strategy | Must update codegen template mapping | LLM reads current type catalog |

### Files to Create/Modify

| File | Action | Risk |
|------|--------|------|
| `application_sdk/cli/export_type_catalog.py` | Create — introspects SDK, emits structured type catalog | None |
| `scripts/export_atlas_typedefs.py` | Create — reads `models` repo typedef JSON, emits per-connector typedef index | None |
| `docs/connector-brief-schema.yaml` | Create — JSON Schema for connector brief format | None |
| `pyproject.toml` | Modify — add `export-type-catalog` poe task | None |

---

## How the Five Changes Compose

With all five changes, a single connector brief + LLM generates production-ready Python that ships in Docker.

### As hand-written Python (for complex connectors that need escape hatches)

```python
class SnowflakeClient(BaseSQLClient):
    DB_CONFIG = DatabaseConfig(
        template="snowflake://{username}@{account}/{database}",
        required=["username", "account"],
    )
    AUTH_STRATEGIES = {
        BasicCredential: BasicAuthStrategy(),
        CertificateCredential: KeypairAuthStrategy(),
        OAuthClientCredential: OAuthAuthStrategy(
            url_query_params={"authenticator": "oauth"},
        ),
    }


class SnowflakeExtractor(SqlMetadataExtractor):
    client_class = SnowflakeClient

    entities = [
        EntityDef(name="databases", sql="SELECT ... FROM SNOWFLAKE.ACCOUNT_USAGE.DATABASES"),
        EntityDef(name="schemas",   sql="SELECT ... FROM SNOWFLAKE.ACCOUNT_USAGE.SCHEMATA"),
        EntityDef(name="tables",    sql="SELECT ... FROM SNOWFLAKE.ACCOUNT_USAGE.TABLES"),
        EntityDef(name="columns",   sql="SELECT ... FROM SNOWFLAKE.ACCOUNT_USAGE.COLUMNS"),
        EntityDef(name="views",     sql="SELECT ... FROM SNOWFLAKE.ACCOUNT_USAGE.VIEWS"),
        EntityDef(name="stages",    sql="SELECT ... FROM SNOWFLAKE.ACCOUNT_USAGE.STAGES"),
        EntityDef(name="streams",   sql="SELECT ... FROM SNOWFLAKE.ACCOUNT_USAGE.STREAMS"),
        EntityDef(name="pipes",     sql="SELECT ... FROM SNOWFLAKE.ACCOUNT_USAGE.PIPES"),
        EntityDef(name="functions", sql="SELECT ... FROM SNOWFLAKE.ACCOUNT_USAGE.FUNCTIONS"),
        EntityDef(name="procedures",sql="SELECT ... FROM SNOWFLAKE.ACCOUNT_USAGE.PROCEDURES"),
        EntityDef(name="dynamic_tables", sql="SELECT ...", phase=2),
    ]

    entity_mappings = [
        EntityMapping(entity_name="stages", atlas_type="SnowflakeStage", ...),
        EntityMapping(entity_name="streams", atlas_type="SnowflakeStream", ...),
        EntityMapping(entity_name="pipes", atlas_type="SnowflakePipe", ...),
        EntityMapping(entity_name="dynamic_tables", atlas_type="SnowflakeDynamicTable", ...),
    ]

    # Override only the one task that needs custom logic
    @task(timeout_seconds=3600)
    async def fetch_columns(self, input: FetchColumnsInput) -> FetchColumnsOutput:
        """Batched column extraction for large schemas."""
        ...


class SnowflakeHandler(Handler):
    async def test_auth(self, input: AuthInput) -> AuthOutput: ...
    async def preflight_check(self, input: PreflightInput) -> PreflightOutput: ...
    async def fetch_metadata(self, input: MetadataInput) -> SqlMetadataOutput: ...
```

~80 lines of declarative config + SQL queries + 1 custom task override. Down from 2,000+ lines today.

### As LLM-generated Python (the "one prompt" target)

The LLM receives a connector brief + context sources and generates the same Python that a human would write. No intermediate YAML format. No codegen CLI. The LLM targets the SDK primitives directly.

**Input: Connector Brief (~10 lines)**

```yaml
target: snowflake
type: sql
connection: "snowflake://{username}@{account}/{database}"
auth: [basic, keypair, oauth]
entities: [databases, schemas, tables, columns, views, stages, streams, pipes, functions, procedures]
```

**LLM queries context sources, then generates:**

The same `SnowflakeClient`, `SnowflakeExtractor`, and `SnowflakeHandler` classes shown above — with correct SQL queries (from Snowflake docs), correct Atlas mappings (from typedef index), and correct auth strategies (from SDK type catalog).

The LLM resolves everything the old YAML manifest required a human to specify:
- SQL queries → from target system documentation
- `EntityMapping` attribute maps → from Atlas typedef index (knows `SnowflakeStage` has `stageType`, `stageUrl`, etc.)
- `AuthStrategy` selection → from SDK type catalog (knows `KeypairAuthStrategy` maps `CertificateCredential`)
- Parent-child hierarchy → from Atlas typedef index (knows `SnowflakeStage.parent = SnowflakeSchema`)
- Qualified name patterns → from Atlas typedef index

**Flow: Tableau (REST API connector)**

```yaml
# connector-brief.yaml
target: tableau
type: api
base_url: "https://{server}/api/{api_version}"
auth: [basic, api_key, bearer]
entities: [projects, workbooks, datasources, views, fields]
```

LLM generates `TableauClient(BaseApiClient)`, `TableauExtractor(ApiMetadataExtractor)`, `TableauHandler(Handler)` — with correct API endpoints, pagination styles, response item keys, and Atlas entity mappings. All resolved from context sources, not hand-specified.

**What ships in Docker:** only the generated static Python. No YAML, no manifest loader, no codegen CLI.

---

## Coverage Summary

| Pattern | Count | Covered by | Coverage |
|---------|-------|-----------|----------|
| SQL Catalog Query | ~22 | Changes 1-3 + 5 (Auth Strategy + Entity Registry + Transformer Mapping + SqlMetadataExtractor + Context Sources) | Full |
| REST/GraphQL API | ~38 | Changes 1-5 (Auth Strategy + Entity Registry + Transformer Mapping + ApiMetadataExtractor + Context Sources) | Full |
| Hybrid (API + SQL) | ~9 | Combination of SqlMetadataExtractor + BaseApiClient | Partial — hybrid orchestration needs case-by-case |
| Wire Protocol | ~12 | Auth Strategy only (Change 1) | Minimal — native clients need custom code |
| File/Artifact Ingestion | ~3 | Not covered | None |
| OpenLineage Receiver | ~6 | Not covered | None — fundamentally different (passive, not crawl) |
| **Total** | **93** | | **~60/93 (65%) full, ~69/93 (74%) partial** |

---

## Implementation Order

Changes are ordered to minimize risk and provide incremental value at each step.

| Phase | Change | Depends on | Value |
|-------|--------|-----------|-------|
| 1 | Auth Strategy Pattern | Nothing | Multi-auth connectors (SQL + REST) get simpler immediately. |
| 2 | Entity Registry | Nothing | Multi-entity connectors get simpler immediately. |
| 3 | Transformer Mapping Registry | Entity Registry | Connectors can declare entity mappings as data. |
| 4 | REST API Client + Template | Auth Strategy + Entity Registry | 38 REST API connectors gain the same declarative pattern as SQL connectors. |
| 5 | Context Sources | All four | SDK type catalog export + Atlas typedef index + connector brief schema. Gives the LLM structured context to generate correct Python targeting the SDK primitives. |

Phases 1 and 2 can proceed in parallel. Phase 3 depends on 2 (entity names must match). Phase 4 depends on 1 + 2. Phase 5 depends on all four — the context sources describe the primitives the LLM must target.

```
Phase 1 (Auth Strategy) ──────────────┐
                                       ├──→ Phase 4 (REST Client + Template) ──→ Phase 5 (Context Sources)
Phase 2 (Entity Registry) ──→ Phase 3  │
         (Transformer Mapping) ────────┘
```

---

## What This Does NOT Cover

- **Wire protocol connectors** (Kafka x5, MongoDB x2, Cosmos DB, DynamoDB, DataStax) — ~12 connectors that use native client libraries (Kafka AdminClient, pymongo, etc.). The auth strategy pattern (Change 1) applies, but extraction logic needs specialized client classes per protocol.
- **File/artifact ingestion** (dbt Core, S3, GCS) — ~3 connectors that read manifest files or list objects from storage. Fundamentally different pattern — would need a `FileIngestionExtractor` template.
- **OpenLineage receivers** (Airflow, Spark, Astronomer, MWAA, GCC) — ~6 connectors that passively receive lineage events. Not crawl-based, so the entity registry pattern doesn't apply.
- **Query mining / lineage workflows** — a separate workflow type (`SqlQueryExtractor`) with different orchestration. Extending it with the entity registry pattern is a follow-up.
- **Incremental extraction** — `IncrementalSqlMetadataExtractor` has a 5-phase state machine that's more complex than phase-based fan-out. Adapting it to the entity registry is a follow-up.
- **Deployment config** — PKL contracts, Helm charts, and Docker config live in `connector-os`. The LLM generates Python source files, not deployment artifacts.
- **Handler complexity** — the AI-native plan's Handler Strategies (Phase 3A) covers this. This proposal doesn't duplicate that work.

---

## Verification

**Auth Strategy Pattern (Change 1):**
- Unit test: `SnowflakeClient` with `BasicCredential` produces correct connection string
- Unit test: `SnowflakeClient` with `CertificateCredential` passes decrypted key in `connect_args`
- Unit test: `SnowflakeClient` with `OAuthClientCredential` triggers refresh when expired
- Unit test: `BaseApiClient` with `ApiKeyCredential` sends correct `X-API-Key` header
- Unit test: `BaseApiClient` with `BearerTokenCredential` sends `Authorization: Bearer` header
- Integration test: existing Postgres example still works with default `BasicAuthStrategy`

**Entity Registry (Change 2):**
- Unit test: `SqlMetadataExtractor` with default `entities` produces same output as today
- Unit test: subclass with custom `entities` list extracts all declared types
- Unit test: `_resolve_entities()` merges legacy `fetch_database_sql` with entity definitions
- Unit test: phase ordering — phase 2 entities start only after phase 1 completes
- Unit test: `EntityDef` with `endpoint` field dispatches to `_generic_api_fetch()`
- Integration test: existing `examples/application_sql.py` still works

**Transformer Mapping Registry (Change 3):**
- Unit test: `EntityMapping` for Database produces same Atlas entity as existing `Database` class
- Unit test: custom mapping for SnowflakeStage produces correct qualifiedName and attributes
- Unit test: custom mapping for TableauWorkbook produces correct hierarchy with parent project
- Integration test: round-trip — extract + transform + verify Atlas entity structure

**REST API Client + Template (Change 4):**
- Unit test: `BaseApiClient.fetch_all()` with `pagination="offset"` fetches all pages
- Unit test: `BaseApiClient.fetch_all()` with `pagination="cursor"` follows cursor chain
- Unit test: `BaseApiClient` retries on 429 with exponential backoff
- Unit test: `BaseApiClient` extracts nested items via `items_key` (e.g. `"workbooks.workbook"`)
- Unit test: `ApiMetadataExtractor` orchestrates entities by phase using endpoint-based fetch
- Integration test: mock Tableau API server → `TableauExtractor` produces correct entity counts

**Context Sources (Change 5):**
- Unit test: `export-type-catalog` output includes all registered `AuthStrategy` subclasses
- Unit test: `export-type-catalog` output includes all `EntityDef` and `EntityMapping` fields with descriptions
- Unit test: Atlas typedef export for Snowflake includes all entity types, attributes, and parent relationships
- Unit test: connector brief schema validates correct briefs and rejects malformed ones
- Integration test: LLM given Snowflake connector brief + context sources generates Python that imports, type-checks, and runs against mock SQL backend
- Integration test: LLM given Tableau connector brief + context sources generates Python that imports, type-checks, and runs against mock API server
