# Code Review

PRs to `main` are reviewed by AI agents. Two reviewers currently run in
**parallel** so the team can compare outputs side-by-side:

| Trigger | Reviewer | Backend | Workflow file |
|---|---|---|---|
| `@sdk-review` | Production reviewer (stable) | Anthropic's `claude-code-action` | `.github/workflows/claude.yml` |
| `@test-sdk-review` | Experimental reviewer | Mothership Rover Direct API, with orchestration in `.mothership/pr-review/` | `.github/workflows/test-sdk-review.yml` |

Both can be triggered on the same PR. They produce separately-marked
comments, set distinct commit-status checks (`sdk-review` vs
`test-sdk-review`), and write to separately-prefixed labels — so they
don't collide.

## Trigger surface (the experimental `@test-sdk-review` flow)

Comment on any PR:

| What you type | What happens |
|---|---|
| `@test-sdk-review` | Standard review via the mothership Rover backend. The orchestration picks the right mode based on PR state and prior review history. |
| `@test-sdk-review <free-form text>` | Same, but the trailing text is forwarded as `COMMENTER_INTENT`. The agent interprets it and decides what to do — auto-fix, dispute a finding, stop, override, focus on a specific area, etc. |

Examples:

- `@test-sdk-review please also fix the lint warnings` — review and
  auto-fix any PATCH-scope findings.
- `@test-sdk-review focus on the new metric reader code` — standard
  review, but the agent weights that area extra.
- `@test-sdk-review stop` — cancel an in-flight review.
- `@test-sdk-review override: rule does not apply to internal helper` —
  admin force-pass (the orchestration verifies the commenter is a repo
  admin before honouring this).
- `@test-sdk-review challenge: ARCH-001 — this is intentional because …`
  — re-evaluate a specific finding with the author's reasoning.

There is no command enumeration in the workflow YAML. All intent
inference happens in `.mothership/pr-review/ORCHESTRATION.md`
(§Intent Inference) so it can be tuned without editing CI.

## Trigger surface (the production `@sdk-review` flow)

Refer to the existing `claude.yml` workflow and the
`.claude/skills/sdk-review/` rules for the production reviewer. The
command surface there is unchanged from before this experimental
parallel reviewer was introduced.

Only repo OWNER, MEMBER, or COLLABORATOR can trigger. External fork
contributors cannot.

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

- **Claude Opus 4.6** reviews the code (3 domain agents in parallel)
- **GPT-5.3-codex** challenges every finding (adversarial)
- Findings where the models disagree are dropped (model bias)
- Guardrail violations are always kept regardless of model agreement

## Auto-Fix Loop (experimental reviewer only)

When the human's intent text asks the experimental agent to fix (e.g.
`@test-sdk-review apply fixes`):

1. Review the PR
2. Apply PATCH-scope findings (exact code changes from the review)
3. Push the fix commit
4. Re-review with the new diff
5. Repeat until clean or max 3 iterations

The whole loop runs inside the same Cloudflare sandbox via Rover
Direct's `session_id` resume — no re-dispatch from CI. MIGRATE,
REFACTOR, and DESIGN_CHANGE scope findings are left for humans.

## Disputing a Finding (experimental reviewer)

Comment `@test-sdk-review challenge: <your explanation>` (or any
equivalent phrasing — the orchestration parses intent, not exact
commands). The next review run re-evaluates the cited findings
against your context:

- If your reasoning is valid: finding is dropped
- If partially valid: severity is downgraded
- If not valid: finding stays with explanation

## Override (Admin Only, experimental reviewer)

`@test-sdk-review override: <reason>` — sets the `test-sdk-review`
status check to success. Only repo admins can do this; the
orchestration verifies via
`gh api repos/<repo>/collaborators/<commenter>/permission` before
honouring the override. Logged for audit trail.

## Merge Queue

After a PR is approved, the merge queue workflow automatically:

- Updates the branch when it falls behind main
- Lets GitHub's native auto-merge handle the final squash
- Labels `needs-rebase` if conflicts arise
- Auto-triggers `@test-sdk-review` (the experimental mothership reviewer)
  if post-update CI fails on an approved PR — whichever reviewer
  originally approved (production label `sdk-review-approved` or
  experimental label `test-sdk-review-approved` both qualify the PR)

Enable auto-merge on the PR or add the `auto-merge` label to opt in.

## Required configuration

The `test-sdk-review` and `sdk-evolution-cron` workflows (mothership
flows) need these repo-level secrets and variables. The production
`@sdk-review` flow (claude.yml) uses its own existing configuration
unchanged.

| Kind | Name | Purpose |
|---|---|---|
| Secret | `HARNESS_TOKEN` | Bearer token for mothership's `/api/sandbox/execute` (must be a valid entry in mothership's `SANDBOX_API_KEYS` map). |
| Secret | `GLOBALPROTECT_USERNAME` | VPN auth — `mothership.atlan.dev` is on the private network. |
| Secret | `GLOBALPROTECT_PASSWORD` | VPN auth. |
| Variable | `GLOBALPROTECT_PORTAL_URL` | VPN portal URL. |
| Variable | `MOTHERSHIP_URL` | e.g. `https://mothership.atlan.dev`. |

`GITHUB_TOKEN` inside the sandbox is auto-injected by mothership from
its GitHub App installation — do not try to override it via `env_vars`.

## Reference

- Orchestration: `.mothership/pr-review/ORCHESTRATION.md`
- Reviewer identity: `.mothership/pr-review/CLAUDE.md`
- Severity rubric: `.mothership/pr-review/severity-rubric.yaml`
- Reference rules: `.mothership/pr-review/references/`
- Mothership Rover Direct API: `docs/reference/rover-direct-api.md` in
  `atlanhq/mothership`.
