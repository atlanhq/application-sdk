# Integration Lane Ledger

A hand-maintained, citation-backed inventory of which connector workflows are
actually protected by an integration test — and one number, **IRR**, derived
from it.

## What problem this solves

The test-readiness scorecard's integration tier is fed by JUnit pass-rate,
`coverage.py` line coverage, and tier-presence. All three are volume metrics.
An app whose integration tests hit a real source and assert only
`{"success": is_true(), "data": is_list()}` grades acceptably under all three
while verifying nothing about what the pipeline produced.

## Scope of "integration test"

Runs from the **source system**, through the app's own pipeline, ending at the
app's **handoff artifact** — the `transformed/` object-store prefix that system
apps consume. System apps (publish / lineage / QI) are **not** executed; that is
e2e. This matches the tiering architecture already described in rule T011's
rationale.

Note that lane *location* does not decide qualification — several connectors
keep qualifying lanes under `tests/e2e/` (bigquery, monte-carlo, databricks).
What matters is what the lane exercises, not which directory it sits in.

## Why an inventory and not runtime tracing

Three instrumentation designs were attempted and discarded before this one.
Each failed the same way: an automated evidence source that only sees part of
the fleet, presented as a fleet-wide number.

- A Temporal interceptor sees only dbt, fivetran and snowflake. monte-carlo,
  tableau, bigquery and powerbi test in-process, below the App layer.
- A `@task`/`@entrypoint` decorator wrapper fixes none of those four:
  bigquery's suite makes zero App-layer calls, powerbi's rigorous lanes are
  helper-level, and tableau's replay harness monkeypatches `upload_to_atlan`
  and skips uploads entirely without a Dapr sidecar.
- Reading `expected_data` / `schema_base_path` out of SDK `Scenario` artifacts
  is blind to the five deepest suites in the fleet, which use repo-local
  harnesses, and its denominator is gameable — mssql scores 18% on it while
  having the second-best posture, and could nearly double that by *deleting*
  its shallow auth scenarios.

Ten repos use five different harness styles. No single instrumentation point
sees them all. A hand-audited ledger sees all of them by construction, and its
bias is legible — which beats a dozen mechanisms whose combined bias nobody can
re-audit.

## The number

```
        workflows with a lane at realism ∈ {L,R} AND depth ∈ {G,V} AND cadence = A
IRR  =  ─────────────────────────────────────────────────────────────────────────
                              total product workflows
```

Denominator = the app's product-workflow declarations, minus ledger-listed
exclusions (each needing a reason and a ticket).

The ledger is TOML rather than YAML: the conformance package deliberately ships
only pydantic, jsonschema and jinja2, and `tomllib` is stdlib on the supported
Python floor.

## What stops it rotting

Two of the three axes are machine-verified; only `depth` is trusted from the
ledger.

| Axis | Source of truth |
|---|---|
| denominator | AST scan of the repo's product-workflow declarations (`@entrypoint`, or a `run()` override where the app declares none). Mismatch with the ledger is a **hard failure**, so adding a workflow breaks CI until someone classifies it. |
| `cadence` | GitHub Actions API, via `evidence.ci_workflow` / `evidence.ci_job`. Writing `A` in the ledger earns nothing if the job does not run on an automatic trigger. |
| `realism`, `depth` | Human, citation-backed, reviewed like code. Audited by the quarterly mutation sample. |

Gated lanes score zero rather than half credit. A lane that does not run does
not protect anything, and partial credit for dormant lanes reintroduces exactly
the zombie-credit failure mode this design exists to avoid. This is a
deliberate, reversible judgment call.

## Current state (surveyed 2026-08-05/06)

| Connector | IRR |
|---|---|
| bigquery | 3/3 |
| monte-carlo | 1/1 |
| snowflake | 0/2 |
| mssql | 0/1 |
| databricks | 0/2 |
| dbt | 0/1 |
| tableau | 0/2 |
| powerbi | 0/2 |
| fivetran | 0/1 |
| salesforce | 0/1 |
| **fleet** | **4/16 ≈ 25%** |

The finding that matters more than the number: **the fleet's best validation
machinery is built and switched off.** Snowflake's schema suites, mssql's
Pandera lanes, databricks' Pandera suites and tableau's 14 live suites all
exist and all sit behind labels, env flags, or no CI job at all. The cheapest
points on the board are un-gating actions, not writing tests:

1. Nightly cron for snowflake's full mode (`mode: full` / `E2E_RUN_WORKFLOW=1`)
2. Nightly cron for mssql with `E2E_RUN_WORKFLOW=1`
3. Wire tableau's t-suites into a scheduled job, and populate
   `tests/replay/registry.yaml` — the nightly replay currently runs a synthetic
   micro smoke against an empty registry
4. Un-skip fivetran's baseline scenario (capture the KAA-720 baseline)
5. Add one salesforce workflow scenario with a schema directory

## What is deliberately not measured

- **Config / filter / auth-axis coverage.** Unmeasurable fairly across contracts
  this heterogeneous: flat per-value units punish rich contracts, and
  per-class normalization pays a premium for contract poverty (monte-carlo would
  bank full marks for one hidden single-value enum while snowflake needs five
  live IdP tenants). Auth appears as an informational column only, never in the
  number.
- **Data-shape corpus adequacy** — e.g. WARE-2350, where Jinja2 `striptags`
  swallowed `<=`. No metric can see whether a golden corpus happens to contain
  the pathological input. Growing the replay corpus is the only attack, so it is
  an action item rather than a formula term.
- **Interaction pairs and row volumes.** Conceded unrepresentable at acceptable
  cost. The mutation badge is the safety net.

## Falsification

Abandon or rework this if any of the following holds:

1. **Mutation check** — if IRR-covered workflows do not catch seeded transform
   mutations at a materially higher rate than uncovered ones across two
   quarterly samples, the depth classification is not measuring quality. Drop
   the number, keep the ledger as documentation.
2. **Drift check** — if a spot audit one quarter in finds >20% of cells no
   longer matching the cited code, human classification has failed.
3. **Gaming check** — if teams relabel `C`-depth lanes as `V` to move IRR, the
   citation-review premise has failed.
