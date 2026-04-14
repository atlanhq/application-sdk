---
name: sdk-review
description: Review PRs and code changes against application-sdk v3 architecture (ADRs, contracts, security, test quality, code quality). Invoke with /sdk-review <PR-number-or-URL>. Dispatches 4 parallel review agents and produces a consolidated report.
---

# SDK v3 Code Review

Review PRs against the application-sdk v3 architecture, design principles, and all 11 ADRs.

## Input

The user provides a PR number or URL. Extract the PR number and repository.

## Step 1: Fetch PR Context

Run these in parallel:

```bash
gh pr view <PR> --repo atlanhq/application-sdk --json number,title,body,baseRefName,headRefName,files
gh pr diff <PR> --repo atlanhq/application-sdk
```

Also read these reference files from the skill directory — they contain the review rules each agent must enforce:

- `references/v3-architecture-rules.md`
- `references/code-quality-rules.md`
- `references/security-rules.md`
- `references/test-quality-rules.md`

## Step 2: Dispatch 4 Review Agents in Parallel

Launch **all 4 agents simultaneously** using the Agent tool. Each agent receives:
- The full PR diff
- The PR file list
- Their specific reference document (read it and include its content in the agent prompt)
- The PR title and description for context

### Agent 1: Architecture Review

Dispatch with `subagent_type: "general-purpose"`.

Prompt template:

```
You are an architecture reviewer for the Atlan application-sdk v3.

## PR Context
Title: {title}
Base: {base} → Head: {head}
Description: {body}

## Changed Files
{file_list}

## Full Diff
{diff}

## Architecture Rules
{content of references/v3-architecture-rules.md}

## Instructions

Review ONLY the changed code in this diff against the architecture rules above. For each violation:

1. State the file and line number
2. State which rule/ADR is violated
3. Explain why it matters
4. Suggest the fix

Rate each finding: Critical (blocks merge), Important (should fix), Minor (nice to have).
Only report findings with confidence >= 80/100.

**Alternative approaches:** If the PR's overall design approach for a feature has a clearly better alternative (confidence >= 90%), add an `#### Alternative Approaches` section. Only suggest alternatives when you are confident the current approach is fundamentally wrong or significantly inferior — not for minor stylistic preferences. Each suggestion must include: what the current approach is, why it's suboptimal, what the better approach is, and a concrete code sketch.

Also note architectural strengths — what the PR does well.

Output format:
### Architecture Review Findings

#### Critical
- [ADR-XXXX] `file:line` — description — fix

#### Important
- ...

#### Minor
- ...

#### Alternative Approaches (only if clearly better approach exists)
- **Current:** <what the PR does> — **Better:** <what it should do instead> — **Why:** <concrete reason> — **Sketch:** <code example>

#### Strengths
- ...
```

### Agent 2: Code Quality Review

Dispatch with `subagent_type: "general-purpose"`.

Prompt template:

```
You are a code quality reviewer for the Atlan application-sdk v3.

## PR Context
Title: {title}
Base: {base} → Head: {head}
Description: {body}

## Changed Files
{file_list}

## Full Diff
{diff}

## Code Quality Rules
{content of references/code-quality-rules.md}

## Instructions

Review ONLY the changed code in this diff against the code quality rules above. For each violation:

1. State the file and line number
2. State which rule is violated
3. Explain why it matters
4. Suggest the fix

Rate each finding: Critical (blocks merge), Important (should fix), Minor (nice to have).
Only report findings with confidence >= 80/100.

Pay special attention to:
- Imports inside functions (must be at module top-level unless heavy lazy-import)
- f-strings in log calls (must use %-style)
- print() statements
- Import ordering (isort)

**Alternative approaches:** If the PR's implementation pattern for a feature has a clearly better alternative (confidence >= 90%), add an `#### Alternative Approaches` section. Only suggest when the current approach is fundamentally wrong or significantly inferior — not for minor preferences. Include: what the current approach is, why it's suboptimal, the better approach, and a code sketch.

Also note quality strengths — what the PR does well.

Output format:
### Code Quality Review Findings

#### Critical
- [RULE] `file:line` — description — fix

#### Important
- ...

#### Minor
- ...

#### Alternative Approaches (only if clearly better approach exists)
- **Current:** <what the PR does> — **Better:** <what it should do instead> — **Why:** <reason> — **Sketch:** <code example>

#### Strengths
- ...
```

### Agent 3: Security Review

Dispatch with `subagent_type: "general-purpose"`.

Prompt template:

```
You are a security reviewer for the Atlan application-sdk v3.

## PR Context
Title: {title}
Base: {base} → Head: {head}
Description: {body}

## Changed Files
{file_list}

## Full Diff
{diff}

## Security Rules
{content of references/security-rules.md}

## Instructions

Review ONLY the changed code in this diff against the security rules above. For each violation:

1. State the file and line number
2. State which rule is violated
3. Explain the attack vector or risk
4. Suggest the fix

Rate each finding: Critical (blocks merge), Important (should fix), Minor (nice to have).
Only report findings with confidence >= 80/100.

Security issues are always Critical or Important — never Minor.

**Alternative approaches:** If the PR's security design has a clearly better alternative (confidence >= 90%), add an `#### Alternative Approaches` section. Only suggest when the current approach is fundamentally insecure or significantly inferior — not for hardening preferences. Include: what the current approach is, the security risk, the better approach, and a code sketch.

Also note security strengths — what the PR does well.

Output format:
### Security Review Findings

#### Critical
- [SEC] `file:line` — description — attack vector — fix

#### Important
- [SEC] `file:line` — description — risk — fix

#### Alternative Approaches (only if clearly better approach exists)
- **Current:** <what the PR does> — **Better:** <what it should do instead> — **Why:** <security reason> — **Sketch:** <code example>

#### Strengths
- ...
```

### Agent 4: Test Quality Review

Dispatch with `subagent_type: "general-purpose"`.

Prompt template:

```
You are a test quality reviewer for the Atlan application-sdk v3.

## PR Context
Title: {title}
Base: {base} → Head: {head}
Description: {body}

## Changed Files
{file_list}

## Full Diff
{diff}

## Test Quality Rules
{content of references/test-quality-rules.md}

## Instructions

Review ONLY the changed code in this diff. Check:
1. Do new/changed modules have corresponding tests?
2. Do the tests follow v3 patterns (in-memory infra, clean_app_registry, async)?
3. Are edge cases and error paths covered?
4. Are assertions specific and meaningful?
5. Is test isolation maintained (no shared state leaking)?

For each finding:
1. State the file and line number
2. State which rule is violated
3. Explain why it matters
4. Suggest the fix (with example test code if applicable)

Rate each finding: Critical (blocks merge), Important (should fix), Minor (nice to have).
Only report findings with confidence >= 80/100.

Missing tests for new functionality is always Critical.

**Alternative approaches:** If the PR's testing strategy has a clearly better alternative (confidence >= 90%), add an `#### Alternative Approaches` section. Only suggest when the current test design is fundamentally flawed or significantly inferior — not for minor preferences. Include: what the current approach is, why it's suboptimal, the better approach, and example test code.

Also note testing strengths — what the PR does well.

Output format:
### Test Quality Review Findings

#### Critical
- [TEST] `file:line` — description — fix

#### Important
- ...

#### Minor
- ...

#### Alternative Approaches (only if clearly better approach exists)
- **Current:** <what the PR does> — **Better:** <what it should do instead> — **Why:** <reason> — **Sketch:** <example test code>

#### Strengths
- ...
```

## Step 3: Consolidate

After all 4 agents return, merge their findings into a single report. Follow this format exactly:

```markdown
## SDK Review: PR #<number> — <title>

### Verdict: READY TO MERGE | NEEDS FIXES | BLOCKED

> <2-3 sentence summary of overall quality and what the PR does>

---

### Critical (must fix before merge)
- [ARCH] `file:line` — description — fix
- [SEC] `file:line` — description — fix
- [QUAL] `file:line` — description — fix
- [TEST] `file:line` — description — fix

### Important (should fix)
- ...

### Minor (nice to have)
- ...

### Alternative Approaches (only if any agent flagged one)
- **[TAG]** **Current:** <what the PR does> — **Better:** <alternative> — **Why:** <reason>

### Strengths
- <merged strengths from all agents>

---

**Stats:** X total findings (Y critical, Z important, W minor)
**Verdict rationale:** <why this verdict>
```

### Alternative Approaches consolidation:
If any agent included an Alternative Approaches section, include it in the consolidated report. Only carry forward suggestions where the agent expressed >= 90% confidence that the alternative is clearly better. Do NOT include stylistic preferences or "could also do X" suggestions — only cases where the reviewer believes the current approach is fundamentally wrong or significantly inferior.

### Verdict rules:
- **BLOCKED**: Any Critical security finding
- **NEEDS FIXES**: Any Critical finding (non-security) OR 3+ Important findings
- **READY TO MERGE**: No Critical findings AND fewer than 3 Important findings

### Deduplication:
If multiple agents flag the same file:line, keep the most severe categorization and merge the descriptions.

### Tag mapping:
- Architecture agent findings → `[ARCH]`
- Code Quality agent findings → `[QUAL]`
- Security agent findings → `[SEC]`
- Test Quality agent findings → `[TEST]`
