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
the check runs at class creation. `ReasonRegistry` maps a code string back to its class, for rows
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

The coverage rate is `VALID / (VALID + INVALID + MISSING)`. `PENDING` and `VALID_NONE` stay out of
the denominator.

### Ref identity v2

The emitting app and the lookup both hash the ref's `arsIdentity.components`. The hashed fields are
`connectorType|databaseName|schemaName|tableName|name`, where `name` falls back to `columnName`.
That fallback mirrors publish's `COALESCE(name, columnName)` (`app/lib/partitioning/resolve/edges.py`).
v1 hashed `columnName` but not `name`, so every table ref in a schema got the same hash.

Publish has the raw `components_json` at the point it resolves edges. It can compute the same hash
there and join on `(processQualifiedName, ordinal)`. Keifu can compute the same function, but it
resolves its own ETL refs, not the refs a BI app emits.

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
   `VALID` with a flag instead?
3. `CACHE_MISS` means "lookup miss" under ARS. Rename it now, while the set is unreleased?
4. Fold this into #2204, or open a new PR?

## Not in this spike

The generic tracker, the artifact writer, moving the Tableau app onto this module (with a row-parity
check against the in-app build), the publish-side outcome hook, and an `adopt-lineage-coverage`
skill modelled on `typed-failures`.
