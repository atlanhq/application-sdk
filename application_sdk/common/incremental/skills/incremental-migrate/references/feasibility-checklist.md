# Incremental Extraction Feasibility Checklist

Use this checklist to determine whether a connector's source system supports
reliable incremental extraction BEFORE writing any code. If any of the five
non-negotiable rules fails, incremental extraction is not feasible for that
source (or requires a fundamentally different approach).

---

## Section 1: The 5 Non-Negotiable Rules

### Rule 1: DDL-Only Timestamps

**What it means:** The source system must expose a timestamp that updates ONLY
when the schema or metadata definition changes (DDL events), NOT when data
content changes (DML events like INSERT, UPDATE, DELETE on rows).

Atlan connectors extract metadata (table definitions, column types, dashboard
structures), not data. A timestamp that bumps on every data refresh is useless
for incremental metadata extraction -- it would flag every entity as "changed"
on every run.

**How to verify:**
1. Record the `updatedAt` / `modified_time` / `LAST_DDL_TIME` for an entity
2. Insert or update DATA in that entity (e.g., add rows to a table, refresh
   a dashboard's data extract)
3. Re-read the timestamp
4. If the timestamp changed, this is a DML timestamp -- NOT suitable

**Disqualification:** If the only available timestamp reflects data changes
(DML), incremental extraction will produce false positives on every run,
offering no benefit over full extraction. Either find a DDL-specific
timestamp or mark the connector as incremental-ineligible.

### Rule 2: Right Granularity

**What it means:** The timestamp must exist at the same granularity level as
the entities you want to detect changes for.

- **SQL connectors:** Need table-level timestamps (one timestamp per table)
- **REST connectors:** Need entity-level timestamps (one per workbook,
  dashboard, datasource, etc.)

A schema-level timestamp (one timestamp for the entire schema/database) is
too coarse -- any change to any table marks ALL tables as changed. A row-level
timestamp is irrelevant -- it tracks data changes, not metadata.

**How to verify:**
1. Change entity A (e.g., add a column to Table A)
2. Check entity B's timestamp (e.g., Table B in the same schema)
3. If entity B's timestamp also changed, the granularity is too coarse

**Disqualification:** If the finest available granularity is schema-level or
database-level, incremental extraction will over-fetch on every run. This may
still be useful if the schema contains thousands of tables and changes are
rare, but the efficiency gain is limited.

### Rule 3: Covers All DDL Events

**What it means:** The timestamp must update for ALL metadata-changing
operations, not just a subset.

Required coverage:
- `CREATE TABLE` / create entity
- `ALTER TABLE` (add column, drop column, rename column, change type)
- `DROP TABLE` / delete entity
- Rename operations
- Permission/ownership changes (if the connector extracts these)

**How to verify:**
For each DDL operation above:
1. Record the timestamp
2. Perform the operation
3. Verify the timestamp updated

**Disqualification:** If `ALTER TABLE ADD COLUMN` updates the timestamp but
`ALTER TABLE DROP COLUMN` does not, incremental extraction will miss dropped
columns. Partial DDL coverage produces silent data staleness.

### Rule 4: No Silent Schema Changes

**What it means:** No operation that changes the effective schema of an entity
without updating that entity's timestamp.

This is the most insidious failure mode. Examples:
- A shared/embedded datasource is modified, which changes the effective schema
  of all workbooks that use it, but those workbooks' timestamps do not update
- A view's underlying table is altered, changing the view's effective columns,
  but the view's timestamp does not change
- A permission change propagates through a hierarchy without updating child
  timestamps

**How to verify:**
1. Identify all INDIRECT change propagation paths in the source system
2. For each path: make the upstream change, check the downstream timestamp
3. If the downstream timestamp does not update, this is a silent change

**Disqualification:** Silent schema changes mean incremental extraction will
serve stale metadata for affected entities. Mitigation options:
- Implement cascade detection (GUARD-IMPL-13) for known propagation paths
- Apply a larger prepone buffer to catch delayed updates
- If propagation paths are too numerous or unpredictable, mark as ineligible

### Rule 5: Deterministic Timestamps

**What it means:** The same event must produce the same timestamp value when
read multiple times. There must be no jitter, no randomness, and no eventual
consistency beyond the prepone buffer window.

**How to verify:**
1. Perform a DDL operation
2. Read the timestamp immediately
3. Read the timestamp again 1 second later
4. Both reads must return the same value
5. Wait for the eventual consistency window (if documented), read again
6. The value must still be the same

**Disqualification:** If timestamps are non-deterministic (e.g., they include
a random component, or they shift during replication), incremental detection
will produce inconsistent results. A bounded eventual consistency delay
(e.g., Salesforce's 15-minute delay) is acceptable IF the prepone buffer
(`marker_offset_hours`) is set to cover it.

---

## Section 2: SQL Connector Pitfalls

### Timestamp Column Coverage Varies by Database

| Database | Column | DDL Coverage | DML Coverage | Notes |
|---|---|---|---|---|
| Oracle | `LAST_DDL_TIME` | Full DDL | No DML | Best case |
| PostgreSQL | `pg_stat_user_tables.last_autoanalyze` | Partial | Partial | Not reliable for DDL |
| Snowflake | `LAST_ALTERED` | Full DDL | No DML | But see caching note |
| MySQL | `UPDATE_TIME` | Partial DDL | Includes DML | Not suitable |

### Timezone Mismatches

The incremental marker is stored as UTC ISO 8601 (`2024-01-15T10:30:00Z`).
Database metadata views may return timestamps in the server's local timezone
without timezone information. Always normalize to UTC before comparison.

### Schema-Level vs Table-Level Granularity

Some databases only expose schema-level DDL timestamps. If one table in a
schema of 10,000 tables changes, all 10,000 are flagged as changed. This
is still better than full extraction if changes are rare, but set
expectations accordingly.

### System Metadata View Caching

Snowflake's `INFORMATION_SCHEMA` is cached for up to **2 hours**. The
`ACCOUNT_USAGE` schema has up to **45-minute latency**. Set
`marker_offset_hours` to at least 2.5 for Snowflake.

### Materialized View Refreshes

Some databases bump the table metadata timestamp when a materialized view
is refreshed, even if the schema did not change. This produces false
positives. Identify materialized views and consider excluding them from
incremental detection or using a secondary check.

---

## Section 3: REST Connector Pitfalls

### API Pagination Changes Between Versions

REST APIs may change pagination behavior between versions (offset-based to
cursor-based). If the incremental implementation depends on pagination
stability, pin the API version or implement both pagination strategies.

### Rate Limits on Filtered Queries

Server-side filtering (`?updatedSince=...`) may hit different rate limits
than unfiltered queries. Some APIs do not index the `updatedAt` field,
making filtered queries significantly slower than full fetches. Test
filtered query performance before committing to server-side filtering.

### Eventual Consistency Delays

| Source System | Delay | Impact |
|---|---|---|
| Salesforce | ~15 minutes | Set `marker_offset_hours >= 0.5` |
| Tableau | 5-40 minutes | Set `marker_offset_hours >= 1.0` |
| Looker | ~5 minutes | Set `marker_offset_hours >= 0.25` |

### Soft Deletes vs Hard Deletes

REST APIs handle deletions in two ways:
- **Soft delete:** Entity remains in API with `isDeleted=true` or `status=trashed`
- **Hard delete:** Entity disappears from API responses entirely

For soft deletes, the entity's `updatedAt` changes and incremental detection
picks it up. For hard deletes, the entity simply vanishes -- detection
requires comparing the full entity list against the previous run's list
(GUARD-IMPL-17).

### Extract/Data Refreshes Bumping updatedAt

Some REST sources (notably Tableau) update the `updatedAt` timestamp when a
data extract is refreshed, even though no metadata changed. This violates
Rule 1 (DDL-Only Timestamps).

**Mitigation:** Use a secondary check -- compare the entity's structural
fields (columns, schema hash) between runs. If only `updatedAt` changed but
structure is identical, skip the entity.

---

## Section 4: GraphQL Connector Pitfalls

### Node Limits on Filtered Queries

GraphQL APIs often impose per-query node limits. Tableau's Metadata API
limits responses to **20,000 nodes per query**. If the filtered result set
exceeds this limit, the API silently truncates results. Implement pagination
or chunked queries for large result sets.

### Indexing Delays

GraphQL metadata APIs often have longer indexing delays than REST APIs.
Tableau's Metadata API takes **5-40 minutes** to reflect changes. This is
separate from the REST API's `updatedAt` and must be accounted for
independently.

### Field Nesting Depth Varies by Entity

Widely-used entities (e.g., a published datasource used by 50 workbooks)
produce deeply nested response trees. A single entity query can return
**50+ downstream nodes**. This affects:
- Query response size (may hit node limits)
- Incremental detection scope (one change cascades)
- State artifact size (storing nested relationships)

### updatedAt Semantics Vary Between Entity Types

Published entities and embedded entities may have different `updatedAt`
semantics in the same GraphQL API:
- Published datasource: `updatedAt` reflects direct modifications
- Embedded datasource: `updatedAt` may reflect parent workbook changes

Always verify `updatedAt` semantics per entity type, not per API.

### No Server-Side Incremental API

Many GraphQL APIs provide NO server-side filtering by modification date.
The only option is:
1. Fetch the full entity list (with minimal fields)
2. Compare against the previous run's list client-side
3. Fetch full details only for changed entities

This "fetch-all-diff-locally" pattern is valid but requires:
- Persisting the previous entity list (factor into state artifact size)
- A lightweight "list" query that returns IDs and timestamps only
- A separate "detail" query for full entity data
