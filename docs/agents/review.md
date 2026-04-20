# Code Review

PRs to `main` are reviewed by the SDK Reviewer agent running on mothership.

## Trigger

Comment on any PR:

| Command | What It Does |
|---|---|
| `@sdk-review` | Review only — findings posted, author fixes manually |
| `@sdk-review auto-complete` | Review + auto-fix loop (max 3 iterations) |
| `@sdk-review challenge: <reason>` | Re-evaluate disputed findings with your context |
| `@sdk-review override: <reason>` | Force-pass (repo admins only) |
| `@sdk-review stop` | Cancel in-progress review |

Extra context is passed through: `@sdk-review auto-complete Fix type annotations but skip perf findings`

Only repo OWNER/MEMBER/COLLABORATOR can trigger. External fork contributors cannot.

## What It Checks

### Guardrails (Block Merge)

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

### Review Dimensions

- **Architecture** — 11 ADR compliance, dependency direction, contract evolution
- **Security** — secrets, injection, deserialization, multi-tenant isolation
- **Code Quality** — imports, logging, naming, sizing, error handling
- **Test Quality** — coverage gaps, patterns, isolation, edge cases
- **Developer Experience** — API ergonomics, error messages, migration paths
- **Structural** — symptoms vs causes, file health, design coherence

### Cross-Model Review

The review uses two model families to eliminate bias:
- **GPT-5.3-codex** reviews the code (3 domain agents)
- **Claude Opus** challenges every GPT finding (adversarial)
- Findings where the models disagree are dropped (model bias)
- Guardrail violations are always kept regardless of model agreement

## Auto-Complete Loop

When triggered with `auto-complete`:
1. Bot reviews the PR
2. Bot fixes PATCH-scope findings (exact code changes)
3. Bot re-reviews with the new diff
4. Repeats until clean or max 3 iterations
5. MIGRATE/REFACTOR/DESIGN_CHANGE scope findings are left for humans

## Disputing a Finding

Comment `@sdk-review challenge: <your explanation>`. The next review run
re-evaluates all findings against your context:
- If your reasoning is valid: finding is dropped
- If partially valid: severity is downgraded
- If not valid: finding stays with explanation

## Override (Admin Only)

`@sdk-review override: <reason>` — sets the status check to success.
Only repo admins can do this. Logged for audit trail.

## Merge Queue

After a PR is approved, the merge queue workflow automatically:
- Updates the branch when it falls behind main
- Lets GitHub's native auto-merge handle the final squash
- Labels `needs-rebase` if conflicts arise

Enable auto-merge on the PR or add the `auto-merge` label to opt in.

## Reference

- Detailed checklist: `docs/standards/review-checklist.md`
- Severity rubric: managed in mothership `snapshots/sdk-reviewer/severity-rubric.yaml`
- Reference rules: `.claude/skills/sdk-review/references/`
