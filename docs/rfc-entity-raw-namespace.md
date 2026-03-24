# RFC: `int_entity_raw` Namespace — Per-Application Raw Data Tables

**PRs**:
- MDLH: [atlanhq/mdlh#270](https://github.com/atlanhq/mdlh/pull/270)
- Application SDK: [atlanhq/application-sdk#1134](https://github.com/atlanhq/application-sdk/pull/1134)
- Redshift App (reference impl): [atlanhq/atlan-redshift-app#184](https://github.com/atlanhq/atlan-redshift-app/pull/184)

**Author**: Mrunmayi Tripathi
**Date**: 2026-03-23
**Status**: In Review

---

## Problem

Today, the metadata lakehouse only stores **transformed/enriched** data in `entity_metadata`. The raw records from source systems are discarded after transformation. This creates four key gaps:

1. **No auditability or compliance** — we cannot trace back to exactly what the source system sent. If a customer disputes a metadata value, there's no source-of-truth to verify against. For regulated environments, there may be requirements to retain source data for audit trails — without raw storage, MDLH cannot serve as the system of record for metadata provenance.

2. **No reprocessing or debugging** — if transformation logic changes (bug fix, schema evolution, new fields), we cannot re-derive transformed data from source records. Re-extraction from the source is expensive and may not reproduce historical state. Investigating data quality issues (missing assets, wrong counts, stale data) also requires going back to the source system, which is slow, often requires customer credentials, and the source state may have changed since the original extraction.

3. **No diffing** — we cannot compare what changed between two extraction runs at the raw level. Identifying whether a data discrepancy is caused by a source-side change vs. a transformation bug is guesswork without access to both the before and after raw records.

4. **No cross-connector visibility** — there's no unified place to query raw data across connectors for features like raw-to-transformed lineage, coverage reports, or extraction health dashboards. Connector developers also have no visibility into what their extraction actually produced, independent of the transformation layer, making it hard to validate and debug connector output.

## Proposal

Introduce a new Iceberg namespace **`int_entity_raw`** with **one table per registered Application** (e.g. `int_entity_raw.snowflake`, `int_entity_raw.redshift`).

Each table stores the **full raw record as a JSON string** (`raw_record` column) alongside common metadata columns (`typename`, `connection_qualified_name`, `workflow_run_id`, `extracted_at`, `tenant_id`). These metadata columns are intentionally aligned with `entity_metadata` fields so that raw and transformed data can be correlated with a simple equi-join — enabling debugging, diffing, and lineage across the two layers.

Table names are **not arbitrary** — they must match a registered `Application` entity in Atlas. MDLH proactively creates tables for all known applications at startup and every 10 minutes, and also validates on-demand creation requests against the Atlas application registry. This prevents namespace pollution while keeping the system self-service for onboarded connectors.

The feature is **opt-in per connector** via a single environment variable (`ENABLE_LAKEHOUSE_LOAD=true`). When disabled, behavior is unchanged — no raw data is written, no new API calls are made.

This is a coordinated change across three repos:
- **MDLH** — creates and manages the `int_entity_raw` tables in Iceberg, validates `/load` requests against the Atlas application registry
- **Application SDK** — new Temporal activities that convert raw parquet files into the common `int_entity_raw` schema and submit load jobs to MDLH via the `/load` REST API
- **Connector apps** (e.g. Redshift) — register the new SDK activities in their workflow and configure env vars. No extraction logic changes needed.




## Design

### Schema

**Namespace**: `int_entity_raw` | **Table name**: application name (e.g. `snowflake`, `redshift`)

| Column | Type | Required | Purpose |
|--------|------|----------|---------|
| `typename` | string | yes | Partition key — entity type (e.g. `table`, `column`) |
| `connection_qualified_name` | string | yes | Join key to `entity_metadata.connectionqualifiedname` |
| `workflow_id` | string | yes | Workflow that produced this record |
| `workflow_run_id` | string | yes | Join key to `entity_metadata.lastsyncrun` |
| `extracted_at` | long | yes | Epoch millis when the record was extracted |
| `tenant_id` | string | yes | Tenant identifier |
| `entity_name` | string | no | Best-effort entity name from raw data |
| `raw_record` | string | yes | Full raw row serialised as JSON |

Raw and transformed data can be correlated via:
```sql
SELECT r.raw_record, em.*
FROM   int_entity_raw.snowflake r
JOIN   entity_metadata.table em
  ON   r.connection_qualified_name = em.connectionqualifiedname
 AND   r.workflow_run_id           = em.lastsyncrun
 AND   LOWER(r.typename)           = LOWER(em.typename)
```

### Table Lifecycle

Tables are named after registered **Application** entities in Atlas.

**Proactive (startup + every 10 min)** — MDLH queries Atlas for all ACTIVE `Application` entities and pre-creates a table for each. This runs during MDLH init (first install) and on every Notification Processor cycle (`*/10 * * * *`). Any new Application registered in Atlas gets its table within 10 minutes.

**Reactive (on `/load` request, guarded)** — if a `/load` request targets a non-existent table in `int_entity_raw`, MDLH checks whether the table name matches a registered Application. If yes, it auto-creates. If not, it rejects with a clear error listing valid applications. This handles race conditions where a new app sends data before the 10-minute scheduler catches up, while preventing arbitrary table names from polluting the namespace.



## End-to-End Data Flow

```
┌──────────────┐     raw parquet        ┌──────────────┐    JSONL POST /load    ┌──────────────┐
│  Source DB   │ ─────────────────────▶ │  App (SDK)   │ ─────────────────────▶ │    MDLH      │
│  (Redshift)  │  fetch & extract       │  Temporal    │  prepare + load        │  Temporal    │
└──────────────┘                        │  Activities  │                        │  Workflows   │
                                        └──────────────┘                        └──────┬───────┘
                                                                                       │
                     ┌─────────────────────────────────────────────────────────────────┘
                     ▼
          ┌────────────────────────────────────────────────────────────┐
          │                      Iceberg Catalog                       │
          │                                                            │
          │   int_entity_raw/                 entity_metadata/           │
          │   ├─ redshift  (raw JSON)        ├─ database               │
          │   ├─ snowflake                   ├─ schema                 │
          │   └─ bigquery                    ├─ table                  │
          │                                  └─ column                 │
          │         ↕ JOIN on workflow_run_id + connection_qn ↕        │
          └────────────────────────────────────────────────────────────┘
```

### MDLH table management

```
Atlas ──query registered apps──▶ ensureRawMetadataTables()
(Application typedef)            ├─ on startup (init workflow)
                                 └─ every 10 min (notification processor)

POST /load ──▶ Validator
               ├─ table exists? → proceed
               ├─ registered app? → auto-create, then proceed
               └─ unknown name? → reject
```



## Application SDK & Connector Integration

### What the SDK does

The SDK adds two new Temporal activities available to all connectors:

1. **`prepare_raw_for_lakehouse`** — reads raw parquet files produced during extraction and wraps each row into the `int_entity_raw` schema as JSONL, adding metadata columns (`typename`, `connection_qualified_name`, `workflow_run_id`, `extracted_at`, `tenant_id`) alongside the original row as a `raw_record` JSON string.

2. **`load_to_lakehouse`** — submits a load job to the MDLH `/load` API with an S3 glob pattern, then polls the status endpoint until completion or terminal failure.

### Workflow execution order

```
preflight_check → get_workflow_args
  ↓
asyncio.gather(fetch_databases, fetch_schemas, fetch_tables, fetch_columns, ...)
  ↓
prepare_raw_for_lakehouse        ← NEW: raw parquet → common-schema JSONL
load_to_lakehouse (raw)          ← NEW: JSONL → int_entity_raw.{app_name}
  ↓
upload_to_atlan                  (existing)
load_to_lakehouse (transformed)  ← NEW: JSONL → entity_metadata.{typename}
```

### Configuration

All lakehouse loading is controlled by environment variables — **no code changes needed** in connector apps beyond registering the two activities:

| Variable | Default | Purpose |
|----------|---------|---------|
| `ENABLE_LAKEHOUSE_LOAD` | `false` | Master switch |
| `MDLH_BASE_URL` | `http://lakehouse.atlas.svc.cluster.local:4541` | MDLH service URL |
| `LH_LOAD_RAW_NAMESPACE` | `int_entity_raw` | Raw table namespace |
| `LH_LOAD_RAW_TABLE_NAME` | `APPLICATION_NAME` | Raw table name (e.g. `redshift`) |
| `LH_LOAD_RAW_MODE` | `APPEND` | Write mode for raw data |
| `LH_LOAD_TRANSFORMED_NAMESPACE` | `entity_metadata` | Transformed table namespace |
| `LH_LOAD_TRANSFORMED_MODE` | `APPEND` | Write mode for transformed data |
| `LH_LOAD_POLL_INTERVAL_SECONDS` | `10` | Status poll interval |
| `LH_LOAD_MAX_POLL_ATTEMPTS` | `360` | Max poll attempts (1 hour at 10s) |

### What connector apps need to do

Minimal — register the two SDK activities in the workflow's activity list and add a connection metadata normalizer to handle different connection object shapes. The Redshift app PR ([#184](https://github.com/atlanhq/atlan-redshift-app/pull/184)) serves as the reference implementation.



## Rollout

All changes are additive and idempotent. No migration needed.

1. **MDLH deploys first** — raw tables created at init (new installs) or within 10 min (existing installs)
2. **SDK publishes** — new version with lakehouse activities
3. **Connectors opt in** — set `ENABLE_LAKEHOUSE_LOAD=true` per-connector, per-environment

### Rollback


- Set `ENABLE_LAKEHOUSE_LOAD=false` → stops writing immediately
- Existing raw tables remain (no data loss), can be cleaned up later

## Security

- Table names validated against registered Application entities in Atlas — no arbitrary creation
- Apps communicate with MDLH over internal K8s service mesh
- MDLH `/load` API requires `X-Atlan-Tenant-Id` header

## Open Questions

1. **Retention policy** — should raw data have a TTL / automatic cleanup schedule?
2. **Partitioning** — currently by `typename`; add time-based partitioning on `extracted_at`?
3. **Compression** — should `raw_record` use ZSTD-compressed bytes instead of plain JSON?
4. **Backfill** — backfill raw records for existing connectors, or capture going forward only?
5. **Size limits** — max size for `raw_record` to prevent extremely large JSON blobs?
