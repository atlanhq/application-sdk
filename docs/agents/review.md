# Code Review

PRs to `main` are reviewed on request by **lens**, an AI reviewer that runs
in `.github/workflows/lens.yml`. Comment `/lens` on a PR to start a review;
a push never triggers one. Only OWNER, MEMBER, or COLLABORATOR comments
can trigger it, and bot comments are refused. Maintainers can also start a
review with `workflow_dispatch` from the Actions tab; that path has no bot
check of its own, only the repo's Actions write permission.

Each run continues from the previous one on the same PR. lens posts a sticky
summary comment and, when every finding is fixed or dismissed, an `atlan-ci`
code-owner approval. `/lens force` bypasses the unchanged-head and round-cap
rules; the per-PR cost cap still applies.

The old `@sdk-review` / `@sdk-resolve` (mothership) triggers are retired. A
comment starting with either from a human OWNER, MEMBER, or COLLABORATOR is
answered with a pointer to `/lens`; other comments get no reply.

## Where lens is configured

| What | Where |
|---|---|
| Model, cost cap, round limits | `.github/lens/config.toml` |
| Which review cards apply to which paths | `.github/lens/rules.toml` |
| Review cards (what to check per area) | `.github/lens/cards/` |
| Reviewer code | `.github/scripts/lens/` |

Config is read from the base branch only, so a PR cannot retune its own
reviewer.

## Review checklist

Use this when reviewing a PR by hand or with an agent.

### Guardrails

| ID | Name | Trigger |
|----|------|---------|
| G1 | Security Blockers | Any Critical security finding |
| G2 | Contract Safety | Field removed/renamed/retyped on Input/Output (breaks Temporal replay) |
| G3 | Determinism | Non-deterministic ops in `run()`/`@entrypoint` |
| G4 | Test Coverage | New public API without tests |
| G5 | Secret Safety | Hardcoded secrets or credentials in logs |
| G6 | Breaking API | Public export removed without deprecation shim |
| G7 | CI Must Pass | All required CI checks must be green |
| G8 | Branch Mergeable | No unresolvable conflicts |

### CI Cost

Applies to any change under `.github/workflows/`, a reusable workflow, or
a bootstrap template. The rules and the reasoning behind them are in
[CI cost](../standards/ci.md#ci-cost). Anything in a template or reusable
runs in ~140 repos, so judge it by its fleet total.

- New billed jobs — could the work be steps in an existing job?
- "Nothing to do" filters sit on the job's `if:`, not on steps
- No matrix whose legs finish in under a minute
- Every PR-triggered workflow cancels superseded runs, or explains why not
- Every `upload-artifact` sets `retention-days`
- A new `workflow_run` trigger states its fan-in; a new cron justifies
  its frequency
- No check re-runs on PR, merge queue and release with unchanged inputs
