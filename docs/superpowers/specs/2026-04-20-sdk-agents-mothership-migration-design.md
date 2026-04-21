# SDK Agents → Mothership Migration Design

**Date:** 2026-04-20 (revised)
**Author:** Vaibhav Chopra + Claude
**Status:** Draft
**Scope:** Migrate sdk-review and sdk-evolution to mothership rover-native snapshots
**Repo:** https://github.com/atlanhq/mothership (beta branch — snapshots + dispatch) + https://github.com/atlanhq/application-sdk (workflows/config)
**Depends on:** Mothership beta environment, VPN, llmproxy.atlan.dev, Linear API, Cloudflare sandbox

---

## 1. Problem Statement

The current `@sdk-review` and `/sdk-evolution` pipelines run as Claude Code skills triggered by GitHub Actions. This architecture has persistent reliability issues:

- **Status check stuck pending** — GHA runner timeout or silent failure leaves `sdk-review` in `pending` forever.
- **PR noise pollution** — Duplicate review comments from multiple GHA runs.
- **Linear ticket state drift** — "Done" while PRs open, parent not updated, orphaned tickets without PRs.
- **Evolution not completing** — Stops mid-run with no recovery. No visible stage markers.
- **Gate 2 too lenient** — Stale findings on unchanged code slip through.
- **Branch-keeper failures** — Fails on conflicts, token expiry, race conditions.
- **Auto-complete unreliable** — Comment-based self-triggering (`@sdk-review --iteration=N`) misses events.

**Root cause:** No persistent state, no crash recovery, no enforced transitions, GitHub comments as state bus.

---

## 2. Solution: Rover-Native Snapshots

Mothership beta has **replaced LangGraph multi-node pipelines** with **rover-native execution**: a single Claude Code CLI run per task inside a Cloudflare sandbox, driven by snapshot configuration. The old `agents/pr_code_reviewer/` graph has been deleted.

We follow this pattern exactly:

- **`snapshots/sdk-reviewer/`** — replaces `@sdk-review` and `@sdk-review auto-complete`
- **`snapshots/sdk-evolution/`** — replaces `/sdk-evolution` nightly pipeline
- **Dispatch via `POST /api/pr-review/dispatch`** (reviewer) and new cron-triggered dispatch (evolution)
- **`submit_review` tool** — server-side review posting with rubric clamping, dedup, label swap
- **Sandbox isolation** — each run gets its own Cloudflare DO, credential proxy, ephemeral

**Why not LangGraph:** The beta branch proves that Claude Code CLI + snapshot is simpler, more reliable, and already battle-tested. The old graph approach is being removed from mothership.

---

## 3. Architecture Overview

```
SDK REVIEWER:
  GHA comment (@sdk-review) → GHA workflow → POST /api/pr-review/dispatch
    → Create sandbox (unique DO per dispatch)
    → Write snapshot files (ORCHESTRATION, modes, agents, references, rubric)
    → Write session files (PR.md, DIFF.patch, POLICY.md, CUSTOM/)
    → Run Claude Code CLI (Opus 4.6, --output-format stream-json)
    → Claude follows ORCHESTRATION.md 5-phase loop
    → Claude calls submit_review tool (curl to proxy)
    → Handler: rubric clamp, diff-scope validation, dedup, post review
    → Sandbox destroyed

SDK EVOLUTION:
  Cron (nightly 2am UTC) → POST /api/sdk-evolution/dispatch
    → Create sandbox (unique DO)
    → Write snapshot files (ORCHESTRATION, discovery agents, references, rubric)
    → Write session files (codebase index from R2, suppression list)
    → Run Claude Code CLI (Opus 4.6)
    → Claude follows ORCHESTRATION.md multi-stage pipeline
    → Claude calls submit_evolution tool (curl to proxy)
    → Handler: create Linear tickets, create fix PRs, trigger sdk-reviewer dispatches
    → Sandbox destroyed
```

---

## 4. Repository Structure

```
mothership/snapshots/
  sdk-reviewer/                         # NEW: SDK PR review snapshot
    snapshot.yaml                       # extends: pr-reviewer
    ORCHESTRATION.md                    # 5-phase SDK review loop
    CLAUDE.md                           # SDK reviewer identity + rules
    modes/
      standard.md                       # SDK-specific standard review checklist
      auto-complete.md                  # Auto-fix loop orchestration
    agents/
      correctness.md                    # ARCH + SEC + BUG sub-agent
      quality.md                        # QUAL + TEST + DX sub-agent
      structure.md                      # STRUCT + root cause sub-agent
      adversarial.md                    # Cross-model challenge (GPT via proxy)
      disprove.md                       # Per-finding disprove sub-agent
      reachability.md                   # Caller chain analysis
    references/
      v3-architecture-rules.md          # 11 ADRs
      code-quality-rules.md
      security-rules.md
      test-quality-rules.md
      dx-rules.md
      structural-rules.md
      performance-rules.md
    severity-rubric.yaml                # SDK-specific severity definitions
    repos/
      application-sdk/
        modes/
          standard.md                   # Repo-specific additive rules
        POLICY.md                       # By-design patterns for SDK

  sdk-evolution/                        # NEW: Nightly evolution snapshot
    snapshot.yaml                       # extends: _base
    ORCHESTRATION.md                    # Multi-stage evolution pipeline
    CLAUDE.md                           # Evolution identity + guardrails
    agents/
      code-quality.md                   # Discovery sub-agent
      architecture.md                   # Discovery sub-agent (11 ADRs)
      test-quality.md                   # Discovery sub-agent
      v2-patterns.md                    # Discovery sub-agent
      bug-hunter.md                     # Discovery sub-agent
      security.md                       # Discovery sub-agent
      performance.md                    # Discovery sub-agent
      gate1-challenger.md               # GPT cross-model challenge prompt
      gate2-validator.md                # Fix validation prompt
    references/
      (same rule files as sdk-reviewer)
    tools.md                            # submit_evolution endpoint + Linear tools

mothership/harness/api/routers/
  sdk_evolution_dispatch.py             # NEW: Cron + manual dispatch for evolution

mothership/rover-worker/src/
  submit-evolution.ts                   # NEW: Handler for evolution results
```

---

## 5. Model Strategy

**Same bias elimination principle:** primary and adversarial are always different model families.

| | Primary | Adversarial | Fix/CI |
|---|---|---|---|
| **SDK Reviewer** | GPT-5.3-codex (Wave 1, 3 sub-agents) | Claude Opus 4.6 (Wave 2 challenge) | Opus for fixes, Sonnet for CI/conflicts |
| **SDK Evolution** | Claude Opus 4.6 (7 discovery agents) | GPT-5.3-codex (Gate 1 challenge) | Opus for fix PRs, Sonnet for Gate 2 |

**How cross-model works in sandbox:**
Claude Code CLI runs as the orchestrator. For GPT calls, Claude uses the credential proxy:

```bash
# Inside sandbox, Claude calls GPT via proxy
curl -s "$PROXY_BASE/proxy/llm/chat/completions" \
  -H "Authorization: Bearer $PROXY_JWT" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-5.3-codex",
    "messages": [{"role": "system", "content": "<adversarial prompt>"},
                 {"role": "user", "content": "<findings + diff>"}],
    "max_tokens": 12000
  }'
```

The proxy routes through llmproxy.atlan.dev. Claude never sees the real API key.

---

## 6. SDK Reviewer Snapshot

### `snapshot.yaml`

```yaml
name: sdk-reviewer
description: >
  SDK v3 multi-model PR reviewer with auto-fix loop.
  GPT-5.3-codex primary review + Claude Opus adversarial.
  Supports @sdk-review and @sdk-review auto-complete.
extends: pr-reviewer
```

Inherits the pr-reviewer's dispatch flow, submit_review tool, proxy setup, and cleanup. Overrides ORCHESTRATION, CLAUDE, modes, agents, references.

### `ORCHESTRATION.md` — 5-Phase SDK Review Loop

```markdown
# SDK Review Orchestration

You are reviewing a PR against the Atlan application-sdk v3.
Follow these phases EXACTLY. Do not skip phases.

## Phase 0: Orient (~15s)

1. Read `session/PR.md` for PR metadata, diff stats, commit SHA
2. Read `session/DIFF.patch` (authoritative diff from GitHub API)
3. Clone repo: `git clone --depth=50 /opt/repo-cache/application-sdk /workspace/repo`
4. Checkout PR: `git fetch origin pull/<N>/head && git checkout <sha>`
5. Read repo's `CLAUDE.md` for conventions
6. Determine review mode from session/PR.md:
   - `@sdk-review` → standard mode, auto_fix = false
   - `@sdk-review auto-complete` → standard mode, auto_fix = true
   - `@sdk-review override: <reason>` → override mode (check admin permission)
7. Read `modes/standard.md` for the review checklist
8. Read `modes/auto-complete.md` if auto_fix = true

## Phase 1: Context Gather (~30s)

1. Holistic file assessment (NO LLM — use grep/wc/ast-grep):
   - For each changed file: line count, caller count, domain tags
   - Flag DUMPING_GROUND if file has 3+ unrelated domains
   - Flag V3_REPLACEMENT if a v3 equivalent exists
   - Flag DEPRECATED if file has deprecation warnings

2. Dispatch sub-agents IN PARALLEL:
   - agents/reachability.md — who calls the changed functions?
   - agents/structure.md — root cause assessment, file health

## Phase 2: Review (~90s)

1. Dispatch 3 GPT-5.3-codex review sub-agents IN PARALLEL via proxy:
   For each agent, POST to $PROXY_BASE/proxy/llm/chat/completions with:
   - model: "gpt-5.3-codex"
   - The agent prompt from agents/{name}.md
   - Full PR diff, file contents, holistic annotations, reference rules

   Agents:
   a. agents/correctness.md — [ARCH] [SEC] [BUG] findings
   b. agents/quality.md — [QUAL] [TEST] [DX] findings
   c. agents/structure.md — [STRUCT] findings, root cause assessment

2. Parse all 3 responses. Collect findings.

3. Dispatch Opus adversarial challenge (YOU are Opus — do this yourself):
   Read agents/adversarial.md for instructions.
   For each GPT finding, evaluate: AGREE / DISAGREE / PARTIAL.
   Discover any findings GPT missed.

4. De-bias (deterministic — no LLM):
   - GPT >= 90% + Opus AGREE → keep
   - GPT >= 80% + Opus AGREE → keep
   - GPT >= 80% + Opus DISAGREE → drop
   - GPT >= 80% + Opus PARTIAL → keep, downgrade severity
   - Opus-only >= 90% → keep (blind spot)
   - Guardrail violation → ALWAYS keep

5. Apply guardrails G1-G8 (see CLAUDE.md)

6. Determine verdict:
   - BLOCKED: any G1/G2/G3/G5 violation
   - NEEDS_HUMAN: any DESIGN_CHANGE scope
   - NEEDS_FIXES: any Critical, G4/G6 violated, 3+ Important, CI failing
   - READY_TO_MERGE: no Critical, < 3 Important, CI passing

## Phase 3: Submit Review (~30s)

Call submit_review via curl (see tools.md).
Include all findings, inline comments for BLOCKING/CRITICAL/HIGH.
Retry up to 3x on 422, once on 5xx.

## Phase 4: Auto-Fix (only if auto_fix = true AND verdict = NEEDS_FIXES)

Read modes/auto-complete.md for the fix loop:
1. For each PATCH-scope finding (all severities):
   - Read full file, understand context
   - Apply the exact fix from the finding's fix instructions
   - Run: uv run pre-commit run --files <changed_files>
   - Run: uv run pytest tests/unit/ -x --timeout=60
   - If fails → revert this fix, note why
2. Commit + push
3. Re-run Phase 0-3 (gather new diff, re-review)
4. If verdict = READY_TO_MERGE → Phase 5
5. If iteration >= 3 → submit with NEEDS_HUMAN verdict
6. Otherwise → repeat Phase 4

## Phase 5: Finalize (only if READY_TO_MERGE)

1. Submit final review with approval_recommendation = APPROVE
2. The handler will approve the PR and swap labels
```

### `CLAUDE.md` — SDK Reviewer Identity

```markdown
# SDK Reviewer Identity

You are reviewing PRs for the Atlan application-sdk v3.
This SDK enables connector builders to build Temporal-backed metadata extractors.

## SDK Architecture (Critical Context)

- App = Temporal Workflow + Activities
- run()/@entrypoint MUST be deterministic (no datetime.now, uuid4, random, I/O)
- @task methods handle all I/O (retryable, heartbeatable)
- Contracts: one Input, one Output per method (Pydantic BaseModel)
- Infrastructure: Dapr-backed state/secret/pubsub via Protocols
- Dependency direction: app/ → execution/ → infrastructure/ (never reverse)
- CredentialRef in contracts, CredentialResolver in @task only
- run_in_thread() for blocking operations in @task

## Guardrails (Block Merge)

G1: Security critical findings → BLOCKED
G2: Contract field removal/rename/retype → BLOCKED (breaks Temporal replay)
G3: Non-deterministic ops in run()/@entrypoint → BLOCKED
G4: New public API without tests → Critical finding
G5: Hardcoded secrets/credentials in logs → BLOCKED
G6: Breaking API without deprecation shim → Critical finding
G7: CI must pass → noted in review
G8: Branch must be mergeable → attempted auto-resolve

## Rules

- Follow ORCHESTRATION.md exactly
- Read-only on repos (no git push from this snapshot)
- Use submit_review tool to post review (never post directly)
- PATCH scope findings: include exact fix code
- MIGRATE scope: include migration path
- DESIGN_CHANGE scope: flag for human, never auto-fix
- Builder perspective: "Does this make building a connector easier or harder?"

## Cross-Model Review

GPT-5.3-codex is your primary reviewer (via proxy). YOU are the adversarial.
Challenge every GPT finding. Catch what GPT rationalizes away.
Also catch what GPT misses (your blind spots are different).
```

### `severity-rubric.yaml` — SDK-Specific

```yaml
categories:
  - id: temporal-determinism
    patterns:
      - pattern_id: io-in-workflow
        canonical_severity: BLOCKING
        max_severity: BLOCKING
        description: "I/O operation in run()/@entrypoint method"
        override_allowed: false
      - pattern_id: non-deterministic-in-workflow
        canonical_severity: BLOCKING
        max_severity: BLOCKING
        description: "datetime.now/uuid4/random in run()/@entrypoint"
        override_allowed: false

  - id: contract-safety
    patterns:
      - pattern_id: field-removed
        canonical_severity: BLOCKING
        max_severity: BLOCKING
        description: "Field removed from Input/Output contract"
        override_allowed: false
      - pattern_id: field-renamed
        canonical_severity: BLOCKING
        max_severity: BLOCKING
        description: "Field renamed on Input/Output contract"
        override_allowed: false
      - pattern_id: field-type-changed
        canonical_severity: BLOCKING
        max_severity: BLOCKING
        description: "Field type changed on Input/Output contract"
        override_allowed: false
      - pattern_id: mutable-default
        canonical_severity: HIGH
        max_severity: CRITICAL
        description: "Mutable default on Input/Output field (use Field(default_factory=...))"
        override_allowed: true

  - id: blocking-operations
    patterns:
      - pattern_id: blocking-in-task
        canonical_severity: HIGH
        max_severity: CRITICAL
        description: "Blocking operation in @task without run_in_thread()"
        required_conditions_for_critical:
          - "Long-running operation (>5s)"
          - "Task has heartbeat enabled"
        override_allowed: true

  - id: infrastructure-abstraction
    patterns:
      - pattern_id: direct-temporal-import
        canonical_severity: HIGH
        max_severity: HIGH
        description: "Direct temporalio import outside execution/_temporal"
        override_allowed: false
      - pattern_id: direct-dapr-import
        canonical_severity: HIGH
        max_severity: HIGH
        description: "Direct dapr import outside infrastructure/_dapr"
        override_allowed: false

  - id: api-surface
    patterns:
      - pattern_id: public-api-no-test
        canonical_severity: CRITICAL
        max_severity: CRITICAL
        description: "New public API without corresponding test"
        override_allowed: false
      - pattern_id: breaking-change-no-deprecation
        canonical_severity: CRITICAL
        max_severity: CRITICAL
        description: "Public export removed without deprecation shim"
        override_allowed: false

  - id: credential-safety
    patterns:
      - pattern_id: raw-credential-in-contract
        canonical_severity: HIGH
        max_severity: CRITICAL
        description: "Raw credential value in Input/Output (use CredentialRef)"
        override_allowed: false
      - pattern_id: credential-in-log
        canonical_severity: BLOCKING
        max_severity: BLOCKING
        description: "Credential or secret logged"
        override_allowed: false

  - id: error-handling
    patterns:
      - pattern_id: bare-except
        canonical_severity: MEDIUM
        max_severity: HIGH
        description: "Bare except clause swallowing errors"
        override_allowed: true
      - pattern_id: silent-exception
        canonical_severity: HIGH
        max_severity: HIGH
        description: "Exception caught and not re-raised or logged"
        override_allowed: true
```

### Large PR Handling

In `ORCHESTRATION.md`, Phase 1 includes:

```markdown
## Large PR Handling

If diff_stats shows > 2000 lines changed:
  - Partition files by directory for sub-agents
  - CORRECTNESS gets: app/, execution/, credentials/, contracts/, infrastructure/
  - QUALITY gets: tests/ + common/ + remaining source
  - STRUCTURE gets: file list + top 10 most-changed files full content

If diff_stats shows > 20000 lines changed:
  - Classify files: HIGH_RISK (critical dirs, __init__.py), MEDIUM, LOW
  - Review HIGH_RISK fully, spot-check MEDIUM, skip LOW
  - Note in review: "N low-risk files not reviewed"
```

---

## 7. SDK Evolution Snapshot

### `snapshot.yaml`

```yaml
name: sdk-evolution
description: >
  Nightly SDK codebase evolution. Discovers bugs, architecture violations,
  security issues, v2 remnants. Creates Linear tickets + fix PRs.
  Hands off to sdk-reviewer for review.
extends: _base
```

### `ORCHESTRATION.md` — Multi-Stage Evolution Pipeline

```markdown
# SDK Evolution Orchestration

You are running the nightly SDK evolution pipeline.
Follow ALL stages. NEVER stop early. Print stage markers.

## Stage 0: Preflight

1. Pull latest: git checkout main && git pull origin main
2. Read session/SUPPRESSION.md for existing tickets and open PRs
3. Read session/INDEX.md for the codebase index (from R2 persistent storage)
4. Determine scan mode:
   - If session/SCAN_MODE says "full" (Sunday or manual) → scan everything
   - Otherwise → incremental (scan changed files + dependency cone)
5. Print: [Stage 0/8 complete] → Proceeding to Stage 1

## Stage 1: Update Index

1. Run index update script (pure Python, zero LLM):
   python /workspace/.mothership/scripts/update_index.py
   This:
   - AST-parses changed files
   - Updates import/call graph
   - Re-classifies dumping grounds
   - Spot-checks 5 random files for staleness
   - Writes updated INDEX.md
2. Read the updated INDEX.md
3. Determine scan_targets from index
4. Print: [Stage 1/8 complete] → Proceeding to Stage 2

## Stage 2: Discovery (7 sub-agents, PARALLEL)

Dispatch ALL 7 sub-agents simultaneously using the Agent tool.
Each receives: codebase index, scan_targets file contents, their reference rules, suppression list.

1. agents/code-quality.md
2. agents/architecture.md
3. agents/test-quality.md
4. agents/v2-patterns.md
5. agents/bug-hunter.md
6. agents/security.md
7. agents/performance.md

Collect all findings. Pre-filter:
- Finding on file NOT in scan_targets (and not full scan) → kill
- Finding on file with zero callers + severity low/medium → kill
- Finding on deprecated file + category != security → kill

Print: [Stage 2/8 complete] → Found N raw findings, M pre-filtered. Proceeding to Stage 3

## Stage 3: Gate 1 — GPT Challenges (PARALLEL, batches of 5)

For each surviving finding, call GPT-5.3-codex via proxy:
POST to $PROXY_BASE/proxy/llm/chat/completions with gate1-challenger.md prompt.

GPT verdict: survived | killed.
Cross-model: Opus discovered → GPT challenges.

Print: [Stage 3/8 complete] → N survived, M killed. Proceeding to Stage 4

## Stage 4: Create Tickets + Fix PRs

For each Gate 1 survivor:
1. Create Linear sub-ticket (call Linear API via proxy)
2. Create fix branch from main
3. Apply fix with TDD where required (bug/security/performance → regression test)
4. Run pre-commit + pytest
5. Commit + push + create PR via gh
6. Link PR to Linear ticket
7. IF PR creation fails → cancel ticket immediately (no orphans)

Print: [Stage 4/8 complete] → Created N tickets, M PRs. Proceeding to Stage 5

## Stage 5: Gate 2 — Validate Fixes

For each PR (deterministic + Sonnet for edges):
1. Does PR diff touch the flagged file? If not → kill
2. Finding on unchanged code? If incremental → kill
3. Test included where required? If not → needs_changes
4. Diff minimal? >200 lines for non-critical → needs_changes
5. CI passing? If not → needs_changes
6. (Sonnet only if above passed) Does fix actually solve the finding?

Kill → close PR + cancel Linear ticket with reason.

Print: [Stage 5/8 complete] → N passed, M killed. Proceeding to Stage 6

## Stage 6: Handoff to SDK Reviewer

For each PR that passed Gate 2:
Call submit_evolution tool with the PR details.
The handler dispatches an sdk-reviewer sandbox for each PR.

Print: [Stage 6/8 complete] → Handed N PRs to SDK Reviewer. Proceeding to Stage 7

## Stage 7: Summary

1. Gather metrics (discovered, killed, survived, PRs, handed off)
2. Call submit_evolution with final summary
3. Handler updates Linear parent ticket with summary table
4. Handler audits Linear ticket consistency
5. Security audit: check all PR descriptions for secrets/tokens

Print: [Stage 7/8 complete] → Summary posted. Run complete.

========================================
 SDK Evolution — {date}
========================================
Findings: N discovered → M survived Gate 1
PRs:      P created → Q passed Gate 2
Handoff:  R PRs sent to SDK Reviewer
========================================
```

### Codebase Index via R2 Persistent Storage

Mothership beta has R2 persistent storage mounted at `/workspace/.shared-memory`. The codebase index lives there:

```
/workspace/.shared-memory/sdk-evolution/
  index.json                 # CodebaseIndex (files, import graph, call graph)
  last_run.json              # Last run metadata (date, SHA, metrics)
```

The dispatch handler writes `session/INDEX.md` from R2 before Claude starts. The `update_index.py` script (shipped in the snapshot) reads index.json, updates it incrementally, and writes it back. On the next run, the dispatch handler reads the updated index from R2.

**Staleness prevention** (same as before):
- SHA mismatch → incremental update
- >40% files changed → full rebuild
- Sunday or >7 days since full rebuild → full rebuild
- Spot-check 5 random files per run
- 3-day TTL on index

---

## 8. Dispatch Endpoints

### SDK Reviewer: Use Existing PR Review Dispatch

The existing `POST /api/pr-review/dispatch` already handles everything. The only change: pass `snapshot: "sdk-reviewer"` instead of `"pr-reviewer"`:

```python
# In the GHA workflow, the dispatch call includes:
{
  "pr_url": "https://github.com/atlanhq/application-sdk/pull/42",
  "review_mode": "standard",
  "pipeline": "rover",
  "snapshot": "sdk-reviewer"   # NEW: use SDK snapshot instead of generic
}
```

The dispatcher resolves the snapshot, writes the SDK-specific files, and launches Claude.

### SDK Evolution: New Dispatch Endpoint

```python
# harness/api/routers/sdk_evolution_dispatch.py
@router.post("/api/sdk-evolution/dispatch")
async def dispatch_sdk_evolution(request: EvolutionRequest, background_tasks: BackgroundTasks):
    """Dispatch nightly SDK evolution run."""

    # 1. Load snapshot
    snapshot = load_snapshot("sdk-evolution")

    # 2. Load codebase index from R2
    index = await load_index_from_r2("sdk-evolution/index.json")

    # 3. Build suppression list from Linear
    suppression = await build_suppression_list()

    # 4. Create sandbox
    sandbox = await create_sandbox(
        thread_id=f"sdk-evolution-{request.run_date}-{unique_suffix()}",
        snapshot=snapshot,
    )

    # 5. Write session files
    await write_session_files(sandbox, {
        "session/INDEX.md": format_index(index),
        "session/SUPPRESSION.md": format_suppression(suppression),
        "session/SCAN_MODE": request.scan_mode or ("full" if is_sunday() else "incremental"),
    })

    # 6. Write snapshot files (ORCHESTRATION, agents, references, scripts)
    await write_snapshot_files(sandbox, snapshot)

    # 7. Launch Claude Code CLI (fire-and-forget)
    background_tasks.add_task(
        run_claude_in_sandbox,
        sandbox_id=sandbox.sandbox_id,
        prompt="Read .mothership/ORCHESTRATION.md and follow the pipeline exactly.",
        model="claude-opus-4-6",
    )

    return {"dispatch_id": sandbox.sandbox_id, "status": "accepted"}
```

---

## 9. Tool Handlers (rover-worker)

### `submit_review` — Already Exists

The existing pr-reviewer `submit_review` handler works for sdk-reviewer. It already handles:
- Zod schema validation
- Rubric severity clamping (we add SDK-specific patterns via severity-rubric.yaml)
- Diff-scope validation (CRIT/HIGH must anchor to diff)
- Inline comment budget
- Stale review dismissal
- Label swap (mothership-review → mothership-reviewed)

### `submit_evolution` — New Handler

```typescript
// rover-worker/src/submit-evolution.ts

// Called by Claude at Stage 6 (handoff) and Stage 7 (summary)
// Two action types:

// Action: "handoff" — dispatch sdk-reviewer for each PR
{
  action: "handoff",
  prs: [
    { pr_url, pr_number, ticket_identifier, finding_id }
  ]
}
// Handler: for each PR, POST to /api/pr-review/dispatch with snapshot="sdk-reviewer"

// Action: "summary" — update Linear parent ticket
{
  action: "summary",
  parent_ticket_id: "...",
  metrics: { discovered, killed, survived, prs_created, prs_passed_gate2, prs_handed_off },
  ticket_updates: [
    { ticket_id, status, comment }
  ]
}
// Handler: update Linear tickets via Linear API, audit consistency
```

---

## 10. Pain Point Fixes

### Fix 1: Status Check Not Updating

**How it's fixed:** The `submit_review` handler (rover-worker TypeScript) posts the review and swaps labels server-side. This is NOT a `gh api` call from a GHA runner — it's a direct GitHub API call from the Cloudflare Worker with the GitHub App token. If Claude crashes, the sandbox has a 1-hour TTL. The dispatch handler checks for the commit marker after Claude finishes; if no marker found, retains the sandbox for diagnosis and posts a "review unavailable" comment.

### Fix 2: PR Noise Pollution

**How it's fixed:** The `submit_review` handler already has:
- Commit marker idempotency (`<!-- commit:{sha} mode:{mode} -->`)
- Stale review dismissal (dismiss reviews with different commit markers)
- Per-dispatch unique sandbox (prevents concurrent race)

### Fix 3: Linear Ticket State Drift

**How it's fixed:** The `submit_evolution` handler enforces transitions server-side:
- Valid transition map (Backlog → Todo → In Progress → In Review → Done)
- "Done" only when PR state = merged (checked via GitHub API)
- Parent status derived from children
- End-of-run audit via the summary action

### Fix 4: Ticket Without PR (Orphan)

**How it's fixed:** In ORCHESTRATION.md Stage 4, the instruction is explicit: "IF PR creation fails → cancel ticket immediately." The `submit_evolution` handler validates that every ticket in the summary has either a PR or a "Canceled" status.

### Fix 5: Gate 2 Too Lenient

**How it's fixed:** Gate 2 is mostly deterministic (see Stage 5 in ORCHESTRATION.md). Stale/weak findings killed before they become PRs. Sonnet only for ambiguous edge cases.

### Fix 6: Branch-Keeper Failures

**How it's fixed:** New lightweight GHA workflow:
- API-only for common path (updateBranch)
- Explicit 422 conflict handling → label `needs-rebase` + actionable comment
- No LLM for common path

### Fix 7: Auto-Complete Unreliable

**How it's fixed:** The auto-complete loop runs INSIDE Claude Code CLI (Phase 4 in ORCHESTRATION.md). Claude edits files, runs tests, pushes, then re-reviews — all within the same sandbox session. No comment-based self-triggering. No GHA re-triggers.

---

## 11. Changes to application-sdk Repo

| Action | File | Purpose |
|---|---|---|
| Replace | `.github/workflows/claude.yml` | Thin wrapper: calls mothership dispatch with `snapshot: "sdk-reviewer"` |
| Replace | `.github/workflows/branch-keeper.yml` | Lightweight: API update + conflict labeling |
| Add | `.mothership/review.yaml` | Reviewer config (used by dispatch for POLICY.md) |
| Update | `docs/agents/review.md` | Document mothership-backed review |
| Update | `docs/agents/testing.md` | Add reviewer test expectations |
| Update | `docs/agents/coding-standards.md` | Add determinism + contract evolution sections |
| Add | `docs/agents/evolution.md` | Document nightly evolution pipeline |

---

## 12. Rollout Plan

### Phase 1 (Week 1): Build sdk-reviewer snapshot

- Create `snapshots/sdk-reviewer/` with all files
- Add SDK patterns to severity-rubric.yaml
- Test on 3-5 real PRs via manual dispatch
- Compare output quality with current GHA-based review

### Phase 2 (Week 2): Switch sdk-review to mothership

- Update application-sdk `claude.yml` → dispatch to mothership with `snapshot: "sdk-reviewer"`
- Update `branch-keeper.yml` → lightweight version
- Monitor for 1 week: status check reliability, review quality, auto-complete loop

### Phase 3 (Week 3): Build sdk-evolution snapshot + dispatch

- Create `snapshots/sdk-evolution/` with all files
- Build `submit_evolution` handler in rover-worker
- Build `sdk_evolution_dispatch.py` endpoint
- Build codebase index scripts (shipped in snapshot)
- Test with dry runs (findings only, no PRs)

### Phase 4 (Week 4): Enable nightly evolution

- Add cron workflow in mothership
- First nightly run creates tickets + PRs
- Monitor: Linear state management, PR quality, handoff to reviewer
- Enable weekly full scan on Sundays

---

## 13. What Was Cut

| Feature | Reason |
|---|---|
| LangGraph multi-node graph | Mothership beta removed this pattern. Rover-native is the standard. |
| Reconciler agent | Deterministic de-bias rules are sufficient |
| Q&A reply agent | Deferred to v2; dispute via re-trigger |
| Holistic assessment as LLM call | Pure Python (grep, AST, wc) |
| Triage as LLM call | Deterministic |
| Bug Hunter B | Merged into one Bug Hunter |
| Retrospective as discovery agent | Moved to summary stage |
| MIGRATE auto-fix | Human judgment needed; PATCH only for v1 |
| Smart Codex skip | Optimize later with cost data |
| Docs discovery agent | Removed per request |

---

## 14. Success Criteria

1. `sdk-review` status check NEVER stuck in `pending`
2. One review comment per PR, always current
3. Every Linear ticket matches expected state
4. Zero orphaned tickets
5. Evolution completes all stages every night
6. Gate 2 kills stale/weak findings
7. Branch-keeper handles conflicts gracefully
8. Auto-complete loop runs reliably within single sandbox session
9. Nightly evolution cost < $30
10. Review cost < $10 for typical PR with auto-complete

---

## 15. Invocation Flows

### SDK Reviewer: All Trigger Paths

The GHA workflow in application-sdk parses the `@sdk-review` comment, extracts the command + any context, and dispatches to mothership with structured data.

**Comment parsing rules:**

| Comment | `command` | `auto_fix` | `command_context` |
|---|---|---|---|
| `@sdk-review` | `review` | false | (empty) |
| `@sdk-review auto-complete` | `auto-complete` | true | (empty) |
| `@sdk-review resolve all issues` | `auto-complete` | true | (empty, alias) |
| `@sdk-review challenge: pattern is intentional per ADR-042` | `challenge` | false | "pattern is intentional per ADR-042" |
| `@sdk-review override: approved by security team` | `override` | false | "approved by security team" |
| `@sdk-review stop` or `@sdk-review cancel` | `stop` | false | (empty) |
| `@sdk-review auto-complete Focus on security, skip perf findings` | `auto-complete` | true | "Focus on security, skip perf findings" |
| `@sdk-review The singleton here is by design` | `review` | false | "The singleton here is by design" |

**Dispatch payload sent to mothership:**

```json
{
  "pr_url": "https://github.com/atlanhq/application-sdk/pull/42",
  "review_mode": "standard",
  "pipeline": "rover",
  "snapshot": "sdk-reviewer",
  "command": "auto-complete",
  "command_context": "Focus on security, skip perf findings",
  "commenter": "vaibhav-chopra"
}
```

**How the dispatcher handles each command:**

- **`review`** — Creates sandbox, writes `session/PR.md` + `session/DIFF.patch`. If `command_context` present, writes `session/AUTHOR_CONTEXT.md`. Claude reviews and posts via `submit_review`.
- **`auto-complete`** — Same as review but also writes `session/AUTO_FIX` marker. Claude reviews, then enters fix loop (edit → test → push → re-review, max 3 iterations). If `command_context` present, writes `session/INSTRUCTIONS.md` (specific fix guidance).
- **`challenge`** — Writes `session/CHALLENGE.md` with the author's explanation. Claude re-evaluates previous findings against this context. Valid challenges → finding dropped. Invalid → finding kept with explanation.
- **`override`** — No sandbox. Dispatcher checks commenter's GitHub permission (`admin` required). If admin → sets status = success, posts "Overridden by @admin: reason", swaps labels. If not admin → posts "Only admins can override."
- **`stop` / `cancel`** — No sandbox. Dispatcher finds active sandbox for this PR, destroys it. Posts "Review cancelled." Removes in-progress labels.

**Permission enforcement:** Only `OWNER`, `MEMBER`, `COLLABORATOR` author associations can trigger (checked in GHA workflow `if:` condition). External fork contributors cannot.

**Session files written per command:**

| File | Written when | Content |
|---|---|---|
| `session/PR.md` | Always | PR metadata: URL, title, body, labels, files, stats |
| `session/DIFF.patch` | Always | Authoritative diff from GitHub API |
| `session/AUTO_FIX` | `auto-complete` command | Marker file (contains "true") |
| `session/AUTHOR_CONTEXT.md` | Any command with extra text | Author's additional context |
| `session/INSTRUCTIONS.md` | `auto-complete` with extra text | Specific fix guidance |
| `session/CHALLENGE.md` | `challenge` command | Author's dispute with instructions to re-evaluate |
| `session/CUSTOM/modes/*.md` | If repo has `.mothership/` overrides | Per-repo review customization |
| `session/CUSTOM/POLICY.md` | If repo has `.mothership/review.yaml` | By-design patterns, severity overrides |

**ORCHESTRATION.md Phase 0 reads all of these:**

```markdown
## Phase 0: Orient
1. Read session/PR.md
2. Read session/DIFF.patch
3. Check if session/AUTO_FIX exists → auto_fix = true
4. Check if session/CHALLENGE.md exists → challenge mode (re-evaluate disputed findings)
5. Check if session/AUTHOR_CONTEXT.md exists → consider during review
6. Check if session/INSTRUCTIONS.md exists → specific fix guidance for auto-complete
7. Check if session/CUSTOM/ exists → load repo-specific overrides
```

### SDK Evolution: All Trigger Paths

| Trigger | How | When |
|---|---|---|
| **Nightly cron** | GHA `schedule` → POST `/api/sdk-evolution/dispatch` | Daily 2am UTC |
| **Manual trigger** | GHA `workflow_dispatch` → POST with optional `scan_mode` | Anytime |
| **Linear ticket** | Create "Run SDK Evolution" issue → Linear webhook → mothership dispatch | Ad-hoc |

**GHA cron workflow (in mothership repo):**

```yaml
# mothership/.github/workflows/sdk-evolution-cron.yml
name: SDK Evolution (Nightly)
on:
  schedule:
    - cron: '0 2 * * *'
  workflow_dispatch:
    inputs:
      scan_mode:
        description: 'Scan mode: incremental, full, or auto (Sunday=full)'
        required: false
        default: 'auto'

jobs:
  trigger:
    runs-on: ubuntu-latest
    steps:
      - name: Connect VPN
        uses: atlanhq/github-actions/globalprotect-connect-action@main
        with:
          portal-url: ${{ vars.GLOBALPROTECT_PORTAL_URL }}
          username: ${{ secrets.GLOBALPROTECT_USERNAME }}
          password: ${{ secrets.GLOBALPROTECT_PASSWORD }}

      - name: Dispatch evolution
        run: |
          SCAN_MODE="${{ inputs.scan_mode || 'auto' }}"
          curl -s -X POST "${{ vars.MOTHERSHIP_URL }}/api/sdk-evolution/dispatch" \
            -H "Content-Type: application/json" \
            -H "Authorization: Bearer ${{ secrets.HARNESS_TOKEN }}" \
            -d "{
              \"repo_url\": \"https://github.com/atlanhq/application-sdk\",
              \"run_date\": \"$(date +%Y-%m-%d)\",
              \"scan_mode\": \"$SCAN_MODE\"
            }"
```

**Linear webhook trigger:** When a Linear issue titled "Run SDK Evolution" is created in the App SDK v3.0 project, mothership's existing Linear webhook handler routes it to the evolution dispatch. The issue description can contain:
- `scan_mode: full` — force full scan
- `skip_gate2: true` — dry run (findings + tickets only, no PRs)

**Evolution dispatch payload:**

```json
{
  "repo_url": "https://github.com/atlanhq/application-sdk",
  "run_date": "2026-04-20",
  "scan_mode": "auto"
}
```

**What the dispatcher does:**

1. Resolve scan_mode: `auto` → check day of week (Sunday = full, else incremental)
2. Load codebase index from R2 persistent storage
3. Build suppression list from Linear (existing tickets + open PRs)
4. Create sandbox with `snapshot: "sdk-evolution"`
5. Write session files:
   - `session/INDEX.md` — serialized codebase index
   - `session/SUPPRESSION.md` — file:rule pairs to skip
   - `session/SCAN_MODE` — "incremental" or "full"
   - `session/RUN_DATE` — date string
6. Launch Claude Code CLI (Opus 4.6)
7. Claude follows ORCHESTRATION.md stages 0-7
8. At Stage 6 (handoff), Claude calls `submit_evolution` tool which dispatches sdk-reviewer for each PR
9. At Stage 7 (summary), Claude calls `submit_evolution` tool which updates Linear

---

## 16. Queue-to-Update-and-Auto-Merge

### The Problem

Two approved PRs (#41, #42) both target `main`. #41 merges. Now #42 is behind — needs branch update, CI re-run, then merge. If 5 PRs are approved, someone babysits this sequentially for hours.

### The Solution

A new GHA workflow that watches for the auto-merge condition and automatically updates + merges approved PRs in sequence.

**Trigger condition:** PR has auto-merge enabled (GitHub's native auto-merge toggle) OR has `sdk-review-approved` + `auto-merge` labels.

```yaml
# .github/workflows/merge-queue.yml
name: Merge Queue
on:
  # Trigger when a PR is merged (other approved PRs may now be behind)
  push:
    branches: [main]
  # Trigger when auto-merge is enabled on a PR
  pull_request:
    types: [auto_merge_enabled]
  # Trigger when review approval happens
  pull_request_review:
    types: [submitted]
  # Trigger when our label is added
  issues:
    types: [labeled]

jobs:
  process-queue:
    runs-on: ubuntu-latest
    permissions:
      contents: write
      pull-requests: write
    # Prevent concurrent queue processing
    concurrency:
      group: merge-queue
      cancel-in-progress: false    # Don't cancel — let the current one finish
    steps:
      - name: Process merge queue
        uses: actions/github-script@v7
        with:
          script: |
            const owner = 'atlanhq';
            const repo = 'application-sdk';

            // Find all open PRs that are merge-eligible
            const { data: prs } = await github.rest.pulls.list({
              owner, repo, state: 'open', base: 'main',
              sort: 'created', direction: 'asc',  // oldest first
            });

            for (const pr of prs) {
              // Check eligibility: auto-merge enabled OR (approved + labeled)
              const autoMergeEnabled = pr.auto_merge !== null;
              const hasLabel = pr.labels.some(l =>
                l.name === 'auto-merge' || l.name === 'sdk-review-approved'
              );

              // Need BOTH approval and auto-merge intent
              const { data: reviews } = await github.rest.pulls.listReviews({
                owner, repo, pull_number: pr.number,
              });
              const approved = reviews.some(r => r.state === 'APPROVED');

              const eligible = approved && (autoMergeEnabled || hasLabel);
              if (!eligible) continue;

              console.log(`PR #${pr.number}: eligible for merge queue`);

              // Check if branch is behind
              const { data: prData } = await github.rest.pulls.get({
                owner, repo, pull_number: pr.number,
              });

              if (prData.mergeable_state === 'behind') {
                // Update branch
                try {
                  await github.rest.pulls.updateBranch({
                    owner, repo, pull_number: pr.number,
                  });
                  console.log(`PR #${pr.number}: branch updated, waiting for CI`);
                  // Stop here — CI needs to run. The next push to main
                  // (or CI completion) will re-trigger this workflow.
                  // Process ONE PR at a time to avoid conflicts.
                  return;
                } catch (e) {
                  if (e.status === 422) {
                    // Conflicts — can't auto-update
                    await github.rest.issues.addLabels({
                      owner, repo, issue_number: pr.number,
                      labels: ['needs-rebase'],
                    });
                    await github.rest.issues.createComment({
                      owner, repo, issue_number: pr.number,
                      body: 'Merge queue: branch has conflicts after base update. Manual rebase needed.',
                    });
                    console.log(`PR #${pr.number}: conflicts, labeled needs-rebase`);
                    continue;  // Try next PR
                  }
                  throw e;
                }
              }

              if (prData.mergeable_state === 'clean') {
                // All checks passing + up to date → merge
                try {
                  await github.rest.pulls.merge({
                    owner, repo, pull_number: pr.number,
                    merge_method: 'squash',
                  });
                  console.log(`PR #${pr.number}: merged!`);
                  // After merge, the push-to-main event re-triggers
                  // this workflow for the next PR in queue
                  return;
                } catch (e) {
                  console.log(`PR #${pr.number}: merge failed: ${e.message}`);
                  continue;
                }
              }

              if (prData.mergeable_state === 'blocked') {
                // CI still running or checks failing — skip for now
                console.log(`PR #${pr.number}: blocked (CI running or failing), skip`);
                continue;
              }

              if (prData.mergeable_state === 'unstable') {
                // Some checks failing but not required — can still merge
                // Check if REQUIRED checks pass
                const { data: checks } = await github.rest.checks.listForRef({
                  owner, repo, ref: prData.head.sha,
                });
                const requiredFailing = checks.check_runs.some(c =>
                  c.conclusion === 'failure' && c.name === 'sdk-review'
                );
                if (!requiredFailing) {
                  await github.rest.pulls.merge({
                    owner, repo, pull_number: pr.number,
                    merge_method: 'squash',
                  });
                  console.log(`PR #${pr.number}: merged (unstable but required checks pass)`);
                  return;
                }
                console.log(`PR #${pr.number}: unstable + required check failing, skip`);
                continue;
              }
            }

            console.log('No eligible PRs to process');
```

### How It Works (Step by Step)

```
PR #41 and #42 are both approved + auto-merge enabled

1. PR #41 merges (manual or via this workflow)
     ↓
2. Push to main triggers merge-queue workflow
     ↓
3. Workflow scans open PRs: #42 is eligible (approved + auto-merge)
     ↓
4. #42 is behind → calls updateBranch API
     ↓
5. CI starts on #42's updated branch
     ↓
6. CI passes → push event to main... wait no,
   CI completion doesn't trigger push-to-main.
   But #42 has auto-merge enabled (GitHub native) →
   GitHub merges automatically once CI passes.
     ↓
7. #42 merges → push to main → workflow re-triggers
     ↓
8. No more eligible PRs → done
```

**Key design decisions:**

- **Process ONE PR at a time** — update branch, then stop. The merge (after CI) re-triggers for the next one. This avoids race conditions.
- **Oldest first** — FIFO ordering. PRs merge in the order they were created.
- **Concurrency group** — only one queue processor runs at a time. No cancel-in-progress (let the current one finish).
- **Conflicts → label + skip** — don't block the queue. Label the conflicting PR, move to the next one.
- **Works with GitHub native auto-merge** — if the author enabled auto-merge, GitHub handles the final merge once CI passes. The workflow just ensures the branch is up-to-date.
- **Also works with label-based** — for PRs where auto-merge isn't enabled but `auto-merge` or `sdk-review-approved` label is present. Chris's suggestion: "approved + auto-merge label present."

### Replaces Branch-Keeper

This workflow subsumes the branch-keeper functionality. The old branch-keeper only updated branches for `sdk-review-auto-maintained` PRs. This workflow:
- Updates branches for ANY eligible PR (auto-merge enabled OR approved + labeled)
- Also merges when ready (branch-keeper didn't merge)
- Handles conflicts gracefully (branch-keeper failed silently)

Remove the old `branch-keeper.yml` workflow and replace with this.

---

## 17. Open Questions

1. **Snapshot parameter in dispatch:** The existing `pr_review_dispatch.py` needs a `snapshot` field added to accept `"sdk-reviewer"` instead of hardcoded `"pr-reviewer"`. Check if beta already supports this or if it needs adding.
2. **Command + context fields in dispatch:** The existing dispatch schema needs `command`, `command_context`, and `commenter` fields added. These are new for sdk-reviewer.
3. **submit_evolution handler:** New TypeScript handler needed in rover-worker. Confirm with harness team the pattern for adding new tool endpoints.
4. **R2 index persistence:** Verify that R2 mount is writable from Claude Code CLI inside the sandbox (the update_index.py script writes back to R2).
5. **GPT proxy routing:** Confirm that `$PROXY_BASE/proxy/llm/chat/completions` routes to llmproxy.atlan.dev and supports `gpt-5.3-codex` model.
6. **Evolution sandbox TTL:** Default DO TTL is 1 hour. Evolution runs can take 30-60 min. May need extended TTL for the evolution snapshot.
7. **Linear API in sandbox:** The pr-reviewer snapshot is read-only (no Linear). The evolution snapshot needs Linear write access via proxy. Confirm proxy supports Linear routes for this snapshot.
8. **Override permission check:** The dispatcher needs to call GitHub API to verify commenter is admin. Confirm the GitHub App token has permission to read collaborator roles.
