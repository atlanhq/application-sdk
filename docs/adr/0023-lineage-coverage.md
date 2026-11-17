# ADR-0023: Lineage Coverage, a Verdict and a Reason for Every Asset

## Status
**Proposed** (spike). Builds on the draft in application-sdk#2204.

## Context

When a BI asset has no lineage, support finds out why by reading run artifacts by hand. There's
no fleet-wide view of how much lineage is missing, where it was lost, or whether a customer can fix
it.

#2204 drafted an SDK tracker for this. A Tableau proof of concept then ran the idea inside the app
end to end: one row per asset per run with a verdict, a gap and a reason, written to the tenant's
lakehouse. On a replayed 1.02M-asset run, lineage output was identical and the lineage step went
from 140 s / 481 MiB to 164 s / 586 MiB.

The next step is to make it reusable so every connector reports coverage the same way. This ADR
takes the AppErrors model (ADR-0013): the SDK owns closed categories and generic reasons, and apps
subclass them.

## Decision

### Taxonomy

| | Owned by | Set |
| --- | --- | --- |
| `ReasonCategory` | SDK, closed | The 12 from #2204. The only reason-derived value allowed as a metric label. |
| `GapCategory` | SDK, closed | `SOURCE` (the source gave too little), `PROCESSING` (the app couldn't build it), `ASSET_RESOLUTION` (the lookup couldn't match a ref) |
| `Verdict` | SDK, closed | `VALID`, `INVALID` (partial or possibly wrong), `MISSING`, `VALID_NONE` (none expected), `PENDING` (waiting on the lookup) |
| `LineageReason` leaves | SDK | One per `ReasonCategory`, plus 12 generic reasons (custom SQL parse failures, non-SQL source, generic connection type, upstream not in Atlan, and others) |
| App reasons | App | Subclasses of a leaf or a generic reason, each with its own `code` |

A reason is a class with ClassVars: `code`, `category`, `gap`, `expected`, `description` and
`customer_message`. It is never instantiated. A subclass inherits `category`, `gap` and `expected`,
and may override them. For example, a null qualifiedName on a column edge is `MISSING_METADATA`
but its gap is `PROCESSING`, because it's an app bug. Every class must declare its own `code`, and
the check runs at class creation. An app can restate an SDK reason under the same code by
subclassing it, to give it the app's own customer wording. Its category can't change. The Tableau
port needed this for 5 codes. `ReasonRegistry` maps a code string back to its class, for rows
read back and for call sites that still pass strings. An unknown code resolves to
`UNINSTRUMENTED_PATH`, with one warning per code, so a typo is never counted as covered.

### Verdicts

`assess(status, reason)` gives the app-side verdict. Then `settle(assessment, outcomes)` folds in
the lookup's per-ref outcomes (`MATCHED`, `MISSED`, `AMBIGUOUS`). An asset stays `PENDING` until
every ref has an outcome.

| Ref outcomes | Settled verdict |
| --- | --- |
| All matched | `VALID`, or `INVALID` if the app already had a partial failure |
| Some missed | `INVALID` / `UPSTREAM_NOT_IN_ATLAN` |
| None matched | `MISSING` / `UPSTREAM_NOT_IN_ATLAN` |
| Matched, but at least one was ambiguous | `INVALID` / `UPSTREAM_AMBIGUOUS` |
| A ref the app marked as not expected to resolve (`NOT_EXPECTED`, e.g. a file source) | Ignored; all of them: `VALID_NONE` |

The coverage rate is `VALID / (VALID + INVALID + MISSING)`. `PENDING` and `VALID_NONE` stay out of
the denominator.

`NOT_EXPECTED` matters in practice: on one tenant's replay, 63% of the refs the lookup couldn't
match were file sources or refs the app should never have emitted, so without it a lookup miss
mostly measures app noise.

### Ref outcomes from the lookup (stage 2)

The lookup reports one row per ref; the app's rows join it on `(processQualifiedName,
componentsHash)`:

| Field | Meaning |
| --- | --- |
| `processQualifiedName`, `componentsHash` | Join key (identity v2) |
| `outcome` | `MATCHED`, `MISSED` or `AMBIGUOUS` |
| `candidateCount` | Catalog candidates the ref matched; `AMBIGUOUS` when more than 1 |
| `resolvedQualifiedName`, `connector` | What the lookup picked |
| `missReason` | Why it missed, when the lookup knows (absent, filtered, no searchable connector) |

Publish already computes the outcome per ref (its edge resolve keeps the best candidate per ref and
leaves unmatched refs with no resolved name); it doesn't export it, and the candidate count is lost
when it keeps the best. The shared resolver being built for lineage lookups returns a hit with its
candidates or a miss with a reason, and plans to export failures as rows by connector and reason.
That export is the natural source for this table; the SDK only defines the shape and `settle`.

### Funnel buckets

`funnel.funnel_bucket(assessment)` maps a verdict onto the support `lineage-funnel` labels (one per
closed lineage ticket), so run-time counts can be compared with closed tickets. The bucket comes
from the reason category; a reason can override it. `VALID` and `PENDING` have none; `VALID_NONE`
is a product expectation; a lookup miss that left other upstreams in place has none, because the
funnel's lookup bucket means no source table resolved.

| Category | Bucket |
| --- | --- |
| `PARSER_FAILURE` | Parse error |
| `ASSET_NOT_FOUND` | Assets and query history not extracted |
| `CACHE_MISS` (nothing resolved) | Failure to lookup all source tables |
| `MISSING_METADATA`, `NO_UPSTREAM_DATA`, `UNSUPPORTED_SOURCE_FEATURE`, `NO_SEARCHABLE_DIALECT` | Source-specific lineage derivation |
| `PROCESS_MAPPING_FAILURE` | Lineage process not created |
| `CONNECTOR_LIMITATION` | Product expectation mismatch |
| `RELATIONSHIP_FALLBACK`, `OBSERVABILITY_DISABLED`, `UNINSTRUMENTED_PATH` | none |

The funnel has no bucket for a partial lookup miss or a wrong pick among several candidates. Its
SQL-only buckets (temporary-table chain, outdated DDL, not delivered to QI, target-table lookup)
don't arise for BI apps.

### Ref identity v2

The emitting app and the lookup both hash the ref's `arsIdentity.components`. The hashed fields are
`connectorType|databaseName|schemaName|tableName|name`, where `name` falls back to `columnName`.
That fallback mirrors publish's `COALESCE(name, columnName)` (`app/lib/partitioning/resolve/edges.py`).
v1 hashed `columnName` but not `name`, so every table ref in a schema got the same hash.

Publish has the raw `components_json` at the point it resolves edges. It can compute the same hash
there and join on `(processQualifiedName, componentsHash)`. Not on the input's position: on real
runs the app's ref order and the Process's input order differ for multi-input Processes, so a
positional join pairs the wrong refs. Keifu can compute the same function, but it resolves its own
ETL refs, not the refs a BI app emits.

### Where the output goes

| What | Where | Why |
| --- | --- | --- |
| Per-asset and per-ref rows | The tenant's store: run artifacts, and the app's own `apps.<app>` lakehouse namespace when the app declares `lakehouse: true` | Rows carry asset names; they stay in the tenant |
| Run totals | One `Lineage coverage outcome` log row: counts per verdict as scalars, gap and category counts as one JSON string. Keys are allowlisted in `logger_adaptor`. | Lands in ClickHouse like the asset-validation outcome row (ADR-0020). An app can't add allowlisted keys itself, so this belongs in the SDK. |

Reason codes never go to logs or metrics, only to the per-asset rows.

## Changes from #2204

| #2204 | Here | Why |
| --- | --- | --- |
| The SDK ships no reason codes | The SDK ships generic reasons | The same parse failure in two apps should count as one reason |
| String codes in a dict registry | Reason classes; the registry is built from them | Same as AppErrors; duplicate and malformed codes fail at import |
| Misses only | Every asset gets a row (the Tableau POC rolls fields up into their parent) | The denominator lives in the same table as the misses |
| ARS-dependent assets left out of the denominator | `PENDING` is its own verdict, outside the rate until settled | It's visible, and doesn't drag the rate either way |
| Identity hash v1 | v2, adding `name` | Table refs hashed the same within a schema |

The tracker, the output writer and the interceptor/context from #2204 aren't in this spike yet.

## What the spike tested

The Tableau app was ported onto this module on a test branch. Its reasons became subclasses, its
classifier calls `assess`, and it emits the outcome row. It was then compared with the in-app build
on real runs: an internal tenant (replayed and live), a 1.02M-asset run and six more tenants' latest
runs, all read-only.

| Check | Result |
| --- | --- |
| Rows and lineage output, in-app vs SDK | Identical in all 11 comparisons: asset rows, ref rows and Process files |
| Classifier branches reached by real data | 8 of 10; built-but-partial and inherited-at-row-level only in unit tests |
| Ref hash | Equal to the stored hash on 377 of 377 refs |
| `settle` vs a query-side settle | Same verdict on 51 of 51 pending assets, including 10 seeded cases with known answers. Both sides are our rules, so this is agreement, not a test against publish |
| The settle rule vs publish's own lookup | Publish's edge resolve replayed locally on two tenants' runs and caches, read-only: same outcome on 6,843 of 6,845 and 4,041 of 4,041 refs. Publish's picks match the live graph on 2,645 of 2,696 and 878 of 878 refs |
| A ticket with an engineer-confirmed cause | The pre-fix rows flagged every datasource the fix later recovered (27 of 27, nothing else), including 7 failures that logged no warning; publish's lookup then matched the 2 of 4 tables the engineer predicted |
| Outcome row | Lands in ClickHouse once per run, with the allowlisted keys; 3 days of scheduled runs |
| Time and memory at 1M assets | Within run-to-run noise of the in-app build |

A second app was mapped the same way (atlan-powerbi-app branch `owen/lineage-coverage-sdk`, tag
`lineage-coverage/pbi-spike-1`). Power BI's 26 outcome codes became subclasses of the SDK's
reasons; over its golden corpus (2,199 recorded outcomes, 6 codes) and a live run (1 more code),
every outcome maps to an SDK category and its verdict agrees with Power BI's own. One gap: Power
BI counts expected skips but records no code for them, so those assets can't carry a reason yet.

Two things the tests changed in this design. Lookup outcomes join on `componentsHash`, because ref
positions differ from Process input order and even between two runs of the same build. And an app
can restate an SDK reason under its code to keep its own wording (5 Tableau codes needed it).

## Options Considered

* **A P003-style code prefix (`<CATEGORY>_<SPECIFIC>`)**. Rejected: every row already carries
  `reasonCategory` and `gapCategory`, so the prefix adds nothing and would rename every existing
  code. A conformance rule that checks app reasons subclass an SDK leaf is still worth having.
* **Writing rows through system-workflows**, as preflight results do. Not possible today. The
  `continuous-preflight/check-results` route writes one hard-coded `preflight_check_results` table
  in its own namespace and accepts no table or schema from the caller.
* **An SDK lakehouse writer.** Deferred. pyiceberg would be a new SDK dependency. The Tableau POC's
  writer stays in the app until a second app needs one.

## Open questions

1. Should the SDK ship generic reasons? This reverses #2204's rule.
2. `UPSTREAM_AMBIGUOUS` is counted as `INVALID`, but the lookup's pick may be right. Should it be
   `VALID` with a flag instead? Data point: on one tenant, 775 refs had 2 candidates (the same tables
   crawled through two connections); publish broke every Redshift tie (749) towards the newer
   connection. Neither the app's extract (no server host) nor the catalog (one connection has no
   host) records enough to say whether that's right.
3. `CACHE_MISS` means "lookup miss" under ARS. Rename it now, while the set is unreleased?
4. Fold this into #2204, or open a new PR?
5. Restating an SDK reason by subclassing it under the same code: the right shape, or should
   customer wording live outside the reason class?
6. A field that inherits its parent's verdict gets the parent's gap but keeps its own code. Decided
   for the Tableau port: its category also comes from the parent's reason, so category and gap
   name the same stage; the code stays its own and the row records `inheritedFrom`.
7. Rolling fields up into their parent keeps field column refs only as counts, so stage 2 can't
   settle them per field. For now fields settle from their parent's outcome: the parent is the
   unit people act on. Per-field settle (keeping the refs, at a memory cost) can come later.
8. A whole phase failing (Power BI's report-definition step catches one exception for all of it)
   would surface as `UNINSTRUMENTED_PATH`. A reason for that, or a run-level flag?
9. The funnel mapping names support's Linear labels. Keep it in the SDK, next to the categories it
   must cover (a test enforces that), or move it to the consumer that reads the rows?

## Not in this spike

The generic tracker, the artifact writer, the lookup-side outcome export (proven feasible by the
local replay, not built), and an `adopt-lineage-coverage` skill modelled on `typed-failures`. The Tableau port lives on an app test
branch, not here.
