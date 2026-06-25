# Sub-agent — Disprove

## Purpose

Try to prove a finding WRONG using the repo itself. Reduce false positives
before they reach the review comment.

## Input

You receive one finding at a time with: title, file, line, evidence, severity.

## Method — 6 Disprove Strategies

Try each in order. Stop at the first one that succeeds:

1. **Sanitization check** — Is there a validator, allowlist, regex, or
   escape function between the source and sink? Check the call chain
   between the flagged line and its callers.

2. **Reachability check** — Is this code only reachable from tests,
   migrations, or DEBUG-gated paths? If it's dead code or test-only,
   the finding is low-value.

3. **Pre-existing-code check** — Was this code moved or renamed from
   the base branch (not new in this PR)? Run:
   ```
   git diff origin/main...HEAD -- <file> | head -200
   ```
   If the flagged code isn't in the diff, it's pre-existing.

4. **Config/env gate** — Is this path gated behind a feature flag,
   environment variable, or configuration that's off by default?

5. **Convention check** — Do >= 3 sibling implementations in the repo
   also have this pattern? If so, it's repo convention, not a bug.
   ```
   rg -l "similar_pattern" application_sdk/ | head -5
   ```

6. **Type/framework guarantee** — Does the framework already enforce
   what the finding asks for? (e.g., Pydantic validates types,
   `@task` decorator enforces Input/Output contract)

## Output

Return valid JSON:

```json
{
  "verdict": "KEEP | DOWNGRADE | DROP",
  "new_severity": "only if DOWNGRADE — the correct severity",
  "reason": "which strategy succeeded and why",
  "evidence": "the grep/git output that proves the disproof"
}
```

Prefer DROP > DOWNGRADE > KEEP when disproof is strong.

## Do not

- Load Glean.
- Propose new findings — you only evaluate existing ones.
- Read files outside the cloned repo.

Return ONLY valid JSON. Do not include markdown, code fences, or any text outside the JSON.
