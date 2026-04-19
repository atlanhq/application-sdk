---
name: sdk-evolution
description: Autonomous SDK Evolution — holistic review of the main branch. Dispatches 10 discovery agents, cross-model adversarial gates (Claude discovers + Codex reviews), creates Linear tickets with branches, raises PRs with TDD, then hands off to @sdk-review auto-complete for review/fix loop/confirmation. Self-improves SKILL.md. Invoke with /sdk-evolution.
---

# Autonomous SDK Evolution

Holistic review of the application-sdk main branch. Finds bugs, architectural issues, test gaps, doc drift, security concerns, performance problems, and v2 remnants. Every finding survives two adversarial challenges before a PR is raised. After Gate 2 passes, posts `@sdk-review auto-complete` on each PR — the existing GHA workflow handles SDK Review, fix loop, and confirmation autonomously.

**Spec:** `docs/superpowers/specs/2026-04-09-autonomous-sdk-evolution-design.md`

**Zero memory dependency:** This skill does NOT use `.claude/memory/`. All state lives in Linear tickets, git branches, and `SKILL.md` guardrails. Self-improvement happens by committing updates to this file and the reference rules.

---

## CRITICAL: Operational Guardrails

These guardrails are NON-NEGOTIABLE. Violating them caused real failures in production runs.

### 1. Follow ALL stages sequentially — never skip stages
The pipeline has 9 stages. Execute every one. Do NOT declare "run complete" after Stage 5 (Linear tickets). Stages 6-8 (Fix & PR, Gate 2, Handoff to @sdk-review) are part of the pipeline. If you skip them, the user will have to ask you to go back.

### 2. Update Linear tickets at EVERY state transition
- When a PR is created → set ticket to "In Review", attach the PR link, leave a comment
- When a PR is closed → leave a Linear comment with reason (not just description edit)
- When a finding is canceled → set ticket to "Canceled" (American spelling — check `list_issue_statuses` first!)
- When work starts → set ticket to "In Progress"
- When review feedback is addressed → leave a Linear comment summarizing what was fixed
- When all gates pass → set ticket to "In Review" (NOT "Done") and leave comment "All gates passed — ready for human merge"
- **"Done" means MERGED** — only set "Done" when the PR is actually merged into the target branch. Until then, the ticket stays "In Review". The 2026-04-14 run incorrectly set 9 tickets to "Done" while PRs were still open.
- **Parent ticket follows the same rule** — the parent stays "In Review" until ALL child tickets are resolved (merged, canceled, or needs-human-review). Only then does the parent move to "Done".
- **Always call `list_issue_statuses` before the first state update** to get exact state names for the workspace
- **Use Linear comments for decisions/reasoning** — keep the description clean and short, put the narrative in comments so people can follow the thread

### 3. Verify branch bases IMMEDIATELY after creation
After creating any fix branch, run: `git log --oneline -1` and verify the parent commit matches `origin/main` HEAD. Do this BEFORE making any changes.

### 4. `isolation: "worktree"` creates branches from the repo DEFAULT branch (main), NOT the current checkout
**NEVER use `isolation: "worktree"` for fix agents** in this pipeline. The worktree always bases off `main`, which has different content than the default branch. Instead:
- Do fixes sequentially on the main working directory
- Create branches explicitly from `origin/main`: `git checkout -b <branch> origin/main`
- Or use worktrees ONLY if you explicitly `git checkout main` inside the worktree first

### 5. Never run parallel fix agents without worktree isolation
Multiple agents sharing the same working directory will stomp on each other's changes. Commits get mixed, branches contain wrong files, and git state becomes corrupted. If you cannot use worktrees (see #4), do fixes **sequentially** — one at a time.

### 6. Tell discovery agents to return findings in their response text, not write files
Subagents frequently lack Write/Bash permissions. Instruct them to return JSON in their response message. The orchestrator (you) writes the findings files.

### 7. Holistic context review BEFORE writing any fix — never patch a symptom
**This is the most important guardrail.** Before writing a single line of fix code, answer these questions:

**Step 0a: Understand the file, not just the line**
Read the FULL file the finding is in (not just the flagged function). Ask:
- Is this file a utils/helpers dumping ground? (Multiple unrelated domains, 500+ lines, functions that should live in domain-specific modules)
- How many different concerns does this file mix? (SQL filters + credential parsing + thread handling + file I/O = dumping ground)
- If it IS a dumping ground → the fix is NOT "patch the function" — it's "move the function to its correct home or replace it with the v3 equivalent"

**Step 0b: Check if v3 already has a replacement**
Before fixing broken code, search for whether v3 already solved this problem correctly elsewhere:
```
Grep for the function's PURPOSE (not its name) across:
- application_sdk/execution/     (thread handling, workers, retry)
- application_sdk/infrastructure/ (state, secrets, pub/sub)
- application_sdk/storage/        (file I/O, object store)
- application_sdk/credentials/    (credential handling)
- application_sdk/app/            (app lifecycle, context)
```
If a v3 replacement exists → the fix is "migrate callers to the v3 API and deprecate/delete the legacy function." Do NOT patch the legacy function.

**Step 0c: Check who actually calls this function**
```bash
grep -rn "from.*import.*<function_name>\|<module>.<function_name>" application_sdk/ tests/
```
- If zero consumers → delete the function, don't fix it
- If only 1-2 consumers → consider inlining or migrating them directly
- If the consumers are all in deprecated modules → the fix is on the v3 migration path, not a point patch

**Step 0d: Decide the right fix scope**
Based on 0a-0c, the fix is ONE of:
1. **Migrate callers to v3 replacement** — if v3 already has a better API (e.g., `run_in_thread` exists, so migrate Azure clients there and delete `run_sync`)
2. **Move to correct module** — if the function is fine but lives in the wrong place (e.g., SQL functions in `common/utils.py` should be in `common/sql_utils.py`)
3. **Holistic refactor** — if the finding reveals a dumping-ground file that needs decomposition (create a Linear ticket for the cleanup, and fix the finding as part of that broader work)
4. **Point fix** — ONLY if the function is in the right place, has no v3 replacement, and the bug is genuinely just a code error

**The 2026-04-14 run violated this guardrail on PR #1308:** `run_sync` was broken, but the right fix was to migrate the 2 Azure callers to `execution/heartbeat.run_in_thread` (which uses a dedicated executor, safer than `run_sync`'s default executor) and delete `run_sync`. Instead, the pipeline patched `run_sync` with `functools.partial` — fixing the symptom while leaving the architectural debt. A human reviewer (Chris) caught this and flagged the entire `common/utils.py` as a dumping ground that needs holistic review. The pipeline should have seen this first.

### 7b. SDLC enforcement for EVERY fix — pre-commit, existing tests, TDD
This is the full checklist for fix agents. EVERY fix PR MUST complete ALL steps:

**Step A: Pre-commit**
```bash
uv run pre-commit run --files <changed_files>
```
Re-run after auto-formatting. Check 0 errors in pyright (warnings OK).

**Step A.5: Validate the fix is worthwhile — ask "is this even a real problem?"**
Before writing any fix, re-examine the finding with fresh eyes:
- **Call frequency:** Is the flagged code in a hot loop (thousands of calls) or called a few times per workflow? A per-call allocation that runs 3 times is fine; one that runs 10,000 times is not.
- **Proposed fix side effects:** Does the fix introduce new coupling (e.g., importing from an unrelated module)? Could it pollute a shared resource (e.g., reusing an executor meant for a different purpose)?
- **Would a senior engineer on this team care?** If you showed them the finding and the proposed fix, would they approve the PR or say "this is fine as-is"?

If the answer is "the current code is acceptable," **cancel the ticket** with a clear explanation rather than shipping a questionable fix. A canceled finding with good reasoning is better than a merged PR that a human reviewer has to revert.

*Lesson from 2026-04-16 run: PR #1407 was closed after human review found the ThreadPoolExecutor-per-query pattern was acceptable (low call frequency). The fix would have polluted the heartbeat executor. Gate 1 should have killed this finding.*

**Step B: Run existing unit tests**
```bash
uv run pytest tests/unit/ -x -q --timeout=60
```
The fix MUST NOT break any existing tests. If tests fail, investigate and fix before proceeding.

**Step C: TDD for required tests**
Test policy — only write tests when genuinely needed:
- Bug fixes → regression test REQUIRED (TDD: write failing test FIRST, then fix)
- Security fixes → regression test REQUIRED (TDD)
- Performance fixes → behavior test REQUIRED (TDD: e.g., test that timeout is set)
- Code quality fixes (e.g., `from e`) → NO new test (pre-commit + existing tests only)
- Doc fixes → NO new test
- v2 cleanup → NO new test (existing tests must still pass)

When TDD is required:
1. Write the failing test FIRST
2. Run it and confirm it FAILS: `uv run pytest <test_file> -x`
3. Implement the fix
4. Run it and confirm it PASSES: `uv run pytest <test_file> -x`

**Step D: Run full test suite after fix**
```bash
uv run pytest tests/unit/ -x -q --timeout=60
```

**Step E: Validate all CI checks pass**
All pre-commit hooks, unit tests, and type checks must be green before pushing. Do NOT push if anything is red.

**Step F: Verify the diff contains ONLY intended changes — no formatter noise**
Before committing, run `git diff --stat` and verify:
1. Only the files you intentionally changed are listed
2. No unrelated files were reformatted by pre-commit (ruff-format, isort)
3. The total line count is proportional to the fix — a 7-line permissions change should NOT produce a 400-line diff

If pre-commit reformatted unrelated files (because the working tree had dirty files from other branches), `git checkout -- <unrelated_files>` to discard them before committing. Only `git add` the specific files you changed.

*Lesson from 2026-04-16 run: PR #1405 was closed because it contained ~400 lines of auto-formatter rewrites across 15+ unrelated files. The actual permissions fix was 7 lines buried in noise. This happened because pre-commit ran on a dirty working tree.*

**Step G: For CI/infrastructure changes, validate downstream impact**
If the fix modifies GitHub Actions permissions, workflow triggers, Helm values, or Dapr configs:
1. Identify every job/step that uses the changed permission or config
2. Verify each one still works — don't assume `contents: read` is sufficient without checking
3. For permission downgrades: search the workflow for actions that may need the old permission (e.g., `add-pr-comment` may need `contents: write` or `pull-requests: write`)
4. For CI-only PRs: run the workflow on a test branch before merging to main

*Lesson from 2026-04-16 run: PR #1405 downgraded `contents: write` to `contents: read` without verifying the docs preview job (which uses add-pr-comment + S3 sync) still works.*

If ANY step fails, fix the issue before committing. Do NOT push a PR that hasn't passed all steps.

### 8. Check for regressions in reformatted code
When ruff-format reformats code, check if it changed semantics. Python implicit string concatenation (`"a" "b"` → `"ab"`) is the most common trap — the formatter may merge them into one string instead of splitting into list entries.

### 9. Retrospective findings are exempt from Gate 1
Per retro-009: Gate 1 uses code-review criteria which will systematically kill pipeline improvement suggestions. Skip Gate 1 for retrospective category findings.

### 10. Security agent needs explicit scan directives
The security agent prompt must explicitly instruct scanning of `.github/workflows/`, `constants.py`, and Dockerfile. Without explicit file targets, the agent tends to produce 0 findings even when violations exist.

### 11. Gate 2 MUST verify test coverage
Gate 2 challengers must explicitly check:
- Bug fix PRs: "Does this PR include a regression test?" — if not, verdict is `needs-changes`
- Performance fix PRs: "Does this PR include a test for the new behavior?" — if not, verdict is `needs-changes`
- The checklist item "Are tests included?" must be enforced, not advisory

### 12. @sdk-review auto-complete handles SDK Review, fix loop, and confirmation
After Gate 2 passes, this pipeline posts `@sdk-review auto-complete` on the PR. The GHA workflow handles: 4-agent cross-model review, inline comments, fix loop (max iterations), and final confirmation/approval. This pipeline does NOT run SDK Review internally.

### 13. Cross-model review is mandatory (in discovery + Gate 1)
Claude and Sonnet alternate as discoverer/challenger in Stages 2-4. This eliminates single-model bias during finding discovery and Gate 1 challenges.

### 14. PR labels track state
Every PR gets these labels as it progresses:
- `Autonomous SDK Evolution` — always present
- `needs-review` — after creation, pending Gate 2
- `needs-design-review` — architecture/design findings that need human input
- After Gate 2 passes and `@sdk-review auto-complete` is posted, label management is handled by the GHA workflow

### 16. Branch = Linear ticket identifier
Branch name MUST be the Linear ticket identifier (e.g., `BLDX-123`). This auto-links commits, PRs, and context in Linear. Set the branch on the Linear ticket at creation time.

### 17. Self-improvement updates SKILL.md, not memory
After each run, if patterns emerge (recurring false positives, recurring review feedback, new guardrails needed), commit updates directly to this SKILL.md and the reference rules files. Do NOT write to `.claude/memory/`.

### 18. Run security audit on pipeline output
Before completing, verify:
- No secrets in PR descriptions or comments
- No credentials in test fixtures
- No internal IPs/URLs exposed in PR content
- No tokens or API keys in any generated content

### 19. NEVER print "run complete" until summary.json shows prs_created > 0
The 2026-04-09 run stopped at Stage 5 (tickets only, 0 PRs). The 2026-04-14 run did the same — stopped after 3 PRs and printed a completion banner while 6 tickets had no PRs. **Before printing ANY completion message:**
1. Verify `prs_created >= surviving_findings_count` (or document why each was skipped)
2. Verify Gate 2 ran on every PR (check `gate2/` directory has verdicts)
3. Verify `@sdk-review auto-complete` was posted on every PR that passed Gate 2
4. Verify all Linear tickets for Gate 2-passed PRs are set to "In Review"
5. If ANY of these fail, you are NOT done — keep going

### 20. Gate 2 agents MUST read PR diffs from GitHub, NEVER local working tree
The 2026-04-16 run had Gate 2 agents reading local files (which were on different branches) instead of the actual PR diff. This caused false "needs-changes" verdicts for PRs whose changes were correct on GitHub but invisible locally.

**Gate 2 agent prompts MUST include this instruction:**
```
IMPORTANT: Do NOT read local files to verify PR changes. The working directory may be
on a different branch. Instead, use Bash to run:
  gh pr diff {pr_number} --repo atlanhq/application-sdk
This gives you the actual diff that will be merged. Only use Read on files for
understanding context (existing code around the change), not to verify the change itself.
```

### 21. Labels are STATE TRANSITIONS, not defaults — never apply labels prematurely
Labels reflect actual pipeline state. Do NOT batch-apply labels before running the stage that determines them.
- `needs-review` → apply ONLY after PR creation (Stage 6), before Gate 2
- After Gate 2 passes → `@sdk-review auto-complete` is posted and the GHA workflow manages subsequent labels
- **NEVER apply `needs-human-review` or `ready-to-merge` as a placeholder.** Those are managed by the @sdk-review workflow.

### 21. Cross-model discovery uses GPT-5 via LiteLLM — do not silently substitute Sonnet
The config specifies GPT-5 (secondary model) for discovery agents 2,4,6,9,10. This eliminates single-model bias in finding discovery.
- **Check LiteLLM proxy availability** at the start of the run: `curl -s https://llmproxy.atlan.dev/health`
- If available: use the curl pattern from the Configuration section for secondary-model discovery agents
- If unavailable: **log it explicitly** as `"cross_model_discovery": "degraded — GPT-5 unavailable, used Sonnet"` in summary.json. Do NOT silently substitute without logging.
- SDK Review cross-model review is now handled by the `@sdk-review` GHA workflow — this pipeline no longer runs SDK Review internally.

### 22. Retrospective rule updates MUST be committed in the same run
The 2026-04-09 retrospective found 12 improvements. Zero were committed to rule files. The 2026-04-14 retrospective found the same 12 — still uncommitted. Guardrail #17 says "commit updates directly to SKILL.md and reference rules files" but this was never enforced.
**In Stage 10, BEFORE writing summary.json:**
1. Read all retrospective findings that survived
2. For each finding that updates a rule file: make the edit
3. `git add .claude/skills/sdk-evolution/ && git commit -m "chore(sdk-evolution): self-improvement from {DATE} run" && git push origin main`
4. Log which rules were updated in summary.json under `"rules_updated": [...]`
5. If no retrospective findings need rule updates, log `"rules_updated": []` with reasoning

### 23. Every stage must log a checkpoint before proceeding
Print `[Stage N/9 complete] → Proceeding to Stage N+1` after each stage finishes. This makes premature stops visible in the conversation. If a user sees `[Stage 5/9 complete]` as the last checkpoint, they know stages 6-9 didn't run. This was added because the 2026-04-14 run had no visible stage markers — it silently stopped and printed a final banner.

### 24. Check CI status on EVERY PR before posting @sdk-review — fix failures yourself, never give up
After pushing a PR and before posting `@sdk-review auto-complete`, wait for CI and verify it passes:
```bash
gh pr checks {pr_number} --repo atlanhq/application-sdk --watch
```
If any check fails — **do NOT stop, ask the user, or cancel the PR. Fix it yourself:**
1. Read the failure logs: `gh run view {run_id} --repo atlanhq/application-sdk --log-failed`
2. If the failure is in your changed code → diagnose the root cause, fix the code on the same branch, run pre-commit + unit tests locally to verify, push, wait for CI again. Keep iterating until green.
3. If the failure is a flaky/infra issue (e.g., segfault during teardown with all tests passing, timeout on a specific OS) → re-run the failed job: `gh run rerun {run_id} --repo atlanhq/application-sdk --failed`. Wait for re-run result.
4. **NEVER cancel a PR because of CI failures.** CI failures are fixable — that's the whole point of this pipeline. Keep fixing until it's green.
5. **NEVER post `@sdk-review auto-complete` while CI is red.** The 2026-04-14 run labeled PR #1309 as ready-to-merge with a failing check that was never investigated.

---

## Configuration

Read the pipeline configuration:

```
skills/sdk-evolution/config.yaml
```

This controls model assignments, Linear settings, PR strategy, and pipeline guardrails. Parse the YAML and use these values throughout the pipeline.

**Model dispatch rules:**
- `primary` (Claude Opus 4.6): Use the Agent tool with default model (inherits from session)
- `secondary` (GPT-5): Invoke via `curl` to the LiteLLM proxy. Claude Code cannot natively call GPT-5, so use this pattern:

```bash
export LITELLM_API_KEY="${LITELLM_API_KEY}"
curl -s https://llmproxy.atlan.dev/chat/completions \
  -H "Authorization: Bearer $LITELLM_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "GPT-5",
    "messages": [
      {"role": "system", "content": "<system prompt with rules>"},
      {"role": "user", "content": "<the review/discovery task>"}
    ],
    "max_tokens": 16000
  }'
```

**GPT-5 is a reasoning model** — it uses reasoning tokens internally. Always set `max_tokens: 16000` minimum for review tasks. The response content may be empty if `max_tokens` is too low.

**For discovery agents using secondary model:** Instead of `Agent tool with model: "sonnet"`, use a Bash tool call with the curl pattern above. Parse the JSON response and extract the findings.

**For SDK Review agents using secondary model (Architecture + Code Quality):** Same curl pattern. Include the full PR diff and reference rules in the user message.

---

## Stage 0: Pre-Flight — Deduplication & Full Project Context

Before doing ANY work, build a complete picture of what's already tracked and in progress. This prevents duplicate work and gives context on what other milestones are doing.

### 0.1 Scan Autonomous SDK Evolution Milestone

```
Use mcp__linear__list_issues with:
  project: "App SDK v3.0"
  milestone: "Autonomous SDK Evolution"
  state: "Backlog" OR "Todo" OR "In Progress" OR "In Review" OR "Done"
```

### 0.2 Scan ALL Other Milestones in Project

```
Use mcp__linear__list_milestones for project "App SDK v3.0"
For EACH milestone: list_issues to get open tickets
```

This gives full project context — what's being worked on elsewhere, design decisions in flight, etc.

### 0.3 Scan ALL Open PRs on main

```bash
gh pr list --repo atlanhq/application-sdk --base main --state open --json number,title,headRefName,labels
```

### 0.4 Resolve User for Ticket Assignment

```
Use mcp__linear__list_users to find the user to assign tickets to.
Look for "vaibhav" or the pipeline owner. Store the user_id for ticket creation.
```

### 0.5 Build Suppression List

For each open ticket (from 0.1 + 0.2), extract:
- The file path(s) mentioned in the description
- The rule ID(s) cited
- The finding category

For each open PR (from 0.3), extract:
- The branch name and files changed
- Any linked Linear ticket identifiers

Store as a suppression list. During Stage 2 (Discovery), any finding that matches:
- An existing ticket's file + rule combination → SKIP
- An open PR's changed files → SKIP (already being fixed)
- An in-progress ticket in any milestone → SKIP (being worked on)

### 0.6 Log Context

Print:
```
Found N existing open tickets across M milestones.
Found P open PRs on main.
Suppressing duplicates for: [list of file:rule pairs]
```

---

## Stage 1: Setup

### 1.1 Pull Latest

```bash
git checkout main
git pull origin main
```

### 1.2 Create Workspace

```bash
RUN_DATE=$(date +%Y-%m-%d)
WORKSPACE="tmp/sdk-evolution/${RUN_DATE}"
mkdir -p "${WORKSPACE}/findings" "${WORKSPACE}/challenged" "${WORKSPACE}/tickets" "${WORKSPACE}/prs" "${WORKSPACE}/gate2" "${WORKSPACE}/sdk-review" "${WORKSPACE}/confirmation"
```

### 1.3 Load Reference Rules

Read ALL 4 reference rule files from `skills/sdk-evolution/references/`:

1. `v3-architecture-rules.md`
2. `code-quality-rules.md`
3. `security-rules.md`
4. `performance-rules.md`

Store each file's content — you will pass them into agent prompts in Stage 2.

### 1.4 Check for Previous Runs

Check if `tmp/sdk-evolution/` has previous run directories. If so, note them — the Retrospective agent will use this data.

---

## Stage 2: Discovery

Dispatch **10 discovery agents in parallel**. Each agent scans the codebase against its reference rules and returns findings as JSON in their response text (Guardrail #6).

### Finding Schema

Every agent MUST output this exact JSON format:

```json
{
  "agent": "<agent-name>",
  "model": "<opus|sonnet>",
  "timestamp": "<ISO-8601>",
  "findings": [
    {
      "id": "<agent-prefix>-NNN",
      "category": "<code-quality|test-quality|architecture|documentation|v2-patterns|bug|security|performance|retrospective>",
      "severity": "<critical|high|medium|low>",
      "file": "path/to/file.py",
      "line": 245,
      "rule": "ADR-0010 / QUAL-003 / BUG-016 / etc.",
      "title": "Short description of the issue",
      "description": "Detailed explanation of what is wrong and why it matters",
      "suggested_fix": "How to fix it, with code if applicable",
      "confidence": 85
    }
  ]
}
```

**Rules:**
- Only report findings with `confidence >= 80`
- Each finding must cite a specific rule ID from the reference file
- Each finding must include file path and line number
- Each finding must include a concrete suggested_fix
- **Filter against suppression list** — skip findings matching suppressed file:rule combos

### Agent Dispatch

Dispatch all 10 agents in a **single message with 10 parallel Agent tool calls**. For agents using the secondary model, set `model: "sonnet"`.

**IMPORTANT:** Each agent prompt must include:
1. The relevant reference rules content (pasted in full)
2. The suppression list (so agents skip known issues)
3. Instruction to return findings as JSON in their response text (NOT write files)

---

### Agent 1: Code Quality (primary/opus)

```
Agent tool call:
  description: "SDK Evolution: Code Quality"
  model: (default — opus)
  prompt: |
    You are a code quality reviewer for the Atlan application-sdk v3.

    ## Your Reference Rules
    {paste full content of references/code-quality-rules.md}

    ## Suppression List (SKIP these)
    {suppression list from Stage 0}

    ## Instructions
    Scan ALL Python files in `application_sdk/` directory. Use Glob to find files,
    Read to examine them. For each file, check against every rule in your reference.

    Focus on:
    - Import violations (top-level only, ordering, star imports)
    - Logging violations (f-strings G004, concatenation G003, print T201)
    - Naming convention violations
    - Function size and complexity
    - Error handling (bare except, silent swallowing, missing chaining)
    - Async pattern violations
    - Pre-commit compliance issues

    Only report findings with confidence >= 80. Cite the specific rule ID.
    Skip any finding that matches the suppression list.

    ## Output
    Return your findings as JSON in your response text.
    Use the Finding Schema format. Use "cq-" prefix for finding IDs (e.g., cq-001).
```

### Agent 2: Test Quality (secondary/sonnet)

```
Agent tool call:
  description: "SDK Evolution: Test Quality"
  model: "sonnet"
  prompt: |
    You are a test quality reviewer for the Atlan application-sdk v3.

    ## Your Reference Rules
    No formal test-quality-rules.md exists — apply general test quality principles: typed dataclasses/Pydantic for fixtures, asyncio_mode="auto", clean_app_registry after tests defining App subclasses, no real external calls in unit tests, no redundant @pytest.mark.asyncio decorators, mock infrastructure (MockStateStore/MockSecretStore/MockPubSub), meaningful assertions over `assert result is not None`.

    ## Suppression List (SKIP these)
    {suppression list from Stage 0}

    ## Instructions
    Scan ALL test files in `tests/` and corresponding source in `application_sdk/`.
    Use Glob to find test files, Read to examine them.

    Check:
    - Missing tests for public modules (map application_sdk/ to tests/unit/)
    - Tests requiring Dapr/Temporal sidecars (should use in-memory mocks)
    - Missing clean_app_registry fixture in tests defining App subclasses
    - Redundant @pytest.mark.asyncio decorators
    - Vague assertions (assert result, assert result is not None)
    - Tests checking implementation details instead of behavior
    - Missing edge case and error path tests
    - Test isolation violations (shared state, execution order deps)
    - Real external calls in unit tests
    - Contract tests (payload safety, round-trip, evolution)
    - Correct marker usage (integration/e2e)
    - Coverage gaps in complex branching logic

    Only report findings with confidence >= 80. Cite the specific rule ID.
    Skip any finding that matches the suppression list.

    ## Output
    Return your findings as JSON in your response text.
    Use the Finding Schema format. Use "tq-" prefix for finding IDs.
```

### Agent 3: Architecture (primary/opus)

```
Agent tool call:
  description: "SDK Evolution: Architecture"
  model: (default — opus)
  prompt: |
    You are an architecture reviewer for the Atlan application-sdk v3.

    ## Your Reference Rules
    {paste full content of references/v3-architecture-rules.md}

    ## Suppression List (SKIP these)
    {suppression list from Stage 0}

    ## Instructions
    Scan the FULL repository. Focus on:
    - `application_sdk/app/` — App, @task, registries
    - `application_sdk/contracts/` — typed contracts
    - `application_sdk/handler/` — handler framework
    - `application_sdk/execution/` — Temporal abstraction
    - `application_sdk/infrastructure/` — protocol implementations
    - `application_sdk/credentials/` — credential system
    - `application_sdk/storage/` — object storage
    - `helm/` — deployment templates
    - `examples/` — example apps

    Check against ALL 11 ADRs:
    - ADR-0001: Per-App Handlers (stateless, typed contracts)
    - ADR-0002: Per-App Workers (dedicated task queues, scale-to-zero)
    - ADR-0003: Correlation-Based Tracing (correlation_id propagation)
    - ADR-0004: Build-Time Type Safety (Pydantic, pyright, no Dict[str, Any])
    - ADR-0005: Infrastructure Abstraction (no direct temporalio/dapr imports)
    - ADR-0006: Schema-Driven Contracts (single Input/Output, additive evolution)
    - ADR-0007: Apps as Coordination Unit (tasks internal; call_by_name deactivated — BLDX-878)
    - ADR-0008: Payload-Safe Bounded Types (no Any/bytes/unbounded collections)
    - ADR-0009: Separate Handler/Worker (ATLAN_ prefix, mode flags)
    - ADR-0010: Async-First (run_in_thread, internal timeouts)
    - ADR-0011: Logging Levels (DEBUG/INFO/WARNING/ERROR only)

    Also check general architecture: app/base.py size, registry safety, deprecation shims.

    Only report findings with confidence >= 80.
    Skip any finding that matches the suppression list.

    ## Output
    Return your findings as JSON in your response text.
    Use the Finding Schema format. Use "arch-" prefix for finding IDs.
```

### Agent 4: Documentation (secondary/sonnet)

```
Agent tool call:
  description: "SDK Evolution: Documentation"
  model: "sonnet"
  prompt: |
    You are a documentation quality reviewer for the Atlan application-sdk v3.

    ## Your Reference Rules
    No formal docs-quality-rules.md exists — apply general documentation quality principles: code examples must match current API, no v2 import paths (from application_sdk.workflows/activities/handlers), no @dataclass on Input/Output contracts (should be plain Pydantic BaseModel subclass), no bare `pyatlan.` imports (use pyatlan_v9), all referenced module paths must exist in current codebase.

    ## Suppression List (SKIP these)
    {suppression list from Stage 0}

    ## Instructions
    Scan `docs/` directory and cross-reference against actual code in `application_sdk/`.

    Check:
    - Code-doc drift (docs referencing APIs/classes that changed or don't exist)
    - v2 import paths or patterns shown in docs
    - Example code accuracy (would it run against current codebase?)
    - Architecture doc consistency with ADRs
    - Link integrity (broken internal links, wrong branch references)
    - Missing docs for public APIs
    - MkDocs nav consistency (mkdocs.yml vs actual files)

    For each finding, verify by reading both the doc file AND the code it references.

    Only report findings with confidence >= 80.
    Skip any finding that matches the suppression list.

    ## Output
    Return your findings as JSON in your response text.
    Use the Finding Schema format. Use "doc-" prefix for finding IDs.
```

### Agent 5: v2 Pattern Detector (primary/opus)

```
Agent tool call:
  description: "SDK Evolution: v2 Patterns"
  model: (default — opus)
  prompt: |
    You are a v2 pattern detector for the Atlan application-sdk v3.

    ## Your Reference Rules
    No formal v2-pattern-rules.md exists — scan for: imports from application_sdk.workflows/activities/handlers (removed in v3); ActivitiesInterface/WorkflowInterface/HandlerInterface subclasses; direct DaprClient/temporalio usage outside execution/_temporal/_dapr modules; @workflow.defn/@activity.defn decorators; ObjectStore from application_sdk.services; BaseSQLMetadataExtractionActivities; credentials as plain dict (should use CredentialRef); bare pyatlan. imports (should be pyatlan_v9); @dataclass on Input/Output subclasses (should be plain Pydantic BaseModel).

    ## Suppression List (SKIP these)
    {suppression list from Stage 0}

    ## Instructions
    Scan `application_sdk/` and `tests/` for ALL v2 patterns listed in your rules.

    For EACH rule (V2-001 through V2-020):
    1. Use Grep with the specified grep pattern
    2. For each match, read surrounding context to confirm it's a real v2 usage
       (not a comment, docstring, or migration helper)
    3. Exclude files in _temporal/, _dapr/, _redis/ private modules (they are
       allowed to use internal APIs)

    Be thorough — grep for every pattern. False negatives are worse than false positives
    here (Gate 1 will filter false positives).

    Only report findings with confidence >= 80.
    Skip any finding that matches the suppression list.

    ## Output
    Return your findings as JSON in your response text.
    Use the Finding Schema format. Use "v2-" prefix for finding IDs.
```

### Agent 6: Bug Hunter A (secondary/sonnet)

```
Agent tool call:
  description: "SDK Evolution: Bug Hunter A"
  model: "sonnet"
  prompt: |
    You are a bug hunter for the Atlan application-sdk v3.

    ## Your Reference Rules
    No formal bug-hunting-rules.md exists — hunt for: race conditions on shared mutable state across coroutines; Temporal determinism violations (random/time/IO in run()); missing heartbeats in long @task methods; resource leaks (unclosed DB connections, file handles); None/Optional mishandling; swallowed exceptions hiding real failures; blocking calls in async context without run_in_thread(); mutable default arguments; concurrency bugs in asyncio.gather usage.

    ## Suppression List (SKIP these)
    {suppression list from Stage 0}

    ## Instructions
    Scan ALL Python files in `application_sdk/`. Hunt for real bugs — not style issues,
    not improvements, but actual correctness problems.

    Focus areas (in order of severity):
    1. Race conditions and concurrency bugs (BUG-004 to BUG-006)
    2. Temporal-specific bugs — non-determinism in run(), missing heartbeats (BUG-025 to BUG-028)
    3. Resource leaks — unclosed handles, missing context managers (BUG-007 to BUG-009)
    4. None safety — missing checks on Optional returns (BUG-010 to BUG-012)
    5. Exception handling bugs — swallowed errors, broken chaining (BUG-013 to BUG-015)
    6. Async bugs — missing await, blocking in async (BUG-016 to BUG-018)
    7. Data integrity — mutable defaults, shared state mutation (BUG-022 to BUG-024)
    8. Contract bugs — type mismatches, missing defaults (BUG-029 to BUG-031)

    For each potential bug:
    - Read the full file context (not just the flagged line)
    - Trace data flow to confirm the bug is reachable
    - Check if there are tests that would catch it
    - Only report if you are >= 80% confident it's a real bug
    - Skip any finding that matches the suppression list

    ## Output
    Return your findings as JSON in your response text.
    Use the Finding Schema format. Use "bug-a-" prefix for finding IDs.
```

### Agent 7: Bug Hunter B (primary/opus)

```
Agent tool call:
  description: "SDK Evolution: Bug Hunter B"
  model: (default — opus)
  prompt: |
    You are a bug hunter for the Atlan application-sdk v3.

    ## Your Reference Rules
    No formal bug-hunting-rules.md exists — hunt for: race conditions on shared mutable state across coroutines; Temporal determinism violations (random/time/IO in run()); missing heartbeats in long @task methods; resource leaks (unclosed DB connections, file handles); None/Optional mishandling; swallowed exceptions hiding real failures; blocking calls in async context without run_in_thread(); mutable default arguments; concurrency bugs in asyncio.gather usage.

    ## Suppression List (SKIP these)
    {suppression list from Stage 0}

    ## Instructions
    Scan ALL Python files in `application_sdk/`. Hunt for real bugs — not style issues,
    not improvements, but actual correctness problems.

    Focus areas (in order of severity):
    1. Retry/timeout bugs — missing timeouts, non-idempotent retries (BUG-019 to BUG-021)
    2. Logic errors — off-by-one, wrong comparisons, inverted conditions (BUG-001 to BUG-003)
    3. Async bugs — deadlocks, event loop blocking (BUG-016 to BUG-018)
    4. Resource leaks — connections, file handles, cursors (BUG-007 to BUG-009)
    5. Temporal-specific — payload safety, determinism, heartbeats (BUG-025 to BUG-028)
    6. None/null safety — Optional mishandling (BUG-010 to BUG-012)
    7. Data integrity — shallow copy bugs, mutable defaults (BUG-022 to BUG-024)
    8. Exception handling — silent failures, lost tracebacks (BUG-013 to BUG-015)

    NOTE: Bug Hunter A is scanning the same codebase simultaneously with different
    focus ordering. Your findings will be cross-referenced. Do NOT coordinate — hunt
    independently.

    For each potential bug:
    - Read the full file context (not just the flagged line)
    - Trace data flow to confirm the bug is reachable
    - Check if there are tests that would catch it
    - Only report if you are >= 80% confident it's a real bug
    - Skip any finding that matches the suppression list

    ## Output
    Return your findings as JSON in your response text.
    Use the Finding Schema format. Use "bug-b-" prefix for finding IDs.
```

### Agent 8: Security (primary/opus)

```
Agent tool call:
  description: "SDK Evolution: Security"
  model: (default — opus)
  prompt: |
    You are a security reviewer for the Atlan application-sdk v3.

    ## Your Reference Rules
    {paste full content of references/security-rules.md}

    ## Suppression List (SKIP these)
    {suppression list from Stage 0}

    ## Instructions
    Scan ALL Python files in `application_sdk/`. Check for security vulnerabilities.

    Priority order:
    1. Secret management — hardcoded secrets, secrets in logs, credential handling
    2. SQL injection — string interpolation in queries
    3. Command injection — os.system, shell=True, eval, exec
    4. Deserialization — pickle, unsafe yaml, eval
    5. Multi-tenant isolation — missing tenant scoping, cross-tenant access
    6. Input validation — missing validation at system boundaries
    7. Path traversal — unsanitized file paths
    8. Error info disclosure — stack traces in responses
    9. Dependency security — unpinned versions, unsafe deps
    10. CORS and network — wildcard origins, binding 0.0.0.0

    EXPLICITLY SCAN THESE FILES (do not skip):
    - `Dockerfile` and `.github/workflows/` for supply chain issues
    - `helm/` for security misconfigurations
    - `application_sdk/constants.py` for hardcoded values

    Security issues are always Critical or Important — never Minor.
    Only report findings with confidence >= 80.
    Skip any finding that matches the suppression list.

    ## Output
    Return your findings as JSON in your response text.
    Use the Finding Schema format. Use "sec-" prefix for finding IDs.
```

### Agent 9: Performance (secondary/sonnet)

```
Agent tool call:
  description: "SDK Evolution: Performance"
  model: "sonnet"
  prompt: |
    You are a performance reviewer for the Atlan application-sdk v3.

    ## Your Reference Rules
    {paste full content of references/performance-rules.md}

    ## Suppression List (SKIP these)
    {suppression list from Stage 0}

    ## Instructions
    Scan ALL Python files in `application_sdk/`. Look for performance issues.

    Priority order:
    1. Blocking in async — sync calls without run_in_thread (PERF-001 to PERF-003)
    2. Missing timeouts — HTTP/DB calls without timeout (PERF-011, PERF-012)
    3. Unbounded memory — loading full datasets, unbounded lists (PERF-005 to PERF-007)
    4. N+1 patterns — loop fetching instead of batching (PERF-010)
    5. Missing connection pooling (PERF-004)
    6. Serialization — json vs orjson, unnecessary round-trips (PERF-008, PERF-009)
    7. Expensive logging — f-strings in log calls (PERF-013)
    8. Import performance — heavy top-level imports (PERF-014)
    9. File I/O — sync large file ops (PERF-015)
    10. Concurrency — sequential where parallel possible (PERF-016, PERF-017)

    Only report findings with confidence >= 80.
    Skip any finding that matches the suppression list.

    ## Output
    Return your findings as JSON in your response text.
    Use the Finding Schema format. Use "perf-" prefix for finding IDs.
```

### Agent 10: Retrospective (secondary/sonnet)

```
Agent tool call:
  description: "SDK Evolution: Retrospective"
  model: "sonnet"
  prompt: |
    You are the retrospective reviewer for the Autonomous SDK Evolution pipeline.

    ## Instructions
    Your job is to review the pipeline itself and suggest improvements.

    1. Read the pipeline config: skills/sdk-evolution/config.yaml
    2. Read the SKILL.md: skills/sdk-evolution/SKILL.md
    3. Read ALL reference rule files in skills/sdk-evolution/references/
    4. Check for previous run data in tmp/sdk-evolution/ (if any exist)

    Evaluate:

    **A. Rule Quality:**
    - Are any rules stale or overly broad (would generate noise)?
    - Are there patterns in the codebase that no rule currently covers?
    - Are confidence thresholds appropriate?
    - Are model assignments balanced?
    - Are there gaps in the review dimensions?
    - Could any reference rules be improved with better code examples?
    - Do any rules contradict each other? (e.g., PERF-014 lazy imports vs code-quality top-level imports)

    **B. SDLC Compliance (check previous run PRs if they exist):**
    - Did fix PRs run pre-commit before pushing? Check PR CI status.
    - Did fix PRs run existing unit tests? Check for test failures in CI.
    - Did bug fix PRs include regression tests? Read the PR diffs — if a bug fix has no new test file, flag it.
    - Did performance fix PRs include behavior tests?
    - Were Linear tickets updated at every state transition? Check ticket history for stale states.
    - Were branch bases verified? Check if PRs target the correct base branch.

    **C. Pipeline Execution Quality (check previous run data):**
    - Were all stages executed? Check if summary.json shows prs_created > 0 (Stage 6 ran).
    - Did Gate 2 catch issues? Check gate2/ verdicts — if all "passed" with no "needs-changes", the gate may be too lenient.
    - Were findings deduplicated against existing milestone tickets? Check for duplicate Linear tickets.
    - Did the review fix loop actually fix issues? Or did it max out iterations?

    If previous runs exist, also check:
    - What percentage of findings were killed at Gate 1? (too many = noisy rules)
    - Were there recurring false positives? (rules need tightening)
    - Were there findings that Gate 1 killed incorrectly? (challenger too aggressive)
    - Did any PRs get merged that later caused CI failures? (SDLC gaps)

    Only report actionable improvement suggestions with confidence >= 80.

    ## Output
    Return your findings as JSON in your response text.
    Use the Finding Schema format. Use "retro-" prefix for finding IDs.
    Category should be "retrospective".
```

---

## Stage 3: Bug Hunter Merge

After all 10 agents complete, merge Bug Hunter A and B findings.

### 3.1 Read Both Results

Parse the JSON from Bug Hunter A and Bug Hunter B agent responses.

### 3.2 Cross-Reference

For each finding in A, check if B has a finding:
- In the **same file**
- With **overlapping line range** (within 10 lines)
- With **similar title or description** (same type of bug)

**If matched (dual-confirmed):**
- Keep the finding with the more detailed description
- Set confidence to `min(max(A.confidence, B.confidence) + 10, 99)`
- Set ID prefix to `bug-` (replacing `bug-a-` or `bug-b-`)
- Add note: "Dual-confirmed by both Opus and Sonnet"

**If only in A (not matched in B):**
- Dispatch a single **Opus** agent (primary) to confirm or kill:
  ```
  prompt: "Read {file} around line {line}. Is this a real bug? Finding: {finding JSON}.
           Respond with JSON: {\"verdict\": \"confirmed|killed\", \"reasoning\": \"...\"}"
  ```
- If confirmed: keep it, change prefix to `bug-`
- If killed: discard it

**If only in B (not matched in A):**
- Dispatch a single **Sonnet** agent (secondary, `model: "sonnet"`) with same prompt
- If confirmed: keep it, change prefix to `bug-`
- If killed: discard it

### 3.3 Write Merged Results

Write the merged findings to: `{WORKSPACE}/findings/bugs-merged.json`

Print: "Bug Hunter merge: X dual-confirmed, Y single-confirmed, Z killed"

---

## Stage 4: Gate 1 — Devil's Advocate

Challenge every finding before it becomes a ticket.

### 4.1 Collect All Findings

Read all findings (from agent responses and written files) EXCEPT raw bug-hunter-a and bug-hunter-b (use `bugs-merged.json` instead):

- code-quality findings
- test-quality findings
- architecture findings
- documentation findings
- v2-patterns findings
- bugs-merged findings
- security findings
- performance findings
- retrospective findings (EXEMPT from Gate 1 — per Guardrail #9, skip to Stage 5)

Merge into a single list sorted by severity (critical first).

### 4.2 Challenge Each Finding

For each finding (except retrospective), dispatch a **challenger agent** using the **opposite model** from the discoverer:

| Discoverer Model | Challenger Model |
|-----------------|-----------------|
| opus (primary) | sonnet (secondary, `model: "sonnet"`) |
| sonnet (secondary) | opus (primary, default model) |

**Challenger prompt template:**

```
You are a Devil's Advocate reviewing a finding from the Autonomous SDK Evolution pipeline.
Your job is to CHALLENGE this finding — kill it if it's not a real issue.

## The Finding
{finding JSON}

## The Flagged File
{full content of the file at finding.file}

## Git Blame (lines around the finding)
{output of: git blame -L {line-5},{line+5} {file}}

## Relevant Rules
{content of the reference rules file for this finding's category}

## Your Checklist — evaluate EACH point:

1. **Intentional design?**
   Check the git blame and commit messages. Was this pattern deliberately chosen?
   If a commit message or ADR explicitly justifies this pattern, KILL the finding.

2. **Maps to a rule?**
   The finding cites rule "{finding.rule}". Verify this rule actually exists in the
   reference document and that the code actually violates it. If the rule doesn't
   match or the code is compliant, KILL the finding.

3. **False positive?**
   Read the FULL surrounding context (not just the flagged line). Does the broader
   context justify this pattern? For example, a "missing None check" might be safe
   if the caller guarantees non-None. If context justifies it, KILL the finding.

4. **Noise?**
   Would a senior engineer care about this? If it's a minor style issue that
   pre-commit should catch, or a theoretical concern with no practical impact, KILL it.

5. **Already tracked?**
   Check if this issue is already tracked. Use:
   - Grep for TODO/FIXME comments near the flagged code
   - Check if the pattern is documented as a known limitation

## Output

Return your verdict as JSON in your response text.

Format:
{
  "finding_id": "{finding.id}",
  "challenger_model": "opus|sonnet",
  "verdict": "survived|killed",
  "reasoning": "Detailed explanation of why this finding survived or was killed",
  "checked_against": ["list", "of", "rules/ADRs", "checked"],
  "checked_git_blame": true,
  "intentional_design": false
}

Be rigorous. Only let through findings that are genuine issues a senior engineer would want fixed.
```

**Parallelization:** Dispatch up to **5 challenger agents at a time**. Wait for batch to complete, then dispatch next batch.

### 4.3 Collect Verdicts

After all challenges complete:

- Collect findings where `verdict == "survived"` → these proceed to Stage 5
- Log killed findings with their reasoning

Print: **"Gate 1 complete: X findings survived, Y killed out of Z total."**

---

## Stage 5: Linear Ticket Creation

Create Linear tickets for all surviving findings.

### 5.1 Find or Create Milestone

```
Use mcp__linear__list_teams to find "Builder Experience" team → get team_id
Use mcp__linear__list_projects to find "App SDK v3.0" project → get project_id
Use mcp__linear__list_milestones to search for "Autonomous SDK Evolution" → get milestone_id
If milestone not found: use mcp__linear__save_milestone to create it in the project
```

### 5.2 Get Issue Statuses

```
Use mcp__linear__list_issue_statuses for the team
```

Cache the status names — use these exact strings for all subsequent state transitions.

### 5.3 Create Parent Ticket

```
Use mcp__linear__save_issue:
  title: "Daily SDK Evolution — {RUN_DATE}"
  team_id: {team_id}
  project_id: {project_id}
  milestone_id: {milestone_id}
  assignee_id: {user_id from Stage 0.4}
  description: |
    ## Autonomous SDK Evolution — {RUN_DATE}

    | Category | Discovered | Survived Gate 1 |
    |----------|-----------|-----------------|
    | Code Quality | X | Y |
    | Test Quality | X | Y |
    | Architecture | X | Y |
    | Documentation | X | Y |
    | v2 Patterns | X | Y |
    | Bugs | X | Y |
    | Security | X | Y |
    | Performance | X | Y |

    Sub-tickets and PRs will be linked as they are created.
    See comments for run progress and decisions.
  labels: ["Autonomous SDK Evolution"]
```

Write parent ticket info to `{WORKSPACE}/tickets/parent.json`:
```json
{"id": "<ticket-id>", "url": "<ticket-url>", "identifier": "<BLDX-123>"}
```

### 5.4 Create Sub-Tickets

For each surviving finding, create a sub-ticket. **Keep descriptions concise** — readable by a human in 30 seconds. Put reasoning and decisions in comments.

```
Use mcp__linear__save_issue:
  title: "[{SEVERITY}] {category}: {title}"
  team_id: {team_id}
  project_id: {project_id}
  parent_id: {parent_ticket_id}
  assignee_id: {user_id from Stage 0.4}
  priority: mapped from config.yaml priority_map (critical→Urgent, high→High, medium→Medium, low→Low)
  description: |
    **Rule:** {rule}
    **File:** `{file}:{line}`
    **Confidence:** {confidence}%

    ### What's Wrong
    {description — 2-3 sentences max}

    ### Suggested Fix
    {suggested_fix — concrete, actionable}

    ---
    *Generated by Autonomous SDK Evolution*
  labels: ["Autonomous SDK Evolution", "{category}"]
```

**Immediately after creating each sub-ticket:**

1. **Set the branch** on the ticket to the ticket identifier (e.g., `BLDX-456`). This is the branch name that will be used in Stage 6.
2. **Add a comment** with the Gate 1 trail:
   ```
   Use mcp__linear__save_comment:
     issue_id: {ticket_id}
     body: |
       **Discovery:** {agent} ({model}) — confidence {confidence}%
       **Gate 1:** Challenged by {challenger_model} — SURVIVED
       **Reasoning:** {challenger reasoning summary}
   ```
3. **Tag the ticket** based on whether it needs design review:
   - Architecture findings with severity critical/high → add label `needs-design-review`
   - All other ready-to-fix findings → proceed to Stage 6

Write ticket info to `{WORKSPACE}/tickets/{finding.id}.json`:
```json
{"id": "<ticket-id>", "url": "<ticket-url>", "identifier": "<BLDX-456>", "finding_id": "<finding-id>", "branch": "BLDX-456"}
```

**If no findings survived Gate 1:** Still create the parent ticket with description: "Clean run — no issues found. The SDK is in good shape."

**Design review findings:** For findings tagged `needs-design-review`, do NOT create a PR. Leave them as tickets for human decision. Add a comment:
```
This finding requires design discussion before implementation.
It may need a holistic solution rather than a point fix.
Leaving for human review.
```

---

## Stage 6: Fix & PR Creation

Create PRs with fixes for all surviving findings tagged as ready-to-fix (NOT `needs-design-review`).

### 6.1 Group Findings by PR Strategy

| Category | PR Strategy |
|----------|------------|
| bug | Individual PR per finding |
| architecture | Individual PR per finding |
| security | Individual PR per finding |
| performance | Individual PR per finding |
| code-quality | Grouped into one PR |
| test-quality | Grouped into one PR |
| documentation | Grouped into one PR |
| v2-patterns | Grouped into one PR |

### 6.2 Enforce PR Limit

Max PRs per run: **10** (from config.yaml `pipeline.max_prs_per_run`).

If more than 10 PR groups exist, prioritize:
1. Critical severity findings first
2. Then by category: security > bug > architecture > performance > v2-patterns > test-quality > code-quality > documentation
3. Drop lowest priority groups until at 10

### 6.3 Fix Each PR Group (Sequential)

For **each PR group**, fix sequentially (Guardrail #5 — no parallel fixes without worktree isolation):

**Fix process for each group:**

```
1. Create branch from main using the Linear ticket identifier:
   git checkout -b {TICKET_IDENTIFIER} origin/main
   (e.g., git checkout -b BLDX-456 origin/main)

2. Verify branch base (Guardrail #3):
   git log --oneline -1
   Confirm parent matches origin/main HEAD.

3. Read the flagged file(s) to understand full context.

4. TDD cycle (if test is required per Guardrail #7):
   a. Write the failing test FIRST
   b. Run: uv run pytest <test_file> -x — confirm it FAILS
   c. Implement the fix
   d. Run: uv run pytest <test_file> -x — confirm it PASSES

5. If no test required: just implement the fix.

6. Run pre-commit:
   uv run pre-commit run --files <changed_files>
   Fix any issues, re-run until clean.

7. Run full unit test suite:
   uv run pytest tests/unit/ -x -q --timeout=60
   ALL tests must pass.

8. Commit with conventional commit format:
   git add <specific_files>
   git commit -m "{type}({scope}): {description} [{TICKET_IDENTIFIER}]"

   Type mapping:
   - bug → fix
   - security → fix
   - performance → perf
   - code-quality → refactor
   - v2-patterns → refactor
   - documentation → docs
   - test-quality → test
   - architecture → refactor

9. Push:
   git push origin {TICKET_IDENTIFIER}

10. Create PR:
    gh pr create \
      --repo atlanhq/application-sdk \
      --base main \
      --title "{type}({scope}): {short description} [{TICKET_IDENTIFIER}]" \
      --label "Autonomous SDK Evolution" \
      --label "needs-review" \
      --body "$(cat <<'EOF'
    ## What was found

    **Rule:** {finding.rule}
    **File:** `{finding.file}:{finding.line}`
    **Severity:** {finding.severity}

    {finding.description}

    ## Why it matters

    {1-2 sentences on impact — what breaks if this isn't fixed}

    ## What was fixed

    {description of the actual code changes and design decisions}

    ## TDD Evidence

    {if test was written:}
    - Test file: `{test_path}`
    - Test fails without fix: YES (verified)
    - Test passes with fix: YES (verified)
    {if no test required:}
    - Category ({category}) does not require new tests per policy
    - Existing tests verified: ALL PASSING

    ## Validation

    - [x] Pre-commit passes
    - [x] All unit tests pass
    - [x] pyright clean
    - [ ] Gate 2 (merge-readiness): PENDING
    - [ ] SDK Review (4-agent): PENDING
    - [ ] Reviewer Confirmation: PENDING

    ## Linear

    {ticket_url}

    ## Review Trail

    - Discovery: {finding.agent} ({finding.model})
    - Gate 1: {challenger_model} — SURVIVED
    - Gate 2: PENDING
    - SDK Review: PENDING
    - Confirmation: PENDING
    EOF
    )"

11. Update Linear ticket:
    - Set status → "In Review"
    - Add comment: "PR #{pr_number} created — awaiting SDK review"
    - Attach PR link to ticket

12. Switch back to main:
    git checkout main
```

Write PR info to `{WORKSPACE}/prs/{finding.id}.json`:
```json
{"pr_number": N, "pr_url": "...", "branch": "BLDX-456", "finding_ids": ["..."], "category": "...", "ticket_identifier": "BLDX-456"}
```

**For grouped PRs (docs, tests, v2-cleanup):** Same process but:
- Use the FIRST finding's ticket identifier as the branch name
- Address each finding in sequence
- PR body lists all findings addressed
- Link all related ticket URLs

---

## Stage 7: Gate 2 — Merge-Readiness Check

Challenge every fix before it reaches SDK review.

### 7.1 Review Each PR

After all fixes complete, for each PR:

1. Fetch the PR diff:
   ```bash
   gh pr diff {pr_number} --repo atlanhq/application-sdk
   ```

2. Read the original finding(s) and Gate 1 verdict

3. Dispatch a **Gate 2 challenger agent** (secondary model, `model: "sonnet"`):

```
You are a Gate 2 Devil's Advocate reviewing a PR from the Autonomous SDK Evolution pipeline.
Your job is to verify this fix is correct and doesn't introduce new problems.

## Original Finding(s)
{finding JSON}

## PR Diff
{full PR diff}

## Relevant Rules
{reference rules content}

## Your Checklist:

1. **Does the fix match the finding?**
   Is it actually solving what was reported? Or did it fix something tangential?

2. **Does it introduce regressions?**
   - New bugs in the changed code?
   - Broken contracts (changed field types, removed fields)?
   - Changed behavior that other code depends on?

3. **Does it respect all ADRs?**
   The fix shouldn't violate a different ADR while solving one issue.
   Check against all 11 ADRs.

4. **Is it minimal scope?**
   No scope creep, no unnecessary refactoring, no "while I'm here" changes.

5. **Are tests included where required?**
   - Bug fixes MUST have a regression test — if missing, verdict is needs-changes
   - Security fixes MUST have a regression test — if missing, verdict is needs-changes
   - Performance fixes MUST have a behavior test — if missing, verdict is needs-changes
   - Code quality/docs/v2 fixes do NOT need new tests

6. **TDD evidence?**
   If a test was required, does the PR description show TDD evidence (test fails without fix, passes with)?

## Output

Return your verdict as JSON in your response text.

Format:
{
  "pr_number": {pr_number},
  "verdict": "passed|needs-changes|failed",
  "reasoning": "Detailed explanation",
  "issues": ["list of specific issues if needs-changes or failed"],
  "challenger_model": "sonnet"
}
```

### 7.2 Handle Verdicts

**`passed`:** PR proceeds to Stage 8 (SDK Review). Update PR label: remove `needs-review`, add `in-sdk-review`.

**`needs-changes`:** Fix on the same branch, push, re-run Gate 2. **Max 1 retry at this stage.**
- Add a Linear comment: "Gate 2 requested changes: {issues list}"

**`failed`:** Close the PR:
```bash
gh pr close {pr_number} --repo atlanhq/application-sdk --comment "Closed by Gate 2: {reasoning}"
```
- Update Linear ticket status → "Canceled"
- Add Linear comment: "PR closed by Gate 2 — {reasoning}"

---

## Stage 8: Handoff to @sdk-review auto-complete

After Gate 2 passes, hand off each PR to the `@sdk-review auto-complete` GHA workflow. This workflow handles the full 4-agent cross-model SDK review, fix loop (max iterations), and final confirmation/approval autonomously.

### 8.1 Wait for CI on Each PR — Fix Failures Yourself (Guardrail #24)

Before posting the review comment, verify CI is green:

```bash
gh pr checks {pr_number} --repo atlanhq/application-sdk --watch
```

If CI fails — **fix it yourself, do NOT stop or ask the user:**
1. Read the failure: `gh run view {run_id} --repo atlanhq/application-sdk --log-failed`
2. If failure is in changed code → diagnose the root cause, fix it on the same branch, run pre-commit + unit tests locally to verify, push, wait for CI again. Keep iterating until green.
3. If flaky/infra → re-run: `gh run rerun {run_id} --repo atlanhq/application-sdk --failed`
4. **NEVER cancel a PR because of CI failures.** CI failures are fixable — keep fixing until it's green.
5. **Do NOT post `@sdk-review` while CI is red.**

### 8.2 Post @sdk-review auto-complete Comment

For each PR that passed Gate 2 and has green CI:

```bash
gh pr comment {pr_number} --repo atlanhq/application-sdk --body "@sdk-review auto-complete"
```

This triggers the `@sdk-review` GHA workflow which will:
- Run 4-agent cross-model review (Architecture, Code Quality, Security, Test Quality)
- Post inline PR comments for each finding
- Automatically fix issues and iterate (up to max iterations)
- Confirm and approve when all gates pass
- Label `ready-to-merge` or `needs-human-review` as appropriate

### 8.3 Update Linear Tickets to "In Review"

Immediately after posting `@sdk-review auto-complete` on each PR:

```
Use mcp__linear__save_issue to update the ticket:
  status: "In Review"

Use mcp__linear__save_comment:
  issue_id: {ticket_id}
  body: |
    PR #{pr_number} passed Gate 2. Posted `@sdk-review auto-complete` — GHA workflow will handle SDK review, fix loop, and confirmation autonomously.
```

### 8.4 Track Handoff

Write handoff info to `{WORKSPACE}/sdk-review/{pr_number}.json`:
```json
{
  "pr_number": N,
  "gate2_verdict": "passed",
  "sdk_review_triggered": true,
  "sdk_review_comment": "@sdk-review auto-complete",
  "linear_status": "In Review",
  "ci_status": "green",
  "timestamp": "<ISO-8601>"
}
```

**For PRs that failed Gate 2:**
- Already handled in Stage 7.2 (closed or retried)
- Do NOT post `@sdk-review` on failed PRs

---

## Stage 9: Summary & Self-Improvement

### 9.1 Gather Metrics

```json
{
  "run_date": "{RUN_DATE}",
  "total_findings_discovered": N,
  "findings_killed_gate1": N,
  "findings_survived": N,
  "findings_needs_design_review": N,
  "prs_created": N,
  "prs_passed_gate2": N,
  "prs_handed_to_sdk_review": N,
  "prs_closed_by_gate2": N,
  "by_category": {
    "code-quality": {"discovered": N, "survived": N, "pr_status": "..."},
    ...
  }
}
```

### 9.2 Update Linear Parent Ticket

Update the parent ticket description with the final summary:

```
Use mcp__linear__save_issue to update the parent ticket:

## Run Summary — {RUN_DATE}

| Metric | Count |
|--------|-------|
| Findings discovered | {total} |
| Killed at Gate 1 | {killed} |
| Survived to tickets | {survived} |
| Needs design review | {design_review} |
| PRs created | {prs_created} |
| PRs ready to merge | {ready} |
| PRs needs human review | {human_review} |
| PRs closed | {closed} |

### PRs Ready to Merge
{for each PR with APPROVED status:}
- PR #{number}: [{title}] — {ticket_url}

### PRs Needing Human Review
{for each PR with needs-human-review:}
- PR #{number}: [{title}] — {remaining issues} — {ticket_url}

### Design Review Needed
{for each needs-design-review ticket:}
- {ticket_identifier}: [{title}] — {why design review needed}

### PRs Closed
{for each closed PR:}
- PR #{number}: [{title}] — closed by {gate} — {reason}
```

Add a comment to the parent ticket with pipeline health metrics.

### 9.3 Handle Retrospective Findings

For any retrospective findings that survived Gate 1, create separate Linear sub-tickets under the parent with:
- Label: `pipeline-improvement`
- Priority: Low
- Description includes the suggested improvement
- Assigned to: {user_id}

### 9.4 Self-Improvement

**This skill does NOT use memory. It updates itself.**

Check if any patterns emerged during this run:

1. **Recurring false positives:** If 3+ findings from the same rule were killed at Gate 1 → update the reference rules file to tighten the rule or raise the confidence threshold.

2. **Recurring review feedback:** If the same type of SDK review comment appeared on 2+ PRs → add it as a guardrail in this SKILL.md.

3. **Gate effectiveness:** If Gate 2 passed everything but SDK Review caught issues → Gate 2 checklist needs strengthening — update this SKILL.md.

4. **New guardrails:** If a fix caused an issue (test failure, semantic regression) → add a guardrail to the CRITICAL section above.

If updates are needed:
```bash
git checkout main
# Edit SKILL.md and/or reference rules
git add .claude/skills/sdk-evolution/
git commit -m "chore(sdk-evolution): self-improvement from {RUN_DATE} run"
git push origin main
```

### 9.5 Security Audit of Pipeline Output (Guardrail #18)

Before completing, scan all PR descriptions, comments, and Linear ticket content for:
- Secrets, tokens, API keys
- Internal IPs or URLs
- Credentials in test fixtures
- Any sensitive data that shouldn't be in public PR content

If found, edit the PR/comment to redact immediately.

### 9.6 Write Summary File

Write full metrics to `{WORKSPACE}/summary.json`.

### 9.7 Cleanup

```bash
git worktree prune
```

Keep `tmp/sdk-evolution/{RUN_DATE}/` intact — it serves as the audit trail.

### 9.8 Terminal Output

Print:

```
========================================
 Autonomous SDK Evolution — {RUN_DATE}
========================================

Findings:  {total} discovered → {survived} survived Gate 1
PRs:       {prs_created} created → {gate2_passed} passed Gate 2
Handoff:   {handed_to_sdk_review} PRs sent to @sdk-review auto-complete
Design:    {design_review} findings need design review
Closed:    {closed} PRs closed by Gate 2

Handed to @sdk-review (review/fix loop/approval handled by GHA):
  - PR #{N}: {title} [{TICKET}] — @sdk-review auto-complete posted
  ...

Design Review Needed (human decision required):
  - {TICKET}: {title} — {why}
  ...

Linear: {parent_ticket_url}
========================================
```

Print: **"Autonomous SDK Evolution run complete. All PRs handed to @sdk-review auto-complete — GHA workflow handles review, fixes, and approval. Linear tickets set to In Review."**
