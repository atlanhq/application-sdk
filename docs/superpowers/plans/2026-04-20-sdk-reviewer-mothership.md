# SDK Reviewer Snapshot — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create a mothership rover-native snapshot (`sdk-reviewer`) that replaces the current `@sdk-review` Claude Code skill, fixing status check reliability, PR noise, and auto-complete loop issues.

**Architecture:** A snapshot directory (`snapshots/sdk-reviewer/`) containing ORCHESTRATION.md (5-phase review loop), sub-agent prompts, SDK-specific severity rubric, and reference rules. The existing `pr_review_dispatch.py` is modified to accept a `snapshot` parameter. A thin GHA workflow in application-sdk parses `@sdk-review` comments and dispatches to mothership.

**Tech Stack:** Python (FastAPI/Pydantic), TypeScript (Cloudflare Worker), GitHub Actions, Claude Code CLI, Markdown prompt engineering

**Spec:** `docs/superpowers/specs/2026-04-20-sdk-agents-mothership-migration-design.md`

**Repos:**
- `mothership` (beta branch) — snapshot + dispatch modification
- `application-sdk` — GHA workflow + .mothership config

---

## File Map

### Mothership (beta branch)

| Action | File | Purpose |
|---|---|---|
| Create | `snapshots/sdk-reviewer/snapshot.yaml` | Snapshot manifest, extends _base |
| Create | `snapshots/sdk-reviewer/CLAUDE.md` | SDK reviewer identity, guardrails G1-G8, architecture context |
| Create | `snapshots/sdk-reviewer/tools.md` | Available tools: submit_review endpoint, sub-agents, git |
| Create | `snapshots/sdk-reviewer/ORCHESTRATION.md` | 5-phase review loop (orient → gather → review → submit → fix/finalize) |
| Create | `snapshots/sdk-reviewer/modes/standard.md` | SDK standard review checklist |
| Create | `snapshots/sdk-reviewer/modes/auto-complete.md` | Auto-fix loop orchestration |
| Create | `snapshots/sdk-reviewer/agents/correctness.md` | ARCH + SEC + BUG sub-agent prompt |
| Create | `snapshots/sdk-reviewer/agents/quality.md` | QUAL + TEST + DX sub-agent prompt |
| Create | `snapshots/sdk-reviewer/agents/structure.md` | STRUCT + root cause sub-agent prompt |
| Create | `snapshots/sdk-reviewer/agents/adversarial.md` | Cross-model challenge prompt (Opus vs GPT findings) |
| Create | `snapshots/sdk-reviewer/agents/disprove.md` | Per-finding disprove sub-agent |
| Create | `snapshots/sdk-reviewer/agents/reachability.md` | Caller chain analysis sub-agent |
| Create | `snapshots/sdk-reviewer/references/v3-architecture-rules.md` | 11 ADRs |
| Create | `snapshots/sdk-reviewer/references/code-quality-rules.md` | Import, logging, naming, sizing rules |
| Create | `snapshots/sdk-reviewer/references/security-rules.md` | Secrets, injection, deserialization |
| Create | `snapshots/sdk-reviewer/references/test-quality-rules.md` | Coverage, patterns, isolation |
| Create | `snapshots/sdk-reviewer/references/dx-rules.md` | API ergonomics, errors, migration |
| Create | `snapshots/sdk-reviewer/references/structural-rules.md` | Dumping grounds, file health, dependency direction |
| Create | `snapshots/sdk-reviewer/references/performance-rules.md` | Blocking, timeouts, N+1, memory |
| Create | `snapshots/sdk-reviewer/severity-rubric.yaml` | SDK-specific severity patterns |
| Create | `snapshots/sdk-reviewer/repos/application-sdk/modes/standard.md` | Repo-specific additive rules |
| Create | `snapshots/sdk-reviewer/repos/application-sdk/POLICY.md` | By-design patterns |
| Modify | `harness/api/routers/pr_review_dispatch.py` | Add `snapshot`, `command`, `command_context`, `commenter` fields |

### Application-SDK

| Action | File | Purpose |
|---|---|---|
| Create | `.github/workflows/sdk-review.yml` | GHA: parse @sdk-review comments, dispatch to mothership |
| Create | `.github/workflows/merge-queue.yml` | GHA: auto-update + auto-merge approved PRs |
| Create | `.mothership/review.yaml` | Repo config (used by dispatch for POLICY) |
| Update | `docs/agents/review.md` | Document mothership-backed review |
| Update | `docs/agents/testing.md` | Add reviewer test expectations |
| Update | `docs/agents/coding-standards.md` | Add determinism + contract sections |

---

## Tasks

### Task 1: Create Snapshot Skeleton + Manifest

**Files:**
- Create: `mothership/snapshots/sdk-reviewer/snapshot.yaml`
- Create: `mothership/snapshots/sdk-reviewer/CLAUDE.md`
- Create: `mothership/snapshots/sdk-reviewer/tools.md`

- [ ] **Step 1: Create snapshot directory structure**

```bash
cd /Users/vaibhav.chopra/work/mothership
git checkout beta && git pull origin beta
mkdir -p snapshots/sdk-reviewer/{modes,agents,references,repos/application-sdk/modes}
```

- [ ] **Step 2: Write snapshot.yaml**

Create `snapshots/sdk-reviewer/snapshot.yaml`:

```yaml
name: sdk-reviewer
description: >
  SDK v3 multi-model PR reviewer with auto-fix loop.
  GPT-5.3-codex primary review + Claude Opus adversarial.
  Supports @sdk-review, @sdk-review auto-complete, challenge, override, stop.
extends: _base
```

- [ ] **Step 3: Write CLAUDE.md**

Create `snapshots/sdk-reviewer/CLAUDE.md` — this is the SDK reviewer identity. Full content:

```markdown
# SDK Reviewer

You are reviewing PRs for the Atlan application-sdk v3.
This SDK enables connector builders to build Temporal-backed metadata extractors.

Follow `.mothership/ORCHESTRATION.md` exactly. Do not skip phases.

## Critical Files to Read First

1. `.mothership/ORCHESTRATION.md` — your playbook (MANDATORY)
2. `.mothership/tools.md` — available tools
3. `session/PR.md` — PR metadata
4. `session/DIFF.patch` — authoritative diff

## SDK v3 Architecture Context

- **App** = Temporal Workflow + Activities
- **`run()`/`@entrypoint`** = workflow orchestration — MUST be deterministic
- **`@task`** = activity — handles all I/O, retryable, heartbeatable
- **Contracts**: every method has exactly one `Input`, one `Output` (Pydantic BaseModel)
- **Infrastructure**: Dapr-backed state store, secret store, pub/sub via Protocols
- **Dependency direction**: `app/` -> `execution/` -> `infrastructure/` (NEVER reverse)
- **Credential pattern**: `CredentialRef` in contracts, `CredentialResolver` in `@task` only
- **Blocking ops**: use `self.run_in_thread()` for blocking operations in `@task`

### Determinism Rules (Temporal replay safety)

In `run()`/`@entrypoint` methods:
- NO `datetime.now()`, `datetime.utcnow()` -> use `self.now()`
- NO `uuid.uuid4()` -> use `self.uuid()`
- NO `random.*` calls
- NO I/O, network calls, file reads -> move to `@task`
- Violations corrupt in-flight workflow history

### Contract Evolution (running workflow safety)

- NEVER remove or rename fields on Input/Output
- NEVER change field types
- Add new fields with defaults only
- Use `Field(default_factory=list)` for mutable defaults, never `field: list = []`
- Breaking contracts silently corrupts in-flight Temporal workflows

## Guardrails (Block Merge)

These guardrails determine whether the `sdk-review` status check passes or fails.
Guardrail violations are ALWAYS reported regardless of confidence score.

| ID | Name | Trigger | Verdict |
|----|------|---------|---------|
| G1 | Security Blockers | Any [SEC] Critical finding | BLOCKED |
| G2 | Contract Safety | Field removed/renamed/retyped on Input/Output | BLOCKED |
| G3 | Determinism | datetime.now/uuid4/random/I/O in run()/@entrypoint | BLOCKED |
| G4 | Test Coverage | New public API without tests | NEEDS_FIXES (Critical) |
| G5 | Secret Safety | Hardcoded secret, credential in logs | BLOCKED |
| G6 | Breaking API | Public export removed without deprecation shim | NEEDS_FIXES (Critical) |
| G7 | CI Must Pass | Required CI checks failing | Noted in review |
| G8 | Branch Mergeable | Unresolvable conflicts | Status = failure |

## Cross-Model Review Strategy

GPT-5.3-codex is your primary reviewer (via `$PROXY_BASE/proxy/llm/chat/completions`).
YOU (Claude Opus) are the adversarial challenger.

- GPT reviews the code with 3 domain sub-agents
- You challenge every GPT finding: AGREE / DISAGREE / PARTIAL
- You also discover findings GPT missed (your blind spots differ)
- De-bias is deterministic (no LLM needed)

## Rules

- Follow ORCHESTRATION.md EXACTLY. Do not skip phases.
- Read-only on cloned repos. Do not git push or call GitHub API directly.
- Use `submit_review` tool (curl to proxy) to post reviews.
- PATCH scope findings: include exact fix code in `<!--FIX-->` blocks.
- DESIGN_CHANGE scope: flag for human, NEVER auto-fix.
- Builder perspective: "If I'm building a connector with this SDK, does this code make my life easier or harder?"
- Never log, commit, or output secrets, tokens, or credentials.
```

- [ ] **Step 4: Write tools.md**

Create `snapshots/sdk-reviewer/tools.md`:

```markdown
# SDK Reviewer Tools

## Phase 0 (Context Priming)

Standard Claude tools:
- `Read` — read session files, CLAUDE.md files in repo, test files
- `Glob` — locate files by pattern
- `Bash` — git clone, git fetch, git checkout

## Phases 1-2 (Code Navigation + Review)

- `Grep` — fast search (ripgrep)
- `Bash` — git log, git blame, ast-grep (structural search)
- `Agent` — dispatch sub-agents (see `.mothership/agents/`)

### Cross-Model Review (GPT-5.3-codex via Proxy)

Call GPT for Wave 1 review agents. DO NOT use Claude sub-agents for Wave 1.

```bash
curl -s "$PROXY_BASE/proxy/llm/chat/completions" \
  -H "Authorization: Bearer $PROXY_JWT" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-5.3-codex",
    "temperature": 0.2,
    "max_tokens": 16000,
    "messages": [
      {"role": "system", "content": "<agent prompt from agents/*.md>"},
      {"role": "user", "content": "<PR diff + file contents + rules>"}
    ]
  }'
```

Parse the JSON response: `jq -r '.choices[0].message.content'`

If the call fails (5xx, timeout) -> retry once after 10s.
If retry fails -> skip Wave 2 adversarial, note "Single-model review (GPT unavailable)".

## Phase 3 (Submit Review)

### submit_review Endpoint

This is the ONLY way to post a review. NEVER post comments or set status directly.

```bash
curl -X POST "$PROXY_BASE/sandbox/submit-review" \
  -H "Authorization: Bearer $PROXY_JWT" \
  -H "Content-Type: application/json" \
  --data-binary @payload.json
```

**Payload schema:**

```json
{
  "pr_url": "https://github.com/atlanhq/application-sdk/pull/42",
  "commit_sha": "<40-hex SHA>",
  "review_mode": "standard",
  "approval_recommendation": "APPROVE | CONDITIONAL_APPROVE | REQUEST_CHANGES | REJECT",
  "summary": "<max 600 chars>",
  "findings": [
    {
      "title": "<max 120 chars>",
      "pattern_id": "<from severity-rubric.yaml or descriptive kebab-case>",
      "severity": "BLOCKING | CRITICAL | HIGH | MEDIUM | LOW | INFO",
      "category": "security | bug | test | defensive | style",
      "confidence": 0.85,
      "file": "application_sdk/app/base.py",
      "line": 42,
      "evidence": "<quoted code snippet or grep output>",
      "attack_path": "<for security CRIT/HIGH, otherwise null>",
      "reachable_from": "<from reachability sub-agent>",
      "by_design_check": "<from glean-verifier or PENDING>",
      "suggested_fix": "<optional>",
      "escalate_to_linear": false
    }
  ],
  "inline_comments": [
    {
      "file": "application_sdk/app/base.py",
      "line": 42,
      "body": "**[BLOCKING]** [SEC] — Credential leaked...",
      "severity": "BLOCKING"
    }
  ],
  "confidence_metrics": {
    "test_quality": 0.8,
    "defensive_coding": 0.7,
    "critical_issues": 0.9
  },
  "github_token": "<from session/PR.md>",
  "schema_version": 1
}
```

Inline comments are REQUIRED for BLOCKING/CRITICAL/HIGH findings.
Retry up to 3x on 422 (schema error). Retry once on 5xx.

## Phase 4 (Auto-Fix — if auto_fix=true)

When fixing code:
- `Edit` — apply code changes
- `Bash` — run `uv run pre-commit run --files <files>`, `uv run pytest tests/unit/ -x --timeout=60`
- `Bash` — `git add <files> && git commit -m "fix(review): address SDK review findings"`, `git push`

NEVER use `git add -A` or `git add .`. Always add specific files.
NEVER add Co-Authored-By lines.
Follow Conventional Commits format.

## Prohibited

- No `gh` CLI or GitHub API calls (handler manages GitHub interaction)
- No Linear API calls
- No `git push` except in Phase 4 auto-fix
- No package installs
- No dev-server starts
```

- [ ] **Step 5: Verify snapshot loads**

```bash
cd /Users/vaibhav.chopra/work/mothership
python -c "
from harness.core.config.rover_snapshots import load_snapshot
s = load_snapshot('sdk-reviewer')
print(f'Name: {s.name}')
print(f'Extends: {s.extends}')
print(f'Workspace files: {list(s.workspace_files.keys())}')
print(f'Extra files: {list(s.extra_files.keys())}')
print('OK')
"
```

Expected: snapshot loads with workspace files inherited from `_base`, overridden by sdk-reviewer's CLAUDE.md and tools.md. Extra files list should be empty (agents/modes/references not yet created).

- [ ] **Step 6: Commit**

```bash
cd /Users/vaibhav.chopra/work/mothership
git add snapshots/sdk-reviewer/
git commit -m "feat(sdk-reviewer): create snapshot skeleton with identity and tools"
```

---

### Task 2: Write Severity Rubric

**Files:**
- Create: `mothership/snapshots/sdk-reviewer/severity-rubric.yaml`

- [ ] **Step 1: Write severity-rubric.yaml**

Create `snapshots/sdk-reviewer/severity-rubric.yaml` with SDK-specific patterns. Use the exact content from the spec (Section 6, severity-rubric.yaml). The rubric covers:

- `temporal-determinism`: io-in-workflow (BLOCKING), non-deterministic-in-workflow (BLOCKING)
- `contract-safety`: field-removed (BLOCKING), field-renamed (BLOCKING), field-type-changed (BLOCKING), mutable-default (HIGH)
- `blocking-operations`: blocking-in-task (HIGH, upgradable to CRITICAL)
- `infrastructure-abstraction`: direct-temporal-import (HIGH), direct-dapr-import (HIGH)
- `api-surface`: public-api-no-test (CRITICAL), breaking-change-no-deprecation (CRITICAL)
- `credential-safety`: raw-credential-in-contract (HIGH), credential-in-log (BLOCKING)
- `error-handling`: bare-except (MEDIUM), silent-exception (HIGH)

Copy the YAML verbatim from spec Section 6 `severity-rubric.yaml`.

- [ ] **Step 2: Commit**

```bash
git add snapshots/sdk-reviewer/severity-rubric.yaml
git commit -m "feat(sdk-reviewer): add SDK-specific severity rubric"
```

---

### Task 3: Copy Reference Rules

**Files:**
- Create: `mothership/snapshots/sdk-reviewer/references/` (7 files)

- [ ] **Step 1: Copy reference rules from application-sdk**

```bash
cd /Users/vaibhav.chopra/work/mothership
cp /Users/vaibhav.chopra/work/application-sdk/.claude/skills/sdk-review/references/v3-architecture-rules.md snapshots/sdk-reviewer/references/
cp /Users/vaibhav.chopra/work/application-sdk/.claude/skills/sdk-review/references/code-quality-rules.md snapshots/sdk-reviewer/references/
cp /Users/vaibhav.chopra/work/application-sdk/.claude/skills/sdk-review/references/security-rules.md snapshots/sdk-reviewer/references/
cp /Users/vaibhav.chopra/work/application-sdk/.claude/skills/sdk-review/references/test-quality-rules.md snapshots/sdk-reviewer/references/
cp /Users/vaibhav.chopra/work/application-sdk/.claude/skills/sdk-review/references/dx-rules.md snapshots/sdk-reviewer/references/
cp /Users/vaibhav.chopra/work/application-sdk/.claude/skills/sdk-review/references/structural-rules.md snapshots/sdk-reviewer/references/
cp /Users/vaibhav.chopra/work/application-sdk/.claude/skills/sdk-evolution/references/performance-rules.md snapshots/sdk-reviewer/references/
```

- [ ] **Step 2: Verify all 7 files exist**

```bash
ls -la snapshots/sdk-reviewer/references/
```

Expected: 7 markdown files.

- [ ] **Step 3: Commit**

```bash
git add snapshots/sdk-reviewer/references/
git commit -m "feat(sdk-reviewer): add reference rules from application-sdk"
```

---

### Task 4: Write Sub-Agent Prompts

**Files:**
- Create: `mothership/snapshots/sdk-reviewer/agents/correctness.md`
- Create: `mothership/snapshots/sdk-reviewer/agents/quality.md`
- Create: `mothership/snapshots/sdk-reviewer/agents/structure.md`
- Create: `mothership/snapshots/sdk-reviewer/agents/adversarial.md`
- Create: `mothership/snapshots/sdk-reviewer/agents/disprove.md`
- Create: `mothership/snapshots/sdk-reviewer/agents/reachability.md`

- [ ] **Step 1: Write agents/correctness.md**

This is the GPT-5.3-codex CORRECTNESS agent prompt. Tags: [ARCH], [SEC], [BUG].

Port the Agent 1 (CORRECTNESS) prompt from the existing sdk-review SKILL.md (Section 2d, Agent 1). Adapt it for the submit_review finding schema — each finding must include `pattern_id`, `severity`, `category`, `confidence`, `file`, `line`, `evidence`, `attack_path`, `reachable_from`, `suggested_fix`.

Key changes from existing SKILL.md:
- Reference the severity-rubric.yaml for `pattern_id` values
- Include the SDK architecture preamble (Temporal determinism, contract safety, etc.)
- Include the Holistic Context Protocol (check file annotations before flagging)
- Output must be parseable JSON matching the finding schema

Read the existing prompt at `/Users/vaibhav.chopra/work/application-sdk/.claude/skills/sdk-review/SKILL.md` lines 677-740 for the full Agent 1 prompt. Adapt it to the new format.

- [ ] **Step 2: Write agents/quality.md**

Port Agent 2 (QUALITY) from SKILL.md lines 742-815. Tags: [QUAL], [TEST], [DX].

Same adaptations as correctness: rubric pattern_ids, SDK preamble, finding schema output.

- [ ] **Step 3: Write agents/structure.md**

Port Agent 3 (STRUCTURE) from SKILL.md lines 829-900. Tags: [STRUCT].

Higher confidence bar: 85%. Must include Root Cause Assessment output.

- [ ] **Step 4: Write agents/adversarial.md**

This is the Claude Opus adversarial prompt (Wave 2). Port from SKILL.md lines 902-981.

Key difference: this prompt is used by Claude ITSELF (not dispatched to GPT). Claude reads GPT's Wave 1 findings and challenges them.

Output format: for each GPT finding, output `AGREE(confidence) | DISAGREE(confidence, reason) | PARTIAL(confidence, new_severity, reason)`. Plus any new findings GPT missed.

- [ ] **Step 5: Write agents/disprove.md**

Port the disprove agent from the pr-reviewer snapshot (`snapshots/pr-reviewer/agents/disprove.md`). Adapt for SDK context.

6 disprove strategies: sanitization check, reachability check, pre-existing-code check, config/env gate, convention check, type/framework guarantee.

Output: `KEEP | DOWNGRADE{severity, reason} | DROP{reason}`.

- [ ] **Step 6: Write agents/reachability.md**

Port the reachability agent from the pr-reviewer snapshot (`snapshots/pr-reviewer/agents/reachability.md`). Adapt for SDK context — add classification for Temporal workflow vs activity entry points.

Output: JSON array with `{symbol, classification, entry_point, auth_required}`.
Classifications: `public-http`, `temporal-workflow`, `temporal-activity`, `webhook`, `internal`, `test`, `dead`.

- [ ] **Step 7: Commit**

```bash
git add snapshots/sdk-reviewer/agents/
git commit -m "feat(sdk-reviewer): add 6 sub-agent prompts"
```

---

### Task 5: Write Mode Checklists

**Files:**
- Create: `mothership/snapshots/sdk-reviewer/modes/standard.md`
- Create: `mothership/snapshots/sdk-reviewer/modes/auto-complete.md`

- [ ] **Step 1: Write modes/standard.md**

The SDK standard review checklist. This tells Claude what to flag and what to ignore for SDK PRs specifically.

Structure:
- **What to flag**: contract violations, determinism violations, blocking in async, missing tests for public API, breaking API changes, credential leaks, v2 patterns, infrastructure abstraction violations
- **What NOT to flag**: import ordering (isort handles), missing docstrings on private methods, pre-existing code outside diff, style preferences
- **Severity calibration**: map severity to SDK categories using the rubric
- **Pattern IDs**: use rubric pattern_ids for known patterns, descriptive kebab-case for others

Base this on the existing modes/standard.md from pr-reviewer (`snapshots/pr-reviewer/modes/standard.md`) but replace generic patterns with SDK-specific ones from the severity-rubric.yaml.

- [ ] **Step 2: Write modes/auto-complete.md**

The auto-fix loop instructions. Claude reads this when `session/AUTO_FIX` exists.

Content:

```markdown
# Auto-Complete Mode

When auto_fix is enabled, after posting the initial review (Phase 3),
enter the fix loop (Phase 4).

## Fix Loop

For each finding with scope PATCH (any severity — Critical, Important, Minor):

1. Read the full file for context
2. Apply the exact fix from the finding's suggested_fix or FIX block
3. Run: `uv run pre-commit run --files <changed_files>`
   - If pre-commit fails, fix the lint/format issues
4. Run: `uv run pytest tests/unit/ -x --timeout=60`
   - If tests fail, revert this fix and note why
5. Verify only intended files changed: `git diff --stat`
   - If unrelated files were reformatted, `git checkout -- <unrelated_files>`

After all PATCH fixes applied:

6. Stage specific files: `git add <specific_files>` (NEVER `git add -A`)
7. Commit: `git commit -m "fix(review): address SDK review findings"`
8. Push: `git push origin <branch>`

Then re-run Phases 0-3 with the new diff.

## Iteration Limits

- Maximum 3 iterations
- If iteration >= 3 and findings remain: submit with verdict NEEDS_HUMAN
- If CI fails after push: attempt one fix iteration. If CI fails again: stop, submit NEEDS_HUMAN

## Scope Restrictions

- ONLY fix PATCH scope findings
- NEVER touch MIGRATE, REFACTOR, or DESIGN_CHANGE scope
- NEVER modify files the PR doesn't touch
- NEVER add features or "improve" things beyond the findings

## Reading Author Instructions

If `session/INSTRUCTIONS.md` exists, read it. The author may have specific
guidance like "skip the performance findings" or "focus on security only".
Honor these instructions unless they conflict with guardrails G1-G5.
```

- [ ] **Step 3: Commit**

```bash
git add snapshots/sdk-reviewer/modes/
git commit -m "feat(sdk-reviewer): add standard and auto-complete mode checklists"
```

---

### Task 6: Write ORCHESTRATION.md

**Files:**
- Create: `mothership/snapshots/sdk-reviewer/ORCHESTRATION.md`

This is the main playbook — the most critical file.

- [ ] **Step 1: Write ORCHESTRATION.md**

Use the full content from the spec Section 6 (`ORCHESTRATION.md` — 5-Phase SDK Review Loop). This includes:

- Phase 0: Orient (read session files, clone repo, checkout PR, determine mode)
- Phase 1: Context Gather (holistic assessment via grep/wc, dispatch reachability + structure sub-agents)
- Phase 2: Review (3 GPT agents via proxy, Opus adversarial, deterministic de-bias, guardrails)
- Phase 3: Submit Review (curl to submit_review endpoint)
- Phase 4: Auto-Fix (if auto_fix=true, fix loop with pre-commit + pytest)
- Phase 5: Finalize (if READY_TO_MERGE, submit with APPROVE)

Also include:
- Large PR handling (Tier 1/2/3 based on diff size)
- Challenge mode handling (read session/CHALLENGE.md, re-evaluate disputed findings)
- Override mode handling (note: override is handled by dispatcher, not Claude)
- De-bias matrix (GPT+Opus agree/disagree/partial rules)
- Verdict rules (BLOCKED/NEEDS_HUMAN/NEEDS_FIXES/READY_TO_MERGE)

- [ ] **Step 2: Commit**

```bash
git add snapshots/sdk-reviewer/ORCHESTRATION.md
git commit -m "feat(sdk-reviewer): add 5-phase orchestration playbook"
```

---

### Task 7: Write Repo-Specific Overrides

**Files:**
- Create: `mothership/snapshots/sdk-reviewer/repos/application-sdk/modes/standard.md`
- Create: `mothership/snapshots/sdk-reviewer/repos/application-sdk/POLICY.md`

- [ ] **Step 1: Write repos/application-sdk/modes/standard.md**

Additive rules specific to the application-sdk repo (merged with base standard.md):

```markdown
# application-sdk — SDK Review Override

## Repo-Specific Severity Calibration

| Area | Default Severity |
|------|-----------------|
| `application_sdk/app/` | CRITICAL (core app lifecycle) |
| `application_sdk/execution/` | CRITICAL (Temporal abstraction) |
| `application_sdk/contracts/` | CRITICAL (contract evolution) |
| `application_sdk/credentials/` | HIGH (credential handling) |
| `application_sdk/infrastructure/` | HIGH (Dapr abstraction) |
| `application_sdk/handler/` | HIGH (HTTP boundary) |
| `application_sdk/storage/` | MEDIUM (object storage) |
| `application_sdk/common/` | MEDIUM (utilities) |
| `tests/` | MEDIUM (test quality) |
| `docs/` | LOW (documentation) |

## Repo Conventions (Do NOT Flag)

- Import ordering (isort with custom config handles this)
- `asyncio_mode="auto"` in pytest (no @pytest.mark.asyncio needed)
- `Dict[str, Any]` in infrastructure layer (allowed for Dapr interop)
- `print()` in test files (acceptable for debugging)
- Missing docstrings on private methods (prefixed with _)
- Logger without exc_info in happy path

## Repo-Specific Patterns to Flag

- `from application_sdk.workflows` or `from application_sdk.activities` (v2 imports, removed in v3)
- `@workflow.defn` or `@activity.defn` (direct Temporal decorators, use @task)
- `DaprClient()` outside `infrastructure/_dapr/` (direct Dapr usage)
- `ObjectStore` from `application_sdk.services` (v2 pattern)
- `@dataclass` on Input/Output subclasses (should be plain Pydantic BaseModel)
- `credentials: dict` (should use CredentialRef)
```

- [ ] **Step 2: Write repos/application-sdk/POLICY.md**

```markdown
# application-sdk — By-Design Patterns

These patterns are intentional. Do NOT flag them.

## Accepted Patterns

- `common/utils.py` contains mixed utilities — known tech debt, tracked in BLDX milestone.
  Only flag security issues in this file.
- `execution/_temporal/` uses direct temporalio imports — this IS the abstraction layer.
- `infrastructure/_dapr/` uses direct dapr imports — this IS the abstraction layer.
- `infrastructure/_redis/` uses direct redis imports — this IS the abstraction layer.
- ThreadPoolExecutor per query in low-frequency paths (< 10 calls/workflow) is acceptable.
  Only flag if the path is hot (100+ calls).
- `run_in_thread` uses the default executor for non-heartbeat tasks. Only flag if the
  task has heartbeat enabled AND the blocking call is long-running (> 5s).
```

- [ ] **Step 3: Commit**

```bash
git add snapshots/sdk-reviewer/repos/
git commit -m "feat(sdk-reviewer): add application-sdk repo overrides and policy"
```

---

### Task 8: Modify Dispatch to Accept Snapshot + Command

**Files:**
- Modify: `mothership/harness/api/routers/pr_review_dispatch.py`

- [ ] **Step 1: Read the current DispatchRequest schema**

```bash
cd /Users/vaibhav.chopra/work/mothership
```

Read `harness/api/routers/pr_review_dispatch.py` and find the `DispatchRequest` class and the `_SNAPSHOT_NAME` constant.

- [ ] **Step 2: Add snapshot, command, command_context, commenter fields to DispatchRequest**

Add these optional fields to the existing `DispatchRequest` Pydantic model:

```python
    snapshot: Optional[str] = Field(
        default=None,
        description=(
            "Snapshot name to use. When omitted, defaults to 'pr-reviewer'. "
            "Pass 'sdk-reviewer' for SDK-specific review."
        ),
    )
    command: Optional[str] = Field(
        default=None,
        description=(
            "Review command: 'review' (default), 'auto-complete', "
            "'challenge', 'override', 'stop'."
        ),
        pattern="^(review|auto-complete|challenge|override|stop)$",
    )
    command_context: Optional[str] = Field(
        default=None,
        description="Additional context from the commenter (challenge reason, override reason, fix instructions).",
    )
    commenter: Optional[str] = Field(
        default=None,
        description="GitHub username of the commenter (for permission checks).",
    )
```

- [ ] **Step 3: Use the snapshot field instead of hardcoded constant**

Find `_SNAPSHOT_NAME = "pr-reviewer"` and replace the usage:

```python
# Before:
snapshot = load_snapshot(_SNAPSHOT_NAME)

# After:
snapshot_name = req.snapshot or _SNAPSHOT_NAME
snapshot = load_snapshot(snapshot_name)
```

- [ ] **Step 4: Handle command-specific session files**

In the function that composes session files (look for `_compose_writefile_set` or similar), add logic to write command-specific files:

```python
# After the existing session file composition
command = req.command or "review"

if command == "stop":
    # Cancel active sandbox, don't create new one
    await _cancel_active_sandbox(pr_url=str(req.pr_url))
    return DispatchResponse(
        dispatch_id="cancelled",
        sandbox_id=None,
        pipeline="rover",
        accepted=True,
    )

if command == "override":
    # Handle override in dispatcher (no sandbox needed)
    permission = await _check_github_permission(req.commenter, owner, repo)
    if permission != "admin":
        return DispatchResponse(
            dispatch_id="rejected",
            sandbox_id=None,
            pipeline="rover",
            accepted=False,
        )
    await _force_override(pr_url=str(req.pr_url), commenter=req.commenter, reason=req.command_context)
    return DispatchResponse(
        dispatch_id="overridden",
        sandbox_id=None,
        pipeline="rover",
        accepted=True,
    )

# For review / auto-complete / challenge — add to session files
if command == "auto-complete":
    session_payload["AUTO_FIX"] = "true"

if command == "challenge" and req.command_context:
    session_payload["CHALLENGE.md"] = (
        "# Author Challenge\n\n"
        "The author has disputed findings with this context:\n\n"
        f"> {req.command_context}\n\n"
        "Re-evaluate all previous findings against this explanation.\n"
        "If the author's reasoning is valid, drop the finding.\n"
        "If partially valid, downgrade severity.\n"
        "If the challenge doesn't hold, keep the finding and explain why.\n"
    )

if req.command_context and command in ("review", "auto-complete"):
    session_payload["AUTHOR_CONTEXT.md"] = (
        "# Additional Context from Author\n\n"
        f"{req.command_context}\n\n"
        "Consider this context when reviewing.\n"
    )

if req.command_context and command == "auto-complete":
    session_payload["INSTRUCTIONS.md"] = (
        "# Fix Instructions\n\n"
        f"{req.command_context}\n\n"
        "Follow these instructions during the auto-fix loop.\n"
    )
```

- [ ] **Step 5: Run existing dispatch tests**

```bash
cd /Users/vaibhav.chopra/work/mothership
uv run python -m pytest tests/api/routers/test_pr_review_dispatch.py -v
```

Expected: existing tests pass (new fields are Optional with defaults, backward compatible).

- [ ] **Step 6: Write test for new fields**

Create or append to `tests/api/routers/test_pr_review_dispatch.py`:

```python
def test_dispatch_with_sdk_reviewer_snapshot():
    """Verify dispatch accepts sdk-reviewer snapshot."""
    req = DispatchRequest(
        pr_url="https://github.com/atlanhq/application-sdk/pull/42",
        review_mode="standard",
        snapshot="sdk-reviewer",
        command="auto-complete",
    )
    assert req.snapshot == "sdk-reviewer"
    assert req.command == "auto-complete"


def test_dispatch_defaults_to_pr_reviewer():
    """Verify backward compatibility — no snapshot field defaults to pr-reviewer."""
    req = DispatchRequest(
        pr_url="https://github.com/atlanhq/application-sdk/pull/42",
        review_mode="standard",
    )
    assert req.snapshot is None  # Will use _SNAPSHOT_NAME fallback


def test_dispatch_stop_command():
    """Verify stop command is accepted."""
    req = DispatchRequest(
        pr_url="https://github.com/atlanhq/application-sdk/pull/42",
        review_mode="standard",
        command="stop",
    )
    assert req.command == "stop"


def test_dispatch_challenge_with_context():
    """Verify challenge command carries context."""
    req = DispatchRequest(
        pr_url="https://github.com/atlanhq/application-sdk/pull/42",
        review_mode="standard",
        command="challenge",
        command_context="This pattern is intentional per ADR-042",
        commenter="vaibhav-chopra",
    )
    assert req.command == "challenge"
    assert "ADR-042" in req.command_context
```

- [ ] **Step 7: Run tests**

```bash
uv run python -m pytest tests/api/routers/test_pr_review_dispatch.py -v
```

Expected: all tests pass.

- [ ] **Step 8: Commit**

```bash
git add harness/api/routers/pr_review_dispatch.py tests/api/routers/test_pr_review_dispatch.py
git commit -m "feat(dispatch): add snapshot, command, command_context, commenter fields"
```

---

### Task 9: Create GHA Workflow in application-sdk

**Files:**
- Create: `application-sdk/.github/workflows/sdk-review.yml`
- Create: `application-sdk/.mothership/review.yaml`

- [ ] **Step 1: Write sdk-review.yml**

Create `.github/workflows/sdk-review.yml` in the application-sdk repo. This parses `@sdk-review` comments and dispatches to mothership.

Use the exact workflow from spec Section 15 (Invocation Flows). Key elements:
- Trigger: `issue_comment.created` containing `@sdk-review`
- Parse step: extract command, command_context, commenter
- Permission check: only OWNER/MEMBER/COLLABORATOR
- React with eyes emoji
- Connect VPN
- POST to `$MOTHERSHIP_URL/api/pr-review/dispatch` with snapshot, command, command_context, commenter

- [ ] **Step 2: Write .mothership/review.yaml**

Create `.mothership/review.yaml` in the application-sdk repo:

```yaml
review_guidelines: |
  This is the Atlan application-sdk v3 Python library.
  Built on Temporal workflows + Dapr infrastructure abstractions.

  Critical rules:
  - BLOCKING: Non-deterministic operation in run()/@entrypoint
  - BLOCKING: Contract field removal/rename/retype on Input/Output
  - BLOCKING: Hardcoded secrets or credentials in logs
  - CRITICAL: New public API without tests
  - CRITICAL: Breaking API change without deprecation shim

  Architecture:
  - Dependency direction: app/ -> execution/ -> infrastructure/
  - No direct temporalio/dapr imports outside private modules
  - All I/O in @task methods, never in run()
  - Use self.run_in_thread() for blocking operations in @task
  - CredentialRef in contracts, CredentialResolver in @task only

ignore_patterns:
  - "*.md"
  - "docs/**"
  - "*.lock"
  - "**/__pycache__/**"
```

- [ ] **Step 3: Commit (in application-sdk repo)**

```bash
cd /Users/vaibhav.chopra/work/application-sdk
git add .github/workflows/sdk-review.yml .mothership/review.yaml
git commit -m "feat: add mothership-backed sdk-review workflow and config"
```

---

### Task 10: Create Merge Queue Workflow

**Files:**
- Create: `application-sdk/.github/workflows/merge-queue.yml`

- [ ] **Step 1: Write merge-queue.yml**

Use the exact workflow from spec Section 16 (Queue-to-Update-and-Auto-Merge). Key elements:
- Triggers: push to main, auto_merge_enabled, pull_request_review submitted, label added
- Concurrency group `merge-queue` (no cancel-in-progress)
- Scans open PRs for eligibility (auto-merge enabled OR approved + auto-merge/sdk-review-approved label)
- Processes one PR at a time (FIFO)
- Behind → updateBranch API
- Clean → squash merge
- Conflicts → label needs-rebase + comment

- [ ] **Step 2: Commit**

```bash
cd /Users/vaibhav.chopra/work/application-sdk
git add .github/workflows/merge-queue.yml
git commit -m "feat: add merge queue workflow for auto-update and auto-merge"
```

---

### Task 11: Update SDK Docs

**Files:**
- Modify: `application-sdk/docs/agents/review.md`
- Modify: `application-sdk/docs/agents/testing.md`
- Modify: `application-sdk/docs/agents/coding-standards.md`

- [ ] **Step 1: Update docs/agents/review.md**

Replace the current content with the mothership-backed review documentation from spec Section 9 (docs improvements). Cover: triggers, guardrails G1-G8, auto-complete, disputing findings, override.

- [ ] **Step 2: Append to docs/agents/testing.md**

Add the "What SDK Review Checks for Tests" section from spec Section 9.

- [ ] **Step 3: Append to docs/agents/coding-standards.md**

Add the "Temporal Determinism" and "Contract Evolution" sections from spec Section 9.

- [ ] **Step 4: Commit**

```bash
cd /Users/vaibhav.chopra/work/application-sdk
git add docs/agents/review.md docs/agents/testing.md docs/agents/coding-standards.md
git commit -m "docs: update SDK docs for mothership-backed review"
```

---

### Task 12: End-to-End Test

- [ ] **Step 1: Verify snapshot loads with all files**

```bash
cd /Users/vaibhav.chopra/work/mothership
python -c "
from harness.core.config.rover_snapshots import load_snapshot
s = load_snapshot('sdk-reviewer')
print(f'Name: {s.name}')
print(f'Extends: {s.extends}')
print(f'Workspace files: {sorted(s.workspace_files.keys())}')
print(f'Extra files ({len(s.extra_files)}): {sorted(s.extra_files.keys())[:10]}...')
assert 'orchestration' in s.workspace_files or 'ORCHESTRATION.md' in s.workspace_files
assert len(s.extra_files) >= 15, f'Expected 15+ extra files, got {len(s.extra_files)}'
print('All checks passed')
"
```

- [ ] **Step 2: Verify dispatch accepts sdk-reviewer snapshot**

```bash
uv run python -m pytest tests/api/routers/test_pr_review_dispatch.py -v -k "sdk_reviewer"
```

- [ ] **Step 3: Manual test on a real PR (staging)**

1. Deploy mothership beta with the new snapshot
2. Create a test PR in application-sdk
3. Comment `@sdk-review` on the PR
4. Verify: GHA triggers → dispatches to mothership → review comment appears → status check set
5. Comment `@sdk-review auto-complete` on the same PR
6. Verify: fixes applied → re-reviewed → approved (or needs-human-review)
7. Comment `@sdk-review stop` while auto-complete is running
8. Verify: sandbox destroyed, "Review cancelled" comment

- [ ] **Step 4: Verify merge queue on staging**

1. Create two test PRs, both approved
2. Merge PR #1 manually
3. Verify: merge-queue workflow triggers → PR #2 branch updated → CI runs → auto-merges

---

## Deferred: Plan 2 (SDK Evolution) and Plan 3 (additional improvements)

These will be separate implementation plans:

- **Plan 2: SDK Evolution snapshot + dispatch + handler** — snapshots/sdk-evolution/, sdk_evolution_dispatch.py, submit-evolution.ts, cron workflow, codebase index
- **Plan 3: Deprecation cleanup** — remove old claude.yml sdk-review jobs, remove old branch-keeper.yml, update CLAUDE.md references
