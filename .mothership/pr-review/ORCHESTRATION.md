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
with whatever findings you have. Post the review summary + commit
status — never exit without posting to the PR.

---

## Phase 0: Orient (~30s)

The dispatch prompt passes the PR context directly. Read these values
from the prompt header (do NOT re-derive):

```
PR_NUMBER, PR_URL, REPO, HEAD_SHA, BASE_REF, HEAD_REF,
COMMENTER, COMMENT_ID, COMMENTER_INTENT
```

1. **Set working directory** — mothership cloned the repo on the PR head
   ref into `/workspace/application-sdk`:
   ```bash
   cd /workspace/application-sdk

   # Background: install deps so uv run pre-commit/pytest don't wait
   uv sync --all-extras 2>/dev/null &
   ```

2. **Auth setup** — `$GITHUB_TOKEN` is injected by mothership from its
   GitHub App installation (see snapshots/_base). Make `gh` use it:
   ```bash
   echo "$GITHUB_TOKEN" | gh auth login --with-token 2>/dev/null
   ```

3. **Fetch authoritative PR metadata** (no session/PR.md anymore):
   ```bash
   gh pr view "$PR_NUMBER" --repo "$REPO" \
     --json number,state,isDraft,mergeable,mergeStateStatus,headRefName,baseRefName,headRefOid,title,body,labels \
     > /tmp/PR.json
   ```

4. **Fetch authoritative diff** (no session/DIFF.patch anymore):
   ```bash
   gh pr diff "$PR_NUMBER" --repo "$REPO" > /tmp/DIFF.patch
   ```

5. **Stale SHA guard** — bail if the PR has moved since dispatch:
   ```bash
   CURRENT_SHA=$(jq -r '.headRefOid' /tmp/PR.json)
   if [ "$CURRENT_SHA" != "$HEAD_SHA" ]; then
     echo "PR moved from $HEAD_SHA to $CURRENT_SHA since dispatch — aborting cleanly."
     # Submit a minimal review so the status check doesn't stay pending,
     # then exit. A fresh @sdk-review on the new HEAD gets a new session.
     exit 0
   fi
   ```

6. **Read in-repo orchestration assets** — these are the source of truth
   for SDK review behavior. All paths are relative to the repo root:
   - `.mothership/pr-review/CLAUDE.md`
   - `.mothership/pr-review/severity-rubric.yaml`
   - `.mothership/pr-review/modes/standard.md`
   - `.mothership/pr-review/modes/auto-complete.md`
   - `.mothership/pr-review/references/*.md`
   - `.mothership/pr-review/agents/*.md`
   - `.mothership/review-policy.md`
   - `.mothership/review.yaml`

7. **§Intent Inference** — interpret `COMMENTER_INTENT` (free-form text
   the human typed after `@sdk-review`) and set the mode for this run.
   The workflow does NOT parse commands — that is your job.

   Apply these rules in order; first match wins. If `COMMENTER_INTENT`
   is empty, default to **standard review** (Mode A).

   | If the intent contains... | Mode |
   |---|---|
   | (empty) | **A. Standard review** — full multi-agent review, post findings, exit. |
   | `stop`, `cancel`, `abort` | **B. Stop** — no-op. Reply to the triggering comment confirming the cancel, label the PR if applicable, exit cleanly. |
   | `auto-complete`, `resolve all`, `apply fixes`, `fix it`, `fix the issues` | **C. Auto-fix loop** — review, then iterate the in-sandbox fix loop (see §Auto-fix loop). |
   | `challenge` *with* a finding ID, a quoted finding, or a paragraph of reasoning | **D. Targeted challenge** — re-evaluate only the cited findings using the human's explanation as additional context (see Phase 2h). |
   | `override:` followed by a justification | **E. Admin override** — verify the commenter is a repo admin via `gh api repos/$REPO/collaborators/$COMMENTER/permission`. If admin, set status check to success and log the override in REVIEW_DATA. If not admin, reply with the failure reason and exit. |
   | `retrospect`, `retro` | **F. Retrospect** — run the retro flow (see Retrospect Mode section below). |
   | `ci-fix`, `fix ci`, `CI fail` | **G. CI fix** — lightweight Sonnet-only path that fixes a failing CI on an already-approved PR (see CI-Fix Mode section). |
   | Anything else (free-form prose) | **H. Standard review with focus** — run mode A, but treat the prose as a supplementary focus area or extra context the reviewers should weigh (e.g., "look closely at the new metric reader code"). Echo the focus back in the summary so the human sees it landed. |

   Record the chosen mode and pass it forward — every downstream phase
   may behave slightly differently based on it.

8. **Branch freshness + conflict resolution** (before reviewing):
   ```bash
   MERGE_STATUS=$(jq -r '.mergeStateStatus' /tmp/PR.json)
   ```

   If `BEHIND`:
   ```bash
   # Tier 1: GitHub-side update (merges base into the PR branch)
   gh api "repos/$REPO/pulls/$PR_NUMBER/update-branch" \
     -X PUT -f update_method=merge 2>/dev/null
   sleep 10
   # Re-fetch — SHA changed after merge
   git fetch origin "$HEAD_REF" && git reset --hard "origin/$HEAD_REF"
   # Re-fetch authoritative PR metadata and diff after the update
   gh pr view "$PR_NUMBER" --repo "$REPO" --json number,state,isDraft,mergeable,mergeStateStatus,headRefName,baseRefName,headRefOid,title,body,labels > /tmp/PR.json
   gh pr diff "$PR_NUMBER" --repo "$REPO" > /tmp/DIFF.patch
   ```

   If `CONFLICTING`:
   ```bash
   # Tier 2: local git merge (handles non-overlapping conflicts)
   git fetch origin "$BASE_REF"
   git merge "origin/$BASE_REF" --no-edit 2>/dev/null
   ```
   If merge succeeds → `git push origin HEAD`, then re-fetch `/tmp/PR.json`
   and `/tmp/DIFF.patch` and continue review of the merged result.
   If merge fails:
   ```bash
   git merge --abort
   ```
   Submit minimal review: "PR has merge conflicts. Please rebase or
   comment `@sdk-review` after resolving conflicts." Label `needs-rebase`.
   EXIT.

9. **Pre-commit cleanup** (eliminate style noise before review):
   ```bash
   CHANGED=$(git diff --name-only "origin/$BASE_REF"...HEAD -- '*.py')
   if [ -n "$CHANGED" ]; then
     uv run pre-commit run --files $CHANGED 2>/dev/null || true
     if [ -n "$(git status --porcelain -- '*.py')" ]; then
       git add $(git diff --name-only)
       git commit -m "style: auto-format via pre-commit"
       git push origin HEAD
       # Refresh the diff after the auto-format commit.
       gh pr diff "$PR_NUMBER" --repo "$REPO" > /tmp/DIFF.patch
     fi
   fi
   ```
   This ensures the review finds REAL issues, not formatting noise.

10. **CI pre-check** (don't waste review time if CI is broken):
    ```bash
    FAILING=$(gh pr checks "$PR_NUMBER" --repo "$REPO" \
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

11. Read the repo's `CLAUDE.md` for project conventions.

12. **Smart agent routing** — classify the PR:
    ```bash
    TOTAL_FILES=$(grep -c '^+++ b/' /tmp/DIFF.patch || echo 0)
    TEST_FILES=$(grep -c '^+++ b/tests/' /tmp/DIFF.patch || echo 0)
    DOC_FILES=$(grep -c '^+++ b/docs/' /tmp/DIFF.patch || echo 0)
    CONFIG_FILES=$(grep -cE '^\+\+\+ b/(pyproject\.toml|uv\.lock|\.pre-commit|\.github/|helm/)' /tmp/DIFF.patch || echo 0)
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

13. Check diff size for tier:
    - < 2000 lines → `review_tier = "full"`
    - 2000-20000 → `review_tier = "partitioned"`
    - > 20000 → `review_tier = "staged"`

Print: `[Phase 0 complete] PR #<N>, mode=<A-H>, scope=<scope>, tier=<tier>`

---

## Phase 1: Context Gather (~60s)

### 1a. Holistic File Assessment (NO LLM)

For each changed source file in `/tmp/DIFF.patch`:
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
DIFF_CHARS=$(wc -c < /tmp/DIFF.patch)
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
grep -A 9999 "^diff --git a/<file>" /tmp/DIFF.patch | \
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

### 2h. Challenge Mode (if Intent Inference picked Mode D)

**Targeted challenge** — do NOT re-run the full review. Instead:

1. Read the previous review comment from this PR (delta tracking in 2d)
2. Take the author's challenge text from `COMMENTER_INTENT`
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

For each finding, build the object matching the in-sandbox review
payload schema (used for the inline-comment loop in 3f below).

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

### 3c. Label Management (after posting the review in 3f)

There is no mothership-side label handler. You manage all
SDK-specific labels via the `gh` CLI from within the sandbox:

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

There is no mothership-side `submit-review` endpoint. Use the
`gh` CLI directly from the sandbox to post the summary as a PR
comment and each finding as an inline review comment.

```bash
# Summary comment (the body built in 3a, including the
# <!-- SDK_REVIEW_V2 --> marker and the <!-- REVIEW_DATA --> JSON):
gh pr comment "$PR_NUMBER" --repo "$REPO" --body-file /tmp/review-summary.md

# Inline comments — use `gh pr review` (one batched review) or
# `gh api pulls/<n>/comments` per finding. Both routes work; prefer
# `gh pr review --body-file ... --comment` for a single bundled
# review.
gh pr review "$PR_NUMBER" --repo "$REPO" --comment \
  --body-file /tmp/review-summary.md

# Commit status — set the sdk-review check explicitly:
gh api "repos/$REPO/statuses/$HEAD_SHA" \
  -f context="sdk-review" \
  -f state="$STATE" \
  -f description="$DESCRIPTION"
# where STATE ∈ success|failure|pending and DESCRIPTION ≤ 140 chars
```

Retry once on 5xx from the GitHub API. On 422 (malformed inline
comment because the line is not in the diff), drop that one finding
and continue with the rest.

### 3g. CI Check — Fix Failures Before Final Verdict

After posting the review, re-check CI via `gh pr checks "$PR_NUMBER" --repo "$REPO"`. If failures:

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

## Phase 4: Auto-Fix Loop (in-sandbox iteration)

**Runs only when Intent Inference picked Mode C (auto-fix) AND the
verdict is NEEDS_FIXES.** Skip entirely for any other mode/verdict.

The loop happens *inside this same sandbox* — no re-dispatch, no
separate fix-only session. Rover Direct's `session_id` resume means the
GHA workflow blocks on this one dispatch for the whole loop (sync
delivery, capped at `max_timeout_seconds: 7200`).

### Hard limits
- Max 3 iterations total (including this review = iteration 1).
- Wall-clock cap on the loop: 90 minutes from Phase 0 start. If
  approaching, finalize whatever fixes have landed and exit with the
  current verdict.
- One CI retry per push. Two consecutive red CIs ends the loop with
  `REQUEST_CHANGES`.

### Loop body

```
for iteration in 2 3:
  # 1. Read the just-posted review comment (REVIEW_DATA block) and pull
  #    every PATCH-scope finding (any severity). Skip MIGRATE, REFACTOR,
  #    DESIGN_CHANGE — those need human decisions.
  # 2. For each PATCH finding, in severity order:
  #    a. Read the full file
  #    b. Apply the exact suggested_fix
  #    c. uv run pre-commit run --files <file>
  #    d. uv run pytest tests/unit/ -x --timeout=60 (scoped if possible)
  #    e. If anything fails → revert this fix only, note why, continue
  # 3. git diff --stat — verify only intended files changed
  #    If pre-commit reformatted unrelated files: git checkout -- them
  # 4. Stage specific files (NEVER git add -A), commit with Conventional
  #    Commits ("fix(review): address SDK review findings (iter N/3)"),
  #    push origin HEAD.
  # 5. gh pr checks "$PR_NUMBER" --repo "$REPO" --watch (max 10 min)
  #    If CI red and fix is obvious (lint/import/type) → one CI fix
  #    push. If CI red again → exit loop, submit REQUEST_CHANGES.
  # 6. Re-run Phases 0 (steps 3-12 only — skip Intent Inference, mode
  #    stays C) → 1 → 2 → 3 with the new diff.
  # 7. If new verdict == READY_TO_MERGE → break, submit APPROVE
  # 8. If new verdict != NEEDS_FIXES → break, submit current verdict
  # 9. Otherwise continue
```

### Scope restrictions (non-negotiable)
- ONLY fix PATCH scope findings
- NEVER touch MIGRATE, REFACTOR, or DESIGN_CHANGE scope findings
- NEVER modify files the PR doesn't already touch
- NEVER add features, refactor, or "improve" things beyond the findings
- NEVER force-push
- NEVER add Co-Authored-By lines

### Why in-sandbox now
The previous design ran each iteration as a separate sandbox dispatched
by re-posting `@sdk-review --iteration=N+1`. That worked but cost more
(no cache reuse) and was hard to bound under a single GHA job. With
Rover Direct's session resume + sync delivery, one dispatch covers the
whole loop and the GHA workflow simply waits.

Print: `[Phase 4 complete] iter=<N>, verdict=<verdict>` or
`[Phase 4 skipped] mode=<mode>, verdict=<verdict>`

---

## Retrospect Mode (Intent Inference Mode F)

When `COMMENTER_INTENT` contains `retrospect` or `retro`:

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

## CI-Fix Mode (Intent Inference Mode G)

Triggered when `COMMENTER_INTENT` contains `ci-fix`, `fix ci`, or
`CI fail` — typically by the merge-queue workflow when CI fails after a
branch update on an approved PR. This is a LIGHTWEIGHT mode — no full
review, just fix CI and re-approve if the fix is clean.

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

Always post the summary comment + set the commit status before
exiting (see Phase 3f). A PR with no review comment
and no status update is the worst outcome.

Submit minimal:
```json
{
  "approval_recommendation": "REQUEST_CHANGES",
  "summary": "SDK Review could not complete: <reason>. Re-trigger with @sdk-review.",
  "findings": []
}
```
