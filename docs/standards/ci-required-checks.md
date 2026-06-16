# Required CI Checks: the always-run + short-circuit rule

This repo runs many PR checks, but only a subset are **required** status checks
in the `main` branch ruleset. Required checks have a sharp failure mode that
every contributor adding or editing CI must understand.

## The problem GitHub hands us

In GitHub you can only mark a status check **required** or not — you cannot
attach "run only when X changed" conditions to a *required* check. And a
required check only clears the merge when it reports a **conclusion**
(`success`/`failure`). Two things leave it stuck *pending forever*, blocking
both the PR and the merge queue:

1. **The workflow never triggers** — e.g. a trigger-level `paths:` filter
   excludes the changed files, so the check context is never created.
2. **The job is skipped** via a job-level `if:` — GitHub reports the run as
   `skipped`, which a required check does **not** treat as `success`.

So we cannot make a required check cheaper by *not running it*. We make it
cheaper by **always running it and short-circuiting its expensive work**.

## The rule

> A required check is a job that **always runs** (no job-level `if:` that can
> yield `skipped`), detects relevance in its **first step**, **short-circuits**
> the heavy steps to `success` when nothing relevant changed, and **always
> concludes**. It must conclude on `pull_request` **and** `merge_group` — never
> gate a required job out on `github.event_name`.

For a **matrix** (where you don't want N required contexts and can't early-exit
cleanly), keep the matrix skippable and expose a single `if: always()`
**aggregator** job as the required context — it passes when the matrix
succeeded *or* was intentionally skipped.

### Canonical examples in this repo

| Pattern | Where |
|---|---|
| `if: always()` aggregator over a matrix | `pull_request.yaml` → `unit-gate` (required: **Unit Tests Gate**) |
| `if: always()` aggregator over series jobs | `conformance-reusable.yaml` → `gate` (required: **conformance / Full suite**) |
| Always-run job, steps gated on an internal change-detect | `pull_request.yaml` → `integration`, `trivy`; `conformance-reusable.yaml` → `ci`, `error-handling` |
| `merge_group` no-op step keeping the context green | `pr-title-convention.yaml`, `pull_request.yaml` → `commits` |
| Early-success when the relevant path is untouched | `allowlist-approval.yaml`, `validate-allowlist.yaml` |

## Anti-patterns (do NOT do these on a required check)

- `if: needs.changes.outputs.<x> == 'true'` at the **job** level — produces a
  `skipped` conclusion → blocks. Move the condition to the **steps** instead.
- A trigger-level `paths:` filter on a workflow whose job is required — the
  context never appears on out-of-scope PRs → blocks. (Trigger-level `paths:`
  is fine for **advisory** checks, e.g. `contract-toolkit-validate.yml`.)
- Gating a required job out with `if: github.event_name != 'merge_group'` — it
  then never concludes in the queue.

## Change-detection buckets (`pull_request.yaml` → `changes`)

The `changes` job runs `dorny/paths-filter` once and exposes named buckets that
downstream jobs short-circuit on. No explicit `base` is set: for
`pull_request`, paths-filter diffs against the PR base automatically; for
`merge_group` it diffs the queued merge ref against the default branch.

| Bucket | Globs | Consumed by |
|---|---|---|
| `sdk` | `application_sdk/**`, `tests/**`, `pyproject.toml`, `uv.lock`, `.github/actions/{unit-tests,setup-deps}/**` | `unit` (→ Unit Tests Gate), `integration`, `docstr`, e2e jobs |
| `scan` | first-party source (`application_sdk`, `examples`, `contract-toolkit`, `tests`) + lockfiles | `trivy` (Trivy Code Scan) |
| `container` | `Dockerfile`, `entrypoint.sh` | `trivy-container` (advisory) |

`sdk` deliberately **excludes** `contract-toolkit/**` (ships its own package +
CI), `docs/**`, and general `.github/**` workflow-meta — so a workflow-only or
toolkit-only PR no longer drags in the full unit + integration matrix. The two
`.github/actions/*` entries are the test harness itself, so changing *how*
tests run still re-runs them. `scan` keeps a wider net for security coverage but
still short-circuits on pure docs / `.github`-meta / `.security`-only PRs.

## Required checks audit

As of this writing the `main` ruleset requires the nine contexts below. All are
short-circuit-safe:

| Required context | Source | Mechanism |
|---|---|---|
| Conventional Commits | `pull_request.yaml` → `commits` | always-run; no-op step on fork/merge_group |
| Validate PR title | `pr-title-convention.yaml` | merge_group no-op |
| Pre-commit Checks | `codeql.yaml` → `lint` | always runs (relevant to all file types) |
| Capability Manifest Drift | `capability-manifest-check.yaml` | always runs; release-PR no-op internally |
| Unit Tests Gate | `pull_request.yaml` → `unit-gate` | `if: always()` aggregator over the matrix |
| conformance / Full suite | `conformance-reusable.yaml` → `gate` | `if: always()` aggregator over C/E series |
| Authorized Approver Check | `allowlist-approval.yaml` | early-success when `.security/` untouched |
| Integration Tests | `pull_request.yaml` → `integration` | always-run; steps gated on `sdk` |
| Trivy Code Scan | `pull_request.yaml` → `trivy` | always-run; steps gated on `scan` |

> **CodeQL** (`codeql.yaml` → `analyze`) is intentionally **not** required and
> skips `merge_group`. Leave it that way — don't "fix" it.

## Operational constraint: the required list is not in this repo

The set of required contexts lives in the GitHub **branch ruleset** (UI/API),
not as code here — changing it needs a repo admin. **Renaming a required job
breaks the gate until an admin updates the ruleset.** Prefer in-job
short-circuits that keep context **names** stable; only introduce a new required
context (e.g. an aggregator) in coordination with a ruleset edit.
