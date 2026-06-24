# Lineage Observability Framework

> **Status:** Core library shipped (PR application-sdk#2204, branch `feat/lineage-observability-core`).
> Reference integrations: Tableau (PR-2) and Sigma (PR-5) implemented.
> **Owning ADR:** [`docs/adr/0016-lineage-observability-framework.md`](../adr/0016-lineage-observability-framework.md)

This is the definitive guide to the lineage-observability framework that lives in
`application_sdk/observability/lineage/`. It covers **why** it exists, **what** is in
the SDK, **where** everything lives, and **how** to wire it into any BI connector.

---

## 1. TL;DR

Lineage in Atlan is `Process` / `ColumnProcess` entities. For BI connectors, every
"lineage-capable" asset is *expected* to produce a lineage edge — but today, when one
doesn't, it fails **silently** (`return None` / `continue` at transform, or a
`noMatchAction='drop'` absence at publish). You cannot see how much lineage you are
losing, or why.

The framework flips that default:

> **Assume every lineage-capable asset will get lineage. The moment you skip it, record a structured reason.**

That single discipline produces two payoffs:

- **Proactive** — one coverage % + reason-category breakdown per run → Segment/Mixpanel dashboard.
- **Reactive** — a per-asset JSONL artifact (`assetId → reason → details`) for root-cause debugging.

A connector adopts it by importing the SDK, declaring a small reason registry, and
adding `register` / `mark` / `record` calls at its lineage decision points. The engine,
schemas, identity hash, ContextVar interceptor, and metric emission all come from the SDK.

---

## 2. Why — the problem and the goal

### 2.1 The two-stage native-vs-ARS model (universal across BI connectors)

Every BI connector (Tableau, Sigma, Looker, MicroStrategy, PowerBI) creates lineage in
**two stages**:

| Stage | Edge kind | Resolved | Output |
|-------|-----------|----------|--------|
| **Native** | BI→BI (intra-tool, e.g. workbook→dataset, model→model) | at **transform** time, by the connector | `Process`/`ColumnProcess` with `hasLineage=True` |
| **ARS** | BI→warehouse (e.g. dataset→Snowflake table) | at **publish** time, by the publish-app | connector emits an **unresolved `arsIdentity`** (`components`, `matchTypeNames`, `noMatchAction='drop'`); publish JOINs it against the warehouse connector's cache and *then* mints the Process |

Both stages must be instrumented for full coverage. The connector only knows the
**denominator** (which assets are lineage-capable); the publish-app is where the largest
silent losses happen.

### 2.2 How a miss dies silently today

- **At transform:** a branch hits `continue`/`return None` and the edge is simply never written.
- **At publish (ARS):** `noMatchAction='drop'` (the universal default) discards an unresolved
  warehouse upstream. Worse, `arsNoNestedMatchAction='drop'` wipes an **entire `Process`** if
  any one of its inputs misses — likely the single largest silent loss, and today encoded as
  pure absence (nothing to count, nothing to explain).

Coverage is unobservable and misses are unexplained. That is the problem.

### 2.3 The goal

1. **Proactive monitoring** — a coverage % and a reason-category breakdown emitted as one
   business event per run, so a Mixpanel/Prometheus dashboard shows lineage health over time,
   sliceable by `connector_type × stage × category`.
2. **Reactive resolution** — a per-asset artifact that says *exactly which asset* missed and
   *why*, so support/engineering can RCA a specific tenant's missing lineage without a code dive.
3. **Reusable & extensible** — one implementation in the SDK; a new connector gets coverage by
   importing it, not by re-implementing it. (This is Milestone 2 of the Linear *Tableau Lineage
   Observability* project: "a lineage observability integration framework for BI connectors.")
4. **Never break lineage** — all observability work is *fail-open*. An observability error
   degrades and logs; it never blocks lineage generation.

---

## 3. Where it lives — placement & architecture

### 3.1 Central in the SDK (ADR-0016, Decision A)

The framework lives at **`application_sdk/observability/lineage/`** — the single stable import
surface for every connector repo **and** the publish-app. Rejected alternatives (per ADR-0016):
per-connector copies (N-way duplication + drift), a standalone package (ceremony without payoff),
leaving it in `marketplace-scripts` (deprecated argo world), putting it in publish (misses the
transform side), or a dedicated microservice (over-engineering + new failure mode).

The decisive reason: the publish-app already depends on the SDK, so it imports the **same**
`components_hash` — eliminating connector↔publish identity-key drift, the single hardest
correctness risk, *by construction*.

### 3.2 The taxonomy split (load-bearing rule)

> **The SDK owns the top-level codes + the machinery. Connectors own the subcodes.**

- **SDK owns:** the frozen `ReasonCategory` enum (12 members) and the generic machinery
  (`ReasonCode`, `ReasonCodeRegistry`, `get_reason_category`). It ships **no concrete subcode
  registry** — there is no `TABLEAU_REASON_CODES` or `SIGMA_REASON_CODES` in the SDK.
- **Connectors own:** their granular subcodes in their own repo (`app/lineage/reasons.py`), each
  mapped to exactly one SDK category. The publish-app owns the ARS subcodes.

This keeps the SDK free of connector-specific variables/statuses/behavior, and lets a connector
change its taxonomy **without an SDK release**.

### 3.3 Engine provenance (ADR-0016, Decision B)

The in-activity tracker engine is a **verbatim port of the argo `LineageObservabilityTracker`**
(proven at scale — LendingClub ~250k assets, VMO2). `build_output()` is **byte-stable**, locked by
the ported argo test suite as a regression gate. The only additions are two methods that touch new
state only (`emit_intent`, `success_keys`). Everything *above* the engine (distributed merge, ARS
stitch, artifact schemas, the ContextVar interceptor, metric emission) is net-new and AE-native.

### 3.4 Package layout

```
application_sdk/observability/lineage/
├── __init__.py        # public surface (__all__) + create_tracker() factory
├── tracker.py         # LineageObservabilityTracker (the engine) — byte-stable build_output()
├── noop_tracker.py    # NoOpLineageObservabilityTracker — zero-cost methods
├── context.py         # get/set/reset_lineage_tracker (ContextVar accessors)
├── types.py           # ReasonCategory, ReasonCode, ObservabilityConfig, Stage,
│                       #   LineageStatus, RunContext, IntentEdge
├── registry.py        # ReasonCodeRegistry, get_reason_category (machinery only)
├── identity.py        # components_hash, canonical_identity_string, stitch_key,
│                       #   IDENTITY_FIELDS, IDENTITY_SCHEMA_VERSION
├── schema.py          # AssetRecord, CoverageSummary, ArsEdgeInfo (pydantic, camelCase)
├── writers.py         # ChunkedOutputHandler, write_coverage_json, create_asset_details_handler
└── metrics.py         # MissingLineageMetrics (Protocol)

application_sdk/execution/_temporal/interceptors/lineage.py
                       # LineageObservabilityInterceptor — scopes the ContextVar per activity
```

---

## 4. What's in the SDK — the API surface

Everything below is importable from `application_sdk.observability.lineage`.

### 4.1 The factory (start here)

```python
def create_tracker(
    connector_type: str = "",
    config: Optional[ObservabilityConfig] = None,
    metrics: Optional[MissingLineageMetrics] = None,
    asset_details_handler: Optional[ChunkedOutputHandler] = None,
    *,
    run_context: Optional[RunContext] = None,
    registry: Optional[ReasonCodeRegistry] = None,
) -> Union[LineageObservabilityTracker, NoOpLineageObservabilityTracker]
```

- Returns a **NoOp** tracker when `config.enabled is False` (explicit `False` check) — zero
  per-row cost, the kill switch.
- Otherwise returns the real tracker, registers it in the ContextVar (`set_lineage_tracker`), and
  attaches `run_context`/`registry` for the telemetry + distributed layers.

### 4.2 The tracker methods

The four you will use in 95% of integrations:

```python
tracker.register_asset(asset_type, asset_id, qualified_name=None, should_have_lineage=True)
tracker.mark_output_lineage(asset_type, asset_id, qualified_name=None, source="", source_details=None)
tracker.record_missing_reason(asset_type, asset_id, reason, details=None, qualified_name=None)
tracker.record_failed_path_attempt(asset_type, asset_id, path, reason, details=None, qualified_name=None)
```

| Method | Meaning | Critical semantics |
|--------|---------|--------------------|
| `register_asset` | Declare an asset exists and whether it should have lineage (the **denominator**). Idempotent. | `should_have_lineage=False` → denominator-excluded but still visible. |
| `mark_output_lineage` | The asset got a real lineage edge (the **numerator**). | **Clears any previously-recorded miss** (`_clear_missing_state`) and sets `hasLineage=True`. |
| `record_missing_reason` | Terminal: this asset will get no lineage; here's why. | **FIRST-WINS** — a no-op if the asset already `hasLineage` **or** already has a recorded reason. |
| `record_failed_path_attempt` | Diagnostic: one path failed but another might succeed. | Non-terminal; appended to `failedPaths[]`. Does **not** count as a miss. |

Less common:

```python
tracker.mark_input_lineage(...)          # informational; does NOT set hasLineage
tracker.apply_relationship_lineage(...)  # sets hasLineage via relationship (not a Process)
tracker.emit_intent(edge: IntentEdge)    # ARS BI→warehouse intent → PENDING (AE-additive)
tracker.success_keys() -> set[str]       # "{assetType}:{assetId}" for hasLineage assets (reduce numerator)
```

Output:

```python
tracker.build_output() -> dict   # byte-stable coverage summary (see §4.3)
tracker.write_asset_details()    # flush per-asset JSONL via asset_details_handler
tracker.flush() -> dict          # write_asset_details() + build_output()
```

### 4.3 `build_output()` shape (exact, byte-stable)

```jsonc
{
  "totals":            { "totalAssets", "shouldHaveLineage", "withLineage", "missingLineage" },
  "coverage":          { "totalLineageCoverage", "shouldHaveLineageCoverage" },  // percentages
  "totalsByType":      { "<AssetType>": { ...counts, "coverage": {...} } },
  "totalsByReason":    { "<REASON_CODE>": <count> },
  "totalsByTypeAndReason": { "<AssetType>": { "<REASON_CODE>": <count> } },
  "totalsByFailedPath":   { "<path>:<reason>": <count> },
  "config":            { "logSuccessfulLineage", "totalAssetsInOutput", "totalAssetsTracked" }
}
```

Coverage math:
`shouldHaveLineageCoverage = withLineage / shouldHaveLineage * 100`,
`totalLineageCoverage = withLineage / totalAssets * 100` (both `0.0` when the denominator is 0).
The NoOp `build_output()` returns `{}`.

### 4.4 Types

- **`ReasonCategory(str, Enum)` — 12 frozen members** (10 from argo + 2 AE additions). These are
  the **only** reason-derived value ever used as a metric label (low cardinality):
  `PARSER_FAILURE`, `ASSET_NOT_FOUND`, `CACHE_MISS`, `UNSUPPORTED_SOURCE_FEATURE`,
  `MISSING_METADATA`, `NO_UPSTREAM_DATA`, `NO_SEARCHABLE_DIALECT`, `PROCESS_MAPPING_FAILURE`,
  `CONNECTOR_LIMITATION`, `RELATIONSHIP_FALLBACK`, **`OBSERVABILITY_DISABLED`** (distinguishes an
  off run from a genuine 0%), **`UNINSTRUMENTED_PATH`** (a path with no coverage; keeps it from
  masquerading as 100%).
- **`ReasonCode`** — frozen dataclass `(code: str, category: ReasonCategory, message: str)`. The
  high-cardinality, connector-specific code; for RCA artifacts only, **never** a metric label.
- **`ObservabilityConfig`** — `enabled=True`, `log_successful_lineage=False`, `emit_intent=True`,
  `validate_reason_codes=True`, `auto_derive=False`, `fail_open=True`.
- **`Stage(str, Enum)`** — `TRANSFORM | PUBLISH | END_TO_END`. A **dimension** ("where it died"),
  orthogonal to the category ("why"). Not a category.
- **`LineageStatus(str, Enum)`** — `HAS_LINEAGE | PARTIAL_LINEAGE | MISSING_LINEAGE | PENDING`.
- **`RunContext`** — `(connector_type, workflow_id, run_id="", tenant="", stage=..., activity_id="")`.
  Note: `workflow_id` is the **stable** workflow id, not the per-attempt run id, so partials survive
  retries / continue-as-new.
- **`IntentEdge`** — an unresolved ARS edge; `identity_hash` property = `components_hash(components)`;
  `ordinal` is derived from the **sorted** components tuple, not emission order.

### 4.5 Registry (machinery only)

```python
registry = ReasonCodeRegistry(SIGMA_REASON_CODES)        # dict[str, ReasonCode]
registry.category("SIGMA_FIELD_NO_COLUMN_LINEAGE_MATCH")  # -> ReasonCategory (warns → UNINSTRUMENTED_PATH if unknown)
get_reason_category(code, SIGMA_REASON_CODES)             # -> Optional[ReasonCategory] (None if unknown)
```

### 4.6 Identity (the ARS stitch key)

```python
components_hash(components)        # SHA-256 of canonical, case-normalized identity
IDENTITY_FIELDS                    # ('connectorType','databaseName','schemaName','tableName','columnName')
IDENTITY_SCHEMA_VERSION            # = 1; stitch refuses cross-version joins
```

`components_hash` normalizes **asymmetrically**: `connectorType` is lower-cased, all other identity
fields are **UPPER-cased** (to match the publish resolver's `UPPER()` JOIN), in the fixed
`IDENTITY_FIELDS` order (not dict order), with nulls → empty string. Both the connector and the
publish-app import this one function, so the key can never drift.

### 4.7 Schema, writers, metrics, interceptor

- **`AssetRecord` / `CoverageSummary`** (`schema.py`) — pydantic models (camelCase on the wire,
  accept snake_case in) for the v2 artifacts; carry `schemaVersion`, `identitySchemaVersion`, run
  identity, `stage`, and an `ars` sub-record.
- **`ChunkedOutputHandler`** (`writers.py`) — line-oriented JSON writer; a falsy `chunk_size` writes
  a single `{prefix}.json`, otherwise rolls `{prefix}-{n}.json`. `write_coverage_json` and
  `create_asset_details_handler` are convenience helpers.
- **`MissingLineageMetrics`** (`metrics.py`) — a `@runtime_checkable` Protocol with one method,
  `missing_lineage_event(reason: str)`, called once per recorded miss. Implement it (or pass `None`).
- **`LineageObservabilityInterceptor`** (`execution/_temporal/interceptors/lineage.py`) — wraps
  `execute_activity` and calls `reset_lineage_tracker()` at activity start **and** in `finally`, so
  the ContextVar can't leak across activities on a reused thread. It does **no aggregation** — that
  is the (future) finalize step's job.

---

## 5. The coverage model in one paragraph

`register_asset(should_have_lineage=True)` adds to the **denominator**. `mark_output_lineage` adds
to the **numerator** and clears any miss. `record_missing_reason` records a terminal miss (first
one wins; cleared if the asset is later marked). Because `record_missing_reason` is **first-wins +
no-op-once-marked**, you can safely:

1. `register` every capable asset up front,
2. `mark` it wherever an edge is emitted,
3. `record` a specific reason at each skip branch, and
4. run a **global backstop** at the end that records a generic reason for everything still unmarked —

…and the backstop will never clobber a more specific reason or a success. This is the canonical
instrumentation pattern.

---

## 6. How to implement it in a new connector

Six steps. Tableau and Sigma both follow this exact recipe.

### Step 1 — Declare your reason registry (`app/lineage/reasons.py`)

```python
from application_sdk.observability.lineage import ReasonCategory, ReasonCode

MYCONN_REASON_CODES: dict[str, ReasonCode] = {
    "MYCONN_ELEMENT_NO_PARSED_QUERY": ReasonCode(
        "MYCONN_ELEMENT_NO_PARSED_QUERY",
        ReasonCategory.NO_UPSTREAM_DATA,
        "Element has no parsed query (no SQL captured / parser did not run).",
    ),
    "MYCONN_FIELD_NO_COLUMN_MATCH": ReasonCode(
        "MYCONN_FIELD_NO_COLUMN_MATCH",
        ReasonCategory.MISSING_METADATA,
        "Field did not match any parsed upstream column (calculated/renamed).",
    ),
    # ... one entry per distinct skip branch in your transform code
}
```

Guidance:
- One code per *distinct* skip branch — granular codes are the RCA payload.
- Map each to the **most accurate** category. Be specific: prefer `MISSING_METADATA` over a vague
  bucket; reserve `CACHE_MISS` for an actual cache miss (e.g. v3 ARS has **no** connection cache, so
  an unresolvable upstream there is a *sparse identity* = `MISSING_METADATA`, never `CACHE_MISS`).
- Use `CONNECTOR_LIMITATION` for "we structurally can't do this yet", `UNSUPPORTED_SOURCE_FEATURE`
  for deferred source kinds.

### Step 2 — Add the coverage-event emitter (`app/metrics.py`)

Emit **one** event per run, rolled up to the category axis (the only low-cardinality reason
dimension), labelled `send_to_segment="true"`:

```python
from application_sdk.observability.lineage import get_reason_category
from application_sdk.observability.metrics_adaptor import get_metrics
from application_sdk.observability.models import MetricType
from app.lineage.reasons import MYCONN_REASON_CODES

def emit_lineage_coverage_event(coverage: dict, workflow_args: dict | None = None) -> None:
    if not coverage:                       # NoOp tracker returns {} → clean no-op
        return
    by_reason = coverage.get("totalsByReason", {})
    by_category: dict[str, int] = {}
    for reason, count in by_reason.items():
        cat = get_reason_category(reason, MYCONN_REASON_CODES)
        key = cat.value if cat else "UNINSTRUMENTED_PATH"
        by_category[key] = by_category.get(key, 0) + int(count)

    totals, cov = coverage.get("totals", {}), coverage.get("coverage", {})
    get_metrics().record_metric(
        name="myconn_lineage_coverage",
        value=1,
        metric_type=MetricType.GAUGE,
        labels={
            "send_to_segment": "true",
            "connector": "myconn",
            "total_assets": str(totals.get("totalAssets", 0)),
            "should_have_lineage": str(totals.get("shouldHaveLineage", 0)),
            "with_lineage": str(totals.get("withLineage", 0)),
            "should_have_lineage_coverage": str(round(cov.get("shouldHaveLineageCoverage", 0.0), 2)),
            # non-scalar values MUST be JSON strings — the metrics layer drops dict/list labels
            "totals_by_category": json.dumps(by_category, sort_keys=True),
            "totals_by_reason": json.dumps(by_reason, sort_keys=True),
            # + connection_qualified_name / workflow_id / tenant from workflow_args
        },
    )
```

### Step 3 — Add the flag (module level)

```python
_ENABLE_LINEAGE_OBSERVABILITY = (
    os.environ.get("ENABLE_LINEAGE_OBSERVABILITY", "true").strip().lower() in {"true", "1"}
)
```
Default-ON is the low-cost (misses-only) mode; `false` → NoOp, zero cost.

### Step 4 — Create ONE tracker per run and thread it through every processor

In the orchestrating `@task` (e.g. `process_raw_metadata`):

```python
from application_sdk.observability.lineage import (
    ObservabilityConfig, ReasonCodeRegistry, create_tracker,
)
from app.lineage.reasons import MYCONN_REASON_CODES
from app.metrics import emit_lineage_coverage_event

tracker = create_tracker(
    "myconn",
    ObservabilityConfig(enabled=_ENABLE_LINEAGE_OBSERVABILITY),
    registry=ReasonCodeRegistry(MYCONN_REASON_CODES),
)

process_v2_metadata(..., tracker=tracker)            # workbook/element path
process_v2_data_models_from_files(..., tracker=tracker)  # data-model path — SAME tracker
```

Each processor takes the tracker with a **NoOp guard** so it's safe to call unconditionally and
testable in isolation:

```python
from application_sdk.observability.lineage import NoOpLineageObservabilityTracker

def process_v2_metadata(..., tracker: Optional[Any] = None):
    if tracker is None:
        tracker = NoOpLineageObservabilityTracker()
    ...
```

> One tracker per *run*, not per asset. Threading the same instance through all processors makes a
> single coverage summary span every asset type the run produced.

### Step 5 — Instrument the lineage sites (register / mark / record + backstop)

```python
# Denominator: register every lineage-capable asset
tracker.register_asset("MyConnElement", element_id)                    # should_have_lineage=True
tracker.register_asset("MyConnColumn", col_key, should_have_lineage=bool(formula))  # exclude non-capable

# Numerator: mark wherever an edge is actually emitted
out["element-tables"].append(row)
tracker.mark_output_lineage("MyConnElement", element_id)

# Misses: record a specific reason at each skip branch
if not parsed_query:
    tracker.record_missing_reason("MyConnElement", element_id, "MYCONN_ELEMENT_NO_PARSED_QUERY")
    continue

# Backstop: catch anything that produced no edge from any path (safe — first-wins + cleared-by-mark)
for element_id in all_registered_elements:
    tracker.record_missing_reason("MyConnElement", element_id, "MYCONN_ELEMENT_NO_LINEAGE")
```

### Step 6 — Flush + emit (best-effort)

Back in the orchestrating `@task`, after all processors run:

```python
try:
    coverage = tracker.build_output()          # or .flush() to also write per-asset artifacts
    if coverage:
        emit_lineage_coverage_event(coverage, {
            "connection_qualified_name": connection_qn,
            "workflow_id": input.workflow_id,
            "workflow_run_id": input.workflow_run_id,
        })
except Exception:
    logger.warning("lineage coverage emit failed", exc_info=True)   # telemetry NEVER fails the activity
```

### Gotchas checklist

- [ ] **First-wins:** record the most specific reason at the *first* skip; later records are no-ops.
- [ ] **Mark clears misses:** an asset recorded-then-marked ends up covered — rely on this for the backstop.
- [ ] **NoOp guard** at every processor entry; never call methods on `None`.
- [ ] **Denominator honesty:** `should_have_lineage=False` for assets that genuinely can't have lineage
      (e.g. a formula-less column) — don't inflate the denominator, don't fake the numerator.
- [ ] **One event per run**, `send_to_segment="true"`, non-scalar labels as `json.dumps(...)` strings.
- [ ] **Flag default-ON**, NoOp when off.
- [ ] Don't register phantom/uncrawled assets (it inflates the denominator) — use
      `record_failed_path_attempt` for edge-level diagnostics that shouldn't be a terminal miss.

---

## 7. Reference implementations

### 7.1 Tableau (PR-2) — reference impl #1

- `app/lineage/reasons.py` — `TABLEAU_REASON_CODES` (~38 codes), relocated out of the SDK per the
  taxonomy split.
- `app/lineage/bi_sql.py` — `generate_bi_sql_lineage()` creates the tracker (NoOp vs real gated on the
  flag) with an `asset_details_handler`, runs the BI→SQL lineage generator with ~75 `register`/`mark`/
  `record` sites, then `build_output()` → `lineage-coverage.json` + `write_asset_details()` → per-asset
  JSONL.
- `app/tableau.py` — the `ENABLE_LINEAGE_OBSERVABILITY` flag + `emit_lineage_coverage_event`
  (`tableau_lineage_coverage`).
- Note: the engine here is the **byte-identical** SDK import, so the M1 argo Mixpanel dashboard and
  debugging guide keep working.

### 7.2 Sigma (PR-5) — reference impl #2

- `app/lineage/reasons.py` — `SIGMA_REASON_CODES` (**32 codes** across **6 categories**:
  `NO_UPSTREAM_DATA`, `MISSING_METADATA`, `ASSET_NOT_FOUND`, `PARSER_FAILURE`,
  `CONNECTOR_LIMITATION`, `UNSUPPORTED_SOURCE_FEATURE`; **zero `CACHE_MISS`** — v3 has no connection
  cache).
- `app/metrics.py` — `emit_activity_event` + `emit_lineage_coverage_event` (`sigma_lineage_coverage`).
- `app/sigma_app.py` — flag, `create_tracker` in `process_raw_metadata`, threads the **one** tracker
  into both `process_v2_metadata` (workbook path) and `process_v2_data_models_from_files` (data-model
  path), then `build_output()` + emit.
- `app/processors/v2.py` — workbook path: `SigmaDataElement` / `SigmaDataElementField` register/mark/
  record, incl. `SIGMA_ELEMENT_NO_FIELDS` for fieldless elements.
- `app/processors/data_models.py` — data-model path: `SigmaDataModel` + `SigmaDataModelColumn`
  register/mark/record + the **global backstop**.
- Validated locally: a real tenant dump = **81.3%** should-have-lineage coverage with the asset math
  closing exactly; a synthetic data-model fixture = 15/15 branch assertions.

---

## 8. The layers above the engine (designed; phased delivery)

The in-activity tracker is the foundation. Three layers sit above it (full design in
`research/02-architecture.md`). They are **net-new** (argo had none) and ship in later PRs.

### 8.1 Distributed aggregation (multi-activity connectors, e.g. Looker = 31 transform activities)

A process-local tracker can't aggregate across pods. Each activity writes **attempt-scoped,
overwrite-only** partials to the object store:

- `<partial_id>.coverage.json` — advisory, attempt-scoped (never read as truth).
- `<partial_id>.assets.jsonl` — misses-only per-asset RCA detail.
- `<partial_id>.ledger.jsonl` — **success keys** `{assetType, assetId, lineageSource}` — essential
  because the misses-only JSONL drops successes, so the merge can't recover the numerator without it.
- `<partial_id>.intent.parquet` — ARS edges emitted (for the stitch).

A connector-invoked **finalize `@task`** (the last DAG step — *not* a sandboxed interceptor) merges by
**recomputing all counts from merged per-asset state + replaying miss-cancellation** — never naive
summing (re-feeding JSONL through a fresh tracker double-counts and loses the numerator). A
`_manifest.json` of expected partial IDs is written before fan-out; the finalize flags
`partialsMissing` rather than silently undercounting. Partials are namespaced by the **stable
`workflow_id`** so they don't orphan on retry.

### 8.2 The connector↔publish ARS stitch (true end-to-end coverage)

- **Transform:** connector calls `emit_intent(IntentEdge)` → the asset is `PENDING` (covered-but-pending,
  *not* a miss; transform-stage coverage doesn't penalize the connector for publish's work).
- **Publish:** emits a **resolution row at every drop site** (turning today's silent absence into rows):
  single-edge drops, unrouted entities, **whole-parent drops** (`arsNoNestedMatchAction='drop'`), and
  Atlas-remove 404s.
- **Reconcile** (owned by the publish finalize — the only stage seeing both sides) joins intent ↔
  resolution at **two grains**:
  - *edge grain* — `(components_hash, direction, ordinal)` → matched / fallback / partial / drop;
  - *parent grain* — `parent_qualifiedName` → a whole-parent drop demotes the intent Process to
    `MISSING_LINEAGE` **regardless** of individual edge outcomes (this is how the biggest silent loss
    becomes a *counted* miss).
- `endToEndCoverage = (withLineage + partialLineage) / shouldHaveLineage` is the **headline** —
  post-stitch only. Transform-stage coverage is a drill-down until the stitch is live per connector.

### 8.3 Telemetry sinks (strict separation)

- **Prometheus** — low-cardinality only: a coverage gauge + a `missing.by_category` counter labelled
  `category, stage, connector_type`. **Never** `run_id` / `tenant` / `total_assets` (cardinality blowup).
- **Mixpanel/Segment** — exactly one business event per run (`send_to_segment="true"`), carrying
  `total_assets`, coverage, and `top_categories` as a JSON **string** (dicts are dropped by the metrics
  adaptor). One dashboard, sliceable by `connector_type × stage × category`.

---

## 9. Roadmap — done vs pending

| PR | Scope | Status |
|----|-------|--------|
| **PR-1** | SDK core library (engine port + types + identity + schema + writers + context + interceptor + factory) | **Done** — application-sdk#2204 |
| **PR-2** | Tableau: flip disable flag → env-gated; wire Mixpanel emit → first live dashboard | **Done** (local; pending SDK publish) |
| **PR-5** | Sigma: instrument v2 + data-model processors; flag; emit | **Done** (committed `feat/lineage-observability`) |
| **PR-3** | Publish: ARS drop-row emission (makes the biggest silent loss visible) | Pending |
| **PR-4** | Distributed reduce engine + finalize `@task` | Pending |
| **PR-6** | ARS intent↔resolution stitch (end-to-end coverage) | Pending |
| **PR-7** | Looker daft 31-activity instrumentation (columnar adapter) | Pending |
| **PR-8** | Telemetry hardening + dashboard | Pending |
| **PR-9** | Opt-in auto-derivation + MDLH sink | Pending |
| **PR-10** | Re-crawl-deletion handling (flagged gap) | Pending |

Adoption ordering rationale: PR-1 unblocks everything; PR-2/PR-3/PR-5 are mutually independent
quick wins (near-zero-cost dashboard, biggest-loss visibility, two more connectors); the hard novelty
(distributed merge, ARS stitch) is isolated in PR-4/PR-6 where review attention belongs.

---

## 10. Operational notes

- **Default-ON, low cost.** `enabled=True` + `log_successful_lineage=False` → tracker active,
  misses-only detail + success ledger, one Mixpanel event, no per-success detail. Bounded memory at
  200k+ assets.
- **Kill switch.** `ENABLE_LINEAGE_OBSERVABILITY=false` → `create_tracker` returns NoOp; every call is a
  no-op; `build_output()` returns `{}`; emit is skipped. Zero per-row cost.
- **Fail-open is a hard constraint.** Every observability path is wrapped so an error degrades + logs
  but never breaks lineage generation. Keep emit/flush in `try/except`.
- **`get_lineage_tracker()` never returns `None`** — it defaults to a shared stateless NoOp, so it's
  safe to call unconditionally deep in a call stack (the ContextVar route, as an alternative to
  threading the tracker explicitly).
- **Versioned identity.** If you change the hashed field set or normalization, bump
  `IDENTITY_SCHEMA_VERSION`; the stitch refuses cross-version joins.

---

## 11. References

- ADR: [`docs/adr/0016-lineage-observability-framework.md`](../adr/0016-lineage-observability-framework.md)
- Design docs (lineage-observability workspace): `research/01-understanding.md` (instrumentation map),
  `research/02-architecture.md` (full architecture + 10-PR plan), `research/03-tableau-reason-audit.md`,
  `research/04-sigma-instrumentation-map.md`.
- PRs: application-sdk#2204 (core); Tableau `feat/lineage-observability`; Sigma `feat/lineage-observability`.
- Related ADRs: 0012 (observability stack), 0013 (failure taxonomy — distinct from `ReasonCategory`),
  0014 (two-store storage — the partials substrate).
