# Integration Lane Ledger

One number per connector — **IRR** — for how much of the app is actually
protected by an integration test. Nothing is stored: the whole picture is
rebuilt from the repo on every run.

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

Each row states this explicitly in its `boundary` field:

| `boundary` | Meaning | Counts? |
|---|---|---|
| `transformed` | asserts on the app's own `transformed/` output, then stops | yes |
| `post-publish` | continues into system apps and reads back from the tenant | no |

The claim is checked rather than trusted. A lane that verifies past the handoff
needs Atlan tenant credentials to do so — every full-DAG suite in the fleet
gates on `ATLAN_BASE_URL` + `ATLAN_API_KEY` via the same SDK base class. So the
check asks "does this lane need tenant credentials?", not "does it call
publish / lineage / QI" — which system app a DAG invokes varies per connector;
needing a tenant to verify the result does not. One uniform rule, ten repos.

Only the score-inflating direction is an error: claiming `transformed` while
demonstrably reading a tenant fails. Claiming `post-publish` is always accepted,
since detection is a lower bound rather than proof of absence.

This check earned its place immediately — it caught a real bug in the first
draft of this ledger, where tableau's row cited the whole `tests/e2e/` directory
and so swept in `sdr/test_tableau_full_dag.py`, a post-publish lane.

## Where every input comes from

| Input | Source | Hand-maintained? |
|---|---|---|
| product workflows (the denominator) | `@entrypoint` AST scan, or a `run()` override where the app declares none | no |
| the integration/e2e boundary | `app/generated/**/manifest.json` — a node's `workflow_type` is `<connector>:<entrypoint>` for the app's own work, or a bare name (`PublishWorkflow`) for a system app | no |
| which workflow a suite covers | `Scenario.entrypoint`, a declared field (see rule T020) | one line per suite |
| how deeply it validates | `expected_data` → golden, `schema_base_path` → schema-validated. Both are fields the SDK runner acts on, so the depth is what the runner *did*, not a guess about assertions | no |
| cadence | GitHub Actions API | no |

`app_name` is deliberately **not** used to find the boundary: tableau routes its
own process/lineage/post-publish nodes through `automation-engine`, and
salesforce ships an unresolved `{app_name}` placeholder. `workflow_type` is the
reliable discriminator.

## Why derived and not stored

Apps evolve. A stored checklist goes stale the moment someone adds an
entrypoint, and no amount of review discipline fixes that — the file and the
code drift independently. Deriving on every run makes drift structurally
impossible: add an entrypoint and it appears in the denominator, uncovered,
immediately.

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
        workflows with a declared integration lane that validates the app's own
        transformed output, on an automatic green CI job
IRR  =  ──────────────────────────────────────────────────────────────────────
                          the app's product workflows
```

Three ways to miss, each reported per workflow:

- **nothing declares it** — no suite says it covers this workflow
- **declared but unvalidated** — the lane starts the workflow and never checks
  what came out (`schema_base_path` / `expected_data` unset)
- **not automatic, or red** — the job exists but only runs behind a label, a
  manual dispatch, or is failing

A suite naming a workflow the app no longer defines is surfaced separately as an
orphan declaration — that is a real defect, not a scoring detail.

## What it costs a connector

One line per suite:

```python
class TestCrawlerScenarios(BaseIntegrationTest):
    entrypoint = "crawler"
```

or per scenario, when a suite spans several workflows:

```python
Scenario(name="miner run", api="workflow", entrypoint="miner",
         schema_base_path="tests/integration/schema/miner/transformed", ...)
```

Until that lands a repo scores 0, and the report says exactly which workflows
are unaccounted for. That is the honest reading — an undeclared suite is not
attributable to any workflow, so nothing about it is verifiable.

Gated lanes score zero rather than half credit. A lane that does not run does
not protect anything, and partial credit for dormant lanes reintroduces the
zombie-credit failure mode this design exists to avoid. Deliberate, reversible.

## Current state

Every connector scores **0** today, because none declares a workflow entrypoint
in its tests — the field only just exists. The denominators are real and derived:
bigquery 4 workflows, tableau 4, snowflake 2, databricks 5, salesforce 1,
mssql 1, monte-carlo 1.

`tests/fixtures/surveyed-lanes-2026-08.toml` holds a hand-classification of the
same ten repos, done by reading them. It is a **verification fixture** — scoring
never reads it — and it exists so the deriver can be checked against human
judgment.

A hand survey of the same repos found the lanes that *would* qualify once
declared: bigquery 3/3 and monte-carlo 1/1, everything else 0. The finding that
matters more than the number: **the fleet's best validation
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
