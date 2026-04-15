---
name: sdk-review
description: >
  Multi-model PR review pipeline for application-sdk v3. Triggered by
  @sdk-review on a PR. Dispatches 3 Opus domain agents + 1 GPT-5.3-codex
  adversarial reviewer. Reads full file context, detects dumping grounds
  and v3 replacements, posts inline comments with exact fixes, checks CI
  and branch status, resolves conflicts, and escalates to humans when a
  holistic design change is needed. Optional auto-fix loop spawns separate
  sessions to apply fixes and re-reviews until clean.
---

# SDK v3 Code Review — v2

Multi-model, context-aware PR review against the application-sdk v3
architecture, all 11 ADRs, and the full review checklist.

## Trigger

Comment on a PR:
```
@sdk-review                              → Review only
@sdk-review please resolve all issues    → Review + auto-fix loop
@sdk-review override: <reason>           → Force-pass (repo admins only)
@sdk-review --iteration=N --max=M        → Internal loop continuation
```

PR number comes from the GitHub event context — no need to specify it.

---

## Models (Pinned — No Fallback)

| Role | Model | Via |
|------|-------|----|
| Review agents (Wave 1, 3 agents) | `claude-opus-4-6` | `llmproxy.atlan.dev` |
| Fix session | `claude-opus-4-6` | `llmproxy.atlan.dev` |
| Adversarial reviewer (Wave 2) | `gpt-5.3-codex` | `llmproxy.atlan.dev` (curl) |

No model substitution. No sonnet fallback. These exact models every time.

---

## Guardrails (Non-Negotiable — Block Merge)

The `sdk-review` status check is REQUIRED on the base branch. These
guardrails determine whether the check passes or fails. A PR cannot
merge if any guardrail is violated (unless a repo admin overrides).

### G1: Security Blockers
Any `[SEC]` finding at Critical severity → status = failure.
No exceptions.

### G2: Contract Safety
Any change to an existing `Input`/`Output` contract that removes a field,
renames a field, or changes a field type → status = failure.
Breaks in-flight Temporal workflow replay.

### G3: Determinism
Any non-deterministic operation in a workflow `run()` or `@entrypoint`
method body (datetime.now, uuid4, random, I/O not inside @task) →
status = failure.

### G4: Test Coverage
New public API (module, class, or method exported in `__init__.py`)
without corresponding tests → status = failure (Critical finding).

### G5: Secret Safety
Any hardcoded secret, credential in logs, or token in error message →
status = failure.

### G6: Breaking API Change
Any removal or signature change of a public export without a deprecation
shim with `warnings.warn()` → status = failure (Critical finding).

### G7: CI Must Pass
All required CI checks must be passing. If CI is failing, note which
checks fail in the review. Status reflects this.

### G8: Branch Must Be Mergeable
Branch must not have conflicts. If conflicts exist, attempt auto-resolution
in Stage 1. If auto-resolution fails → status = failure with instructions.

---

## Override Mechanism

Reviews can have false positives. Two escape hatches:

### Author Dispute
Author comments `@sdk-review` with an explanation of why a finding is
wrong. The next review run re-evaluates with the explanation as context.

### Admin Force-Override
A repo admin comments:
```
@sdk-review override: <reason>
```

The skill checks the commenter's permission:
```bash
gh api repos/atlanhq/application-sdk/collaborators/<commenter>/permission \
  --jq .permission
```

If permission is `admin`:
- Set status check to success
- Add note to review comment: "Overridden by @admin: <reason>"
- Log the override in REVIEW_DATA for audit trail

If permission is NOT `admin`:
- Reply: "Only repo admins can override the review. Please dispute
  individual findings by commenting @sdk-review with your explanation."

---

## Pipeline

### Stage 1: Orient

Every run, always. No exceptions.

#### 1a. Parse Command
```
"@sdk-review"                         → auto_fix = false
"@sdk-review resolve all"             → auto_fix = true
"@sdk-review please resolve all ..."  → auto_fix = true  (contains "resolve")
"@sdk-review override: ..."           → override mode (skip to override logic)
"@sdk-review --iteration=N --max=M"   → auto_fix = true, iteration = N
```

#### 1b. PR State
```bash
gh pr view <PR> --repo atlanhq/application-sdk \
  --json state,isDraft,mergeable,mergeStateStatus,headRefName,baseRefName,headRefOid
```
- Draft → exit: "Skipping draft PR."
- Closed/merged → exit: "PR is already closed/merged."

#### 1c. Previous Review Detection
```bash
gh api repos/atlanhq/application-sdk/issues/<PR>/comments \
  --jq '.[] | select(.body | contains("<!-- SDK_REVIEW_V2 -->")) | {id: .id, body: .body}'
```
If found → store previous findings for delta tracking in Stage 3.

#### 1d. Set Status to Pending
```bash
gh api repos/atlanhq/application-sdk/statuses/<HEAD_SHA> \
  -f state=pending \
  -f context=sdk-review \
  -f description="SDK Review in progress..."
```

#### 1e. CI Status
```bash
gh pr checks <PR> --repo atlanhq/application-sdk
```
Record which checks are passing/failing. Include in review.

#### 1f. Branch Freshness & Conflicts
```bash
gh pr view <PR> --repo atlanhq/application-sdk --json mergeStateStatus,mergeable
```

If branch is behind:
```bash
# Update branch by merging base into PR branch
gh api repos/atlanhq/application-sdk/pulls/<PR>/update-branch \
  -X PUT -f update_method=merge
```

If conflicts (mergeable = CONFLICTING):

**Three-tier resolution strategy:**

**Tier 1: GitHub API update-branch** (cleanest, already attempted above)
If it succeeded, no further action needed.

**Tier 2: git merge** (handles non-overlapping conflicts)
```bash
git fetch origin <base_branch> <head_branch>
git checkout <head_branch>
git merge origin/<base_branch> --no-edit
```
- If merge succeeds → push, continue
- If merge fails (real conflicts) → `git merge --abort`, fall through to Tier 3.

**Tier 3: Claude Code semantic conflict resolution**
Spawn a SEPARATE Claude Code session (new claude-code-action step) with:

```
You are resolving merge conflicts on PR #<number>.

Branch: <head_branch>
Base:   <base_branch>

Process:
1. git fetch origin <base_branch> <head_branch>
2. git checkout <head_branch>
3. git merge origin/<base_branch> --no-edit (creates conflict markers)
4. List conflicting files: git diff --name-only --diff-filter=U
5. For each conflicting file:
   a. Read the FULL file (with conflict markers)
   b. Read the relevant context — what does each side intend to do?
   c. Resolve the conflict markers based on intent:
      - Both added imports → combine and let isort sort
      - Both added tests in same class → include both
      - Function renamed in base, called with old name in PR →
        update call sites to new name
      - Same function modified differently → READ the full file,
        understand both intents, merge if they're compatible
      - Genuine logic conflict (incompatible changes) → ABORT
   d. Verify the file parses (no leftover conflict markers)

6. SAFETY GUARDRAILS — abort if any conflict involves:
   - Business logic changes in the same function in incompatible ways
   - Security-sensitive code (auth, credentials, secrets)
   - Infrastructure/deployment configuration
   - Database migrations or schema changes
   - Removed in one branch, modified in the other (decide intent)

7. Verify the merge:
   a. uv run pre-commit run --all-files
   b. uv run pytest tests/unit/ -x --timeout=120
   c. If any verification fails → git merge --abort

8. If verification passes:
   git add <resolved files>
   git commit -m "merge: resolve conflicts with <base_branch> (auto-resolved)"
   git push origin <head_branch>

9. If aborted at any step:
   git merge --abort
   Output: ABORTED: <reason>
```

After Tier 3:
- Success → continue Stage 2 (review the merged result, since the
  review must reflect the merged code)
- Aborted → set status = failure:
  "Conflicts require manual resolution. Files: <list>. Reason: <why aborted>"
  Add label: `needs-rebase`. STOP.

#### 1g. Resolve Previously Posted Inline Comments
Check each inline comment from previous review. If the code at that
line has changed (diff shows the line was modified) → resolve the comment:
```bash
# GitHub doesn't have a direct "resolve" API for review comments.
# Instead, we track resolved findings in the summary comment and
# post a reply: "This finding has been addressed in the latest push."
```

---

### Stage 2: Review

#### 2a. Context Gathering (parallel)

Run ALL in parallel:

**PR diff:**
```bash
gh pr diff <PR> --repo atlanhq/application-sdk
```

**Reference rules** (read from skill directory):
- `references/v3-architecture-rules.md`
- `references/code-quality-rules.md`
- `references/security-rules.md`
- `references/test-quality-rules.md`
- `references/dx-rules.md`
- `references/structural-rules.md`

Also read:
- `docs/standards/review-checklist.md`
- `docs/agents/testing.md`

**Full file context:**
For EVERY changed source file → Read the complete file (not just diff).
For EVERY changed test file → Read the complete file.

**PR file list and commit history:**
```bash
gh pr view <PR> --repo atlanhq/application-sdk --json files,commits
```

#### 2b. Holistic File Assessment

Dispatch an Explore agent to produce file annotations:

For each changed source file:

1. **SIZE**: Line count. Flag if > 500 lines.
2. **DOMAIN COHERENCE**: Single domain or dumping ground?
   Flag `DUMPING_GROUND` if multiple unrelated domains.
3. **V3 REPLACEMENT**: For each function being modified:
   ```bash
   grep -rn "def <function_name>" application_sdk/ --include="*.py"
   ```
   Flag `V3_REPLACEMENT_EXISTS` with the replacement path if found.
4. **CALLER ANALYSIS**: Who calls the modified functions?
   ```bash
   grep -rn "<function_name>" application_sdk/ --include="*.py" -l
   ```
5. **DEPRECATION STATUS**: Is the file/function already deprecated?

Output per file:
```
file: <path>
lines: <count>
flags: [DUMPING_GROUND?, V3_REPLACEMENT_EXISTS?, DEPRECATED?]
replacement: <v3 path if exists>
callers: <count and list>
recommendation: PATCH_IN_PLACE | MIGRATE_CALLERS | DECOMPOSE_FILE | DESIGN_REVIEW_NEEDED
```

#### 2c. Large PR Handling

If > 100 changed files:
- Partition files by top-level directory (application_sdk/, tests/, docs/)
- Each Wave 1 agent gets its relevant partition with full context
- Architecture agent gets all files
- Test agent gets test files + corresponding source files
- Quality/Security get source files
- DX gets public API files (__init__.py, contracts, decorators)
- Structural gets the largest/most-changed files

#### 2d. Wave 1: Three Opus Domain Agents (parallel)

Launch ALL 3 agents simultaneously with `model: "opus"`.

The 3 agents collapse the previous 6-agent design into focused groupings.
Each agent still tags its findings with the underlying domain so reviewers
can tell whether something is a security issue, architecture concern, etc.

Every agent receives (include ALL of this in the agent prompt):
- Full PR diff (from 2a)
- File list (from 2a)
- Their specific reference document content (read and INLINE it, not just the filename)
- PR title and description
- Holistic file annotations from 2b
- Full file content for the files they review (from 2a)
- For Agent 2 (QUAL): also include content of `docs/standards/review-checklist.md`
- For Agent 4 (TEST): also include content of `docs/agents/testing.md`

## Prompt Caching (REQUIRED for cost optimization)

The 6 Opus agents share large amounts of identical content (reference
docs, holistic context preamble, file annotations). Use Anthropic's
prompt caching to get a 90% discount on these shared portions.

**Structure each agent's prompt in this exact order:**

```python
{
  "model": "claude-opus-4-6",
  "messages": [{
    "role": "user",
    "content": [
      # Block 1: Holistic Context Preamble (IDENTICAL across all 3 agents)
      {
        "type": "text",
        "text": "<HOLISTIC CONTEXT PREAMBLE>",
        "cache_control": {"type": "ephemeral"}
      },
      # Block 2: Reference doc (per agent — IDENTICAL across iterations
      # within a 5-min window)
      {
        "type": "text",
        "text": "<REFERENCE DOC CONTENT>",
        "cache_control": {"type": "ephemeral"}
      },
      # Block 3: PR-specific shared context (IDENTICAL across all 3 agents
      # for THIS PR — file annotations, full file content)
      {
        "type": "text",
        "text": "<PR FILE ANNOTATIONS + FULL FILES>",
        "cache_control": {"type": "ephemeral"}
      },
      # Block 4: Agent-specific (NOT cached — varies per agent)
      {
        "type": "text",
        "text": "<AGENT-SPECIFIC INSTRUCTIONS + PR DIFF + OUTPUT FORMAT>"
      }
    ]
  }]
}
```

**Why this works:**
- Block 1 (preamble): same for all 3 agents → cached after agent 1 runs
- Block 2 (rules): same per agent across re-reviews within 5 min → cached
- Block 3 (PR context): same for all 3 agents within a single review → cached
- Block 4 (specific): NOT cached because it varies per agent

**Result: ~60-70% cost reduction** on Wave 1 with no quality impact.

For auto-fix loops (multiple iterations within 1 hour), use 1-hour TTL:
`"cache_control": {"type": "ephemeral", "ttl": "1h"}`

**Cache mechanics:**
- First agent's request writes the cache (full price + 25% write premium)
- Agents 2-6 read from cache at 10% of normal input price
- Cache key = exact byte-for-byte match of cached prefix
- TTL: 5 min (default) or 1 hour (with explicit ttl)

**Net savings on a typical PR:**
- Without caching: 3 agents × ~30k shared input = 90k × $15/MTok = $1.35
- With caching: 30k (write) + 2 × 30k × 0.10 (read) = effective 36k = $0.54
- **Savings: ~60% on shared content per review cycle**

## Cost Optimizations (REQUIRED — apply all of Tier 1 + 2)

These optimizations preserve quality while reducing token consumption.

### Optimization 1: Smart Codex Skip (Tier 1)

Skip the Wave 2 adversarial pass when the PR is clearly low-risk.

**Skip GPT-5.3-codex when ALL of these are true:**
- < 20 lines changed total in the diff
- No files in any of these directories (high-risk areas):
  - `application_sdk/app/`
  - `application_sdk/handler/`
  - `application_sdk/credentials/`
  - `application_sdk/contracts/`
  - `application_sdk/execution/`
  - `application_sdk/infrastructure/`
- No changes to any `__init__.py` (no new public exports)
- No new files added
- All Wave 1 agents reported zero Critical findings

**Implementation in Phase 2e (between Wave 1 and Wave 2):**

```python
def should_skip_codex(diff, files_changed, wave_1_findings):
    high_risk_dirs = [
        "application_sdk/app/",
        "application_sdk/handler/",
        "application_sdk/credentials/",
        "application_sdk/contracts/",
        "application_sdk/execution/",
        "application_sdk/infrastructure/",
    ]
    if diff.line_count >= 20:
        return False
    if any(f.path in high_risk_dirs for f in files_changed):
        return False
    if any(f.path.endswith("__init__.py") for f in files_changed):
        return False
    if any(f.is_new for f in files_changed):
        return False
    if any(f.severity == "critical" for f in wave_1_findings):
        return False
    return True
```

If skipped, note in the review: "Adversarial pass skipped — trivial PR
with no high-risk file changes and no Critical findings."

### Optimization 2: Strip Test Files from Non-Test Agents (Tier 1)

Test files are reviewed by Agent 4 (TEST). Other agents waste tokens
reading test content they don't analyze.

**Apply to context distribution in Stage 2c:**

```
Agent 1 (ARCH), 2 (QUAL), 3 (SEC), 5 (DX), 6 (STRUCT):
  - Receive: Full content of changed SOURCE files (application_sdk/**/*.py)
  - Receive: List of changed test files (file paths only, no content)

Agent 4 (TEST):
  - Receive: Full content of changed test files
  - Receive: Full content of corresponding source files (already covered)
```

This typically saves 20-30% input tokens since tests are often half the diff.

### Optimization 3: Reuse Phase 2 Holistic Assessment (Tier 1)

The holistic file assessment from Phase 2b (file annotations, dumping
ground detection, v3 replacement search, caller analysis) runs ONCE
and is shared across:
- All 6 Wave 1 agents
- The Wave 2 adversarial pass
- The auto-fix session (if triggered)
- Re-review iterations within 5 minutes (cached)

**Do NOT re-run grep/file-reads in subsequent stages.** The Phase 2b
output is the source of truth.

### Optimization 4: Smart File Truncation for Large Files (Tier 2)

For files exceeding 2000 lines, send a structured digest instead of
the full content:

**Format for large files:**

```
=== FILE: application_sdk/app/base.py (1629 lines) ===

[Truncated full-file view — see relevant sections below]

--- File Index (class/function map) ---
class App: lines 50-450
  def __init__: line 52
  def run: line 102
  ...
def _pascal_to_kebab: line 478
def _scan_entrypoints: line 1376
def _apply_app_registration: line 1415
def generate_workflow_class: line 1500
...

--- Section 1: Lines 1-100 (file header, imports, top-level definitions) ---
<actual content>

--- Section 2: Lines 442-588 (CHANGED SECTION + 100 lines context above/below) ---
<actual content of lines 342-688>

--- Section 3: Lines surrounding other touched lines ---
<each changed hunk + 100 lines context>

--- Available on request ---
Lines 689-1375 are not shown. If you need to see this section to
make a finding, output a request:
NEED_MORE_CONTEXT: file=application_sdk/app/base.py lines=689-900 reason=<why>

The orchestrator will fetch and re-prompt you with the requested section.
```

**Mitigation: agents can request more context.** If an agent's review
needs to verify cross-file dependencies in a truncated section:

1. Agent outputs at the END of its response (in addition to findings):
   ```
   NEED_MORE_CONTEXT:
   - file=path/to/file.py lines=500-700 reason=verifying caller pattern
   - file=other.py lines=1-200 reason=checking import context
   ```

2. Orchestrator detects these requests, fetches the requested sections,
   re-prompts the agent with ONLY the additional context (cached prefix
   stays valid), and merges the augmented findings.

3. Hard limit: max 2 follow-up requests per agent per review (prevents
   loops). After 2, the agent must finalize findings with available
   context.

**Result: 30-50% savings on large-file PRs with no quality loss** —
agents only spend tokens on context they actually need.

### Optimization 5: 1-Hour Cache TTL for Auto-Fix Loops (Tier 2)

When an auto-fix loop runs multiple iterations within ~1 hour, use
extended cache TTL so iterations 2-N reuse the cache from iteration 1.

```python
"cache_control": {"type": "ephemeral", "ttl": "1h"}
```

Apply to:
- Holistic Context Preamble (always identical)
- Reference doc content (changes only when references/*.md is edited)
- Phase 2b output for THIS PR (rebuilt only if files change between iters)

**Do NOT cache** the per-iteration changing parts (PR diff after fixes,
agent-specific instructions, output format). These vary per iteration.

### Cost Impact Summary

With Tier 1 + 2 applied:

| PR Type | Without Optimization | With Optimization | Savings |
|---------|----------------------|-------------------|---------|
| Trivial (5 lines, no high-risk files) | $1-2 | $0.30-0.50 | ~70% |
| Typical (10-20 files, ~1k lines) | $8-12 | $3-5 | ~60% |
| Large (50+ files, ~5k lines) | $15-25 | $6-10 | ~60% |
| Auto-fix loop (5 iter, large PR) | $75-125 | $25-45 | ~65% |
| Monthly (50 PRs, 25 with auto-fix) | $1500-2300 | $500-800 | ~65% |

**Quality is preserved.** No findings are lost — only redundant token
spend is eliminated. Agents can request more context if truncation
removed something they need.

Every agent prompt includes this **Holistic Context Preamble** (prepended
to the agent-specific prompt below):

```
## Holistic Context Protocol

BEFORE flagging any finding, check the file annotations:

1. If file is DUMPING_GROUND → recommend decomposition, not point fix.
2. If function has V3_REPLACEMENT_EXISTS → recommend migration:
   "Migrate N callers to <replacement> and delete this function."
3. If caller count <= 5 → recommend migrating callers over patching.
4. If file is DEPRECATED → only flag security issues.

For each finding, assign a fix SCOPE:
- PATCH: Fix in place, location is correct
- MIGRATE: v3 has a better API, move callers there
- REFACTOR: Code in wrong place or file needs decomposition
- DESIGN_CHANGE: Needs human architectural decision

## Confidence & Filtering

Rate each finding with a confidence score from 0 to 100.
Only report findings with confidence >= 80.
For structural/holistic opinions, only report >= 85.

Guardrail violations (G1-G8) are ALWAYS reported regardless of score.

## Fix Instructions Format

For PATCH scope findings, you MUST provide exact fix code in this format:

<!--FIX
{"file": "<path>", "line_start": N, "line_end": M,
 "action": "replace", "old_code": "...", "new_code": "...",
 "finding_id": "<TAG>-NNN"}
-->

For MIGRATE scope, provide the migration path:
<!--MIGRATE
{"file": "<path>", "function": "<name>",
 "callers": ["file:line", ...],
 "replacement": "<v3 API path>",
 "finding_id": "<TAG>-NNN"}
-->

DESIGN_CHANGE and REFACTOR scope findings do NOT get fix instructions —
they need human decisions.

## Output Format (REQUIRED — all agents use this)

### <Agent Name> Review Findings

#### Critical
- [<TAG>-NNN] `file:line` — description — scope: <SCOPE> — confidence: <N>/100
  <!--FIX or MIGRATE block here if PATCH/MIGRATE scope-->

#### Important
- [<TAG>-NNN] `file:line` — description — scope: <SCOPE> — confidence: <N>/100

#### Minor
- [<TAG>-NNN] `file:line` — description — scope: <SCOPE> — confidence: <N>/100

#### Holistic Recommendations
- [<TAG>] `file` — DUMPING_GROUND/V3_REPLACEMENT/REFACTOR recommendation

#### Alternative Approaches (only if confidence >= 90% that a clearly better approach exists)
- **Current:** <what the PR does> — **Better:** <alternative> — **Why:** <reason> — **Sketch:** <code>

#### Strengths
- <what the PR does well>
```

##### Agent 1: CORRECTNESS Review

**Covers:** Architecture (ADRs), Security, and Logical correctness.
**Domain tags it can emit:** [ARCH], [SEC], [BUG]

Each finding MUST tag its underlying domain so reviewers can tell at a
glance whether it's a security issue, an architecture violation, or a
logic bug.

Prompt template (after preamble):

```
You are a senior reviewer covering correctness for the Atlan application-sdk v3.
Your domain spans: architecture (ADRs), security, and logical correctness.

## PR Context
Title: {title}
Base: {base} → Head: {head}
Description: {body}

## Changed Files
{file_list}

## Full Diff
{diff}

## Holistic File Context
{file_annotations from Phase 2b}

## Architecture Rules
{content of references/v3-architecture-rules.md}

## Security Rules
{content of references/security-rules.md}

## Instructions

Review the changed code for correctness, architecture, and security issues.

For EVERY finding, you MUST tag the underlying domain so reviewers can
tell what kind of issue it is:
- [SEC]  — Security (secrets, injection, isolation, disclosure)
- [ARCH] — Architecture (ADR violations, contract evolution, infra abstraction)
- [BUG]  — Logical correctness (race conditions, missing edge cases, broken logic)

Check guardrails specifically:
- G1 (Security Blockers): Any Critical security finding? → [SEC] tag
- G2 (Contract Safety): Field removed/renamed/retyped? → [ARCH] tag
- G3 (Determinism): Non-deterministic ops in run()/@entrypoint? → [ARCH] tag
- G5 (Secret Safety): Hardcoded secrets, credentials in logs? → [SEC] tag

Mark guardrail violations explicitly: "GUARDRAIL G<N> VIOLATION".
Security findings are ALWAYS Critical or Important — never Minor.

For each finding:
1. State the file and line number
2. Tag the domain: [SEC] | [ARCH] | [BUG]
3. State which rule/ADR is violated (if applicable)
4. Explain why it matters (attack vector for SEC, what breaks for ARCH/BUG)
5. Assign scope: PATCH | MIGRATE | REFACTOR | DESIGN_CHANGE
6. Provide exact fix (for PATCH/MIGRATE scope)

Also note correctness strengths — what the PR does well.
```

##### Agent 2: QUALITY Review

**Covers:** Code quality, Tests, and Developer Experience.
**Domain tags it can emit:** [QUAL], [TEST], [DX]

Each finding MUST tag its underlying domain.

Prompt template (after preamble):

```
You are a senior reviewer covering quality for the Atlan application-sdk v3.
Your domain spans: code quality, test coverage, and developer experience.

## PR Context
Title: {title}
Base: {base} → Head: {head}
Description: {body}

## Changed Files
{file_list}

## Full Diff
{diff}

## Holistic File Context
{file_annotations from Phase 2b}

## Code Quality Rules
{content of references/code-quality-rules.md}

## Test Quality Rules
{content of references/test-quality-rules.md}

## Developer Experience Rules
{content of references/dx-rules.md}

## Additional Standards
- From docs/standards/review-checklist.md Phase 2:
  - Max 300 lines per class, max 75 lines per function
  - Max 7 parameters per function, max 3 nested conditionals
  - No commented-out code, no print(), no if False:
- From docs/agents/testing.md:
  - Target: 85% coverage for NEW code

## Instructions

Review the changed code for quality, tests, and DX issues.

For EVERY finding, you MUST tag the underlying domain:
- [QUAL] — Code quality (imports, logging, naming, size, complexity)
- [TEST] — Test quality (coverage, patterns, isolation, edge cases)
- [DX]   — Developer experience (API ergonomics, errors, migration, discoverability)

Check guardrails specifically:
- G4 (Test Coverage): New public API without tests → [TEST] tag
- G6 (Breaking API): Removed/changed export without deprecation → [DX] or [QUAL] tag

For [TEST] findings, check:
- Tests use SDK-provided mocks (MockStateStore etc), not custom unittest.mock
- Tests use clean_app_registry fixture
- No @pytest.mark.asyncio (redundant with asyncio_mode="auto")
- Specific assertions, not assert x
- Heartbeat-enabled tasks have MockHeartbeatController tests
- Edge cases covered (empty, None, boundary values)

For [DX] findings, ask: "If I'm building a connector with this SDK, does
this PR make my life better, worse, or the same?"

For [QUAL] findings, focus on:
- Imports inside functions, f-strings in logs, print() statements
- Function/class/parameter/nesting size violations
- Deprecation patterns

For each finding:
1. State the file and line number
2. Tag the domain: [QUAL] | [TEST] | [DX]
3. State which rule is violated
4. Explain why it matters
5. Assign scope and provide exact fix

Also note quality strengths.
```

##### Agent 3: STRUCTURE Review

**Covers:** Holistic / structural concerns — symptoms vs causes, file health,
design coherence, technical debt, dependency direction.
**Domain tags it can emit:** [STRUCT]

Higher confidence bar: 85% (structural opinions need more certainty).

Prompt template (after preamble):

```
You are a holistic reviewer for the Atlan application-sdk v3.
You look at the big picture, not individual lines. Your job is to catch
the things line-level reviewers miss.

## PR Context
Title: {title}
Base: {base} → Head: {head}
Description: {body}

## Changed Files
{file_list}

## Full Diff
{diff}

## Holistic File Context
{file_annotations from Phase 2b}

## Full Files for Changed Modules
{full file contents from Phase 2a}

## Structural Rules
{content of references/structural-rules.md}

## Instructions

Review structural and design concerns. Use confidence threshold >= 85.

Tag every finding [STRUCT].

1. **Symptoms vs Causes (STRUCT-001):**
   Is the PR fixing the RIGHT thing? Would this fix need to be re-done
   when the root cause is addressed?

2. **File Health (STRUCT-002, STRUCT-003):**
   Are modified files over size thresholds? Is new code going into a
   dumping ground? Should it go somewhere else?

3. **Design Coherence (STRUCT-004):**
   Does this PR introduce a second way to do something? Mixed abstraction?

4. **Tech Debt (STRUCT-005):**
   TODOs without issue refs? Net debt increase or decrease?

5. **Dependency Direction (STRUCT-006):**
   Do imports respect app → execution → infrastructure?

6. **v3 Replacement (STRUCT-007):**
   Is v2 code being patched when callers should migrate to v3?

For each finding: the structural concern, why it matters long-term,
the fix scope (PATCH/MIGRATE/REFACTOR/DESIGN_CHANGE), and what the
holistic solution looks like.

**REQUIRED OUTPUT: Root Cause Assessment**

#### Root Cause Assessment
- PR intent: [what the PR is trying to do]
- Classification: CAUSES | SYMPTOMS | MIXED
- If SYMPTOMS: Root cause is [X]. The right fix is [Y].
- Debt impact: INCREASES | DECREASES | NEUTRAL
- Net line delta: +/-N

Also note structural strengths.
```

#### 2e. Wave 2: Cross-Model Adversarial Review

After Wave 1 completes, call GPT-5.3-codex via the LiteLLM proxy.

```bash
# Build prompt with Wave 1 findings + PR context
cat > /tmp/adversarial_prompt.json << 'PROMPT_END'
{
  "model": "gpt-5.3-codex",
  "temperature": 0.2,
  "max_tokens": 12000,
  "messages": [
    {"role": "system", "content": "You are an adversarial code reviewer..."},
    {"role": "user", "content": "<Wave 1 consolidated findings + diff + annotations>"}
  ]
}
PROMPT_END

response=$(curl -s --max-time 120 \
  "${ANTHROPIC_BASE_URL:-https://llmproxy.atlan.dev}/v1/chat/completions" \
  -H "Authorization: Bearer $LITELLM_API_KEY" \
  -H "Content-Type: application/json" \
  -d @/tmp/adversarial_prompt.json)
```

The adversarial system prompt:

```
You are an adversarial code reviewer providing an independent second
opinion on a PR for the Atlan application-sdk. A different AI model
(Claude Opus) has reviewed this PR with 6 domain-specific agents.
Your job is to reduce model bias and catch blind spots.

## Task 1: CHALLENGE Opus Findings

For EACH finding from the Opus review, state:
- AGREE (confidence N/100): Finding is valid. [brief reason]
- DISAGREE (confidence N/100): Finding is a false positive or model
  bias. [explain why — what context did Opus miss?]
- PARTIAL (confidence N/100): Finding has merit but severity is wrong.
  [what severity should it be and why?]

## Task 2: DISCOVER New Findings

Do your own independent review of the diff. Focus on:
- Logic errors Opus may have rationalized away
- Security issues requiring adversarial thinking (attack scenarios)
- Test gaps only visible with full file context
- Race conditions, resource leaks, timeout issues
- Patterns that will break under production load

For each new finding use the same format:
[TAG-NNN] file:line — description — scope — confidence: N/100

## Task 3: HOLISTIC Assessment

- Is this PR treating symptoms or causes?
- Should any PATCH findings be MIGRATE or DESIGN_CHANGE instead?
- Does the PR introduce a "second way" to do something that already
  has a canonical pattern?
- Would you approve this PR? Why or why not?

## Output Format

### Cross-Model Adversarial Review

#### Challenges to Opus Findings
- [ARCH-001] AGREE (95) — valid, ADR-0005 is clearly violated
- [QUAL-003] DISAGREE (88) — this is an acceptable pattern because...
- [SEC-001] PARTIAL (82) — real issue but Important not Critical because...

#### New Findings (Opus missed)
- [ADV-001] `file:line` — description — scope: PATCH — confidence: 92/100
- [ADV-002] `file:line` — description — scope: MIGRATE — confidence: 88/100

#### Holistic Assessment
- Symptoms vs causes: [CAUSES | SYMPTOMS | MIXED]
- Escalation recommendations: [any findings that should be DESIGN_CHANGE?]
- Overall quality: [1 sentence]
```

If the API call fails or times out → skip gracefully:
"Cross-model review skipped (adversarial model unavailable). Single-model review."

#### 2f. De-bias & Consolidate

**Cross-model agreement filter:**

| Opus | Codex | Action |
|------|-------|--------|
| Flagged >= 90% confidence | AGREE or not reviewed | Keep |
| Flagged >= 80% confidence | AGREE | Keep |
| Flagged >= 80% confidence | DISAGREE | Drop (model bias) |
| Flagged >= 80% confidence | PARTIAL | Keep, downgrade severity by 1 |
| Not flagged | Codex flags >= 90% | Keep (blind spot) |
| Not flagged | Codex flags < 90% | Drop |
| **Guardrail violation** | **Any** | **Always keep** |

If adversarial review was skipped → keep all Opus findings >= 80%.

**Deduplication:** Same file:line from multiple agents → keep most severe,
merge descriptions, note which agents agreed.

**Finding ID generation:**
Each finding gets a deterministic ID: `hash(tag + file + line + summary_first_50_chars)`.
This allows stable tracking across re-reviews. Use a short hex hash (8 chars).
Example: `ARCH-a3f2b901`, `SEC-9c1e44d7`.

**Delta tracking** (if previous review exists):
- Finding in previous review, code changed at that line → `RESOLVED`
- Finding in previous review, code unchanged → `STILL PRESENT`
- New finding not in previous review → `NEW`
- New finding at a location that was changed by the auto-fix session →
  `REGRESSION` (introduced by fix iteration N). Flag prominently:
  "This issue was introduced by the auto-fix in iteration N."

---

### Stage 3: Post Review

#### 3a. Summary Comment

Post new comment (first review) or PATCH existing comment (re-review).

Focus the comment on CURRENT STATUS — what needs resolving now.
Keep iteration history collapsed and minimal.

```markdown
<!-- SDK_REVIEW_V2 -->
## SDK Review: PR #<number> — <title>

### Verdict: READY TO MERGE | NEEDS FIXES | BLOCKED | NEEDS HUMAN REVIEW

> <2-3 sentence summary of current status>

---

### Guardrail Violations (if any)
- [G1] BLOCKED: <description>

### Findings by File

Group ALL findings by the file they apply to (not by severity). Within
each file, order findings by line number ascending. Each line keeps its
domain tag and severity so reviewers can quickly tell what kind of issue
it is and how urgent.

**`<path/to/file.py>`**
- **Critical** [SEC] L42 — Credential leaked into log message. Wrap in redactor.
- **Critical** [ARCH] L88 — Field `count` removed from `FetchOutput` (G2 violation). Restore as deprecated alias.
- **Important** [QUAL] L156 — `dir(cls)` walks inherited attributes; switch to `cls.__dict__`.
- **Minor** [DX] L200 — Parameter name `t` is unclear; use `timeout_seconds` for consistency.

**`<path/to/another.py>`**
- **Important** [TEST] L12 — New `@task` method `fetch_metrics` lacks tests.
- **Minor** [QUAL] L78 — f-string in log call; switch to %-style.

### Holistic Recommendations (if any)
- [STRUCT] Root cause: <what the real fix looks like>

### Needs Human Review (if any DESIGN_CHANGE scope)
- **Why:** <explanation>
- **Decision needed:** <what the human should decide>

### Strengths
- <merged from all agents>

<details>
<summary>Review History</summary>

| Finding | Status |
|---------|--------|
| [previous] | RESOLVED |
| [previous] | STILL PRESENT |
| [new] | NEW |

</details>

<details>
<summary>Review Methodology</summary>

- **Models:** Claude Opus 4.6 (3 agents) + GPT-5.3-codex (adversarial)
- **Cross-model agreement:** X/Y confirmed by both
- **Dropped (bias):** Z removed
- **Guardrails:** G1-G8 checked

</details>

---

**CI:** all passing | N failing: <list>
**Branch:** up to date | behind | conflicts
**Stats:** X findings (Y critical, Z important, W minor)

<!-- REVIEW_DATA
{"version": 2, "reviewed_commit": "<sha>",
 "verdict": "READY_TO_MERGE|NEEDS_FIXES|BLOCKED|NEEDS_HUMAN",
 "auto_fix": false, "iteration": 1,
 "guardrail_violations": [],
 "findings": [
   {"id": "<hash>", "tag": "ARCH", "severity": "critical",
    "file": "<path>", "line": N, "scope": "PATCH",
    "summary": "<one-line>", "status": "open|resolved",
    "fix": {"action": "replace", "old_code": "...", "new_code": "..."}}
 ],
 "overrides": [],
 "previous_review_id": null}
-->
```

#### 3b. Inline Comments

For each finding (max 15, prioritized: guardrail violations first,
then Critical, then Important), post inline PR comments:

```bash
gh api repos/atlanhq/application-sdk/pulls/<PR>/comments \
  -f body="<comment_body>" \
  -f commit_id="<HEAD_SHA>" \
  -f path="<file>" \
  -F line=<line> \
  -f side="RIGHT"
```

Each inline comment format:

```markdown
**[SEVERITY]** [TAG] — Rule Reference

<Description>

**Scope:** PATCH | MIGRATE | REFACTOR | DESIGN_CHANGE
**Confidence:** Both models agree | Single-model

<For PATCH scope, small fixes (< 6 lines):>
```suggestion
<exact replacement code>
```

<For MIGRATE scope:>
**Migration path:** Callers at `file_a.py:L42`, `file_b.py:L87` should
use `application_sdk.path.to.v3_replacement` instead. Then delete this.

<For DESIGN_CHANGE scope:>
**Needs human decision:** <trade-off explanation>
```

Rules:
- One comment per unique finding
- Suggestion blocks ONLY for PATCH scope under 6 lines
- Never post a suggestion that wouldn't pass pre-commit
- Group findings within 10 lines of each other in the same file
- Reply "Addressed in latest push" to resolved inline comments

#### 3b-bis. Handle Inline Comment Replies (Q&A)

The skill must handle two cases on inline comments it previously posted:

**Case 1: Finding is now resolved (code changed at that line)**

Post a threaded reply to the original inline comment:

```bash
gh api repos/atlanhq/application-sdk/pulls/<PR>/comments/<comment_id>/replies \
  -f body="Addressed in latest push (commit <short-sha>). Marking resolved."
```

Then mark the conversation as resolved using GraphQL (REST has no resolve API):

```bash
gh api graphql -f query='
  mutation($threadId: ID!) {
    resolveReviewThread(input: {threadId: $threadId}) {
      thread { isResolved }
    }
  }' -F threadId="<thread_id>"
```

To get the `thread_id`:
```bash
gh api graphql -f query='
  query($owner: String!, $repo: String!, $pr: Int!) {
    repository(owner: $owner, name: $repo) {
      pullRequest(number: $pr) {
        reviewThreads(first: 100) {
          nodes {
            id
            isResolved
            comments(first: 1) { nodes { id databaseId path line } }
          }
        }
      }
    }
  }' -F owner=atlanhq -F repo=application-sdk -F pr=<PR>
```

Match by `path` + `line` to the finding being resolved.

**Case 2: Author replied to an inline comment with a question or pushback**

Every review run, list all inline comments the bot previously posted
that have a NEW reply since the last review:

```bash
# Get all review comments + their threading
gh api repos/atlanhq/application-sdk/pulls/<PR>/comments \
  --jq '.[] | select(.user.login == "github-actions[bot]")'
```

For each bot comment, check if there's a reply newer than the
`reviewed_commit` SHA from previous REVIEW_DATA:

```bash
gh api repos/atlanhq/application-sdk/pulls/<PR>/comments \
  --jq '.[] | select(.in_reply_to_id == <bot_comment_id>) |
              select(.created_at > "<previous_review_timestamp>")'
```

If there's a new reply from the author (not the bot itself), spawn a
small "answer agent" to handle it:

```
You are responding to a question on an SDK review inline comment.

## Original finding (your previous comment)
File: <file>:<line>
Comment: <bot's original comment>

## Author's reply
"<author's question or pushback>"

## File context (full file content)
<file content>

## Diff context (the relevant hunk)
<hunk>

## Your job

1. If the author asked a clarifying question → answer it directly,
   referencing specific lines/concepts in the code.
2. If the author pushed back saying the finding is wrong → re-evaluate.
   - If they're right (you missed context) → reply: "You're right,
     this is correct because <reason>. Removing the finding." Then
     mark this finding as RESOLVED in REVIEW_DATA.
   - If they're partially right → reply with the nuance: "Agreed on X,
     but Y still holds because <reason>."
   - If they're wrong → explain why, citing the specific concern that
     stands.
3. If the author proposed an alternative fix → evaluate it. If it
   addresses the concern, agree and update the suggestion. If not,
   explain what's still missing.
4. If the author just acknowledged ("ok will fix") → do nothing,
   leave the comment open until the code actually changes.

Keep the reply conversational and concise (2-4 sentences typically).
Cite specific lines/concepts. Don't repeat the original finding verbatim.
```

Post the reply:

```bash
gh api repos/atlanhq/application-sdk/pulls/<PR>/comments/<bot_comment_id>/replies \
  -f body="<answer-agent-output>"
```

If the answer agent decided the finding was wrong (author was right),
also remove it from REVIEW_DATA in the next summary update.

#### 3c. Set Commit Status

```bash
# Determine state based on verdict
state="failure"  # default
description="SDK Review: NEEDS FIXES"

if verdict == "READY_TO_MERGE"; then
  state="success"
  description="SDK Review: Ready to merge"
fi

gh api repos/atlanhq/application-sdk/statuses/<HEAD_SHA> \
  -f state="$state" \
  -f context="sdk-review" \
  -f description="$description (X critical, Y important)" \
  -f target_url="<link to review comment>"
```

#### 3d. Label Management

- If verdict = NEEDS HUMAN REVIEW:
  ```bash
  gh label create "needs-human-review" --color "d93f0b" --force 2>/dev/null
  gh pr edit <PR> --add-label "needs-human-review"
  ```
- If previous review had `needs-human-review` and it's now resolved:
  ```bash
  gh pr edit <PR> --remove-label "needs-human-review" 2>/dev/null || true
  ```
- Never leave stale labels.

---

### Stage 4: Auto-Fix (resolve all only)

Only runs if `auto_fix = true` AND verdict is NEEDS FIXES.
Does NOT run if verdict is BLOCKED or NEEDS HUMAN REVIEW.

**This is a SEPARATE Claude Code session** — not the review session.
The review session ends after Stage 3. A new claude-code-action step
starts for fixes.

The fix session receives:
- The review comment (containing REVIEW_DATA with exact fix instructions)
- Instructions to follow TDD: brainstorm → plan → implement → validate

Fix session prompt:
```
You are a code fixer for the Atlan application-sdk. A review has been
posted on this PR with exact fix instructions.

IMPORTANT RULES:
- Follow CLAUDE.md commit conventions (Conventional Commits format)
- NEVER add Co-Authored-By lines to commits
- NEVER introduce new issues — verify each fix doesn't break existing
  behavior before moving to the next one
- Fix ALL findings (Critical, Important, AND Minor) with scope PATCH
  or MIGRATE. Minor/"nice to have" findings are included — resolve them all.
- SKIP all REFACTOR and DESIGN_CHANGE scope findings (need human decisions)

PROCESS:

1. Read the review comment containing <!-- REVIEW_DATA ... -->

2. Parse ALL findings with status "open" and scope "PATCH" or "MIGRATE"
   (all severities — Critical, Important, and Minor)

3. For each PATCH finding (ordered by severity: Critical first):
   a. Read the full file for context — understand WHY this fix is needed
   b. Plan: does the fix make sense in context? Will it break anything?
   c. Apply the exact code change from the FIX instruction block
   d. Run: uv run pre-commit run --files <changed_file>
   e. Run: uv run pytest <corresponding_test_file> -x (if exists)
   f. Verify the fix doesn't introduce new lint errors or type errors
   g. If tests fail or new issues appear → revert ONLY this fix, note why

4. For each MIGRATE finding:
   a. Read all caller files listed in the MIGRATE block
   b. Plan: which callers to update, what replacement to use
   c. Update each caller to use the v3 replacement
   d. If the old function has no remaining callers, delete it
   e. Run pre-commit on all changed files
   f. Run tests for all affected modules
   g. If tests fail → revert, note why

5. After all fixes applied, run verification:
   a. Pre-commit on all changed files:
      uv run pre-commit run --files <all_changed_files>
      If pre-commit fails, fix the issues (formatting, lint) before committing.
   b. Full unit test suite:
      uv run pytest tests/unit/ -x --timeout=120
      If any test fails that passed before → identify which fix caused it
      and revert that specific fix.
   c. Type check (if pyright is available):
      uv run pyright <changed_files> 2>/dev/null || true
      Fix any type errors your changes introduced.
   d. Note: Other CI checks (CodeQL, trivy, integration tests) run
      automatically when you push. The pipeline gets ONE retry if CI
      fails, then stops. So verify thoroughly before committing —
      your goal is zero CI failures after push.

6. Stage and commit (specific files only, never git add -A):
   git add <specific changed files>
   git commit -m "fix(review): address SDK review findings (iteration N/M)"

7. Push:
   git push origin <branch>

Do NOT change anything beyond what the review specifies.
Do NOT add new features, refactor, or "improve" anything.
Do NOT add Co-Authored-By lines.
Do NOT modify files the PR doesn't touch.
```

After the fix session completes, trigger the next review iteration
by posting a comment (which triggers a NEW GitHub Action run = fresh
review session with zero context bias):

```bash
gh pr comment <PR> --body "@sdk-review --iteration=$((N+1)) --max=$MAX"
```

#### Safety Rails

1. **Max 3 iterations.** After 3:
   - Post: "Max iterations reached. N findings remain."
   - Add label: `needs-human-review`
   - STOP

2. **DESIGN_CHANGE findings are NEVER auto-fixed.**
   After 2 iterations with same DESIGN_CHANGE → add `needs-human-review`.

3. **REFACTOR findings are NEVER auto-fixed.**
   They require structural changes beyond point fixes.

4. **PR closed/merged during loop → STOP.**

5. **CI fails after fix push → ONE retry, then STOP.**
   The fix session should verify locally (pre-commit + unit tests) BEFORE
   committing. But if CI still fails after push:
   - First occurrence: trigger ONE more iteration to attempt fixing the
     CI failure (the re-review will flag it as a REGRESSION finding).
   - Second consecutive CI failure: STOP. Post: "CI failing after 2
     consecutive fix attempts. Human investigation needed: <failing checks>"
     Add label: `needs-human-review`.

6. **Push fails (branch updated externally) → STOP.**
   Post: "Branch was updated externally. Re-run @sdk-review."

7. **NEVER auto-fix files the PR doesn't touch.**

8. **NEVER force-push.**

9. **NEVER add Co-Authored-By lines** (per CLAUDE.md).

10. **Each fix commit uses Conventional Commits format:**
    `fix(review): address SDK review findings (iteration 2/5)`

11. **Each agent has a 10-minute timeout.** If an agent doesn't return
    within 10 minutes, skip it and note: "Agent [TAG] timed out."
    Continue with findings from the other agents.

---

### Stage 5: Finalize (READY TO MERGE)

Runs when verdict = READY TO MERGE (from any invocation — review-only
or auto-fix loop).

#### 5a. Update Branch
```bash
gh api repos/atlanhq/application-sdk/pulls/<PR>/update-branch \
  -X PUT -f update_method=merge 2>/dev/null || true
```

#### 5b. Resolve Any New Conflicts
If update-branch created conflicts:
```bash
git fetch origin <base> <head>
git checkout <head>
git merge origin/<base> --no-edit && git push || {
  git merge --abort
  # Can't auto-resolve — note it but don't block
  gh pr comment <PR> --body "Branch updated but new conflicts appeared. Manual rebase needed."
}
```

#### 5c. Check CI
```bash
# Wait for CI to start on updated branch, then poll (max 10 min)
sleep 15
gh pr checks <PR> --repo atlanhq/application-sdk --required --watch \
  --fail-fast 2>/dev/null || CI_STATUS="failing"
```

If CI fails after branch update → do NOT approve. Set status = failure.
Post note: "CI failing after branch update: <failing checks>. Not approving."

If CI doesn't complete within 10 minutes → do NOT approve. Set status = pending.
Post note: "CI still running after branch update. Will approve once CI passes.
Re-run @sdk-review after CI completes."

#### 5d. Approve
```bash
gh pr review <PR> --repo atlanhq/application-sdk --approve \
  --body "SDK Review v2: All checks passed. Approved."
```

#### 5e. Label
```bash
gh label create "sdk-review-approved" --color "0e8a16" --force 2>/dev/null
gh pr edit <PR> --add-label "sdk-review-approved"

# Only add auto-maintained label if this was a "resolve all" flow.
# This enables the branch-keeper to keep the PR up-to-date post-approval.
# Regular @sdk-review (review-only) PRs do NOT get auto-maintenance.
if [ "$auto_fix" = "true" ]; then
  gh label create "sdk-review-auto-maintained" --color "1d76db" --force 2>/dev/null
  gh pr edit <PR> --add-label "sdk-review-auto-maintained"
fi

# Clean up any lingering labels
gh pr edit <PR> --remove-label "needs-human-review" 2>/dev/null || true
gh pr edit <PR> --remove-label "needs-rebase" 2>/dev/null || true
```

#### 5f. Final Status
```bash
gh api repos/atlanhq/application-sdk/statuses/<HEAD_SHA> \
  -f state=success \
  -f context=sdk-review \
  -f description="SDK Review: Approved. Ready to merge."
```

---

## Tag Mapping

| Agent | Tag | Focus |
|-------|-----|-------|
| Architecture (Opus) | `[ARCH]` | ADRs, contracts, abstraction, determinism |
| Code Quality (Opus) | `[QUAL]` | Imports, logging, naming, size, nesting |
| Security (Opus) | `[SEC]` | Secrets, injection, isolation, disclosure |
| Test Quality (Opus) | `[TEST]` | Coverage, patterns, isolation, edge cases |
| Developer Experience (Opus) | `[DX]` | API ergonomics, errors, migration, DX |
| Holistic & Structural (Opus) | `[STRUCT]` | Symptoms/causes, file health, design |
| Adversarial (GPT-5.3-codex) | `[ADV]` | Cross-model blind spots |

## Fix Scope Legend

| Scope | Meaning | Auto-fixable? |
|-------|---------|---------------|
| `PATCH` | Fix in place, location is correct | Yes |
| `MIGRATE` | v3 has better API, move callers | Yes |
| `REFACTOR` | Wrong location, decompose file | No |
| `DESIGN_CHANGE` | Needs human architectural decision | No |

## Verdict Rules

| Verdict | Condition |
|---------|-----------|
| BLOCKED | Any guardrail G1, G2, G3, G5 violated |
| NEEDS HUMAN REVIEW | Any DESIGN_CHANGE scope OR SYMPTOMS root-cause |
| NEEDS FIXES | Any Critical finding OR G4/G6 violated OR 3+ Important OR CI failing OR unresolvable conflicts |
| READY TO MERGE | No Critical, < 3 Important, CI passing, branch mergeable |
