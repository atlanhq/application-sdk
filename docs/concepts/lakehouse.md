# Lakehouse

The Application SDK provides a typed, vendor-agnostic interface for talking to the Atlan lakehouse — Apache Polaris (Iceberg REST Catalog) holding Iceberg-format tables on S3 / GCS / ADLS. Apps work with plain `dict` records and SDK-owned `Schema` declarations; PyIceberg, PyArrow, DuckDB, and Daft live behind underscore-prefixed internal packages and never appear on the public boundary.

---

## When to use what

| App pattern | Right tool | Install extra |
|---|---|---|
| Append small structured batches (events, audit, ack rows) | `LakehouseWriter.append` | `[lakehouse]` |
| Append / overwrite large staged Parquet | `LakehouseWriter.append_bulk` | `[lakehouse-bulk]` |
| Read filtered records from a single table | `LakehouseReader.fetch_records` | `[lakehouse]` |
| Joins / aggregations / window functions across tables | `LakehouseQuery.sql` | `[lakehouse-sql]` |
| Receive an upstream trigger and dispatch events | `events_read` | `[lakehouse]` |
| Publish the AE Parquet ack after a batch | `events_ack` | `[lakehouse]` |

Install:

```bash
pip install atlan-application-sdk[lakehouse]              # core
pip install atlan-application-sdk[lakehouse,lakehouse-sql] # + DuckDB SQL
pip install atlan-application-sdk[lakehouse,lakehouse-bulk] # + Daft writer
```

Apps that don't pull a heavy extra still import everything from `application_sdk.lakehouse` cleanly — DuckDB and Daft are lazy-imported. Calling a method whose backing dep is missing raises `ImportError` with a clear install hint only when the corresponding method is actually invoked.

---

## Core construction — `from_env`

Every public surface follows the same pattern: `from_env()` reads credentials from environment variables and returns a ready-to-use object. Apps don't pass catalogs or credentials around in code.

```python
from application_sdk.lakehouse import LakehouseReader, LakehouseWriter, LakehouseQuery

reader = LakehouseReader.from_env()
writer = LakehouseWriter.from_env(app_namespace="apps.databricks")
query  = LakehouseQuery.from_env()
```

Required env vars:

| Variable | Purpose |
|---|---|
| `ICEBERG_CATALOG_URI` *(or `ATLAN_DOMAIN_NAME`)* | Polaris REST endpoint |
| `ICEBERG_CLIENT_ID` | OAuth2 client id |
| `ICEBERG_CLIENT_SECRET` | OAuth2 client secret |
| `ICEBERG_WAREHOUSE` | Catalog warehouse (default `context_store`) |
| `CLOUD` | `aws` / `gcp` / `azure` (default `aws`) |
| `AWS_REGION` | Required for AWS (DuckDB write paths and per-cloud DuckDB secrets) |

Missing vars raise a `RuntimeError` naming the missing one — no silent fallback.

---

## Schema declaration

Apps describe table shapes via SDK dataclasses. The SDK translates these to `pyiceberg.Schema` and `pa.Schema` internally; apps never touch those types.

```python
from application_sdk.lakehouse import Schema, Field, PartitionBy

audit_schema = Schema(
    fields=[
        Field("workflow_run_id", "string", nullable=False),
        Field("event_id", "string", nullable=False),
        Field("payload", "string", nullable=True),
        Field("status", "string", nullable=False),
        Field("logged_at", "timestamp", nullable=False),
    ],
    partition_by=PartitionBy("logged_at", transform="day"),
)
```

**Field types**: `string`, `int`, `long`, `double`, `boolean`, `timestamp`, `date`.

**Partition transforms**: `identity`, `year`, `month`, `day`, `hour`, `bucket` (with `bucket_count`).

Validation runs at construction:
- Empty fields list rejected
- Duplicate field names rejected
- `PartitionBy` references an unknown column → rejected
- `transform="bucket"` without `bucket_count` → rejected

---

## `LakehouseReader` — record-oriented reads

Reads from any namespace the catalog grants access to. Returns plain dicts — no Iceberg or Arrow types.

```python
from application_sdk.lakehouse import LakehouseReader

reader = LakehouseReader.from_env()

# Simple table scan
events = reader.fetch_records("automation_engine", "reverse_sync_description")

# With filter, limit, sort
events = reader.fetch_records(
    "automation_engine", "reverse_sync_description",
    where="status = 'unprocessed'",
    limit=5000,
    sort_by="received_at",
)

# Project specific columns
events = reader.fetch_records(
    "automation_engine", "reverse_sync_description",
    select=("event_id", "payload"),
)

# Snapshot id (e.g. to record what version of the data you processed)
snap = reader.current_snapshot_id("automation_engine", "reverse_sync_description")
```

**Note on `sort_by` + `limit`**: when both are provided, the SDK reads the full filtered result, sorts in-process, then slices. `limit` is **not** pushed into the Iceberg scan — otherwise you'd sort the wrong N rows.

---

## `LakehouseWriter` — record-oriented append + bulk

Bound to one `app_namespace` at construction. Cross-namespace writes log a warning (defense-in-depth on top of catalog RBAC).

### `append` — small structured batches (PyArrow path)

```python
from application_sdk.lakehouse import LakehouseWriter, Schema, Field, PartitionBy

writer = LakehouseWriter.from_env(app_namespace="apps.databricks")

audit_schema = Schema(
    fields=[
        Field("event_id", "string", nullable=False),
        Field("status", "string", nullable=False),
        Field("logged_at", "timestamp", nullable=False),
    ],
    partition_by=PartitionBy("logged_at", transform="day"),
)

rows = writer.append(
    "audit",
    [
        {"event_id": "e1", "status": "SUCCESS", "logged_at": datetime.now(UTC).replace(tzinfo=None)},
        {"event_id": "e2", "status": "FAILED",  "logged_at": datetime.now(UTC).replace(tzinfo=None)},
    ],
    schema=audit_schema,
)
# → 2
```

If the table doesn't exist, `append` auto-creates it (and its namespace) using the schema and partition spec. If `schema` is omitted, the table must already exist; the writer infers the Arrow schema from the table's metadata.

### `ensure_table` — provision-only

```python
writer.ensure_table("audit", audit_schema)
# Creates the table + namespace if missing; no-op if already present.
```

Useful for migration steps that want to provision tables up-front.

### `append_bulk` — large staged Parquet (Daft path)

For batches large enough that an in-memory `append` becomes expensive. App stages Parquet (anywhere — local disk, S3, GCS, ADLS), passes the prefix path; the SDK reads them via Daft and atomically commits to Iceberg.

```python
# Stage Parquet however your app likes — DuckDB COPY, daft.write_parquet, etc.
# Then commit:
rows = writer.append_bulk(
    "windowed_metrics",
    "s3://my-bucket/staging/run-001/",
    schema=metrics_schema,
    mode="overwrite",   # or "append" (default)
)
```

Requires the `[lakehouse-bulk]` extra. ImportError with install hint if Daft isn't installed.

**Security note on `source_prefix`**: it's dereferenced with the app's own cloud credentials (S3, GCS, ADLS — whatever Daft's IO layer resolves). Must come from a trusted source — workflow input, config, or computed by the app — never directly from end-user input. Daft will read across cloud boundaries (`s3://`, `gs://`, `abfs://`, `file://`, ...) if asked.

### Cross-namespace warning

```python
writer = LakehouseWriter.from_env(app_namespace="apps.databricks")
writer.append("evil", records, namespace="apps.qi", schema=schema)
# WARNING  application_sdk.lakehouse.writer  Cross-namespace write:
#   app_namespace=apps.databricks target=apps.qi — apps should write
#   only to their own namespace
```

The write proceeds; catalog RBAC is the authoritative enforcement. The warning surfaces violations during code review.

---

## `LakehouseQuery` — arbitrary SQL across tables

DuckDB-compatible SQL across one or more lakehouse tables. The SDK loads each referenced table via PyIceberg into Arrow, registers it as a DuckDB view, runs the query in-process, returns dicts.

```python
from application_sdk.lakehouse import LakehouseQuery

q = LakehouseQuery.from_env()

rows = q.sql(
    """
    SELECT a.asset_type, COUNT(*) AS failures
    FROM events e
    JOIN assets a ON e.asset_id = a.guid
    WHERE e.outcome_status = 'FAILED'
      AND e.ingested_at > now() - INTERVAL 1 HOUR
    GROUP BY a.asset_type
    ORDER BY failures DESC
    """,
    tables={
        "events": ("automation_engine", "outcomes"),
        "assets": ("gold", "relational_asset_details"),
    },
    where={"events": "outcome_status = 'FAILED'"},   # pushed into Iceberg scan
)
```

DuckDB tunables (`threads`, `memory_limit`, `temp_dir`, `preserve_insertion_order`) default to popularity-app's settings (single-threaded, 8GB, `/tmp/duckdb_tmp`, insertion order off) and can be overridden per call:

```python
rows = q.sql(query, tables=..., threads=4, memory_limit="16GB")
```

Requires the `[lakehouse-sql]` extra.

**Why Arrow-staging instead of DuckDB ATTACH:** the SDK deliberately avoids DuckDB's Iceberg extension because it has documented issues (multi-table-JOIN credential cache bug, 4-part-name parser quirks, write-path region handling). Loading each table independently via PyIceberg gets correct vended-credentials per table, with DuckDB only doing what it's good at — SQL on Arrow.

---

## `events_read` — AE-triggered ingestion

For apps that consume events via `event-ingestion-app → AE event-consumer-node → source app workflow`. `events_read` takes an async `handler` callable, fetches pending events in batches, dispatches each batch, and returns `(events, results)` — concatenated across all batches and guaranteed 1:1 aligned (RETRY-on-exception, RETRY-on-count-mismatch, no-events-skip).

```python
from application_sdk.lakehouse import events_read, EventResult

async def handler(events: list[dict]) -> list[EventResult]:
    out = []
    for evt in events:
        try:
            await call_my_business_logic(evt)
            out.append(EventResult(status="SUCCESS"))
        except TransientError as exc:
            out.append(EventResult(status="RETRY", error_message=str(exc)))
        except Exception as exc:
            out.append(EventResult(status="FAILED", error_message=str(exc)))
    return out

events, results = await events_read(
    namespace="automation_engine",
    table="reverse_sync_description",
    handler=handler,
    where="status = 'unprocessed'",
    sort_by="received_at",
    batch_size=1000,
    max_events=5000,
)
# events: list[dict]  — concatenated across batches (up to max_events)
# results: list[EventResult]  — aligned 1:1 with events
```

### Batching

| `batch_size` | `max_events` | Behaviour |
|---|---|---|
| `None` | `None` | Single fetch, returns everything available. |
| `N` | `None` | Loop in batches of N until exhausted (CAUTION: unbounded — only safe if the table is known to be finite within the activity timeout). |
| `None` | `M` | Single fetch capped at M. |
| `N` | `M` | Loop in batches of N, total capped at M. **Recommended** for AE-triggered apps — bounds workflow run time and keeps each handler call within heartbeat / memory limits. |

The handler is called once per batch. If a handler call raises (or returns the wrong number of results), that batch's events are marked RETRY and the loop aborts — earlier batches' results are returned as-is.

`events_read` builds its `LakehouseReader` from env credentials each call. No catalog or credentials passed in.

See the module docstring for the full list of known limits of the callable-injection pattern and the planned v2 direction (async-iterator + explicit ack).

---

## `events_ack` — publish the AE Parquet ack

After processing a batch, AE expects a Parquet ack at a specific path layout so it can mark events as acknowledged without re-reading the lakehouse.

```python
from application_sdk.lakehouse import events_ack

path = await events_ack(
    events,
    results,
    app_name="databricks",
    workflow_name="reverse-sync-description",
    workflow_run_id="run-abc-123",
    # filename="events_ack.parquet"   # default
)
# path = "artifacts/databricks/reverse-sync-description/2026/05/06/run-abc-123/events_ack.parquet"
```

The schema is fixed: three columns (`event_id` string non-null, `status` string non-null, `error_message` string nullable). All path components (`app_name`, `workflow_name`, `workflow_run_id`, `filename`) are validated against an allowlist regex on each call — path-traversal is rejected.

---

## End-to-end: AE-triggered ingestion activity

Complete pattern combining `events_read` + `events_ack` inside a Temporal activity:

```python
from temporalio import activity
from application_sdk.lakehouse import events_read, events_ack, EventResult


class MyActivities:
    @activity.defn(name="process_events")
    async def process_events(self, events_table: str, run_id: str) -> str:
        async def _handler(events: list[dict]) -> list[EventResult]:
            return [EventResult(status="SUCCESS") for _ in events]

        events, results = await events_read(
            namespace="automation_engine",
            table=events_table,
            handler=_handler,
            where="status = 'unprocessed'",
            sort_by="received_at",
            batch_size=1000,
            max_events=5000,
        )
        if not events:
            return ""

        return await events_ack(
            events,
            results,
            app_name="myapp",
            workflow_name="ingestion",
            workflow_run_id=run_id,
        )
```

The workflow that wraps this activity receives `{"iceberg_table_name": ...}` from AE's event-consumer-node, passes it through, and writes the ack path back to the workflow output. AE picks up the ack file from the standard layout.

---

## Multi-cloud

The SDK detects the cloud from the `CLOUD` env var (defaults to `aws`) and adjusts catalog construction accordingly. Apps don't write per-cloud branches.

| Cloud | What changes internally | What apps see |
|---|---|---|
| **AWS** | PyIceberg's default S3 FileIO; `AWS_REGION` for DuckDB SET | Nothing different |
| **GCP** | PyIceberg's default GCS FileIO; HMAC keys mapped from `gcp-hmac-keys` k8s secret to `AWS_*` env vars by Helm chart | Nothing different |
| **Azure** | Adds `header.X-Iceberg-Access-Delegation: vended-credentials` (Polaris vends SAS) and `py-io-impl: pyiceberg.io.fsspec.FsspecFileIO` (adlfs); applies Daft monkey-patch for ADLS account-scoped key normalization | Nothing different |

The Azure ADLS patch is idempotent and a no-op when Daft isn't installed.

---

## Security model — vended credentials

Apps deployed by the platform hold only OAuth2 catalog credentials (`ICEBERG_CLIENT_ID` / `ICEBERG_CLIENT_SECRET`). Polaris vends short-lived (~1h), table-scoped storage credentials per `loadTable` request. The app never holds raw cloud keys.

Concretely, when `LakehouseReader.fetch_records` runs:

1. Reader calls Polaris `loadTable(ns.table)` with `X-Iceberg-Access-Delegation: vended-credentials`
2. Polaris RBAC-checks the OAuth2 caller, then calls AWS STS / GCP / Azure to mint scoped creds
3. Polaris embeds the creds in the response's `config` block
4. PyIceberg's FileIO uses those creds to read the table's Parquet files
5. Creds expire automatically; revoking at the catalog stops new reads

This holds for `LakehouseReader`, `LakehouseWriter`, and `LakehouseQuery` (which uses PyIceberg per-table internally). The SDK avoids DuckDB's Iceberg ATTACH path for queries specifically because that path has a documented multi-table-JOIN credential cache bug.

For details on the security model and vending mechanics, see the design doc in `atlan-popularity-app/LAKEHOUSE_INTEGRATION.md`.

---

## Internal layout (reference)

PyIceberg / pyarrow / Polaris / DuckDB / Daft specifics live in:

* `application_sdk.lakehouse._polaris` — catalog construction (URL pattern, OAuth scope, per-cloud config), Daft ADLS patch, DuckDB per-cloud secret builders
* `application_sdk.lakehouse._iceberg` — Iceberg-format ops (identifier, schema mapping, scan / append / ensure_table). Generic across IRC implementations
* `application_sdk.lakehouse._duckdb` — DuckDB connection factory (configurable tunables) + Arrow-staged query engine
* `application_sdk.lakehouse._daft` — Daft DataFrame → Iceberg writer

Apps must not import from these underscore-prefixed packages. They're internal — vendor implementation may change without notice. Public API (`LakehouseReader`, `LakehouseWriter`, `LakehouseQuery`, `events_read`, `events_ack`, `Schema` / `Field` / `PartitionBy`, `EventResult`) is the only supported entry point.
