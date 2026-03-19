# RFC: Lakehouse as Primary Storage for Metadata Extraction Pipeline

**Status:** Draft
**Author:** Mrunmayi Tripathi
**Date:** 2026-03-19

---

## Problem Statement

Today, the metadata extraction pipeline follows this flow:

```
Extract → Raw Parquet (S3/ObjectStore) → Transform → Transformed Parquet (S3) → Publish App → Atlas
```

The **publish app** (AE's `publish_entities` activity) reads transformed Parquet files from S3 via `ParquetFileReader`, batches them, and pushes entities to Atlas via the bulk upsert API. With the introduction of `load_to_lakehouse`, we now *also* load data into Iceberg tables — but as a **side-effect**, not a primary path.

The question: **Should the lakehouse become the primary storage layer**, replacing S3 as the source of truth for downstream consumers (publish app, transforms)?

---

## Motivation

The current S3-based pipeline has several concrete pain points that drive this proposal:

1. **Opaque intermediate storage** — S3 files are blobs. There is no way to query "what was extracted for connection X in the last run?" without downloading and parsing files. Debugging extraction issues requires manual file inspection.

2. **Dual-write overhead** — The `upload_to_atlan` activity copies files from the deployment object store to the upstream (Atlan) object store. This is a full re-upload of every file, adding latency and doubling storage cost for every workflow run.

3. **No cross-run visibility** — Each workflow run produces an isolated set of S3 files. There is no unified view of "all tables extracted from Snowflake across all runs." Historical data is scattered across prefixes and requires custom tooling to aggregate.

4. **No incremental publish** — The publish app processes the entire transformed output every run. It cannot query "what changed since last publish?" without external state tracking (the `publish_state_prefix` hack).

5. **Brittle coupling via file paths** — The extract workflow returns `transformed_data_prefix`, `publish_state_prefix`, and `current_state_prefix` as string paths. Downstream consumers (publish app, AE DAG nodes) must resolve these paths correctly. Any path format change breaks the chain.

6. **Schema drift goes undetected** — When a connector adds or changes columns in its raw output, there is no validation. The transform may silently drop or misinterpret fields. Iceberg's schema enforcement would catch this at write time.

---

## Current Architecture

### Data Flow

```
┌────────────┐     Parquet        ┌──────────────┐     Parquet       ┌─────────────────┐
│  Extract   │ ──────────────────→│  Transform   │ ────────────────→│  Publish App    │
│  (SQL)     │  deployment S3:    │  (Atlas fmt) │  deployment S3:  │  (AE activity)  │
└────────────┘  {output}/raw/     └──────────────┘  {output}/       │  → Atlas API    │
      │                                  │          transformed/     └─────────────────┘
      │  upload_to_atlan                 │                                  │
      │  (copies deployment → upstream)  │                                  │
      ▼                                  ▼                                  │
┌──────────────┐                 ┌──────────────┐                           │
│ Upstream S3  │                 │ Upstream S3  │                           │
│ (Atlan store)│                 │ (Atlan store)│                           │
└──────────────┘                 └──────────────┘                           │
      │                                  │                                  │
      │  (optional side-load)            │  (optional side-load)            │
      ▼                                  ▼                                  ▼
┌──────────────┐                 ┌──────────────┐                    ┌──────────┐
│ Iceberg Table│                 │ Iceberg Table│                    │  Atlas   │
│ (raw)        │                 │ (transformed)│                    │ Metastore│
└──────────────┘                 └──────────────┘                    └──────────┘
```

### Key Details

- **Extract** writes raw Parquet files to local disk, uploads to deployment object store (Dapr binding: `DEPLOYMENT_OBJECT_STORE_NAME`)
- **`upload_to_atlan`** copies from deployment store → upstream store (Dapr binding: `UPSTREAM_OBJECT_STORE_NAME`). These are **two different S3 buckets**
- **Transform** reads raw Parquet from deployment S3 via `ParquetFileReader`
- **Publish app** (AE's `publish_entities`) reads transformed **Parquet** (not JSONL) from S3 via `ParquetFileReader`, nests flat columns into Atlas entity structs via `nest_atlas_entities()`, then bulk-upserts to Atlas
- **`load_to_lakehouse`** reads the S3 object keys and submits them to MDLH — it does not re-upload files, it tells MDLH where the files already are in S3
- **File formats**: Raw stage = Parquet. Transformed stage = JSONL (written by `JsonFileWriter` in SDK), but the AE publish activity reads Parquet via `ParquetFileReader`. This format mismatch needs resolution (see Open Question #8)

### Output Contract (Extract → Publish)

The extract workflow returns these paths for downstream AE DAG nodes:
```python
{
    "transformed_data_prefix": "{output_path}/transformed",
    "connection_qualified_name": "default/snowflake/abc",
    "publish_state_prefix": "persistent-artifacts/apps/atlan-publish-app/state/{conn_qn}/publish-state",
    "current_state_prefix": "argo-artifacts/{conn_qn}/current-state",
}
```

---

## Proposed Future Architecture

```
┌────────────┐     Parquet      ┌──────────────────┐
│  Extract   │ ────────────────→│  Iceberg Table   │
│  (SQL)     │   (via MDLH)     │  (raw)           │
└────────────┘                  └────────┬─────────┘
                                         │ read from lakehouse
                                         ▼
                                ┌──────────────────┐
                                │  Transform       │
                                │  (Atlas fmt)     │
                                └────────┬─────────┘
                                         │ write to lakehouse
                                         ▼
                                ┌──────────────────┐
                                │  Iceberg Table   │
                                │  (transformed)   │
                                └────────┬─────────┘
                                         │ read from lakehouse
                                         ▼
                                ┌──────────────────┐
                                │  Publish App     │
                                │  → Atlas API     │
                                └──────────────────┘
```

---

## Key Design Decisions

### Decision 1: Single Table vs. Multiple Tables for Output

#### Option A: Single Table per Stage (current `load_to_lakehouse` approach)

All entity types (databases, schemas, tables, columns, procedures) land in **one raw table** and **one transformed table**.

```
raw_metadata.postgres_raw          ← all raw Parquet files
transformed_metadata.postgres_transformed  ← all transformed JSONL
```

**Pros:**
- Simple configuration (one namespace + table per stage)
- Matches current env-var-driven model (`LH_LOAD_RAW_TABLE_NAME`)
- Easier to reason about — one table = one workflow run's output
- Simpler publish app query: `SELECT * FROM transformed WHERE workflow_run_id = ?`
- Fewer Iceberg tables to manage (no table explosion per connector)

**Cons:**
- Mixed schemas in one table (database rows have different columns than column rows)
- Schema evolution gets messy — adding a field to `columns` affects the whole table
- Query performance: full scan unless heavily partitioned
- Harder to do type-specific transforms or incremental loads

#### Option B: One Table per Entity Type

Each fetch activity writes to its own Iceberg table.

```
raw_metadata.databases
raw_metadata.schemas
raw_metadata.tables
raw_metadata.columns
raw_metadata.procedures
transformed_metadata.databases
transformed_metadata.schemas
...
```

**Pros:**
- Clean, typed schemas per table — each table has exactly the columns it needs
- Better query performance (smaller, focused scans)
- Natural fit for incremental extraction (e.g., only re-extract columns)
- Easier schema evolution — change one entity type without affecting others
- Enables type-specific transforms to read only what they need

**Cons:**
- More tables to manage and configure
- More complex configuration (N tables instead of 1)
- Current `load_to_lakehouse` activity would need to be called N times or reworked
- Publish app needs to read from multiple tables (or we maintain a union view)

#### Option C: Hybrid — Single Raw Table, Multiple Transformed Tables

Raw data goes into one table (it's already Parquet with a `typename` partition). Transformed data goes into per-type tables since the publish app benefits from typed schemas.

```
raw_metadata.all_raw              ← partitioned by typename
transformed_metadata.databases    ← typed tables
transformed_metadata.schemas
transformed_metadata.tables
transformed_metadata.columns
```

**Pros:**
- Raw stage stays simple (bulk ingest, one table)
- Transformed stage gets clean schemas for publish app
- Publish app can process entity types independently (useful for partial retries)
- Partitioning in raw table gives reasonable query perf for transforms

**Cons:**
- Two different patterns in one pipeline (cognitive overhead)
- Transform activity needs to know which output table to write to
- Requires reworking `load_to_lakehouse` for the transformed stage (see "Write Path" below)

### Recommendation

**Option C (Hybrid)** is the best balance. Raw extraction is inherently bulk and untyped — forcing per-type tables adds complexity with minimal benefit since transforms already handle the type dispatch. Transformed data, however, is consumed by the publish app which benefits enormously from typed, smaller tables for batch processing, partial retries, and incremental publish.

**Implementation impact for hybrid approach:**
- **Raw side**: No change — current `load_to_lakehouse` already handles single-table loads
- **Transformed side**: `load_to_lakehouse` would need to be called once per entity type. Two options:
  1. The workflow calls `_execute_lakehouse_load()` N times (once per typename) after transform completes — simple, explicit
  2. A new `load_to_lakehouse_multi` activity that accepts a list of (typename → table) mappings and submits multiple MDLH jobs — more efficient, fewer Temporal activities

Option (1) is recommended for Phase 2 since it requires no new activity code — just additional workflow orchestration calls.

---

### Decision 2: Should Publish App Read from Lakehouse Instead of S3?

#### Current State

The AE `publish_entities` activity reads transformed Parquet files from S3 via `ParquetFileReader(entities_path, dataframe_type=DataframeType.daft)`. It then calls `nest_atlas_entities()` to convert flat columns into nested Atlas entity structs, batches them, and bulk-upserts to Atlas.

Note: `upload_to_atlan` migrates files from the **deployment** object store to the **upstream** (Atlan) object store. These are separate S3 buckets. The publish app reads from whichever store the `entities_path` points to.

#### Proposed: Publish App Reads from Lakehouse

**Approach:**
1. Extract writes raw → deployment S3 (unchanged)
2. `load_to_lakehouse` loads raw files into Iceberg raw table (unchanged)
3. Transform reads from S3, writes transformed output (unchanged for now)
4. `load_to_lakehouse` loads transformed files into per-type Iceberg tables
5. **Publish app reads from Iceberg transformed tables** instead of S3
6. `upload_to_atlan` (deployment → upstream S3 migration) becomes **unnecessary**

**Benefits:**
- **Eliminates S3 as a coupling layer** — lakehouse is the single source of truth
- **Time travel & auditability** — Iceberg snapshots let you see what was published and when
- **Incremental publish** — query only new/changed rows since last snapshot
- **No more `upload_to_atlan`** — the S3→upstream migration step goes away
- **Cross-run deduplication** — lakehouse can track what's already been published across runs
- **SQL-based filtering** — publish app can use SQL to select/filter entities before pushing to Atlas

**Risks & Mitigations:**

| Risk | Mitigation |
|------|------------|
| MDLH availability becomes critical path | Health checks + fallback to S3 reads (dual-mode behind feature flag) |
| Latency increase (Iceberg query vs. direct file read) | Benchmark; Iceberg metadata caching; partition pruning |
| Schema mismatch between writer and reader | Enforce schema registry or Iceberg schema evolution rules |
| Backward compatibility (existing connectors expect S3) | Feature flag (`PUBLISH_SOURCE=lakehouse\|s3`), phased rollout |

---

### Decision 3: Should Transform Read from Lakehouse Instead of S3?

#### Current State
Transform reads raw Parquet files from S3 using `ParquetFileReader` with Dapr ObjectStore bindings.

#### Option A: Transform Reads from S3 (Keep Current)

Transform continues reading from S3. Lakehouse is a write-only destination from extract's perspective.

**Pros:**
- No changes to transform activity
- Lower latency (direct file read vs. Iceberg query)
- No dependency on MDLH for transform stage

**Cons:**
- S3 remains the coupling layer
- Can't leverage Iceberg features (time travel, schema enforcement) in transforms

#### Option B: Transform Reads from Lakehouse

Transform issues Iceberg/SQL queries against the raw table instead of reading Parquet files.

**Pros:**
- **Single source of truth** — everything flows through lakehouse
- **Predicate pushdown** — only read the rows/partitions transform needs
- **Cross-run joining** — transform can reference previous run data for diffing
- **Schema validation** — Iceberg enforces schema on read

**Cons:**
- Requires a new reader abstraction (`IcebergReader` or MDLH query API)
- Adds MDLH as a hard dependency for transform
- Current `ParquetFileReader` is tightly integrated with Dapr ObjectStore
- Increased latency for small datasets (Iceberg overhead > direct file read)

#### Recommendation

**Short term: Keep transform reading from S3** (Option A). The ROI of changing transform's read path is lower than changing publish's read path. Transform is tightly coupled to `ParquetFileReader` and the batch processing model. The win from lakehouse reads is marginal here.

**Long term: Move to lakehouse reads** (Option B) once we have:
1. An `IcebergReader` abstraction in the SDK
2. MDLH query API that supports efficient batch reads
3. Proof that Iceberg read performance is comparable to direct Parquet reads

---

## Write Path: How Data Gets Into the Lakehouse

This section clarifies the write path at each phase, since the read-path changes above depend on data being reliably present in the lakehouse.

### Phase 1 (Current): S3-First, Lakehouse Side-Load

```
Extract → writes Parquet to local disk → uploads to deployment S3
                                                │
                                    load_to_lakehouse tells MDLH
                                    "read files from S3 at this prefix"
                                                │
                                                ▼
                                        MDLH ingests into Iceberg
```

- Extract does **not** write directly to Iceberg. It writes to S3, then `load_to_lakehouse` submits the S3 glob pattern to MDLH's load API.
- MDLH reads the files from S3 and ingests them into Iceberg. The files must be accessible to MDLH via the same S3 bucket.
- **S3 is still required** — it's the staging area that MDLH reads from.

### Phase 2: S3 + Lakehouse (Dual-Write)

Same write path as Phase 1. The key change is on the **read side** (publish reads from lakehouse instead of S3). S3 remains the write staging area.

The `upload_to_atlan` step (deployment → upstream S3 copy) can be skipped for connectors using lakehouse-based publish, since the publish app no longer reads from upstream S3.

### Phase 3+: Direct Iceberg Write (Future)

```
Extract → writes Parquet to local disk → MDLH direct ingest API
                                         (no S3 intermediate)
```

This requires MDLH to support a **push-based ingest** (accept file bytes directly) rather than the current pull-based model (MDLH reads from S3). This is a prerequisite for fully eliminating S3.

Alternatively, extract could use a lightweight Iceberg writer library (e.g., PyIceberg) to write directly to Iceberg tables without going through MDLH. This trades MDLH dependency for a direct Iceberg catalog dependency.

---

## Failure Modes and Recovery

### Scenario 1: `load_to_lakehouse` Fails After Extract Succeeds

**Current impact (Phase 1):** None — lakehouse is a side-load. S3 files are intact, pipeline continues.

**Future impact (Phase 2+):** Publish app cannot read data. **Mitigation:** Fall back to S3-based publish (`PUBLISH_SOURCE=s3`). The S3 files still exist since extract writes there first.

### Scenario 2: Partial Load — Some Entity Types Loaded, Others Failed

If `load_to_lakehouse` is called per entity type (hybrid model), some types may succeed while others fail.

**Mitigation:**
- Each `load_to_lakehouse` call is a separate Temporal activity with its own retry policy (already configured: `maximum_attempts=6, backoff_coefficient=2`)
- Failed entity types can be retried independently without re-running the whole pipeline
- Publish app should only publish entity types that successfully loaded (check Iceberg snapshot presence)

### Scenario 3: Corrupt Data Written to Lakehouse

Data passes MDLH load but is semantically incorrect (e.g., wrong column mappings, garbled attributes).

**Mitigation:**
- **Iceberg time travel**: Roll back to the previous snapshot (`ROLLBACK TO SNAPSHOT`)
- **Audit trail**: Every Iceberg commit records which files were added, enabling forensics
- **Validation step**: Add a post-load validation activity that samples rows and checks expected fields are populated. (Not yet implemented — candidate for Phase 2.)

### Scenario 4: MDLH Is Down

**Phase 1:** Pipeline succeeds without lakehouse (side-load is optional, feature-flagged).

**Phase 2+:** Publish is blocked. **Mitigation:**
- MDLH health check at workflow start (preflight). If MDLH is unreachable, fall back to S3-based publish.
- Circuit breaker pattern: after N consecutive MDLH failures, auto-switch to S3 mode and alert.

### Scenario 5: Extract Produces Files, But MDLH Cannot Access S3

MDLH reads files from S3 via glob pattern. If the S3 bucket permissions or network path is misconfigured, MDLH load fails silently or returns an error.

**Mitigation:**
- `load_to_lakehouse` already polls for job status and raises `ActivityError` on FAILED/TERMINATED status
- Add a pre-submission check: verify at least one file exists at the S3 prefix before submitting to MDLH
- Log the exact glob pattern submitted for easier debugging

---

## Scale Context

To evaluate the latency and cost tradeoffs, here are typical data volumes in the metadata extraction pipeline:

| Connector Size | Databases | Schemas | Tables | Columns | Raw Files | Raw Size | Transformed Size |
|---------------|-----------|---------|--------|---------|-----------|----------|-----------------|
| Small (dev)   | 1-5       | 10-50   | 100-500 | 1K-5K  | 5-20 files | 1-10 MB | 2-20 MB |
| Medium (prod) | 5-20      | 50-200  | 1K-10K | 50K-500K | 20-100 files | 50-500 MB | 100MB-1GB |
| Large (enterprise) | 20-100 | 200-1K | 10K-100K | 500K-10M | 100-1K files | 500MB-5GB | 1-10 GB |

**Key implications:**
- For small connectors, Iceberg overhead (metadata operations, catalog lookups) may dominate — direct Parquet reads from S3 will be faster
- For large connectors, Iceberg's partition pruning and predicate pushdown provide significant wins
- The crossover point where lakehouse reads outperform direct S3 reads needs benchmarking (likely in the medium range)
- Number of active connectors in a typical deployment: 10-50, meaning 10-50 Iceberg raw tables + 50-250 transformed tables in the hybrid model

---

## Migration Path

### Phase 1: Lakehouse as Side-Load (Current — Done)
- `load_to_lakehouse` activity exists
- Raw and transformed data optionally loaded to Iceberg
- S3 remains primary, lakehouse is write-only
- Feature-flagged via `ENABLE_LAKEHOUSE_LOAD`

### Phase 2: Publish App Reads from Lakehouse
- **Prerequisite: Resolve MDLH read capability** (see Hard Dependencies below)
- Add `IcebergReader` to SDK (or MDLH query endpoint)
- Publish app gains ability to read from Iceberg transformed tables
- New env var: `PUBLISH_SOURCE=lakehouse|s3` (default: `s3`)
- `upload_to_atlan` remains available as fallback
- Validate with 1-2 connectors in staging
- Add post-load validation activity (sample-based sanity check)

### Phase 3: Transform Reads from Lakehouse (Optional)
- Add `IcebergReader` for raw table queries
- Transform activity gains `TRANSFORM_SOURCE=lakehouse|s3` flag
- Benchmark Iceberg read vs. direct Parquet read for typical workloads
- Only proceed if latency is acceptable

### Phase 4: S3 Deprecation
- Default new connectors to lakehouse-only mode
- `upload_to_atlan` deprecated (still available for legacy)
- S3 transformed files no longer written (raw may persist for debugging)
- `ENABLE_ATLAN_UPLOAD` defaults to `false`

---

## Hard Dependencies

These must be resolved before proceeding beyond Phase 1:

1. **MDLH read/query API** — The current MDLH API only supports write (load) operations. For Phase 2, we need one of:
   - A MDLH query endpoint that returns Parquet/Arrow data for a given table + filter
   - A Trino/Spark SQL gateway that can query Iceberg tables
   - Direct PyIceberg reads against the Iceberg catalog (bypassing MDLH)

   **This is a blocker for Phase 2.** Without a read path, lakehouse is write-only regardless of what this RFC proposes. The recommended approach is to evaluate PyIceberg direct reads first (lower infrastructure overhead than Trino), falling back to a MDLH query endpoint if catalog access from app pods is infeasible.

2. **S3 accessibility from MDLH** — MDLH must be able to read from the same S3 bucket/prefix that extract writes to. This is currently assumed but not validated in all deployment topologies.

---

## Table Schema Design

### Raw Table (Single Table, Partitioned)

```
Namespace: raw_metadata
Table: {connector_type}_raw  (e.g., postgres_raw, snowflake_raw)

Columns:
  - workflow_id: string
  - workflow_run_id: string
  - connection_qualified_name: string
  - typename: string  (partition key — "database", "schema", "table", "column", "procedure")
  - extracted_at: timestamp
  - ... (all raw columns from extraction — varies by connector and entity type)

Partitioning: typename, date(extracted_at)
Sort order: connection_qualified_name, typename
```

**Schema note:** Because different entity types have different columns (databases have `database_name`, columns have `data_type`, `ordinal_position`, etc.), the raw table will have a wide, sparse schema. Iceberg handles this via schema evolution — new columns are added as `optional` and old rows return `null` for them. This is acceptable for a raw staging table but is exactly why the transformed tables should be per-type.

### Transformed Tables (Per Entity Type)

```
Namespace: transformed_metadata

Table: databases
  - guid: string
  - typeName: string (always "Database" for this table)
  - qualifiedName: string
  - name: string
  - ... (flattened attribute columns — not a nested struct)
  - workflow_run_id: string
  - connection_qualified_name: string
  - extracted_at: timestamp
  - published_at: timestamp (null until publish marks it)

Table: schemas  (same pattern, schema-specific columns)
Table: tables   (same pattern, table-specific columns)
Table: columns  (same pattern, column-specific columns like data_type, ordinal_position)
Table: procedures (same pattern)

Partitioning: date(extracted_at), connection_qualified_name
```

**Why flat columns, not nested structs:** Atlas entities have deeply nested, variable-schema attributes (`attributes.name`, `attributes.qualifiedName`, `businessAttributes.*, `relationshipAttributes.*`). Mapping these to Iceberg `struct` columns is fragile — any new attribute requires schema evolution of the struct type. Instead, we flatten the most common attributes into top-level columns and store overflow/custom attributes in a `json` string column (`extra_attributes: string`). This gives:
- Fast column-level queries for common fields (name, qualifiedName, typeName)
- Flexibility for connector-specific or custom metadata attributes
- Compatibility with Iceberg's schema evolution (adding a new top-level column is trivial)

---

## Open Questions

1. **Table naming convention** — Should tables be per-connector (`postgres_raw`) or global with a connector partition (`all_raw` partitioned by `connector_type`)? Per-connector is simpler for isolation and permissions; global is better for cross-connector queries.

2. **Schema evolution policy** — When a connector adds new fields (e.g., new column metadata), should the Iceberg table schema be auto-evolved (MDLH default?) or require explicit migration? Auto-evolution is convenient but risks schema drift across runs.

3. **Publish state tracking** — Currently tracked via S3 prefix (`publish_state_prefix`). In the lakehouse model, Iceberg snapshots could replace this — publish reads from snapshot N, next run reads from snapshot N+1. Need to validate this pattern with MDLH.

4. **Cross-connector dedup** — If two connectors extract the same asset (e.g., a shared schema visible from both Snowflake and dbt), how do we handle dedup in the lakehouse? This is an existing problem with S3 too, but lakehouse makes it queryable.

5. **Backfill strategy** — When migrating from S3 to lakehouse, do we need to backfill historical data, or start fresh? Recommendation: start fresh — historical S3 data can remain accessible for debugging, but lakehouse starts from the next extraction run.

6. **Cost implications** — Iceberg metadata overhead + MDLH compute vs. simple S3 GET operations. The scale context section provides volume estimates for benchmarking. Target: lakehouse read latency should be within 2x of direct S3 Parquet read for medium-scale connectors.

7. **MDLH read capability** — Does MDLH expose a read/query endpoint today? If not, what is the timeline for one? Alternatively, can we use PyIceberg for direct reads? **This is a blocker — see Hard Dependencies.**

8. **Parquet vs. JSONL format mismatch** — The SDK's `JsonFileWriter` writes JSONL for transformed data, but the AE publish activity reads Parquet via `ParquetFileReader`. The current `load_to_lakehouse` sends `.jsonl` glob for transformed data. Need to clarify: does MDLH ingest JSONL into Parquet-backed Iceberg tables? If not, the SDK or the transform stage needs to output Parquet for transformed data as well.

---

## Summary

| Aspect | Short Term (Phase 1-2) | Long Term (Phase 3-4) |
|--------|----------------------|----------------------|
| **Raw storage** | S3 + Lakehouse side-load | Lakehouse primary |
| **Transform reads from** | S3 | Lakehouse |
| **Transformed storage** | S3 + Lakehouse side-load | Lakehouse only |
| **Publish reads from** | S3 → Lakehouse (Phase 2) | Lakehouse only |
| **Table structure** | Single table per stage | Hybrid (single raw, per-type transformed) |
| **Write path** | Extract → S3 → MDLH pull | Extract → MDLH push (or PyIceberg direct) |
| **`upload_to_atlan`** | Required | Deprecated |
| **S3 role** | Primary storage + MDLH staging | Debug/backup only |
