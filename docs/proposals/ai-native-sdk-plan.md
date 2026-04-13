# AI-Native Application SDK: One-Prompt Connectors

## The Real Problem

Atlan has **93 connectors** across 13 categories. Looking at what actually varies between them, they fall into **4 patterns**:

| Pattern | Connectors | Count | SDK Template Today |
|---------|-----------|-------|--------------------|
| **SQL/JDBC** | Snowflake, Postgres, BigQuery, Redshift, MySQL, Oracle, Databricks, MSSQL, ClickHouse, Hive, Teradata, SAP HANA, etc. | ~26 | `SqlMetadataExtractor` (exists) |
| **REST API** | Tableau, Power BI, Looker, dbt, Fivetran, Salesforce, Monte Carlo, Sigma, Mode, Domo, Metabase, etc. | ~40 | **Nothing** |
| **OpenLineage** | Airflow, Dagster, Spark, Cloud Composer, SageMaker, etc. | ~7 | **Nothing** |
| **Native SDK** | Kafka, S3, GCS, DynamoDB, Cosmos DB, MongoDB, etc. | ~20 | **Nothing** |

**The SDK only has a template for 26 of 93 connectors.** The biggest gap isn't AI tooling or CLI scaffolding — it's that 67 connectors have no base template to build on.

Even for SQL connectors where a template exists, the developer still writes ~100 lines because the SDK doesn't ship with database-specific SQL queries. But an LLM already knows the `information_schema` for every major database. And it knows the Tableau REST API. And the Looker API. And the dbt Cloud API.

**The key insight**: The LLM already knows the connector-specific knowledge (SQL dialects, API endpoints, pagination). What it doesn't have is a constrained output format that the SDK can execute. Give it that format, and one-prompt connectors become real.

---

## What Actually Varies Per Connector

### SQL Connectors (26): Only 3 things vary

| What varies | Example (Postgres vs Snowflake) | LLM knows this? |
|------------|--------------------------------|-----------------|
| Connection string template | `postgresql://...` vs `snowflake://...` | Yes |
| 4-6 metadata SQL queries | `pg_database` vs `SHOW DATABASES` | Yes |
| System schemas to exclude | `pg_*` vs `INFORMATION_SCHEMA` | Yes |

Everything else — orchestration, chunking, heartbeating, transformation, upload — is identical across all 26.

### REST API Connectors (40): 5 things vary

| What varies | Example (Tableau vs Looker) | LLM knows this? |
|------------|----------------------------|-----------------|
| Base URL + endpoints | `/api/3.x/sites/{site}/workbooks` vs `/api/4.0/looks` | Yes |
| Auth flow | PAT/token vs OAuth2 client_credentials | Yes |
| Pagination strategy | offset+limit vs cursor-based | Yes |
| Response → metadata mapping | `workbook.name` vs `look.title` | Yes |
| Rate limiting | 10 req/s vs token bucket | Mostly |

Everything else — HTTP client, retry logic, error handling, credential resolution, metadata upload — should be SDK-provided.

### OpenLineage Connectors (7): Almost nothing varies

All consume the same OpenLineage event format. Variation is only in how events are collected (HTTP push vs Kafka consume vs file watch).

### Native SDK Connectors (20): API client + entity mapping varies

Similar to REST but using native SDKs (boto3, google-cloud-storage, confluent-kafka). The pattern is: list resources → extract metadata → map to Atlan entities.

---

## The Plan: 3 Layers

### Layer 1: Template Coverage (the biggest lever)

**Goal**: Cover 90%+ of connectors with 4 templates.

#### 1A. Database Profiles for SQL Connectors

The `SqlMetadataExtractor` template already exists but requires developers to provide SQL queries. Ship **pre-built profiles** for the top databases:

```python
# What exists today — developer writes this:
class PostgresExtractor(SqlMetadataExtractor):
    fetch_database_sql = "SELECT datname as database_name FROM pg_database..."
    fetch_schema_sql = "SELECT * FROM information_schema.schemata..."
    fetch_table_sql = "SELECT * FROM information_schema.tables..."
    fetch_column_sql = "SELECT * FROM information_schema.columns..."
```

```yaml
# What should exist — shipped with the SDK:
# application_sdk/profiles/databases/postgres.yaml
name: postgresql
connection:
  template: "postgresql+psycopg://{username}:{password}@{host}:{port}/{database}"
  required: [username, password, host, port, database]
  auth_methods: [basic, iam_user, iam_role]
queries:
  fetch_databases: |
    SELECT datname as database_name 
    FROM pg_database WHERE datistemplate = false
  fetch_schemas: |
    SELECT schema_name, catalog_name
    FROM information_schema.schemata
    WHERE schema_name NOT IN ('pg_catalog', 'information_schema')
      AND schema_name NOT LIKE 'pg_toast%'
  fetch_tables: |
    SELECT table_catalog, table_schema, table_name, table_type
    FROM information_schema.tables
    WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
  fetch_columns: |
    SELECT table_catalog, table_schema, table_name, column_name,
           data_type, ordinal_position, is_nullable, column_default
    FROM information_schema.columns
    WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
exclude_schemas: ["pg_catalog", "information_schema", "pg_toast*"]
```

**Ship profiles for**: PostgreSQL, MySQL, Oracle, MSSQL, Snowflake, BigQuery, Redshift, Databricks, ClickHouse, Teradata, Hive, SAP HANA, Trino, Presto, Dremio, CrateDB, AlloyDB, Cloud SQL, Athena, Azure Synapse, Starburst

Then a "Postgres connector" becomes:

```bash
application-sdk create --profile postgres
# Done. Zero lines of code.
```

Or with the LLM:
> "Build me a Postgres metadata extractor"
> → SDK looks up profile → generates manifest → done

**Files**:
- `application_sdk/profiles/databases/` — one YAML per database (21 files)
- `application_sdk/profiles/loader.py` — loads and validates profiles
- `application_sdk/profiles/__init__.py` — `get_profile(name)`, `list_profiles()`

#### 1B. REST API Extractor Template (NEW — covers 40 connectors)

This is the **highest-impact new code**. A new template alongside `SqlMetadataExtractor`:

```python
class RestApiExtractor(App):
    """Base class for REST API metadata extraction.
    
    Handles: auth, pagination, rate limiting, response mapping, retry.
    Subclass only provides: endpoint definitions + response field mapping.
    """
```

The REST API extractor would accept a declarative config:

```yaml
# Profile for Tableau connector
name: tableau
auth:
  type: pat  # personal access token
  token_header: "X-Tableau-Auth"
  login_endpoint: /api/3.24/auth/signin
  login_body: |
    {"credentials": {"personalAccessTokenName": "{token_name}", 
                      "personalAccessTokenSecret": "{token_secret}",
                      "site": {"contentUrl": "{site}"}}}
  session_token_path: "$.credentials.token"

base_url: "https://{server}/api/3.24"

entities:
  sites:
    endpoint: /sites
    response_path: "$.sites.site"
    pagination: { type: offset, page_param: pageNumber, size_param: pageSize, default_size: 100 }
    fields:
      name: "$.name"
      qualified_name: "{connection}/{site_id}"
      
  workbooks:
    endpoint: /sites/{site_id}/workbooks
    depends_on: sites
    response_path: "$.workbooks.workbook"
    pagination: { type: offset, page_param: pageNumber, size_param: pageSize }
    fields:
      name: "$.name"
      description: "$.description"
      qualified_name: "{connection}/{site_id}/{workbook_id}"
      
  dashboards:
    endpoint: /sites/{site_id}/workbooks/{workbook_id}/views
    depends_on: workbooks
    response_path: "$.views.view"
    fields:
      name: "$.name"
      qualified_name: "{connection}/{site_id}/{workbook_id}/{view_id}"

rate_limit:
  requests_per_second: 10
  retry_on: [429, 503]
  backoff: exponential
```

**What the SDK handles (developer writes NONE of this)**:
- HTTP client with connection pooling and retry
- OAuth2 / API key / bearer token / PAT auth flows
- Cursor, offset, keyset, and link-header pagination
- Rate limiting with backoff
- Entity dependency resolution (sites → workbooks → dashboards)
- Response field extraction via JSONPath
- Parallel fetching within rate limits
- Heartbeating and progress reporting
- Error classification (retryable vs terminal)
- Metadata transformation and upload

**What the developer/LLM provides (the profile)**:
- Endpoints and their response structure
- Field mappings (JSONPath)
- Entity hierarchy
- Auth configuration

**Files**:
- `application_sdk/templates/rest_api_extractor.py` — the template (~500 LOC)
- `application_sdk/templates/contracts/rest_api.py` — typed contracts
- `application_sdk/clients/http.py` — HTTP client with auth, pagination, rate limiting
- `application_sdk/profiles/apis/` — pre-built profiles for top REST connectors

**Ship profiles for**: Tableau, Power BI, Looker, dbt Cloud, Fivetran, Salesforce, Mode, Sigma, Domo, Metabase, Monte Carlo, Soda, Anomalo, Hightouch, Matillion

#### 1C. OpenLineage Extractor Template (NEW — covers 7 connectors)

```python
class OpenLineageExtractor(App):
    """Base class for OpenLineage event consumption.
    
    Handles: event parsing, deduplication, lineage graph construction.
    Subclass only provides: event source configuration (HTTP/Kafka/file).
    """
```

Minimal variation — mostly configuration of the event transport:

```yaml
name: airflow-openlineage
transport: http  # or kafka, file
# That's basically it — OpenLineage is a standard protocol
```

**Files**:
- `application_sdk/templates/openlineage_extractor.py`
- `application_sdk/profiles/openlineage/` — one per transport variant

#### 1D. Coverage Summary After Layer 1

| Pattern | Connectors | Template | Profile needed? |
|---------|-----------|----------|-----------------|
| SQL | 26 | `SqlMetadataExtractor` + profiles | Just pick the profile |
| REST API | 40 | `RestApiExtractor` + profiles | LLM generates endpoint config |
| OpenLineage | 7 | `OpenLineageExtractor` | Near-zero config |
| Native SDK | 20 | Custom `App` | Still needs code |
| **Total covered** | **73 of 93** | | **~80% one-prompt ready** |

---

### Layer 2: Manifest Schema (the LLM's output format)

**Goal**: Define one YAML schema that all templates accept. This is the constrained format the LLM generates.

```yaml
# The universal manifest schema — covers SQL, REST, and OpenLineage
kind: SqlMetadataExtractor | RestApiExtractor | OpenLineageExtractor
name: my-connector
version: "1.0.0"

# Option A: Reference a shipped profile (zero-config)
profile: postgresql

# Option B: Inline config (LLM generates this for new sources)
connection:  # for SQL
  template: "..."
  required: [...]

auth:  # for REST
  type: oauth2 | api_key | bearer | pat
  ...

entities:  # for REST
  ...

queries:  # for SQL
  fetch_databases: "..."
  ...

handler:
  auth: sql_ping | http_health | auto
  preflight: connectivity | auto
  metadata: auto

# Optional: custom Python for edge cases
extensions:
  post_extract: "my_module:add_pii_tags"
```

**Key property**: This schema is itself a Pydantic model, so it auto-exports as JSON Schema. Feed that JSON Schema to an LLM as a tool definition, and the LLM can only produce valid manifests.

**Files**:
- `application_sdk/manifest/schema.py` — Pydantic models for the manifest
- `application_sdk/manifest/loader.py` — YAML → validated manifest → running app
- `application_sdk/manifest/runner.py` — CLI integration

---

### Layer 3: LLM Integration (the "one prompt" experience)

**Goal**: `application-sdk create "Tableau connector"` → working app.

The flow:

```
User prompt: "Tableau connector"
         |
         v
    [1] Check profiles → tableau profile exists? → YES → done
         |                                          
         NO (unknown source)
         |
         v
    [2] Send to LLM with context:
        - Manifest JSON Schema (constrained output)
        - List of available profiles (for reference)
        - Template descriptions (SQL vs REST vs OpenLineage)
        The LLM decides: "this is a REST API" and generates
        a manifest with endpoints, auth, pagination, field mappings.
         |
         v
    [3] Validate manifest against schema
         |
         v
    [4] Dry-run against mock infrastructure
         |
         v
    [5] Write to disk → ready to run
```

**Why this works**: The LLM's job is narrowed from "write 166 lines of Python using an SDK you've never seen" to "fill in a 20-field YAML manifest for an API you already know." The SDK validates the output. The templates execute it.

**For known databases/APIs with shipped profiles**, no LLM is needed at all:
```bash
application-sdk create --profile snowflake  # instant, no API call
```

**For unknown sources**, the LLM generates the manifest:
```bash
application-sdk create "Extract metadata from Notion API"
# LLM: "This is a REST API. I know Notion's API. Here's the manifest..."
```

**Files**:
- `application_sdk/ai/context.py` — builds LLM prompt from profiles + manifest schema
- `application_sdk/ai/generator.py` — calls Claude, receives manifest, validates
- `application_sdk/cli/create.py` — CLI command

---

## Priority Order

### Phase 1: Database Profiles + Manifest Loader (2-3 weeks)
- Ship 10 SQL profiles (Postgres, MySQL, Snowflake, BigQuery, Redshift, Databricks, Oracle, MSSQL, ClickHouse, Teradata)
- Build manifest schema + loader + runner
- `application-sdk run --manifest postgres.yaml` works
- **Result**: 10 SQL connectors become zero-code YAML

### Phase 2: REST API Extractor Template (3-4 weeks)
- Build `RestApiExtractor` with auth, pagination, rate limiting
- Build HTTP client abstraction
- Ship 5 REST profiles (Tableau, Looker, dbt Cloud, Power BI, Sigma)
- **Result**: 15 more connectors become zero-code YAML

### Phase 3: LLM Generation (1-2 weeks)
- Build manifest JSON Schema export
- Build `application-sdk create` command with Claude integration
- Validate + dry-run generated manifests
- **Result**: Any connector an LLM knows about becomes one-prompt buildable

### Phase 4: Profile Expansion (ongoing)
- Add remaining SQL profiles (11 more)
- Add remaining REST profiles (10+ more)
- Add OpenLineage template + profiles
- Community-contributed profiles
- **Result**: 80%+ of the 93 Atlan connectors are profile-driven

---

## What This Means Concretely

### Today (166 LOC Python, requires SDK expertise):
```python
class SQLClient(BaseSQLClient):
    DB_CONFIG = DatabaseConfig(template="postgresql+psycopg://...", required=[...])

class PostgresExtractor(SqlMetadataExtractor):
    fetch_database_sql = "SELECT ..."
    fetch_schema_sql = "SELECT ..."
    fetch_table_sql = "SELECT ..."
    fetch_column_sql = "SELECT ..."
    
    @task(timeout_seconds=1800)
    async def fetch_databases(self, input): return await super().fetch_databases(input)
    # ... 3 more task overrides ...

class SampleSQLHandler(Handler):
    async def test_auth(self, input): return AuthOutput(status=AuthStatus.SUCCESS)
    async def preflight_check(self, input): return PreflightOutput(...)
    async def fetch_metadata(self, input): return MetadataOutput(objects=[])
```

### After Phase 1 (0 LOC, 1 command):
```bash
application-sdk create --profile postgres
```

### After Phase 3 (0 LOC, 1 prompt):
```
"Build me a connector for Notion that extracts databases, pages, and users"
```
→ LLM generates REST API manifest → SDK validates → ready to run

---

## Lineage: Which Connectors Bring It, and How

Lineage is not a single capability — it's extracted through **4 distinct methods**, and most connectors that support lineage do so as a **separate extraction step** from metadata crawling. This matters for SDK design: lineage extraction is often a different template/task than metadata extraction.

### Method 1: SQL Query History Parsing (9 connectors)

Parse historical SQL queries from the database's query log to reconstruct table-level and column-level lineage. The SDK parses the SQL to find source → target relationships.

| Connector | Query Source | Lineage Level | Notes |
|-----------|-------------|---------------|-------|
| **Snowflake** | `ACCOUNT_USAGE.QUERY_HISTORY` | Table + Column | Also: view lineage, stored procedure lineage |
| **Google BigQuery** | `INFORMATION_SCHEMA.JOBS` | Table + Column | Also: routine lineage |
| **Databricks** | Unity Catalog system tables | Table + Column | Also: file path lineage for volumes |
| **Amazon Redshift** | `STL_QUERY`, `STL_DDLTEXT`, `SVL_STATEMENTTEXT` | Table + Column | Not available on Serverless |
| **PostgreSQL** | Query history mining | Table + Column | |
| **Oracle** | Query history mining | Table + Column | |
| **ClickHouse** | Query history mining | Table + Column | |
| **Teradata** | Query history mining | Table + Column | Optional |
| **Microsoft SQL Server** | Query history mining | Table + Column | Via SQL Server Miner |
| **Hive** | Query history mining via S3 | Table + Column | Via Hive Miner (queries uploaded to S3) |

**SDK implication**: These connectors need a `SqlQueryMiner` / `SqlLineageExtractor` template alongside `SqlMetadataExtractor`. The existing `SqlQueryExtractor` template partially covers this. The SDK already has SQL parsing infrastructure. This is the **same pattern** across all 10 — only the query log source table differs (Hive is unique in pulling query logs from S3 rather than a system table).

### Method 2: REST API Crawling (8 connectors)

Crawl the tool's native API to extract lineage relationships. These tools track lineage internally and expose it via API.

| Connector | API Source | Lineage Level | Notes |
|-----------|-----------|---------------|-------|
| **Tableau** | Metadata API (GraphQL) | Field-level | Requires Site Admin Explorer role |
| **Power BI** | Scanner APIs + workspace APIs | Column/measure → page | Full lineage requires Member role |
| **Looker** | REST API + LookML + GitHub | Field-level, cross-project | SQL-based + LookML-based lineage |
| **Domo** | REST API | Upstream dataset lineage | |
| **Metabase** | REST API | Question → source table | Limited for native SQL queries |
| **Microsoft Fabric** | REST API | Lineage extraction | |
| **MicroStrategy** | REST API | SQL-based | Extracts SQL statements + definitions |
| **DataStax Enterprise** | API | Asset + column-level | |

**SDK implication**: Lineage here is just another entity type in the REST API extractor. The `RestApiExtractor` profile already handles entity hierarchies — lineage relationships are edges in the same graph. No separate template needed; lineage is a configuration concern within the REST profile.

### Method 3: OpenLineage Protocol (8 connectors)

Consume standardized OpenLineage events emitted at runtime. All use the exact same event schema.

| Connector | Transport | Lineage Level |
|-----------|----------|---------------|
| **Apache Airflow** | HTTP / Kafka | Job → Dataset |
| **Apache Spark** | HTTP / Kafka | Job → Dataset |
| **Astronomer** | HTTP / Kafka | Job → Dataset |
| **Google Cloud Composer** | HTTP / Kafka | Job → Dataset |
| **Amazon MWAA** | HTTP / Kafka | Job → Dataset |
| **Dagster** | HTTP / Kafka | Asset-level |
| **Alteryx** | OpenLineage events | Dataset or Column |
| **Generic OpenLineage** | HTTP / Kafka / file | Job → Dataset |

**SDK implication**: One `OpenLineageExtractor` template covers all 8. The only variation is transport (HTTP endpoint vs Kafka topic vs file watch). The lineage graph construction logic is identical across all of them.

### Method 4: ETL/Transformation Tool APIs (5 connectors)

Parse transformation definitions (manifests, mapping configs, pipeline definitions) to extract how data flows through the tool.

| Connector | Source | Lineage Level | Notes |
|-----------|--------|---------------|-------|
| **dbt** | `manifest.json` + `catalog.json` | Table + Column | Parses model refs and SQL |
| **Informatica CDI** | Mapping task definitions | Field + Column | Only relational sources/targets |
| **Matillion** | Transformation API | Table + Column | Only Snowflake-to-Snowflake |
| **Talend** | Job definitions | Transformation lineage | |
| **Amazon SageMaker** | ML workflow API | ML pipeline lineage | |

**SDK implication**: Each of these is unique enough to need specific parsing logic. dbt manifest parsing is well-defined (JSON). Informatica CDI needs mapping task API crawling. These are best handled as custom `App` implementations or specific profiles of the `RestApiExtractor`.

### Method 5: No Lineage — Metadata Only (63 connectors)

These connectors extract metadata (assets, schemas, configs) but **do not produce lineage**:

**Databases (no query log access or not SQL-based)**:
MySQL, SAP HANA, CrateDB, Trino, Presto, Starburst, DynamoDB, Cosmos DB, MongoDB (explicitly unsupported), AlloyDB, Cloud SQL, Athena, Azure Synapse, Cloudera Impala

**BI Tools (metadata-only crawl)**:
Sigma, Mode, Sisense, ThoughtSpot, IBM Cognos, Qlik Sense Cloud, Qlik Sense Enterprise, Redash, SSRS, Amazon QuickSight, Anaplan

**ETL/Pipeline (metadata-only)**:
Fivetran, AWS Glue, SSIS, Hightouch, Azure Data Factory, Matillion (limited)

**Data Quality & Observability (quality results, not lineage)**:
Monte Carlo, Soda, Anomalo, Bigeye, Great Expectations, Qualytics, Ataccama

**Privacy & Security**:
BigID, Cyera, Immuta, ALTR

**Event/Messaging (topics + schemas, not lineage)**:
Apache Kafka, Confluent Kafka, Amazon MSK, Aiven Kafka, Redpanda Kafka, Azure Event Hubs, Confluent Schema Registry

**Storage (objects, not lineage)**:
Amazon S3, Google Cloud Storage

**CRM/ERP (entities, not lineage)**:
Salesforce, NetSuite, SAP Datasphere, SAP ECC, SAP S/4HANA

**Other**:
Glean, Dataplex, AWS SageMaker Unified Studio, Iceberg, Dremio

### Summary Matrix

| Lineage Method | Count | SDK Template Needed |
|---------------|-------|-------------------|
| SQL query history parsing | 10 | `SqlLineageExtractor` (extend existing `SqlQueryExtractor`) |
| REST API crawling | 8 | Part of `RestApiExtractor` (lineage = another entity type) |
| OpenLineage protocol | 8 | `OpenLineageExtractor` (new, single template for all 8) |
| ETL tool-specific parsing | 5 | Custom per tool (dbt manifest parser, etc.) |
| **No lineage** | **62** | N/A — metadata extraction only |
| **Total with lineage** | **31 of 93** | |

### What This Means for the SDK

1. **Lineage is not universal** — only 30 of 93 connectors produce lineage. The SDK should treat lineage as an **optional capability**, not a requirement.

2. **SQL lineage is one template** — all 10 SQL lineage connectors use the same pattern (parse query history). The `SqlQueryExtractor` template already exists. Ship query log source profiles per database (just the system table names differ — Hive is unique in reading query logs from S3 rather than a system table).

3. **REST lineage is free** — for BI tools that expose lineage via API, it's just another entity/relationship in the REST API profile. No separate template needed.

4. **OpenLineage is one template** — all 8 OpenLineage connectors use the identical event schema. One template, transport config only.

5. **ETL lineage is bespoke** — dbt, Informatica, Matillion each have unique formats. These are the hardest to templatize. Best handled as specific profiles with custom parsing logic.

### Revised Template Strategy Including Lineage

| Template | Covers | Metadata | Lineage | Total Connectors |
|----------|--------|----------|---------|-----------------|
| `SqlMetadataExtractor` + profiles | SQL databases | databases, schemas, tables, columns | No | 26 |
| `SqlLineageExtractor` + profiles | SQL databases with query logs | query history | Table + column lineage via SQL parsing | 9 (subset of above) |
| `RestApiExtractor` + profiles | BI tools, SaaS, ETL tools | dashboards, reports, datasets, etc. | API-exposed lineage (for ~8 tools) | 40 |
| `OpenLineageExtractor` | Orchestration tools | jobs, datasets | Standard OpenLineage events | 8 |
| Custom `App` | Native SDK connectors, ETL-specific | varies | varies | 20 |
| **Total** | | | | **93** (with overlap) |

A typical SQL connector like Snowflake would use **two templates**: `SqlMetadataExtractor` for metadata + `SqlLineageExtractor` for lineage. Both driven by the same profile YAML — just different query sections.

---

## What's Deliberately NOT in This Plan

- **Type catalog / MCP introspection** — useful but not blocking. The LLM needs the manifest schema, not the full SDK type system. Add later.
- **CLI scaffolding for Python projects** — if the manifest path works, generating Python boilerplate is a fallback, not the primary path.
- **Handler strategy registry** — absorbed into profiles. Each profile declares its handler behavior.
- **Docgen / API docs** — downstream of having profiles. Auto-generate docs from profile YAML.

These aren't wrong — they're just not the highest-ROI path to one-prompt connectors. The bottleneck is template coverage and pre-built profiles, not developer tooling.
