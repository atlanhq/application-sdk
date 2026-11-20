---
name: adopt-lineage-coverage
description: >
  Add lineage coverage (ADR-0023) to a connector app: every asset the app emits
  gets a verdict (VALID, INVALID, MISSING, VALID_NONE, PENDING) and, when
  lineage is missing or partial, a reason from application_sdk.observability.lineage.
  Surveys the app's lineage stages first, maps every skip or failure branch to
  an SDK reason, records reasons without touching lineage, proves the app's
  lineage output is unchanged against its unmodified base build, and reports
  the blind spots and the lineage bugs it found for their owners.
mandatory_triggers:
  - "/adopt-lineage-coverage"
  - "add lineage coverage to this app"
  - "instrument lineage coverage"
optional_triggers:
  - "why is lineage missing, per asset"
  - "lineage reasons for this connector"
owner: "TBD - ADR-0023 reviewers"
last_updated: "2026-11-18"
staleness_days: 90
inputs:
  - app_root: "auto-detected - the directory containing app/ and pyproject.toml"
  - a stored run of the app (its pre-lineage inputs), for the base gate in Phase 4
outputs:
  - app/lineage/reasons.py (app reason subclasses + registry)
  - app/lineage/coverage.py (recorder, classifier, writer) - until the SDK ships them
  - small recorder calls at the app's skip branches
  - tests (registry, hooks driven through real code, artifact layout, failure isolation)
  - an instrumentation map and a findings list (printed, not committed)
gates:
  - "Observe only. No change to what the app emits as lineage. Lineage fixes found on the way go to the findings list, never into this change."
  - "Every app reason subclasses an SDK reason (generic or sub-reason). Apps never invent categories."
  - "The app's lineage output is byte-identical to its unmodified base build on a stored run, coverage off and on (Phase 4)."
  - "A recording error stops coverage, never the run."
---

# adopt-lineage-coverage

Add per-asset lineage coverage to a connector app. The end state: each run writes
one row per asset with a verdict and, where lineage is missing or partial, an SDK
reason; table refs handed to the asset lookup are written with the same identity
hash the lookup uses; and one `Lineage coverage outcome` event per run carries the
counts. A support engineer can then answer "why has this asset no lineage" from
the rows, and fleet counts show which gaps matter most.

Worked examples: atlan-tableau-app `owen/lineage-obs-sdk`, atlan-looker-app
`owen/lineage-coverage-sdk` (built from scratch with this method), and
atlan-powerbi-app `owen/lineage-coverage-sdk` (reasons only). Read the Looker one
first: it is the smallest complete port.

---

## Mission

1. **Survey** every lineage stage and every branch where lineage is skipped,
   fails, or is never attempted - including log-only and silent ones.
2. **Map** each branch to the most specific SDK reason, as an app subclass.
3. **Record** reasons and refs without changing lineage.
4. **Prove** lineage output is unchanged against the unmodified base build.
5. **Report** blind spots (branches still unrecorded) and lineage bugs found,
   for their owners. Do not fix them here.

**Zero lineage changes.** If the survey finds a bug, record it as a reason and put
it on the findings list. A fix is a separate branch off the app's main, decided by
whoever consumes the coverage rows, with the connector owner as reviewer.

---

## SDK contract

From `application_sdk.observability.lineage`:

- `Verdict`, `GapCategory`, `ReasonCategory` - closed sets.
- Generic reasons, one per category (`ParserFailure`, `AssetNotFound`,
  `MissingMetadata`, `NoUpstreamData`, `ProcessMappingFailure`,
  `ConnectorLimitation`, `UnsupportedSourceFeature`, `CacheMiss`, ...).
- Sub-reasons, connector-agnostic: source lineage (`CustomSqlParserFailure`,
  `UpstreamIdentityTooSparse`, `NonSqlSource`, ...) and BI-internal
  (`BiUpstreamNotExtracted`, `BiUpstreamExcludedByFilter`,
  `LineageDisabledBySetting`, `BiDefinitionUnavailable`, `BiReferenceUnresolved`,
  `BiReferenceAmbiguous`, `TemplatedSqlUnresolved`, `EdgeBuildFailed`).
- `assess(status, reason)` gives the app-side verdict; `settle()` folds in the
  lookup's per-ref outcomes later.
- `ReasonRegistry(app_reasons)` resolves recorded codes; an unknown code counts
  as `UNINSTRUMENTED_PATH`, so a typo shows up instead of passing as covered.
- `components_hash()` is the ref join key; `coverage_outcome_fields()` builds the
  outcome event.

Until the SDK releases the module, pin the app to the SDK branch by commit in
`[tool.uv.sources]`, commented "spike only, never merge", and say what the pin
moves in the lockfile.

---

## Phase 1 - Survey (no edits)

Build an **instrumentation map**, one row per lineage stage:

| Stage | Lineage it produces | What is recorded today | Branches that drop lineage |
|---|---|---|---|

Stages to look for: extract (what the API returns), resolution of references
between BI assets, SQL parse, source lineage (BI asset to warehouse table),
BI-internal lineage (sheet, tile, look, report, dashboard, and their fields),
write or publish.

For each branch that drops lineage, note: file and line, whether it logs, whether
it is by design (a text tile, a field with no SQL, a customer setting) or a
failure, and which asset id is in scope at that point. Read the code; don't infer
from log messages. Check the sets and dicts the branch iterates: a `list(set(...))`
or a first-match over an unordered collection is a run-to-run flip waiting to
happen, and coverage should see it.

Report the map and confirm with the developer before Phase 2.

---

## Phase 2 - Map branches to reasons

For each branch, pick the most specific SDK reason and subclass it in
`app/lineage/reasons.py`:

```python
class TileUpstreamExploreNotExtracted(BiUpstreamNotExtracted):
    code = "TILE_UPSTREAM_EXPLORE_NOT_EXTRACTED"
    description = "The tile's query names an explore that is not in the run."
```

Rules:

- **Code:** `<ASSET_KIND>_<SPECIFIC>`, UPPER_SNAKE, describing the branch.
- **Inherit, don't restate:** category, gap, verdict and funnel bucket come from
  the parent. Set `expected = True` only when no lineage is the correct outcome by
  design.
- **Owner by whose code fails:** if the source defines lineage implicitly and the
  app fails to build it, it is the app's (for example, Looker reads the table named
  like the view when a view has no `sql_table_name`; a broken fallback for that is
  `EdgeBuildFailed`, not `NoUpstreamTables`). Check the vendor's docs.
- **Full vs partial:** a full reason means the asset ends with no lineage from
  that branch; a partial reason means the link was made but may be incomplete or
  wrong (a first-wins pick among several candidates is `BiReferenceAmbiguous`,
  partial).
- **New SDK sub-reason** only when the same mechanism shows up in two or more
  apps. One app's mechanism stays an app subclass.
- History doesn't change a category: a gap with old tickets is still recorded.

---

## Phase 3 - Record without changing lineage

Prefer reading the app's own final state over instrumenting every success:

- **Verdicts at the end.** After the app has built its lineage, walk its assets:
  linked in the BI tool, refs handed to the lookup (PENDING), or a recorded reason
  (MISSING or VALID_NONE). An asset with none of these is `UNINSTRUMENTED_PATH`.
- **Reasons at skip branches only.** One recorder call where the code skips:
  `coverage.reason(asset_type, asset_id, Reason, partial=..., **detail)`. Key it
  by the exact asset id the final walk uses.
- **Refs exactly as the mapper emits them:** the same identity components, match
  type names and Process qualified name as the published Process, hashed with
  `components_hash()`. Mark refs the app knows can never resolve.
- **Fields roll up** into their parent's row as counts, a few examples per reason,
  and an order-free digest of (field, upstream) pairs, so two runs can be diffed
  for links that moved.
- **Rows** use the keys other apps write (`assetType`, `assetId`, `qualifiedName`,
  `name`, `verdict`, `reasonCode`, `reasonCategory`, `gapCategory`,
  `funnelBucket`, `refCount`, field counts), so one reader serves every app.

Safety:

- Default to a no-op recorder; the real one is passed in. Code that runs with a
  stand-in `self` (tests do this) must tolerate a missing recorder.
- Guard every recording method: the first error turns recording off and the
  summary says FAILED. Never raise into the app.
- Write coverage files **outside** any directory with a single-file contract (a
  Looker run failed every time when they were written under `processed/`). Declare
  them as their own artifact.
- Don't change call signatures other code or tests depend on; pass context through
  an attribute if needed.
- Keep memory flat: spill refs to disk; keep reasons only for failures.

---

## Phase 4 - Prove lineage is unchanged (the gate)

1. Get one stored run's pre-lineage inputs (read-only; delete after; keep only
   aggregate counts; no customer identifiers in code, commits or PRs).
2. Replay the lineage step through three builds, each in its own venv, at the
   **same path** and the same `PYTHONHASHSEED`: the unmodified base, the coverage
   build with coverage off, and with coverage on. Print which module each run
   imported.
3. Compare every lineage output file byte for byte. Any difference must be
   explained. Path-dependent ordering is a harness artifact: absolute paths in a
   set change hash order, so always run from the same path.
4. Record the time and peak memory for each build.

A flag on vs off comparison alone is not enough: the off path still runs the
edited code.

---

## Phase 5 - Real-run checks and report

- Verdict counts per asset type, and the top reasons.
- Flip check: replay under several hash seeds; the field-link digests must not
  move. If they do, the rows have found nondeterminism.
- The coverage reader reads the rows with no app-specific code.
- Tests: every code resolves; each hook driven through the real loader or
  function; coverage on or off loads the same assets; the artifact layout
  declares; a recording error stops coverage, not the run; an unrecorded miss
  shows as `UNINSTRUMENTED_PATH`.
- Run pre-commit and the app's review skill before any commit.

Report: codes mapped, any SDK reason the app needed, branches still unrecorded
(blind spots), cost, and a **findings list** of lineage bugs found, each with
evidence and an owner, kept apart from this change.
