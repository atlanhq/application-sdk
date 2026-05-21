# SDK Evolution — Orchestration Playbook

Follow ALL stages (0-8). NEVER stop early. NEVER defer work to "later."
Every finding is either DONE (ticket + PR) or KILLED (with reason) by end of run.
Print `[Stage N/9 complete]` after each stage.

## Time Budgets

| Stage | Budget | Cut if over |
|-------|--------|-------------|
| 0: Preflight | 2 min | Nothing — fast |
| 1: Discovery | 10 min | Drop slowest agents, proceed with what returned |
| 2: Gate 1 | 5 min | Batch remaining findings, challenge in groups |
| 3: Gate 2 (feasibility) | 5 min | Kill remaining unchecked findings |
| 4: Fix PRs | 15 min | Stop at current PR, skip remaining |
| 5: Validation | 5 min | Skip CI wait, note in summary |
| 6: Handoff | 2 min | |
| 7: Self-improve | 3 min | |
| 8: Summary | 1 min | |

Hard stop: **60 minutes total.** If approaching, skip to Stage 8 and submit summary with whatever is done.

---

## Stage 0: Preflight

The dispatch prompt passes the run context directly. Read these values
from the prompt header (do NOT re-derive):

```
RUN_DATE, SCAN_MODE   # SCAN_MODE: auto | full | incremental
```

If `SCAN_MODE == auto`: resolve at runtime → `full` on Sunday (UTC),
`incremental` otherwise.

1. **Set working directory** — mothership cloned the repo on `main` into
   `/workspace/application-sdk`:
   ```bash
   cd /workspace/application-sdk

   # Background: install deps so uv run pre-commit/pytest don't wait
   uv sync --all-extras 2>/dev/null &
   ```

2. **Auth setup** — `$GITHUB_TOKEN` is injected by mothership from its
   GitHub App installation. Make `gh` use it:
   ```bash
   echo "$GITHUB_TOKEN" | gh auth login --with-token 2>/dev/null
   ```

3. **Read in-repo orchestration assets** — these are the source of truth
   for SDK Evolution behavior. All paths are relative to the repo root:
   - `.mothership/sdk-evolution/CLAUDE.md`
   - `.mothership/sdk-evolution/tools.md`
   - `.mothership/sdk-evolution/references/*.md`
   - `.mothership/sdk-evolution/agents/*.md`
   - `.mothership/sdk-evolution/scripts/update_index.py`

4. **Build suppression list** (query Linear directly via proxy):
   ```bash
   # Get all open tickets in the "Autonomous SDK Evolution" milestone
   RESPONSE=$(curl -s "$PROXY_BASE/proxy/linear" \
     -H "Authorization: Bearer $PROXY_JWT" \
     -H "Content-Type: application/json" \
     -d '{
       "query": "{ issues(filter: { project: { name: { eq: \"App SDK v3.0\" }}, state: { type: { nin: [\"completed\", \"canceled\"] }}}, first: 100) { nodes { identifier title description state { name } } }}"
     }')
   ```
   Parse the response. For each open ticket, extract:
   - File paths mentioned in the description
   - Rule IDs cited
   Build a suppression list of file:rule pairs to skip during discovery.

   Linear is the canonical source for suppression — there is no pre-loaded
   `session/SUPPRESSION.md` in this model. If the Linear proxy call fails,
   proceed with empty suppression (will discover some duplicates, but
   won't block the run).

5. **Build the codebase index** in-sandbox:
   ```bash
   python .mothership/sdk-evolution/scripts/update_index.py > /tmp/index.json
   ```
   Stage 1 reads `/tmp/index.json` for the AST view of the codebase.
   There is no R2 shared-memory store in this model — the index is
   rebuilt every run from the freshly-cloned source.

6. **Read previous learnings (optional):**
   Learnings, if any, live as comments on the parent Linear ticket for
   the most recent SDK Evolution run. Query Linear for the most recent
   "SDK Evolution — <date>" parent ticket and read its learnings
   section. If none found → first run, no learnings yet.

Print: `[Stage 0/9 complete] scan_mode=<resolved mode>, suppressed=<N> pairs, learnings=<found|none>`

---

## Stage 1: Discovery (7 agents, parallel)

Dispatch ALL 7 via Agent tool simultaneously:

**Replaced 10 narrow agents with 3 broader ones to cut compute by ~60%.**

Each agent reads the same files but applies its own ruleset.

Agents to dispatch:
1. `agents/correctness-quality.md` — code quality + architecture (11 ADRs) + v2 patterns + bug hunting + test quality. Tags: [QUAL] [ARCH] [V2] [BUG] [TEST]
2. `agents/safety.md` — security + performance. Tags: [SEC] [PERF]
3. `agents/evolution.md` — improvement + centralization + deprecation cleanup. Tags: [IMPROVE] [CENTRAL] [DEPREC]

Each agent receives: codebase index, scan_target file contents, reference rules, suppression list.

The `evolution` agent ([IMPROVE]/[CENTRAL]/[DEPREC] findings) bypasses Gate 1
— these aren't bug findings, so adversarial challenge doesn't apply. They go
directly to Gate 2 (feasibility check).

**Pre-filter (before Gate 1):**
- Finding on file NOT in scan_targets + incremental mode → KILL
- Finding on file with zero callers + severity low/medium → KILL
- Finding on deprecated file + category != security → KILL
- Finding matches suppression list → KILL

Print: `[Stage 1/9 complete] <N> raw findings, <M> after pre-filter, <K> improvement suggestions`

---

## Stage 2: Gate 1 — GPT Cross-Model Challenge

For each surviving finding, call GPT-5.3-codex via proxy:

```bash
curl -s "$PROXY_BASE/proxy/litellm/chat/completions" \
  -H "Authorization: Bearer $PROXY_JWT" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-5.3-codex",
    "temperature": 0.2,
    "max_tokens": 4000,
    "messages": [
      {"role": "system", "content": "<gate1-challenger.md with placeholders>"},
      {"role": "user", "content": "Challenge this finding."}
    ]
  }'
```

GPT verdict: `survived` or `killed`.
If GPT unavailable → keep finding (fail-open).

**Improvement suggestions bypass Gate 1** — they're not bugs, so adversarial challenge doesn't apply. They go directly to Stage 3.

Print: `[Stage 2/9 complete] <N> survived, <M> killed`

---

## Stage 3: Gate 2 — Feasibility + Worth Check (BEFORE tickets/PRs)

**This gate runs BEFORE any Linear ticket or PR is created.** Its job:
determine if the finding is worth a PR and if the fix is feasible.

For each Gate 1 survivor:

### 3a. Feasibility Check (you do this — read the code)

1. Read the FULL file containing the finding
2. Read files that call/import the flagged function
3. Answer these questions:
   - **Can this be fixed in < 50 lines?** If not → tag `needs-design-review`
   - **Will the fix break any callers?** Check imports + call graph from index
   - **Does a v3 replacement exist?** If yes → fix = migrate callers, not patch
   - **Is this in a dumping-ground file?** If yes → fix should move the function, not patch it
   - **Would a senior engineer approve this fix?** If the finding is technically correct
     but the fix would make the code worse (e.g., pollute an executor, add coupling) → KILL

2. Classify each finding:
   - **FIX** — feasible, clean, < 50 lines, no caller breakage → proceed to Stage 4
   - **DESIGN** — correct finding, fix is larger or needs human sign-off → STILL gets
     a ticket + PR + branch with the proposed implementation, but labeled
     `needs-design-review`. The team reviews the actual code diff, not just a description.
   - **KILL** — not feasible, not worth it, fix worse than the problem → kill with reason

### 3b. Glean Context Check (max 5 queries total)

Before creating tickets, search Glean for prior discussions about the finding:

```bash
# Dispatch a sub-agent for Glean search
# Query: "<finding title> site:atlanhq application-sdk"
```

For each finding, check if:
- **Already discussed and accepted** (ADR, Slack thread, Confluence doc) → KILL
  with note: "Accepted per <link>"
- **Already discussed, no decision** → note the discussion link in the ticket
- **No prior discussion** → proceed

Max 5 Glean queries per run to stay within budget.
Prioritize: DESIGN findings first (most likely to have prior discussion),
then high-severity FIX findings.

### 3c. Worth Check

For findings classified as FIX, also check:
- **Call frequency from index:** If the flagged code runs < 10 times per workflow and
  the finding is medium severity → KILL (not worth a PR)
- **Existing TODOs:** If there's already a TODO/FIXME near the code → check if there's
  a Linear ticket. If tracked → KILL (already known)
- **Age of code:** `git log --oneline -1 <file>` — if the code hasn't been touched
  in 6+ months and has no open issues → deprioritize (low urgency)

### 3c. Improvement suggestions

For improvement suggestions from Agent 8:
- **Small** (new utility, better default, missing validation) → FIX
- **Large** (new abstraction, API change, DX improvement) → DESIGN
  (still gets a PR with proposed implementation + `needs-design-review`)
- Vague or low-confidence → KILL

Print: `[Stage 3/9 complete] <N> FIX, <M> NEEDS_DESIGN, <K> KILLED`

---

## Stage 4: Create Tickets + Fix PRs

### 4a. Pull latest main (stale guard)

```bash
cd /workspace/application-sdk
git checkout main
git pull origin main
```

This ensures fix branches are based on the latest code, not a stale checkout
from 30 minutes ago.

### 4b. Create Linear Parent Ticket

Via Linear proxy (see tools.md):
1. Find "Builder Experience" team → team_id
2. Find "App SDK v3.0" project → project_id
3. Find/create "Autonomous SDK Evolution" milestone
4. Get workflow states
5. Create parent: "SDK Evolution — {date}" with a description that
   includes the GHA run URL up-front so the parent ticket links back
   to its source run (used in Stage 8 for the summary too):

   ```
   Auto-generated by SDK Evolution.

   **Run:** [view workflow logs + cost](${GHA_RUN_URL})

   Children below, one per finding that survived all gates.
   ```

### 4c. Create DESIGN Tickets + PRs (proposed implementation)

For each DESIGN finding — still create a ticket AND a PR with the proposed
implementation. The team reviews the actual code, not just a description.

Same process as 4d below, but:
- Title: "[DESIGN] {category}: {title} [{TICKET_ID}]"
- Labels: `Autonomous SDK Evolution` + `needs-design-review`
- PR description MUST include alternative approaches analysis:

  ```markdown
  ## Design Review Needed

  This PR proposes a change that needs team sign-off before merging.

  ### Problem
  {what's wrong or what could be better — from the finding description}

  ### Why This Matters
  {impact on connector builders, production reliability, or DX}

  ### Approaches Considered

  **Option A: {name}** (this PR implements this)
  - How: {brief description}
  - Pros: {why this is the best option}
  - Cons: {trade-offs}
  - Effort: {small/medium/large}

  **Option B: {name}**
  - How: {brief description}
  - Pros: {advantages}
  - Cons: {why this wasn't chosen}
  - Effort: {small/medium/large}

  **Option C: {name}** (if applicable)
  - How: {brief description}
  - Pros/Cons: {summary}

  ### Recommendation
  Option A because: {concrete reasoning — not "it's simpler" but
  "it maintains backward compatibility with existing connectors that
  use X pattern, while Option B would require migrating 12 callers"}

  ### How to Review
  Review the code diff — it shows the exact proposed implementation.
  If you prefer Option B, comment and we'll update.

  ### Prior Discussion
  {Glean search result if found: "Discussed in <Slack link> on <date>"}
  {or "No prior discussion found"}
  ```

- Linear ticket status: "In Review"
- The PR will NOT be handed to @sdk-review auto-complete (human reviews DESIGN PRs)

### 4d. Create FIX Tickets + PRs (atomic)

For each FIX finding, sequentially:

```bash
# 1. Clean working directory
git checkout main
git clean -fd 2>/dev/null
git checkout -- . 2>/dev/null

# 2. Create branch
git checkout -b <TICKET_ID> origin/main

# 3. Apply fix
#    - Read the full flagged file
#    - If bug/security/performance: write failing test FIRST (TDD)
#    - Apply the fix
#    - If improvement: implement the improvement

# 4. Validate
uv run pre-commit run --files <changed_files>
uv run pytest tests/unit/ -x -q --timeout=60

# 5. Verify diff is clean
git diff --stat
# If unrelated files changed by pre-commit: git checkout -- <unrelated>

# 6. Commit + push
git add <specific_files>
git commit -m "<type>(<scope>): <desc> [<TICKET_ID>]"
git push origin <TICKET_ID>

# 7. Create PR
gh pr create --repo atlanhq/application-sdk --base main \
  --title "<type>(<scope>): <desc> [<TICKET_ID>]" \
  --label "Autonomous SDK Evolution" \
  --body "..."

# 8. Update Linear: "In Review", attach PR link
```

**If ANY step fails:** Cancel the ticket immediately. No orphans.
**If test failure:** Read the error, attempt ONE fix. If second attempt fails → KILL.

**PR grouping:**
- bug, architecture, security, performance, improvement → individual PR
- code-quality, test-quality, v2-patterns → group into one PR per category

**Max 8 PRs per run.** Priority: security > bug > improvement > architecture > performance > rest.

Print: `[Stage 4/9 complete] <N> tickets, <M> FIX PRs, <K> DESIGN PRs`

---

## Stage 5: Validation

For each PR:

1. **Wait for CI** (max 3 min):
   ```bash
   gh pr checks <PR_NUMBER> --repo atlanhq/application-sdk --watch 2>/dev/null &
   sleep 180 && kill %1 2>/dev/null
   ```

2. **Deterministic checks:**
   - PR diff touches flagged file? If not → close PR + cancel ticket
   - Test included where required? bug/security/performance without test → close PR + cancel ticket
   - Diff > 200 lines for non-critical? → close PR + cancel ticket
   - Unrelated files in diff? → close PR + cancel ticket

3. **CI check** (if CI finished):
   - All green → proceed
   - Failure in PR code → attempt Sonnet CI fix:
     ```bash
     # Read failure, fix, push
     gh run view <run_id> --repo atlanhq/application-sdk --log-failed | head -50
     # Apply fix, pre-commit, pytest, push
     ```
   - Failure persists → close PR + cancel ticket

4. **Close killed PRs with clear reason:**
   ```bash
   gh pr close <PR_NUMBER> --repo atlanhq/application-sdk \
     --comment "Closed by SDK Evolution: <reason>"
   ```
   Update Linear ticket → "Canceled" with reason in comment.

Print: `[Stage 5/9 complete] <N> passed, <M> killed`

---

## Stage 6: Handoff to SDK Reviewer

### FIX PRs → @sdk-review auto-complete

```bash
gh pr comment <PR_NUMBER> --repo atlanhq/application-sdk \
  --body "@sdk-review auto-complete Autonomous SDK Evolution fix — review and merge if clean."
```

Update Linear: "Handed to SDK Reviewer for auto-review."

### DESIGN PRs → @sdk-review (review-only, no auto-fix)

```bash
gh pr comment <PR_NUMBER> --repo atlanhq/application-sdk \
  --body "@sdk-review Design proposal from SDK Evolution — review the approach, not just the code."
```

DESIGN PRs get reviewed but NOT auto-fixed or auto-merged. The team decides
whether to approve, request changes, or close. Update Linear: "In Review — awaiting design feedback."

Print: `[Stage 6/9 complete] <N> FIX PRs auto-complete, <M> DESIGN PRs review-only`

---

## Stage 7: Self-Improvement

### 7a. Analyze This Run

```
- How many findings were discovered? How many killed at each gate?
- If Gate 1 killed > 60% of findings → discovery agents are too noisy
  → tighten confidence thresholds in agent prompts
- If Gate 2 killed > 40% of Gate 1 survivors → Gate 1 is too lenient
  → make GPT challenger prompts more aggressive
- If > 3 findings from the same rule were killed → rule is too broad
  → note for reference rule tightening
- If certain files produced 5+ findings → flag as "needs holistic refactor"
  → create a single DESIGN ticket instead of individual fixes
```

### 7b. Persist Learnings (as Linear ticket comments)

If patterns emerged, post them as a comment on this run's parent Linear
ticket ("SDK Evolution — {date}"). Use the same YAML shape so future
runs can parse it:

```yaml
last_run: {date}
rules_to_tighten:
  - rule: <rule_id>
    reason: "Killed 5/5 findings — too broad"
    suggestion: "Raise confidence to 90%"
rules_to_relax:
  - rule: <rule_id>
    reason: "0 findings in 3 runs — may be too strict or pattern no longer exists"
files_needing_refactor:
  - file: application_sdk/common/utils.py
    finding_count: 7
    recommendation: "Holistic decomposition needed — too many concerns"
recurring_false_positives:
  - pattern: "<description>"
    count: 3
    recommendation: "Add to POLICY.md as by-design"
improvement_backlog:
  - title: "Add batch upload utility"
    rationale: "3 connectors implement this independently"
    priority: medium
```

Linear is the persistent store — there is no R2 shared-memory volume
in this model. The codebase index is rebuilt every run from source.

### 7c. Read Previous Learnings

At the START of Stage 1, query Linear for the most recent
"SDK Evolution — *" parent ticket and read its learnings comment.
Apply:
- Tightened rules → raise confidence thresholds in agent prompts
- Relaxed rules → lower thresholds or skip checks
- Known false positives → add to suppression
- Files needing refactor → create DESIGN tickets instead of point fixes

This creates a **feedback loop**: each run learns from the last.

Print: `[Stage 7/9 complete] <N> rules updated, <M> learnings stored`

---

## Stage 8: Summary

1. **Gather metrics:**
   ```json
   {
     "run_date": "{date}",
     "scan_mode": "incremental",
     "discovered": 25,
     "killed_prefilter": 5,
     "killed_gate1": 8,
     "killed_gate2_feasibility": 4,
     "fix_prs_created": 6,
     "design_prs_created": 2,
     "improvement_suggestions": 3,
     "prs_killed_validation": 1,
     "prs_handed_to_reviewer_fix": 5,
     "prs_handed_to_reviewer_design": 2,
     "improvement_suggestions": 3,
     "centralization_findings": 1,
     "deprecation_cleanups": 2,
     "glean_queries_used": 4,
     "rules_updated": 2,
     "total_cost_estimate": "$35"
   }
   ```

2. **Update Linear parent ticket** with summary table via proxy.
   Always include a `**Run:** [view workflow logs + cost](<GHA_RUN_URL>)`
   line so the ticket reader can jump to the GitHub Actions run that
   produced this evolution pass. `GHA_RUN_URL` is supplied in the
   prompt header.

   ```bash
   SUMMARY_BODY="## SDK Evolution — {date}\n\n**Run:** [logs + cost](${GHA_RUN_URL})\n\n| Metric | Count |\n|---|---|\n| Discovered | {N} |\n| Killed (pre-filter) | {N} |\n| Killed (Gate 1) | {N} |\n| Killed (Gate 2) | {N} |\n| FIX PRs created | {N} |\n| DESIGN PRs created | {N} |\n| Handed to reviewer | {N} |\n| Rules updated | {N} |"

   curl -s "$PROXY_BASE/proxy/linear" \
     -H "Authorization: Bearer $PROXY_JWT" \
     -H "Content-Type: application/json" \
     -d "{
       \"query\": \"mutation(\$id: String!, \$input: IssueUpdateInput!) { issueUpdate(id: \$id, input: \$input) { success }}\",
       \"variables\": { \"id\": \"<parent_ticket_id>\", \"input\": { \"description\": \"$SUMMARY_BODY\" }}
     }"
   ```

3. **Audit Linear consistency:**
   - Every sub-ticket has either: a PR link + "In Review", or "Canceled" status
   - Parent ticket reflects aggregate state
   - No orphans (tickets without PRs that aren't canceled or design)

4. **Security audit:**
   - Scan all PR descriptions for secrets/tokens
   - Scan all Linear ticket content for internal URLs/credentials

5. **Codebase index is ephemeral** — rebuilt next run via
   `scripts/update_index.py`. Nothing to persist here.

Print:
```
[Stage 8/9 complete]

========================================
 SDK Evolution — {date}
========================================
Scan:         {mode} ({N} files scanned)
Findings:     {discovered} → {gate1_survived} (Gate 1) → {gate2_fix} FIX + {gate2_design} DESIGN
PRs:          {prs_created} created → {validation_passed} passed → {handed_off} handed to reviewer
Improvements: {improvement_count} suggestions ({improvement_fix} as PRs, {improvement_design} as tickets)
Self-improve: {rules_updated} rules updated, {learnings_stored} learnings stored
========================================
```

---

## Principles (Non-Negotiable)

### Nothing Is Deferred

Every finding exits the pipeline in ONE of these states:
- **DONE** — ticket + PR created, handed to @sdk-review auto-complete
- **DESIGN** — ticket + PR with proposed implementation, labeled `needs-design-review`,
  handed to @sdk-review (review-only). The team sees the actual code diff.
- **KILLED** — killed with clear reason at a specific gate

There is no "I'll do this later." Everything gets a PR — FIX PRs auto-merge,
DESIGN PRs wait for human approval.

### Gate 2 Is Before Tickets, Not After

The OLD flow was: discover → challenge → create ticket → create PR → validate.
This created orphaned tickets for findings that weren't feasible.

The NEW flow is: discover → challenge → **feasibility check** → create ticket+PR.
Only feasible findings get tickets. Only ticket'd findings get PRs.

### Improvement Discovery Is Part of Evolution

Evolution isn't just about finding bugs. It's about making the SDK better.
Agent 8 (SDK Improvement Scout) looks for patterns that could be new
abstractions, better defaults, shared utilities. Good SDKs evolve — they
don't just get bug fixes.

### Self-Improvement Is Real

Each run reads learnings from previous runs (R2) and adjusts behavior.
Each run writes learnings for future runs. Over time:
- Noisy rules get tightened (fewer false positives)
- Missing rules get added (fewer missed issues)
- Recurring patterns get escalated (holistic refactors)
- The pipeline gets cheaper and more accurate

---

## If You Cannot Finish

If approaching the 45-minute hard stop:

1. Skip remaining stages
2. Create a summary with what was completed
3. Close any open PRs that weren't validated
4. Cancel any tickets without PRs
5. Note in summary: "Run truncated at Stage N due to time limit"
6. Save index + learnings to R2

**A partial run with clean state is better than a full run that dies mid-PR.**
