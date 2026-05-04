# SDK Review — Orchestration Playbook

Follow these phases EXACTLY. Do not skip phases. Do not reorder.
Print `[Phase N complete]` after each phase.

## Time Budgets

Budgets scale with PR size. Determine the tier in Phase 0, then use
the corresponding column. If approaching the limit, finalize with what
you have — a partial review is better than no review.

| Phase | Small (<2K lines) | Large (2K-20K) | Massive (20K+) | What to cut if over |
|-------|-------------------|----------------|----------------|---------------------|
| Phase 0: Orient | 30s | 30s | 60s | Nothing — fast |
| Phase 1: Context | 60s | 2 min | 5 min | Skip reachability, grep-only |
| Phase 2: Review | 5 min | 10 min | 15 min | Drop to 2 agents, skip adversarial |
| Phase 3: Submit | 30s | 30s | 60s | Nothing — just a curl call |
| Phase 4: CI Fix | 3 min | 5 min | 5 min | Skip if complex |

| PR Size | Total Budget | Hard Stop |
|---------|-------------|-----------|
| Small (<2K lines) | ~10 min | 15 min |
| Large (2K-20K) | ~18 min | 25 min |
| Massive (20K+) | ~26 min | 35 min |

If the sandbox has been running past the hard stop, finalize immediately
with whatever findings you have. Submit the review — never exit without
calling submit_review.

---

## Phase 0: Orient (~30s)

1. Read `session/PR.md` for PR URL, number, commit SHA, base ref,
   source branch, github_token, review_mode.

2. Read `session/DIFF.patch` — authoritative diff from GitHub API.
   Do NOT generate your own diff. DIFF.patch is the source of truth.

3. **Auth setup** (use `$GITHUB_TOKEN` env var — injected by dispatcher):
   ```bash
   # Authenticate gh CLI (needed for gh pr edit, gh api graphql, gh pr checks)
   echo "$GITHUB_TOKEN" | gh auth login --with-token 2>/dev/null
   ```

4. **Stale SHA guard**: Check if the PR has been updated since dispatch:
   ```bash
   DISPATCH_SHA=$(grep -oP 'commit_sha:\s*\K[a-f0-9]+' session/PR.md)
   PR_NUM=$(grep -oP 'pr_number:\s*\K\d+' session/PR.md)
   CURRENT_SHA=$(curl -sf -H "Authorization: Bearer $GITHUB_TOKEN" \
     "https://api.github.com/repos/atlanhq/application-sdk/pulls/$PR_NUM" \
     | jq -r '.head.sha')
   if [ "$DISPATCH_SHA" != "$CURRENT_SHA" ]; then
     echo "PR updated since dispatch. Aborting — will be re-triggered."
     # Submit minimal review so status doesn't stay pending
     # ... submit with summary "PR updated during review, re-trigger @sdk-review"
     exit 0
   fi
   ```

5. Clone and checkout:
   ```bash
   cp -r /opt/repo-cache/application-sdk /workspace/repo 2>/dev/null || \
     git clone --depth=50 https://github.com/atlanhq/application-sdk.git /workspace/repo
   cd /workspace/repo
   git fetch origin pull/<PR_NUMBER>/head:pr-branch
   git checkout pr-branch

   # Background: install deps so uv run pre-commit/pytest don't wait
   uv sync --all-extras 2>/dev/null &
   ```

5. **Branch freshness + conflict resolution** (before reviewing):
   ```bash
   # Check merge status
   MERGE_STATUS=$(gh pr view <PR> --repo atlanhq/application-sdk \
     --json mergeStateStatus,mergeable --jq '.mergeStateStatus')
   ```
   
   If `BEHIND`:
   ```bash
   # Tier 1: API update (merges main into PR branch)
   gh api repos/atlanhq/application-sdk/pulls/<PR>/update-branch \
     -X PUT -f update_method=merge 2>/dev/null
   sleep 10
   # Re-fetch — SHA changed after merge
   git pull origin pr-branch
   ```
   
   If `CONFLICTING`:
   ```bash
   # Tier 2: git merge (handles non-overlapping conflicts)
   git fetch origin main
   git merge origin/main --no-edit 2>/dev/null
   ```
   If merge succeeds → `git push origin HEAD`. Continue with review of merged result.
   If merge fails:
   ```bash
   git merge --abort
   ```
   Submit minimal review: "PR has merge conflicts. Please rebase or comment
   `@sdk-review` after resolving conflicts." Label `needs-rebase`. EXIT.

6. **Pre-commit cleanup** (eliminate style noise before review):
   ```bash
   CHANGED=$(git diff --name-only origin/main...HEAD -- '*.py')
   if [ -n "$CHANGED" ]; then
     uv run pre-commit run --files $CHANGED 2>/dev/null || true
     if [ -n "$(git status --porcelain -- '*.py')" ]; then
       git add $(git diff --name-only)
       git commit -m "style: auto-format via pre-commit"
       git push origin HEAD
     fi
   fi
   ```
   This ensures the review finds REAL issues, not formatting noise.

7. **CI pre-check** (don't waste review time if CI is broken):
   ```bash
   FAILING=$(gh pr checks <PR> --repo atlanhq/application-sdk \
     --json name,conclusion --jq '.[] | select(.conclusion=="failure") | .name' 2>/dev/null)
   ```
   
   If CI is failing:
   - Read failure logs: `gh run view <run_id> --log-failed 2>/dev/null | head -100`
   - If failure is in PR code (lint, type error, missing import, test failure):
     → Dispatch Sonnet CI fix sub-agent (see below)
   - If failure is pre-existing or infrastructure: note in review, proceed
   
   **Sonnet CI Fix sub-agent** (lightweight, fast):
   ```
   Read the CI failure logs. Fix categories you CAN handle:
   - Pre-commit failures (ruff, isort) → run pre-commit, commit
   - Import errors → add missing imports
   - Type errors (pyright) → fix type annotations
   - Unit test failures from PR changes → fix the code
   
   Categories you CANNOT handle (skip):
   - Test failures in untouched code (pre-existing)
   - Infrastructure failures (timeout, OOM)
   - Complex logic errors (leave for review)
   
   Run: uv run pre-commit run --files <files> && uv run pytest tests/unit/ -x --timeout=60
   If fix works: git commit -m "fix(ci): <description>" && git push
   If not: skip, note for review
   ```

8. Read the repo's `CLAUDE.md` for conventions.

9. Determine mode from session files:
   - `session/CI_FIX_ONLY` exists → **ci-fix mode** (skip to CI-Fix Mode section)
   - `session/AUTO_FIX` exists → auto_fix = true
   - `session/CHALLENGE.md` exists → challenge mode
   - `session/RETROSPECT` exists → retrospect mode (skip to Retrospect section)
   - `session/AUTHOR_CONTEXT.md` exists → read, consider during review
   - Always read `modes/standard.md`
   
   **Iterations (--iteration=N in command_context):** Every iteration runs
   a FULL review (all agents, adversarial, guardrails) — not a delta.
   The fix may have introduced cross-file regressions that only a full
   review catches. Delta tracking (RESOLVED/STILL/NEW) is a presentation
   layer — the agents still review the entire diff.

10. **Smart agent routing** — classify the PR:
   ```bash
   TOTAL_FILES=$(grep -c '^+++ b/' session/DIFF.patch || echo 0)
   TEST_FILES=$(grep -c '^+++ b/tests/' session/DIFF.patch || echo 0)
   DOC_FILES=$(grep -c '^+++ b/docs/' session/DIFF.patch || echo 0)
   CONFIG_FILES=$(grep -cE '^+++ b/(pyproject\.toml|uv\.lock|\.pre-commit|\.github/|helm/)' session/DIFF.patch || echo 0)
   SOURCE_FILES=$((TOTAL_FILES - TEST_FILES - DOC_FILES - CONFIG_FILES))
   ```
   - If `SOURCE_FILES == 0 && TEST_FILES > 0` → `review_scope=tests-only`
     (QUALITY agent only — focused on test patterns, coverage, assertions)
   - If `SOURCE_FILES == 0 && DOC_FILES > 0 && TEST_FILES == 0` → `review_scope=docs-only`
     (Skip Phase 2 — submit APPROVE with "Docs-only PR, no code review needed")
   - If `SOURCE_FILES == 0 && CONFIG_FILES > 0` → `review_scope=config-only`
     (CORRECTNESS agent only — dependency/security/CI permission check)
   - If `SOURCE_FILES <= 2 && TEST_FILES >= SOURCE_FILES * 3` → `review_scope=tests-focused`
     (QUALITY agent + lightweight CORRECTNESS — mostly tests with a few source changes)
   - Otherwise → `review_scope=full` (all 3 agents)

11. Check diff size for tier:
   - < 2000 lines → `review_tier = "full"`
   - 2000-20000 → `review_tier = "partitioned"`
   - > 20000 → `review_tier = "staged"`

Print: `[Phase 0 complete] PR #<N>, scope=<scope>, tier=<tier>, auto_fix=<bool>`

---

## Phase 1: Context Gather (~60s)

### 1a. Holistic File Assessment (NO LLM)

For each changed source file in DIFF.patch:
```bash
wc -l <file>                                           # line count
rg -l "from.*<module>.*import" application_sdk/ -t py | wc -l  # callers
rg "def <function_name>" application_sdk/execution/ application_sdk/infrastructure/ -t py -l  # v3 replacement?
rg "warnings\.warn\|DeprecationWarning" <file>          # deprecated?
```

Build annotations: lines, callers, DUMPING_GROUND/V3_REPLACEMENT/DEPRECATED flags.

### 1b. Dispatch Reachability Agent (if review_scope=full)

Use Agent tool to dispatch `agents/reachability.md` — classifies each
changed symbol as temporal-workflow, temporal-activity, public-http,
internal, test, or dead.

Skip if review_scope is tests-only, docs-only, or config-only.

### 1c. Prepare Context by Tier

**Token budgets per agent call (hard limits — never exceed):**

| Content | Max tokens (approx) |
|---------|-------------------|
| PR diff sent to agent | 60K tokens (~240K chars) |
| Full file contents sent to agent | 30K tokens (~120K chars) |
| Reference rules + preamble | 10K tokens (~40K chars) |
| **Total per agent** | **100K tokens** |

1 token ≈ 4 chars. Measure with `wc -c` and divide by 4.

#### Tier: Full (< 2K lines changed)

Read ALL changed source + test files completely. Send full diff.
This fits within budget for most PRs.

**Safety check:** Before sending to agents, measure total context:
```bash
DIFF_CHARS=$(wc -c < session/DIFF.patch)
FILE_CHARS=0
for f in <changed_files>; do
  FILE_CHARS=$((FILE_CHARS + $(wc -c < "$f")))
done
TOTAL=$((DIFF_CHARS + FILE_CHARS))
echo "Total context: $TOTAL chars (~$((TOTAL/4)) tokens)"
```

If total > 400K chars (~100K tokens): **auto-upgrade to Partitioned tier.**

#### Tier: Partitioned (2K-20K lines changed)

Split files by directory. Each agent gets only its partition:

| Agent | Gets full content of | Gets file list only for |
|-------|---------------------|------------------------|
| CORRECTNESS | `app/`, `execution/`, `credentials/`, `contracts/`, `infrastructure/`, `handler/` | everything else |
| QUALITY | `tests/`, `common/`, remaining source files | high-risk dirs |
| STRUCTURE | top 10 most-changed files (by line count) | everything else |

**Diff splitting:** Each agent gets only the hunks for files in its partition:
```bash
# Extract hunks for specific files from the full diff
grep -A 9999 "^diff --git a/<file>" session/DIFF.patch | \
  sed '/^diff --git a\//q' | head -n -1
```

**Per-agent safety check:** If a partition still exceeds 100K tokens:
- Truncate the LARGEST files: send first 500 lines + last 100 lines + function index
- Format truncated files as:
  ```
  === FILE: path/to/large_file.py (2100 lines, TRUNCATED) ===
  [Lines 1-500]
  <content>
  
  [Lines 501-2000 OMITTED — function index:]
  - def function_a: line 520
  - class MyClass: line 680
  - def function_b: line 1200
  
  [Lines 2001-2100]
  <content>
  ```

#### Tier: Staged (20K+ lines changed)

This is for migration PRs, bulk refactors, generated code.

**Step 1: Classify files (deterministic, no LLM):**
```bash
for f in <changed_files>; do
  # HIGH_RISK: critical dirs, public API, security-sensitive
  if echo "$f" | grep -qE "^(application_sdk/(app|execution|credentials|contracts|infrastructure|handler)/|.*__init__\.py$)"; then
    echo "HIGH $f"
  # MEDIUM: other source + tests
  elif echo "$f" | grep -qE "\.(py)$"; then
    echo "MED $f"
  # LOW: docs, config, generated, lock files
  else
    echo "LOW $f"
  fi
done
```

**Step 2: Budget allocation:**
- HIGH_RISK files: full content + their diff hunks (up to 60K tokens)
- MEDIUM files: diff hunks only, no full file content (up to 30K tokens)
- LOW files: skipped entirely

**Step 3: If HIGH_RISK alone exceeds 60K tokens:**
- Sort HIGH_RISK by line count descending
- Include files until budget is reached
- Remaining HIGH_RISK files: downgrade to MEDIUM treatment (hunks only)

**Step 4: Note in review comment:**
```
> **Large PR (N files, M lines).** Full review applied to X high-risk files.
> Hunk review applied to Y medium-risk files. Z low-risk files skipped.
> Re-run `@sdk-review` on specific files if needed.
```

#### Single-file overflow (any tier)

If ANY single file exceeds 2000 lines:
```
=== FILE: path/to/huge_file.py (3500 lines, SUMMARIZED) ===

[Imports: lines 1-45]
<content>

[Class/function index:]
- class App(Base): line 50 (400 lines)
  - def __init__: line 52
  - def run: line 102
  - def _register: line 300
- def helper_a: line 460
- def helper_b: line 520
...

[Changed sections with 50-line context above/below:]
--- Section at lines 142-195 (CHANGED) ---
<full content of lines 92-245>

--- Section at lines 1200-1230 (CHANGED) ---
<full content of lines 1150-1280>

[Unchanged sections omitted]
```

This ensures agents see the structure + the actual changes, without
blowing up the context with 3500 lines of unchanged code.

#### Never fail on large diffs

If despite all truncation the context STILL exceeds limits:
1. Drop STRUCTURE agent (least critical)
2. Drop GPT adversarial
3. Send only the diff (no full file contents) to remaining agents
4. Note in review: "Context truncated due to PR size. Some issues may be missed."

**A truncated review is always better than a failed review.**

Print: `[Phase 1 complete] <N> files assessed, tier=<tier>, <M> files truncated`

---

## Phase 2: Review (budget from tier table)

### 2a. Wave 1 — Opus Domain Agents (parallel, native)

Based on `review_scope`, dispatch agents via the Agent tool:

| review_scope | Agents dispatched |
|---|---|
| `full` | correctness.md + quality.md + structure.md (all 3) |
| `tests-only` | quality.md only |
| `tests-focused` | quality.md + correctness.md (lightweight) |
| `config-only` | correctness.md only |
| `docs-only` | SKIP Phase 2 entirely |

Each agent receives: PR diff (or partition), full file contents,
holistic annotations, their reference rules, reachability output.

**Degradation priority** (if running over time budget):
1. Drop STRUCTURE agent first (holistic opinions, least urgent)
2. Drop QUALITY agent second (code patterns, pre-commit catches most)
3. CORRECTNESS is ALWAYS kept (catches guardrail violations G1-G5)

Parse JSON findings from each agent response.

### 2b. Wave 2 — GPT-5.3-codex Adversarial (via proxy)

After Wave 1, call GPT to challenge your findings.

**Skip conditions** (no adversarial):
- `review_scope` is tests-only, config-only, or docs-only
- `review_tier` is "staged" (massive PR — too much context for one GPT call)
- Wave 1 produced zero findings (nothing to challenge)
- Time budget already over 70% consumed

If not skipped:

```bash
curl -s "$PROXY_BASE/proxy/litellm/chat/completions" \
  -H "Authorization: Bearer $PROXY_JWT" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-5.3-codex",
    "temperature": 0.2,
    "max_tokens": 16000,
    "messages": [
      {"role": "system", "content": "<agents/adversarial.md content>"},
      {"role": "user", "content": "<Wave 1 findings + PR diff + annotations>"}
    ]
  }'
```

GPT challenges every Opus finding. GPT also discovers findings Opus missed.

If GPT unavailable or skipped: keep all Opus findings >= 80%.
Note in review: "Cross-model adversarial: <skipped (reason) | ran | unavailable>."

### 2c. De-Bias (deterministic)

| Opus (Wave 1) | GPT (Wave 2) | Action |
|---|---|---|
| >= 90% confidence | AGREE or not reviewed | Keep |
| >= 80% confidence | AGREE | Keep |
| >= 80% confidence | DISAGREE | **Drop** |
| >= 80% confidence | PARTIAL | Keep, downgrade severity |
| Not flagged | GPT >= 90% | Keep (blind spot) |
| Not flagged | GPT < 90% | Drop |
| **Guardrail violation** | **Any** | **Always keep** |

If GPT was unavailable or skipped: keep all Opus findings >= 80%.

### 2d. Delta Tracking (if previous review exists)

Check for a previous review comment on this PR:
```bash
gh api repos/atlanhq/application-sdk/issues/<PR>/comments \
  --jq '.[] | select(.body | contains("<!-- SDK_REVIEW")) | .body' | head -1
```

If found, parse the previous findings and compare:
- Finding was in previous review, code at that line CHANGED → **RESOLVED**
- Finding was in previous review, code UNCHANGED → **STILL PRESENT**
- Finding is new (not in previous review) → **NEW**

Include delta status in the review comment so the author sees what
was fixed vs what remains.

### 2e. Guardrails G1-G8

Check consolidated findings. Any G1/G2/G3/G5 → BLOCKED.

### 2f. Holistic Path Forward (Critical + High only)

For BLOCKING/CRITICAL/HIGH findings, include a `path_forward` in the
inline comment body:
- **Immediate fix** — "Fix this now, it will break in production. Do X."
- **Temporary fix + follow-up** — "Quick fix: X. Right solution: Y (follow-up ticket)."
- **Wrong approach** — "This approach won't work because X. Instead, do Y."
- **Design decision needed** — "Two valid options: A or B. Needs team discussion."

MEDIUM/LOW/INFO findings: one-line suggested_fix only. No path_forward.

### 2g. Determine Verdict

| Verdict | Condition | approval_recommendation |
|---|---|---|
| BLOCKED | G1/G2/G3/G5 violation | REJECT |
| NEEDS_HUMAN | DESIGN_CHANGE scope | REQUEST_CHANGES |
| NEEDS_FIXES | Critical, G4/G6, 3+ Important, CI failing | REQUEST_CHANGES |
| READY_TO_MERGE | No Critical, < 3 Important, CI passing | APPROVE |

### 2h. Challenge Mode (if session/CHALLENGE.md exists)

**Targeted challenge** — do NOT re-run the full review. Instead:

1. Read the previous review comment from this PR (delta tracking in 2d)
2. Read the author's challenge from `session/CHALLENGE.md`
3. Identify which specific findings the challenge addresses
   (match by file, line, or description keywords)
4. For ONLY the disputed findings:
   - Re-read the flagged code with fresh eyes + the author's context
   - Valid challenge → RESOLVED with note: "Accepted: <author's reason>"
   - Partially valid → downgrade severity
   - Not valid → STILL PRESENT with explanation
5. All non-disputed findings: carry forward from previous review unchanged
6. Submit updated review with delta tracking

Print: `[Phase 2 complete] <N> findings, verdict=<verdict>`

---

## Phase 3: Submit Review (~30s)

### 3a. Build Payload

For each finding, build the object matching submit_review schema.

**Strip fields not in the schema** — the handler will 422 on unknown fields.
Only include: title, pattern_id, severity, category, confidence, file, line,
evidence, attack_path, reachable_from, by_design_check, suggested_fix,
escalate_to_linear. Do NOT include scope, domain_tag, guardrail, path_forward
in the findings array — put those in the summary or inline comment body instead.

### 3b. Inline Comments

For BLOCKING/CRITICAL/HIGH findings, create inline comments:
- `file` and `line` must be in DIFF.patch (added lines only)
- Max 15 inline comments
- Format:
  ```
  **[SEVERITY]** [TAG] — description

  **Evidence:** <quoted code>
  **Path Forward:** <immediate fix / temporary fix + follow-up / design decision>
  **Fix:** <exact code suggestion if PATCH scope>
  ```

### 3c. Label Management (after submit_review succeeds)

The submit_review handler swaps generic labels (`mothership-review → mothership-reviewed`).
You manage SDK-specific labels via gh CLI:

```bash
# $GITHUB_TOKEN is set in Phase 0 step 3 (injected by dispatcher)
PR=<pr_number>
REPO="atlanhq/application-sdk"

# Based on verdict:
case "$VERDICT" in
  "READY_TO_MERGE")
    gh pr edit $PR --repo $REPO --add-label "sdk-review-approved"
    gh pr edit $PR --repo $REPO --remove-label "needs-human-review" 2>/dev/null || true
    ;;
  "NEEDS_HUMAN")
    gh pr edit $PR --repo $REPO --add-label "needs-human-review"
    gh pr edit $PR --repo $REPO --remove-label "sdk-review-approved" 2>/dev/null || true
    ;;
  "NEEDS_FIXES"|"BLOCKED")
    gh pr edit $PR --repo $REPO --remove-label "sdk-review-approved" 2>/dev/null || true
    gh pr edit $PR --repo $REPO --remove-label "needs-human-review" 2>/dev/null || true
    ;;
esac
```

### 3d. Resolve Inline Threads (on APPROVE)

If verdict = READY_TO_MERGE, resolve ALL open inline review threads from
previous SDK Review comments. The handler does NOT do this — you must:

```bash
# Get all review threads
gh api graphql -f query='
  query($owner: String!, $repo: String!, $pr: Int!) {
    repository(owner: $owner, name: $repo) {
      pullRequest(number: $pr) {
        reviewThreads(first: 100) {
          nodes {
            id
            isResolved
            comments(first: 1) {
              nodes { body author { login } }
            }
          }
        }
      }
    }
  }' -F owner=atlanhq -F repo=application-sdk -F pr=$PR

# For each unresolved thread posted by the bot, resolve it
gh api graphql -f query='
  mutation($id: ID!) {
    resolveReviewThread(input: {threadId: $id}) {
      thread { isResolved }
    }
  }' -F id="<thread_id>"
```

Only resolve threads from bot-posted comments (check `author.login`).
Do NOT resolve threads from human reviewers.

### 3e. Summary

Use our SDK review template (not mothership's generic template):

```
## SDK Review: PR #<number> — <title>

### Verdict: <READY TO MERGE | NEEDS FIXES | BLOCKED | NEEDS HUMAN REVIEW>

> <2-3 sentence summary. Include the holistic assessment:
>  is this fixing symptoms or causes? What's the right path forward?>

---

### Findings

**`<path/to/file.py>`**
- **Critical** [SEC] L42 — description. *Path: immediate fix — <what to do>*
- **Important** [ARCH] L88 — description. *Path: follow-up ticket — <why>*

### Holistic Recommendations (if any)
- Root cause assessment: is this PR treating symptoms or causes?
- Suggested approach if the current approach is wrong

### Strengths
- <what the PR does well>

---
**CI:** all passing | N failing
**Models:** Claude Opus 4.6 (review) + GPT-5.3-codex (adversarial)
**Cross-model agreement:** X/Y confirmed by both
```

### 3f. Submit

```bash
curl -X POST "$PROXY_BASE/sandbox/submit-review" \
  -H "Authorization: Bearer $PROXY_JWT" \
  -H "Content-Type: application/json" \
  --data-binary @/tmp/review-payload.json
```

Retry up to 3x on 422, once on 5xx.

### 3g. CI Check — Fix Failures Before Final Verdict

After posting the review, if CI status from session/PR.md shows failures:

1. Read the failing check logs (if accessible via gh CLI in sandbox)
2. If the failure is in code the PR touched AND the fix is obvious (lint, type error, import):
   - Apply the fix
   - Run pre-commit + pytest locally
   - Push the fix commit
   - **Do NOT re-run the full review cycle** — just re-submit with updated status
3. If the failure is complex or in untouched code:
   - Note in review: "CI failing: <check name>. This appears to be <in PR code / pre-existing>."
   - Do NOT attempt to fix

Print: `[Phase 3 complete] Review submitted`

---

## Phase 4: Auto-Fix Session Management

**IMPORTANT: The auto-fix loop should NOT run in this same session.**

When auto_fix = true AND verdict = NEEDS_FIXES:

This review session's job is DONE after Phase 3. The structured findings
in the review comment contain all the information a separate fix session needs.

The `submit_review` handler, upon seeing `auto_fix: true` in the session
metadata, will dispatch a NEW sandbox with the `sdk-reviewer` snapshot
in fix-only mode. That session:

1. Reads the review comment (structured findings only — no review reasoning)
2. Applies PATCH-scope fixes
3. Runs pre-commit + pytest
4. Pushes
5. Triggers a fresh review (another new sandbox)

Each session is independent. The fixer never sees the reviewer's reasoning.
The re-reviewer never sees the fixer's context. This eliminates confirmation bias.

If the handler doesn't support auto-dispatch yet, fall back:
Post a comment on the PR: `@sdk-review auto-complete --iteration=2 --max=3`
This triggers a new GHA run → new dispatch → new sandbox.

Print: `[Phase 4 complete] Handed off to fix session` OR `[Phase 4 skipped] review-only mode`

---

## Retrospect Mode (if session/RETROSPECT exists)

When triggered by `@sdk-review retrospect`:

1. Read the last 10 merged PRs:
   ```bash
   gh pr list --repo atlanhq/application-sdk --state merged --limit 10 --json number,title,mergedAt
   ```

2. For each, check if it had SDK Review comments:
   ```bash
   gh api repos/atlanhq/application-sdk/issues/<N>/comments \
     --jq '.[] | select(.body | contains("SDK Review"))'
   ```

3. Analyze patterns:
   - Which findings were most common?
   - Which were resolved vs disputed vs overridden?
   - Any findings that keep recurring (should become POLICY rules)?
   - Any false positives that keep getting challenged (should be suppressed)?

4. Store learnings:
   - Write to `/workspace/.shared-memory/sdk-reviewer/retro-log.yaml` (R2 persistent)
   - If a pattern should be permanent, output it as a recommended POLICY.md update

5. Post a summary comment on the PR:
   ```
   ## SDK Review Retrospective
   
   Analyzed last 10 merged PRs.
   
   ### Recurring Findings
   - <pattern>: flagged N times, resolved M times, disputed K times
   
   ### Recommended POLICY.md Updates
   - Add: "<pattern> is intentional because <reason>"
   
   ### Recommended Rule Changes
   - Tighten: <rule> (too many false positives)
   - Relax: <rule> (too aggressive)
   ```

This is a lightweight Sonnet job — no Opus needed. Use the default model.

Print: `[Retrospect complete]`

---

## CI-Fix Mode (if session/CI_FIX_ONLY exists)

Triggered by merge queue when CI fails after a branch update on an
approved PR. This is a LIGHTWEIGHT mode — no full review, just fix CI
and re-approve if the fix is clean.

**Cost: ~$0.50 (Sonnet only, no Opus agents, no GPT adversarial)**

### Process:

1. Run Phase 0 steps 1-7 as normal (clone, update branch, pre-commit, CI fix).
   The Sonnet CI fix in step 7 will handle most cases.

2. If CI fix succeeded (Sonnet pushed a fix commit):
   - Run `uv run pytest tests/unit/ -x --timeout=60` to verify
   - If tests pass:
     ```bash
     # Minimal delta review: only check the CI fix commit, not the whole PR
     FIX_DIFF=$(git diff HEAD~1..HEAD)
     ```
     - If the fix is purely formatting/imports/types (no logic changes):
       → Submit with `approval_recommendation: "APPROVE"` and summary
       "CI fix applied (auto-format/import/type). No logic changes. Re-approved."
     - If the fix changed logic (test code, source code beyond imports):
       → Run ONLY the CORRECTNESS agent on the fix diff (not the whole PR)
       → If no new findings → APPROVE
       → If new findings → REQUEST_CHANGES with only the new findings

3. If CI fix failed (Sonnet couldn't fix it):
   - Submit with `approval_recommendation: "REQUEST_CHANGES"` and summary:
     "CI failing after branch update. Sonnet could not auto-fix. Manual intervention needed."
   - Include the error logs in the review body

4. Manage labels as per Phase 3c (re-add `sdk-review-approved` if approved,
   or leave it removed if not).

Print: `[CI-Fix complete] <fixed and re-approved | needs manual fix>`

---

## If You Cannot Finish

Always call submit_review before exiting. A PR with no review comment
and no status update is the worst outcome.

Submit minimal:
```json
{
  "approval_recommendation": "REQUEST_CHANGES",
  "summary": "SDK Review could not complete: <reason>. Re-trigger with @sdk-review.",
  "findings": []
}
```
